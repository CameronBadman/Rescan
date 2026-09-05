import pytest

from rescan.pipeline.structure import MIN_USABLE_CHARS, StructuringError, structure_resume
from rescan.schemas import ExtractionResult, WorkRightsStatus


def structure(extractor, llm, path):
    return structure_resume(extractor.extract_path(path), llm)


def test_structures_identity_and_experience(extractor, llm, samples):
    resume = structure(extractor, llm, samples / "001_priya_nair.txt")
    assert resume.identity.full_name == "Priya Nair"
    assert resume.identity.email == "priya.nair@email.com"
    assert resume.identity.suburb == "Sunnybank Hills"
    assert resume.identity.state == "QLD"
    assert resume.total_years_experience == 6.0
    assert len(resume.experience) == 2
    assert resume.experience[0].is_current is True


def test_university_is_kept_as_its_own_field(extractor, llm, samples):
    resume = structure(extractor, llm, samples / "002_james_thompson.txt")
    assert resume.universities == ["University of Queensland"]


def test_aqf_is_assigned_deterministically(extractor, llm, samples):
    resume = structure(extractor, llm, samples / "003_wei_zhang.txt")
    levels = sorted(q.aqf_level for q in resume.qualifications)
    assert levels == [7, 9]
    assert resume.highest_aqf == 9


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("002_james_thompson.txt", WorkRightsStatus.CITIZEN),
        ("001_priya_nair.txt", WorkRightsStatus.PERMANENT_RESIDENT),
        ("003_wei_zhang.txt", WorkRightsStatus.REQUIRES_SPONSORSHIP),
        ("010_fatima_hussein.txt", WorkRightsStatus.VISA_UNRESTRICTED),
    ],
)
def test_work_rights_are_read_not_inferred(extractor, llm, samples, filename, expected):
    resume = structure(extractor, llm, samples / filename)
    assert resume.work_rights.status is expected


def test_every_sample_structures_without_error(extractor, llm, samples):
    for path in sorted(samples.iterdir()):
        resume = structure(extractor, llm, path)
        assert resume.identity.full_name, f"{path.name} produced no name"
        assert resume.qualifications, f"{path.name} produced no qualifications"


def test_too_little_text_raises_instead_of_guessing(llm):
    thin = ExtractionResult(text="x" * (MIN_USABLE_CHARS - 1), char_count=MIN_USABLE_CHARS - 1)
    with pytest.raises(StructuringError):
        structure_resume(thin, llm)


def test_ocr_documents_are_annotated_for_reviewers(extractor, llm, samples):
    extraction = extractor.extract_path(samples / "001_priya_nair.txt")
    extraction.ocr_used = True
    resume = structure_resume(extraction, llm)
    assert any("OCR" in note for note in resume.extraction_notes)


# --------------------------------------------------------------------------
# Model output normalisation
# --------------------------------------------------------------------------


def test_near_miss_enum_values_are_coerced_not_fatal():
    from rescan.pipeline.structure import normalise_model_output
    from rescan.schemas import StructuredResume

    data = {
        "identity": {}, "summary": None,
        "skills": [
            {"name": "Python", "category": "Programming", "years": "4", "proficiency": "fluent", "evidence": None},
            {"name": "", "category": "technical", "years": None, "proficiency": None, "evidence": None},
        ],
        "experience": [
            {"title": "Sr. Engineer", "employer": "X", "start": None, "end": None, "is_current": "yes", "months": "30",
             "summary": None, "seniority": "Senior", "industry": None, "employment_type": "Full-time", "team_size": "4", "technologies": None},
        ],
        "total_years_experience": "6", "qualifications": [{"title": "BSc", "institution": None, "field_of_study": None,
                                                           "completion_year": "2015", "country": None}],
        "universities": [], "work_rights": {"status": "Permanent Resident", "visa_subclass": None, "unrestricted": True, "evidence": None},
        "languages": [], "affiliations": [], "certifications": [], "extraction_notes": [],
    }
    resume = StructuredResume.model_validate(normalise_model_output(data))
    skill = resume.skills[0]
    assert len(resume.skills) == 1, "a nameless skill is dropped"
    assert skill.category == "technical" and skill.proficiency == "advanced" and skill.years == 4.0
    role = resume.experience[0]
    assert role.seniority == "senior" and role.employment_type == "permanent" and role.months == 30.0 and role.team_size == 4
    assert role.is_current is True
    assert resume.work_rights.status.value == "permanent_resident"
    assert resume.qualifications[0].completion_year == 2015
    assert resume.total_years_experience == 6.0


def test_unrecognised_enum_value_is_recorded_as_a_note():
    from rescan.pipeline.structure import normalise_model_output

    data = {"skills": [{"name": "Python", "category": "wizardry", "proficiency": "galactic"}],
            "experience": [], "work_rights": {"status": "martian"}}
    normalise_model_output(data)
    assert data["skills"][0]["category"] == "other" and data["skills"][0]["proficiency"] is None
    assert data["work_rights"]["status"] == "unknown"
    assert any("'wizardry'" in note for note in data["extraction_notes"])
    assert any("'martian'" in note for note in data["extraction_notes"])


def test_structuring_survives_a_near_miss_from_the_model(llm, monkeypatch):
    from rescan.llm.client import LLMResponse
    from rescan.pipeline.structure import structure_resume
    from rescan.schemas import ExtractionResult

    def near_miss(request):
        return LLMResponse(
            data={"identity": {"full_name": "A B"}, "skills": [{"name": "SQL", "category": "technical", "proficiency": "fluent"}],
                  "experience": [], "qualifications": [], "work_rights": {"status": "unknown"}},
            model="m", backend="stub", latency_s=0.0,
        )

    monkeypatch.setattr(llm, "json_call", near_miss)
    resume = structure_resume(ExtractionResult(text="x" * 200, char_count=200), llm)
    assert resume.skills[0].proficiency == "advanced"
