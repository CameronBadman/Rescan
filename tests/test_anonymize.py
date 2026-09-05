import pytest

from rescan.pipeline.anonymize import (
    AnonymizationError,
    anonymize_resume,
    identity_tokens,
    leak_check,
    scrub_text,
)
from rescan.pipeline.structure import structure_resume
from rescan.schemas import Experience, Identity, Skill, StructuredResume


def build(extractor, llm, samples, filename):
    resume = structure_resume(extractor.extract_path(samples / filename), llm)
    return resume, anonymize_resume(resume, llm, candidate_ref="Candidate X")


def test_no_identity_survives_for_any_sample(extractor, llm, samples):
    for index, path in enumerate(sorted(samples.iterdir()), 1):
        resume = structure_resume(extractor.extract_path(path), llm)
        profile = anonymize_resume(resume, llm, candidate_ref=f"Candidate {index}")
        assert leak_check(resume, profile) == [], f"{path.name} leaked identity"


@pytest.mark.parametrize(
    "filename",
    [
        "001_priya_nair.txt",
        "003_wei_zhang.txt",
        "007_mohammed_al_hassan.txt",
        "010_fatima_hussein.txt",
    ],
)
def test_capability_is_preserved_exactly(extractor, llm, samples, filename):
    # Anonymization must not strengthen or weaken a candidate.
    resume, profile = build(extractor, llm, samples, filename)
    assert len(profile.skills) == len(resume.skills)
    assert [s.name for s in profile.skills] == [s.name for s in resume.skills]
    assert len(profile.experience) == len(resume.experience)
    assert profile.total_years_experience == resume.total_years_experience
    assert profile.highest_aqf == resume.highest_aqf
    assert profile.work_rights.status is resume.work_rights.status
    assert profile.languages == resume.languages


def test_university_becomes_a_tier_not_a_name(extractor, llm, samples):
    resume, profile = build(extractor, llm, samples, "002_james_thompson.txt")
    assert resume.universities == ["University of Queensland"]
    assert profile.institution_tiers == ["Australian university (Group of Eight)"]
    assert all(q.institution is None for q in profile.qualifications)


def test_suburb_is_generalised_to_a_region(extractor, llm, samples):
    resume, profile = build(extractor, llm, samples, "004_aisha_rahman.txt")
    assert resume.identity.suburb == "Logan Central"
    assert profile.region and "Logan" not in profile.region


def test_graduation_year_is_dropped_as_an_age_proxy(extractor, llm, samples):
    resume, profile = build(extractor, llm, samples, "009_robert_hughes.txt")
    assert any(q.completion_year for q in resume.qualifications)
    assert all(q.completion_year is None for q in profile.qualifications)
    assert any("Age Discrimination Act" in r.reason for r in profile.redactions)


def test_protected_affiliations_dropped_professional_ones_kept(llm):
    resume = StructuredResume(
        identity=Identity(full_name="Test Person", state="QLD"),
        affiliations=["IEEE member", "Islamic Women's Association of Australia", "Brisbane Cricket Club"],
    )
    profile = anonymize_resume(resume, llm, candidate_ref="Candidate 1")
    assert profile.job_relevant_affiliations == ["IEEE member"]
    reasons = " ".join(r.reason for r in profile.redactions)
    assert "Islamic Women's Association of Australia" in reasons
    assert "protected attribute" in reasons


def test_every_redaction_carries_a_reason(extractor, llm, samples):
    _, profile = build(extractor, llm, samples, "001_priya_nair.txt")
    assert profile.redactions
    for redaction in profile.redactions:
        assert redaction.reason.strip(), f"{redaction.field} redacted without a reason"


def test_scrub_removes_names_from_free_text():
    tokens = ["Priya Nair", "Priya", "Nair"]
    assert "Priya" not in scrub_text("Priya led the migration", tokens)


def test_scrub_is_case_insensitive_and_word_bounded():
    assert scrub_text("WEI ZHANG shipped it", ["Wei Zhang", "Wei", "Zhang"]) == "[redacted] shipped it"
    # A name fragment inside another word must not be mangled.
    assert scrub_text("Weighted average", ["Wei"]) == "Weighted average"


def test_identity_tokens_are_longest_first():
    resume = StructuredResume(
        identity=Identity(full_name="Ann Lee"), universities=["University of Queensland"]
    )
    tokens = identity_tokens(resume)
    assert tokens == sorted(tokens, key=len, reverse=True)


def test_leaked_identity_aborts_rather_than_ranking(llm, monkeypatch):
    resume = StructuredResume(
        identity=Identity(full_name="Jane Doe", state="QLD"),
        experience=[Experience(title="Engineer", summary="Jane Doe built the thing")],
    )
    # Simulate the scrub failing; the pass must refuse to hand identity to ranking.
    monkeypatch.setattr("rescan.pipeline.anonymize.scrub_text", lambda text, tokens: text)
    with pytest.raises(AnonymizationError, match="identity survived"):
        anonymize_resume(resume, llm, candidate_ref="Candidate 1")


def test_work_rights_evidence_is_scrubbed_but_kept(llm):
    resume = StructuredResume(
        identity=Identity(full_name="Sam Tan", state="QLD"),
        skills=[Skill(name="Python", evidence="Sam Tan used Python")],
    )
    profile = anonymize_resume(resume, llm, candidate_ref="Candidate 1")
    assert profile.skills[0].evidence and "Sam" not in profile.skills[0].evidence


def test_employer_names_are_kept(llm):
    from rescan.pipeline.anonymize import anonymize_resume
    from rescan.schemas import Experience, Identity, StructuredResume

    resume = StructuredResume(
        identity=Identity(full_name="Priya Nair"),
        experience=[Experience(title="Engineer", employer="Atlassian", months=24)],
    )
    profile = anonymize_resume(resume, llm, candidate_ref="Candidate 1")
    assert profile.experience[0].employer == "Atlassian"
    assert not [r for r in profile.redactions if r.field == "experience.employer"]


@pytest.mark.parametrize(
    "employer",
    ["Australian Labor Party", "United Workers Union", "St Mary's Catholic Church", "Queensland Malayalee Association", "Brisbane Pride Collective"],
)
def test_politically_charged_employers_are_removed_with_a_reason(llm, employer):
    from rescan.pipeline.anonymize import anonymize_resume
    from rescan.schemas import Experience, Identity, StructuredResume

    resume = StructuredResume(
        identity=Identity(full_name="Priya Nair"),
        experience=[Experience(title="Coordinator", employer=employer, months=12), Experience(title="Engineer", employer="Canva")],
    )
    profile = anonymize_resume(resume, llm, candidate_ref="Candidate 1")
    assert profile.experience[0].employer is None
    assert profile.experience[1].employer == "Canva"
    redaction = next(r for r in profile.redactions if r.field == "experience.employer")
    assert employer in redaction.reason and "protected attribute" in redaction.reason


def test_the_model_decides_which_employers_are_charged(llm, monkeypatch):
    from rescan.llm.client import LLMResponse
    from rescan.pipeline.anonymize import anonymize_resume
    from rescan.schemas import Experience, Identity, StructuredResume

    def judged(request):
        return LLMResponse(
            data={
                "summary": None, "institution_tiers": [], "region": None, "job_relevant_affiliations": [],
                "employers_to_remove": [{"employer": "Northside Collective", "reason": "a political campaign group"}],
                "redactions": [],
            },
            model="m", backend="stub", latency_s=0.0,
        )

    monkeypatch.setattr(llm, "json_call", judged)
    resume = StructuredResume(
        identity=Identity(full_name="Priya Nair"),
        experience=[
            Experience(title="Organiser", employer="Northside Collective"),
            Experience(title="Analyst", employer="Mission Australia"),
        ],
    )
    profile = anonymize_resume(resume, llm, candidate_ref="Candidate 1")
    assert profile.experience[0].employer is None, "the model's judgement is applied"
    assert profile.experience[1].employer == "Mission Australia", "no keyword list second-guesses the model"
    redaction = next(r for r in profile.redactions if r.field == "experience.employer")
    assert "political campaign group" in redaction.reason


def test_model_silence_keeps_every_employer(llm, monkeypatch):
    from rescan.llm.client import LLMResponse
    from rescan.pipeline.anonymize import anonymize_resume
    from rescan.schemas import Experience, Identity, StructuredResume

    def silent(request):
        return LLMResponse(
            data={"summary": None, "institution_tiers": [], "region": None, "job_relevant_affiliations": [], "redactions": []},
            model="m", backend="stub", latency_s=0.0,
        )

    monkeypatch.setattr(llm, "json_call", silent)
    resume = StructuredResume(identity=Identity(full_name="Priya Nair"), experience=[Experience(title="X", employer="Australian Labor Party")])
    profile = anonymize_resume(resume, llm, candidate_ref="Candidate 1")
    assert profile.experience[0].employer == "Australian Labor Party"


def test_an_employer_named_after_the_candidate_is_scrubbed_not_dropped(llm):
    from rescan.pipeline.anonymize import anonymize_resume
    from rescan.schemas import Experience, Identity, StructuredResume

    resume = StructuredResume(
        identity=Identity(full_name="Priya Nair"),
        experience=[Experience(title="Principal", employer="Nair Consulting Pty Ltd")],
    )
    profile = anonymize_resume(resume, llm, candidate_ref="Candidate 1")
    assert "Nair" not in (profile.experience[0].employer or "")
    assert "Consulting" in profile.experience[0].employer
