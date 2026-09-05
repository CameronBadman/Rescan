"""Bias audit: does anonymization actually close the score gap?

Each counterfactual variant is scored twice — once through the full pipeline
with anonymization, and once with identity left attached — and the spread of
scores across demographic groups is compared between the two arms.

Because the variants within a case are byte-identical apart from the
substituted signal, any spread in the identified arm is attributable to that
signal. The number that matters is the difference between the arms: how much of
the gap anonymization removes.

There is no published bias evaluation for the model this system targets, so
this harness is the evidence. Run it against the real inference backend; the
deterministic stub does not read names and will report a zero gap in both arms
by construction, which is a property of the stub, not a finding.
"""

from __future__ import annotations

import logging
import statistics
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import Iterable, Literal

from pydantic import BaseModel, Field

from rescan.audit.corpus import GROUP_LABELS, Case, Variant
from rescan.config import settings
from rescan.llm.client import LLMClient
from rescan.pipeline.anonymize import AnonymizationError, anonymize_resume
from rescan.pipeline.rank import RankingError, score_candidate
from rescan.pipeline.structure import StructuringError, structure_resume
from rescan.schemas import (
    AnonymizedProfile,
    ExtractionResult,
    RoleSpec,
    StructuredResume,
)

log = logging.getLogger(__name__)

Arm = Literal["identified", "anonymized"]


# --------------------------------------------------------------------------
# The identified arm
# --------------------------------------------------------------------------


def identified_profile(resume: StructuredResume, candidate_ref: str) -> AnonymizedProfile:
    """Build a ranking profile with identity deliberately left attached.

    This is the control arm. It uses the same container as the anonymized
    profile so both arms go through an identical scoring path, and the only
    difference between them is the information the scorer can see.
    """
    identity = resume.identity
    header = ", ".join(
        part for part in (identity.full_name, identity.suburb, identity.state) if part
    )
    summary = f"{header}. {resume.summary}" if resume.summary else header

    return AnonymizedProfile(
        candidate_ref=candidate_ref,
        summary=summary or None,
        skills=[skill.model_copy(deep=True) for skill in resume.skills],
        experience=[role.model_copy(deep=True) for role in resume.experience],
        total_years_experience=resume.total_years_experience,
        qualifications=[q.model_copy(deep=True) for q in resume.qualifications],
        # Real institution names and suburb, not tiers and regions.
        institution_tiers=list(resume.universities),
        region=identity.suburb or identity.state,
        work_rights=resume.work_rights.model_copy(deep=True),
        languages=list(resume.languages),
        job_relevant_affiliations=list(resume.affiliations),
        certifications=list(resume.certifications),
    )


# --------------------------------------------------------------------------
# Report models
# --------------------------------------------------------------------------


class GroupStat(BaseModel):
    group: str
    label: str
    n: int
    mean_score: float
    stdev: float | None = None


class ArmSummary(BaseModel):
    arm: str
    scored: int
    failed: int = 0
    group_stats: list[GroupStat] = Field(default_factory=list)
    # Difference between the highest- and lowest-scoring group means.
    group_gap: float = 0.0
    highest_group: str | None = None
    lowest_group: str | None = None
    # Averaged over cases: the spread across groups within one identical resume.
    mean_within_case_spread: float = 0.0
    max_within_case_spread: float = 0.0


class BiasAuditReport(BaseModel):
    backend: str
    model: str
    role_title: str
    cases: int
    variants: int
    arms: list[ArmSummary] = Field(default_factory=list)
    gap_reduction: float | None = Field(
        default=None,
        description="Identified-arm group gap minus anonymized-arm group gap.",
    )
    gap_reduction_pct: float | None = None
    notes: list[str] = Field(default_factory=list)

    def summary_table(self) -> str:
        lines = [
            f"Bias audit — {self.role_title}",
            f"backend={self.backend} model={self.model} "
            f"cases={self.cases} variants={self.variants}",
            "",
        ]
        for arm in self.arms:
            lines.append(f"[{arm.arm}] scored={arm.scored} failed={arm.failed}")
            for stat in sorted(arm.group_stats, key=lambda s: s.mean_score, reverse=True):
                lines.append(f"    {stat.group:3} {stat.label:28} n={stat.n:3} mean={stat.mean_score:.4f}")
            lines.append(
                f"    group gap = {arm.group_gap:.4f}"
                f" ({arm.highest_group} - {arm.lowest_group});"
                f" mean within-case spread = {arm.mean_within_case_spread:.4f}"
            )
            lines.append("")
        if self.gap_reduction is not None:
            lines.append(
                f"Gap closed by anonymization: {self.gap_reduction:+.4f}"
                + (f" ({self.gap_reduction_pct:+.1f}%)" if self.gap_reduction_pct is not None else "")
            )
        lines.extend(f"note: {note}" for note in self.notes)
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Audit
# --------------------------------------------------------------------------


def _score_variant(
    variant: Variant,
    arm: Arm,
    role: RoleSpec,
    client: LLMClient,
    ref: str,
) -> float | None:
    extraction = ExtractionResult(
        text=variant.text, char_count=len(variant.text), backend="audit-corpus"
    )
    try:
        resume = structure_resume(extraction, client)
    except StructuringError as exc:
        log.warning("audit: could not structure %s: %s", variant.variant_id, exc)
        return None

    try:
        profile = (
            identified_profile(resume, ref)
            if arm == "identified"
            else anonymize_resume(resume, client, candidate_ref=ref)
        )
    except AnonymizationError as exc:
        log.warning("audit: anonymization failed for %s: %s", variant.variant_id, exc)
        return None

    try:
        return score_candidate(profile, role, client).score
    except RankingError as exc:
        log.warning("audit: scoring failed for %s: %s", variant.variant_id, exc)
        return None


def _summarise(
    arm: Arm, scores: dict[str, dict[str, list[float]]], failed: int
) -> ArmSummary:
    """Build an arm summary from ``{case_id: {group: [scores]}}``."""
    by_group: dict[str, list[float]] = defaultdict(list)
    within_case: list[float] = []

    for group_scores in scores.values():
        case_means = []
        for group, values in group_scores.items():
            if not values:
                continue
            by_group[group].extend(values)
            case_means.append(statistics.fmean(values))
        if len(case_means) > 1:
            within_case.append(max(case_means) - min(case_means))

    stats = [
        GroupStat(
            group=group,
            label=GROUP_LABELS.get(group, group),
            n=len(values),
            mean_score=round(statistics.fmean(values), 4),
            stdev=round(statistics.stdev(values), 4) if len(values) > 1 else None,
        )
        for group, values in sorted(by_group.items())
    ]

    summary = ArmSummary(arm=arm, scored=sum(len(v) for v in by_group.values()), failed=failed)
    summary.group_stats = stats
    if stats:
        highest = max(stats, key=lambda s: s.mean_score)
        lowest = min(stats, key=lambda s: s.mean_score)
        summary.group_gap = round(highest.mean_score - lowest.mean_score, 4)
        summary.highest_group = highest.group
        summary.lowest_group = lowest.group
    if within_case:
        summary.mean_within_case_spread = round(statistics.fmean(within_case), 4)
        summary.max_within_case_spread = round(max(within_case), 4)
    return summary


def run_bias_audit(
    cases: Iterable[Case],
    client: LLMClient,
    role: RoleSpec,
    *,
    arms: tuple[Arm, ...] = ("identified", "anonymized"),
    workers: int | None = None,
) -> BiasAuditReport:
    """Score every variant under each arm and compare the resulting gaps."""
    cases = list(cases)
    # The ref must not embed the variant id: that carries the substituted first
    # name, and the anonymization leak check would rightly reject it.
    jobs = [
        (case, variant, arm, f"Candidate {index}")
        for index, (case, variant) in enumerate(
            ((case, variant) for case in cases for variant in case.variants), start=1
        )
        for arm in arms
    ]

    def run(item):
        case, variant, arm, ref = item
        return case.case_id, variant.group, arm, _score_variant(variant, arm, role, client, ref)

    with ThreadPoolExecutor(max_workers=workers or settings.pipeline_workers) as pool:
        results = list(pool.map(run, jobs))

    collected: dict[Arm, dict[str, dict[str, list[float]]]] = {
        arm: defaultdict(lambda: defaultdict(list)) for arm in arms
    }
    failures = {arm: 0 for arm in arms}
    for case_id, group, arm, score in results:
        if score is None:
            failures[arm] += 1
            continue
        collected[arm][case_id][group].append(score)

    report = BiasAuditReport(
        backend=settings.llm_backend,
        model="stub" if settings.llm_backend == "stub" else settings.llm_model,
        role_title=role.title,
        cases=len(cases),
        variants=sum(len(case.variants) for case in cases),
        arms=[_summarise(arm, collected[arm], failures[arm]) for arm in arms],
    )

    by_arm = {summary.arm: summary for summary in report.arms}
    if "identified" in by_arm and "anonymized" in by_arm:
        identified_gap = by_arm["identified"].group_gap
        anonymized_gap = by_arm["anonymized"].group_gap
        report.gap_reduction = round(identified_gap - anonymized_gap, 4)
        if identified_gap > 0:
            report.gap_reduction_pct = round(
                (identified_gap - anonymized_gap) / identified_gap * 100, 1
            )

    if settings.llm_backend == "stub":
        report.notes.append(
            "Run against the deterministic stub backend, which does not read "
            "names or institutions. Both arms are identical by construction and "
            "a zero gap here is a property of the stub, not a finding. Point "
            "RESCAN_LLM_BACKEND=openai at the served model for a real result."
        )
    if report.gap_reduction is not None and report.gap_reduction < 0:
        report.notes.append(
            "Anonymization widened the measured gap. Investigate before relying "
            "on it: check the redaction log for capability removed alongside identity."
        )
    return report
