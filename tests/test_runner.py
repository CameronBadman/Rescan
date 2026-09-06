"""Batch ingestion and analysis runs at the pipeline level."""

from collections import Counter

import pytest

from rescan.config import settings
from rescan.extract import Extractor
from rescan.llm.client import build_client
from rescan.pipeline.runner import BatchNotReady, PipelineRunner
from rescan.schemas import BatchStatus, CandidateOutcome, CandidateStatus, RoleSpec, RunStatus
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


@pytest.fixture()
def ingested(runner, documents):
    """A batch that has finished ingesting."""
    pipeline, store = runner
    batch_id = pipeline.create_batch(documents, name="Applicants")
    pipeline.process_batch(batch_id)
    return pipeline, store, batch_id


def analyse(pipeline, batch_id, rules=RULES, plan=None, **kwargs):
    run_id = pipeline.create_run(batch_id, ROLE, **kwargs)
    shortlist = pipeline.execute_run(run_id, rules, plan=plan)
    return run_id, shortlist


# --------------------------------------------------------------------------
# Batches
# --------------------------------------------------------------------------


def test_a_batch_ingests_every_document_to_a_profile(ingested):
    pipeline, store, batch_id = ingested
    batch = store.get_batch(batch_id)
    assert batch["status"] == BatchStatus.COMPLETE.value
    assert batch["ready"] == batch["total"] > 0
    assert all(candidate["anonymized"] for candidate in store.list_candidates(batch_id))


def test_identical_bytes_are_deduped_within_a_batch(runner, documents):
    pipeline, store = runner
    batch_id = pipeline.create_batch(documents + [("copy_of_first.txt", documents[0][1])])
    assert store.batch_counts(batch_id)[CandidateStatus.DUPLICATE.value] == 1
    pipeline.process_batch(batch_id)
    # The duplicate is recorded but never processed.
    assert store.batch_counts(batch_id)[CandidateStatus.DUPLICATE.value] == 1


def test_unreadable_document_is_dead_lettered_not_rejected(runner, documents):
    pipeline, store = runner
    batch_id = pipeline.create_batch(documents + [("corrupt.pdf", b"\x00\x01not a pdf")])
    pipeline.process_batch(batch_id)

    counts = store.batch_counts(batch_id)
    assert counts[CandidateStatus.NEEDS_MANUAL_REVIEW.value] >= 1
    events = [e["event"] for e in store.audit_trail(batch_id)]
    assert "dead_lettered" in events
    assert "extraction_retry" in events, "extraction should be retried before dead-lettering"


def test_no_document_is_silently_lost(runner, documents):
    pipeline, store = runner
    uploads = documents + [("corrupt.pdf", b"\x00\x01"), ("copy.txt", documents[0][1])]
    batch_id = pipeline.create_batch(uploads)
    pipeline.process_batch(batch_id)
    assert sum(store.batch_counts(batch_id).values()) == len(uploads)


def test_candidate_refs_carry_no_identity(ingested):
    pipeline, store, batch_id = ingested
    for candidate in store.list_candidates(batch_id):
        ref = candidate["candidate_ref"]
        if ref:
            assert ref.startswith("Candidate ")


def test_anonymized_record_holds_no_identity(ingested):
    import json

    pipeline, store, batch_id = ingested
    for candidate in store.list_candidates(batch_id):
        anonymized, structured = candidate.get("anonymized"), candidate.get("structured")
        if not anonymized or not structured:
            continue
        name = (structured.get("identity") or {}).get("full_name")
        if name:
            assert name not in json.dumps(anonymized)


def test_ingestion_records_every_stage(ingested):
    pipeline, store, batch_id = ingested
    events = Counter(e["event"] for e in store.audit_trail(batch_id))
    for expected in ("batch_created", "extracted", "structured", "redaction", "batch_complete"):
        assert events[expected] > 0, f"missing audit event {expected}"
    assert all(e["run_id"] is None for e in store.audit_trail(batch_id)), "ingestion belongs to no run"


def test_batch_failure_is_recorded_rather_than_swallowed(runner, documents, monkeypatch):
    pipeline, store = runner
    batch_id = pipeline.create_batch(documents[:1])

    def boom(*args, **kwargs):
        raise RuntimeError("extractor exploded")

    monkeypatch.setattr(pipeline.store, "list_candidates", boom)
    with pytest.raises(RuntimeError):
        pipeline.process_batch(batch_id)
    batch = store.get_batch(batch_id)
    assert batch["status"] == BatchStatus.FAILED.value and "exploded" in batch["error"]


# --------------------------------------------------------------------------
# Runs
# --------------------------------------------------------------------------


def test_a_run_screens_the_batch_and_cuts_a_shortlist(ingested):
    pipeline, store, batch_id = ingested
    run_id, shortlist = analyse(pipeline, batch_id, name="First pass")
    run = store.get_run(run_id)
    assert run["status"] == RunStatus.COMPLETE.value
    assert run["name"] == "First pass" and run["batch_id"] == batch_id
    assert shortlist["entries"], "a completed run should produce a shortlist"
    assert run["screened"] == store.get_batch(batch_id)["ready"]
    assert sum(run["counts"].values()) == run["screened"]


def test_a_run_records_every_decision_against_itself(ingested):
    pipeline, store, batch_id = ingested
    run_id, _ = analyse(pipeline, batch_id)
    events = Counter(e["event"] for e in store.audit_trail(run_id=run_id, include_batch=False))
    for expected in ("run_created", "plan_compiled", "rule_passed", "rule_failed", "scored", "run_complete"):
        assert events[expected] > 0, f"missing run event {expected}"
    assert "extracted" not in events, "ingestion events belong to the batch, not the run"
    merged = {e["event"] for e in store.audit_trail(run_id=run_id, include_batch=True)}
    assert "extracted" in merged and "run_complete" in merged


def test_flagged_rule_is_recorded_with_statute_and_rewrite(ingested):
    pipeline, store, batch_id = ingested
    run_id, _ = analyse(pipeline, batch_id)
    flags = [e for e in store.audit_trail(run_id=run_id) if e["event"] == "rule_risk_flagged"]
    assert flags, "the native-speaker rule should have been flagged"
    detail = flags[0]["detail"]
    assert detail["statutes"] and detail["suggested_rewrite"] and detail["protected_attributes"]


def test_every_exclusion_has_a_reason(ingested):
    pipeline, store, batch_id = ingested
    _, shortlist = analyse(pipeline, batch_id)
    assert shortlist["excluded"]
    for entry in shortlist["excluded"]:
        assert entry["reasons"] and all(reason.strip() for reason in entry["reasons"])


def test_two_runs_over_one_batch_keep_independent_results(ingested):
    pipeline, store, batch_id = ingested
    lenient_id, lenient = analyse(pipeline, batch_id, ["At least 1 year of experience"], name="Lenient")
    strict_id, strict = analyse(pipeline, batch_id, ["At least 15 years of experience"], name="Strict")

    assert len(strict["excluded"]) > len(lenient["excluded"])
    # Both remain readable: the second run did not overwrite the first.
    assert store.get_run(lenient_id)["shortlist"]["excluded"] == lenient["excluded"]
    assert {r["candidate_ref"] for r in store.list_results(lenient_id)} == {
        r["candidate_ref"] for r in store.list_results(strict_id)
    }
    lenient_outcomes = {r["candidate_ref"]: r["outcome"] for r in store.list_results(lenient_id)}
    strict_outcomes = {r["candidate_ref"]: r["outcome"] for r in store.list_results(strict_id)}
    assert lenient_outcomes != strict_outcomes
    assert CandidateOutcome.EXCLUDED.value in strict_outcomes.values()
    # And the batch was never touched again.
    ingestion = [e for e in store.audit_trail(batch_id) if e["event"] == "extracted"]
    assert len(ingestion) == store.get_batch(batch_id)["ready"]


def test_a_run_needs_a_finished_batch(runner, documents):
    pipeline, store = runner
    batch_id = pipeline.create_batch(documents[:2])
    with pytest.raises(BatchNotReady):
        pipeline.create_run(batch_id, ROLE)
    with pytest.raises(KeyError):
        pipeline.create_run("nope", ROLE)


def test_a_run_can_reuse_another_runs_rules(ingested):
    pipeline, store, batch_id = ingested
    first_id, _ = analyse(pipeline, batch_id, ["At least 4 years of professional experience"])
    second_id = pipeline.create_run(batch_id, ROLE, name="Copy")
    pipeline.execute_run(second_id, rules_from=first_id)

    assert [r.dsl for r in pipeline.rule_set_for(second_id).rules] == [
        r.dsl for r in pipeline.rule_set_for(first_id).rules
    ]
    events = {e["event"] for e in store.audit_trail(run_id=second_id, include_batch=False)}
    assert "rules_copied" in events and "plan_compiled" not in events


def test_run_failure_is_recorded_rather_than_swallowed(ingested, monkeypatch):
    pipeline, store, batch_id = ingested
    run_id = pipeline.create_run(batch_id, ROLE)

    def boom(*args, **kwargs):
        raise RuntimeError("inference cluster down")

    monkeypatch.setattr("rescan.pipeline.runner.compile_plan", boom)
    with pytest.raises(RuntimeError):
        pipeline.execute_run(run_id, RULES)
    run = store.get_run(run_id)
    assert run["status"] == RunStatus.FAILED.value and "cluster down" in run["error"]


def test_rescreening_uses_stored_profiles_and_replaces_outcomes(ingested):
    pipeline, store, batch_id = ingested
    run_id, before = analyse(pipeline, batch_id, ["At least 2 years of experience"])
    extracted_before = len([e for e in store.audit_trail(batch_id) if e["event"] == "extracted"])

    pipeline.add_rule(run_id, "At least 12 years of professional experience")
    after = pipeline.rescreen(run_id)

    assert len(after["excluded"]) > len(before["excluded"])
    assert len(store.list_results(run_id)) == store.get_batch(batch_id)["ready"], "one row per candidate, not two"
    assert len([e for e in store.audit_trail(batch_id) if e["event"] == "extracted"]) == extracted_before
