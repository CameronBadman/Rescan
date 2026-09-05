import pathlib
import tempfile
from collections import Counter

import pytest

from rescan.config import settings
from rescan.extract import Extractor
from rescan.llm.client import build_client
from rescan.pipeline.runner import PipelineRunner
from rescan.schemas import CandidateStatus, JobStatus, RoleSpec
from rescan.store import Store

ROLE = RoleSpec(
    title="Senior Backend Engineer",
    required_skills=["Python", "AWS"],
    desirable_skills=["Kubernetes", "Go", "Kafka"],
    min_years_experience=5,
    min_aqf=7,
)
RULES = [
    "At least 4 years of professional experience",
    "Bachelor degree or higher",
    "Must hold unrestricted Australian work rights",
    "Must be a native English speaker",
]


@pytest.fixture()
def runner(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "upload_dir", tmp_path / "uploads")
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    store = Store(tmp_path / "test.db")
    yield PipelineRunner(store, build_client("stub"), Extractor()), store
    store.close()


@pytest.fixture()
def documents(samples):
    return [(p.name, p.read_bytes()) for p in sorted(samples.glob("*.txt"))]


def test_job_runs_to_completion(runner, documents):
    pipeline, store = runner
    job_id = pipeline.create_job(ROLE, documents)
    shortlist = pipeline.run_job(job_id, RULES)
    assert store.get_job(job_id)["status"] == JobStatus.COMPLETE.value
    assert shortlist["entries"], "a completed job should produce a shortlist"


def test_identical_bytes_are_deduped_within_a_job(runner, documents):
    pipeline, store = runner
    duplicated = documents + [("copy_of_first.txt", documents[0][1])]
    job_id = pipeline.create_job(ROLE, duplicated)
    counts = store.status_counts(job_id)
    assert counts[CandidateStatus.DUPLICATE.value] == 1
    pipeline.run_job(job_id, RULES)
    # The duplicate is recorded but never reprocessed.
    assert store.status_counts(job_id)[CandidateStatus.DUPLICATE.value] == 1


def test_unreadable_document_is_dead_lettered_not_rejected(runner, documents):
    pipeline, store = runner
    job_id = pipeline.create_job(ROLE, documents + [("corrupt.pdf", b"\x00\x01not a pdf")])
    pipeline.run_job(job_id, RULES)

    counts = store.status_counts(job_id)
    assert counts[CandidateStatus.NEEDS_MANUAL_REVIEW.value] >= 1
    events = [e["event"] for e in store.audit_trail(job_id)]
    assert "dead_lettered" in events
    assert "extraction_retry" in events, "extraction should be retried before dead-lettering"


def test_no_document_is_silently_lost(runner, documents):
    pipeline, store = runner
    uploads = documents + [("corrupt.pdf", b"\x00\x01"), ("copy.txt", documents[0][1])]
    job_id = pipeline.create_job(ROLE, uploads)
    pipeline.run_job(job_id, RULES)
    counts = store.status_counts(job_id)
    assert sum(counts.values()) == len(uploads)


def test_audit_trail_records_every_stage(runner, documents):
    pipeline, store = runner
    job_id = pipeline.create_job(ROLE, documents)
    pipeline.run_job(job_id, RULES)
    events = Counter(e["event"] for e in store.audit_trail(job_id))
    for expected in (
        "job_created", "extracted", "structured", "redaction",
        "rule_passed", "rule_failed", "scored", "job_complete",
    ):
        assert events[expected] > 0, f"missing audit event {expected}"


def test_flagged_rule_is_recorded_with_statute_and_rewrite(runner, documents):
    pipeline, store = runner
    job_id = pipeline.create_job(ROLE, documents[:2])
    pipeline.run_job(job_id, RULES)
    flags = [e for e in store.audit_trail(job_id) if e["event"] == "rule_risk_flagged"]
    assert flags, "the native-speaker rule should have been flagged"
    detail = flags[0]["detail"]
    assert detail["statutes"] and detail["suggested_rewrite"]
    assert detail["protected_attributes"]


def test_every_exclusion_has_a_reason_naming_a_field(runner, documents):
    pipeline, store = runner
    job_id = pipeline.create_job(ROLE, documents)
    shortlist = pipeline.run_job(job_id, RULES)
    assert shortlist["excluded"]
    for entry in shortlist["excluded"]:
        assert entry["reasons"], f"{entry['candidate_ref']} excluded without a reason"
        assert all(reason.strip() for reason in entry["reasons"])


def test_candidate_refs_carry_no_identity(runner, documents):
    pipeline, store = runner
    job_id = pipeline.create_job(ROLE, documents)
    pipeline.run_job(job_id, RULES)
    for candidate in store.list_candidates(job_id):
        ref = candidate["candidate_ref"]
        if ref:
            assert ref.startswith("Candidate ")


def test_anonymized_record_holds_no_identity(runner, documents):
    pipeline, store = runner
    job_id = pipeline.create_job(ROLE, documents)
    pipeline.run_job(job_id, RULES)
    for candidate in store.list_candidates(job_id):
        anonymized = candidate.get("anonymized")
        structured = candidate.get("structured")
        if not anonymized or not structured:
            continue
        name = (structured.get("identity") or {}).get("full_name")
        if name:
            import json

            assert name not in json.dumps(anonymized)


def test_job_failure_is_recorded_rather_than_swallowed(runner, documents, monkeypatch):
    pipeline, store = runner
    job_id = pipeline.create_job(ROLE, documents[:1])

    def boom(*args, **kwargs):
        raise RuntimeError("inference cluster down")

    monkeypatch.setattr("rescan.pipeline.runner.classify_rules", boom)
    with pytest.raises(RuntimeError):
        pipeline.run_job(job_id, RULES)
    job = store.get_job(job_id)
    assert job["status"] == JobStatus.FAILED.value
    assert "inference cluster down" in job["error"]
