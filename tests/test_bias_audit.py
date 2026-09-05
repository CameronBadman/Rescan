import re
from pathlib import Path

import pytest

from rescan.audit.bias import identified_profile, run_bias_audit
from rescan.audit.corpus import (
    GROUP_LABELS,
    SYNTHETIC_NAMES,
    render_resume,
    synthetic_corpus,
    variant_count,
)
from rescan.llm.client import LLMResponse
from rescan.pipeline.rank import DEFAULT_CRITERIA
from rescan.schemas import Identity, RoleSpec, Skill, StructuredResume

ROLE = RoleSpec(title="Senior Backend Engineer", required_skills=["Python"], min_years_experience=5)


# --------------------------------------------------------------------------
# Corpus
# --------------------------------------------------------------------------


def test_corpus_builds_one_variant_per_group(samples):
    cases = synthetic_corpus(samples)
    assert cases
    for case in cases:
        assert {v.group for v in case.variants} == set(SYNTHETIC_NAMES)


def test_variants_differ_only_in_identity(samples):
    # Strip the identity lines; everything else must be byte-identical, which
    # is what makes a score difference attributable to the name.
    case = synthetic_corpus(samples)[0]

    def body(text):
        lines = text.splitlines()
        return "\n".join(lines[3:])

    bodies = {body(v.text) for v in case.variants}
    assert len(bodies) == 1, "variants differ somewhere other than the identity header"


def test_original_identity_never_survives_substitution(samples):
    for path, case in zip(sorted(samples.glob("*.txt")), synthetic_corpus(samples)):
        original = path.read_text()
        surname = next(
            (line.strip().split()[-1] for line in original.splitlines() if line.strip().isupper()),
            None,
        )
        if not surname or len(surname) < 4:
            continue
        for variant in case.variants:
            assert not re.search(rf"\b{surname}\b", variant.text, re.IGNORECASE), (
                f"{variant.variant_id} still contains the original surname"
            )


def test_derived_handles_are_rebuilt_not_left_stale(samples):
    variant = synthetic_corpus(samples)[0].variants[0]
    assert "priyanair" not in variant.text.lower()
    assert variant.first_name.lower() in variant.text.lower()


def test_proxy_variants_change_institution_and_suburb(samples):
    case = synthetic_corpus(samples, include_proxies=True)[0]
    contexts = {v.proxy_context for v in case.variants}
    assert {None, "advantaged", "disadvantaged"} <= contexts
    advantaged = next(v for v in case.variants if v.proxy_context == "advantaged")
    disadvantaged = next(v for v in case.variants if v.proxy_context == "disadvantaged")
    assert "University of Queensland" in advantaged.text
    assert "Western Sydney University" in disadvantaged.text
    assert "Ascot" in advantaged.text and "Woodridge" in disadvantaged.text


def test_proxy_substitution_preserves_the_qualification_title(samples):
    # A masters must not be overwritten by the institution swap.
    case = next(c for c in synthetic_corpus(samples, include_proxies=True) if c.case_id.startswith("003"))
    variant = next(v for v in case.variants if v.proxy_context == "disadvantaged")
    assert "Master of Computer Science" in variant.text


def test_render_resume_produces_parseable_text():
    text = render_resume(
        "Test Person",
        [{"job_title": "Engineer", "duration_str": "Jan 2020 - Present", "job_order": 0,
          "tasks": [{"content": "Built things."}]}],
        institution="Example University",
    )
    assert "EXPERIENCE" in text and "Engineer" in text and "EDUCATION" in text


# --------------------------------------------------------------------------
# Control arm
# --------------------------------------------------------------------------


def test_identified_arm_keeps_identity_visible():
    resume = StructuredResume(
        identity=Identity(full_name="Jane Doe", suburb="Ascot", state="QLD"),
        summary="Engineer with 5 years experience.",
        universities=["University of Queensland"],
        skills=[Skill(name="Python")],
    )
    profile = identified_profile(resume, "Candidate 1")
    assert "Jane Doe" in profile.summary
    assert profile.institution_tiers == ["University of Queensland"]
    assert profile.region == "Ascot"


# --------------------------------------------------------------------------
# Audit
# --------------------------------------------------------------------------


def biased_client(bonus_by_name: dict[str, float], base: float = 0.5):
    """A scorer that rewards some names, to prove the audit detects a gap."""

    class Biased:
        def json_call(self, request):
            if request.task != "rank":
                from rescan.llm.stub import StubClient

                return StubClient().json_call(request)
            blob = request.user
            score = base
            for name, bonus in bonus_by_name.items():
                if re.search(rf"\b{name}\b", blob, re.IGNORECASE):
                    score = min(1.0, base + bonus)
                    break
            return LLMResponse(
                data={
                    "criteria": [
                        {"criterion": c.key, "score": score, "evidence": "biased"}
                        for c in DEFAULT_CRITERIA
                    ],
                    "rationale": "biased",
                },
                model="biased", backend="test", latency_s=0.0,
            )

    return Biased()


def test_audit_runs_both_arms_over_every_variant(llm, samples):
    cases = synthetic_corpus(samples)[:3]
    report = run_bias_audit(cases, llm, ROLE)
    assert report.cases == 3
    assert report.variants == variant_count(cases)
    assert {arm.arm for arm in report.arms} == {"identified", "anonymized"}
    for arm in report.arms:
        assert arm.failed == 0, f"{arm.arm} arm had failures"
        assert arm.scored == report.variants


def test_audit_detects_a_gap_the_scorer_actually_has(samples):
    # Reward white male-coded first names only.
    client = biased_client({name.split()[0]: 0.4 for name in SYNTHETIC_NAMES["wm"]})
    report = run_bias_audit(synthetic_corpus(samples)[:3], client, ROLE)
    by_arm = {arm.arm: arm for arm in report.arms}

    assert by_arm["identified"].group_gap > 0.1, "the audit failed to detect a real gap"
    assert by_arm["identified"].highest_group == "wm"
    assert by_arm["identified"].mean_within_case_spread > 0


def test_anonymization_closes_a_name_based_gap(samples):
    client = biased_client({name.split()[0]: 0.4 for name in SYNTHETIC_NAMES["wm"]})
    report = run_bias_audit(synthetic_corpus(samples)[:3], client, ROLE)
    by_arm = {arm.arm: arm for arm in report.arms}

    assert by_arm["anonymized"].group_gap == 0.0, (
        "the name is removed before ranking, so a name-based gap must vanish"
    )
    assert report.gap_reduction == pytest.approx(by_arm["identified"].group_gap)
    assert report.gap_reduction_pct == pytest.approx(100.0)


def test_stub_backend_result_is_labelled_as_not_a_finding(llm, samples):
    report = run_bias_audit(synthetic_corpus(samples)[:2], llm, ROLE)
    assert any("not a finding" in note for note in report.notes)


def test_report_summary_table_is_renderable(llm, samples):
    report = run_bias_audit(synthetic_corpus(samples)[:2], llm, ROLE)
    table = report.summary_table()
    for group in GROUP_LABELS:
        assert group in table
    assert "Gap closed by anonymization" in table
