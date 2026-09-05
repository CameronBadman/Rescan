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
