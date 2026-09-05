from rescan.extract import content_hash
from rescan.extract.extractor import Extractor


def test_extracts_every_sample_format(extractor, samples):
    paths = sorted(samples.iterdir())
    results = extractor.extract_many(paths)
    assert len(results) == len(paths)
    for path, result in zip(paths, results):
        assert result.char_count > 200, f"{path.name} yielded {result.char_count} chars"
        assert result.backend != "unknown"


def test_pdf_and_docx_reach_the_same_content_as_source_text(extractor, samples):
    pdf = extractor.extract_path(samples / "012_wei_zhang.pdf")
    assert "Distributed systems engineer" in pdf.text
    docx = extractor.extract_path(samples / "011_james_thompson.docx")
    assert "Atlassian" in docx.text


def test_unparseable_document_is_flagged_not_dropped(extractor):
    result = extractor.extract_bytes(b"\x00\x01\x02\x03", "broken.pdf")
    assert result.char_count == 0
    assert result.warnings, "an unreadable document must carry warnings for triage"


def test_content_hash_dedups_identical_bytes():
    assert content_hash(b"abc") == content_hash(b"abc")
    assert content_hash(b"abc") != content_hash(b"abd")
