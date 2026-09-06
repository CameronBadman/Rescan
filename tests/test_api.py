import io
import json
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from rescan.api.main import app, expand_uploads
from rescan.config import settings

ROLE = {
    "title": "Senior Backend Engineer",
    "required_skills": ["Python", "AWS"],
    "desirable_skills": ["Kubernetes", "Go"],
    "min_years_experience": 5,
    "min_aqf": 7,
}
PLAN = (
    "At least 4 years of professional experience. Bachelor degree or higher. "
    "Must be a native English speaker."
)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", tmp_path / "api.db")
    monkeypatch.setattr(settings, "upload_dir", tmp_path / "uploads")
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "object_store", "local")
    monkeypatch.setattr(settings, "local_object_store_dir", tmp_path / "bucket")
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def upload_files(samples):
    return [
        ("files", (path.name, path.read_bytes(), "text/plain"))
        for path in sorted(samples.glob("*.txt"))
    ]


def wait_for(client, path, timeout=30.0):
    """Poll a batch or run until it settles."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(path).json()
        if body["status"] in {"complete", "failed"}:
            return body
        time.sleep(0.05)
    raise AssertionError(f"{path} did not finish within {timeout}s")


def start_batch(client, upload_files, **data):
    response = client.post("/batches", files=upload_files, data=data)
    assert response.status_code == 202, response.text
    batch_id = response.json()["batch_id"]
    assert wait_for(client, f"/batches/{batch_id}")["status"] == "complete"
    return batch_id


def start_run(client, batch_id, plan="At least 4 years of professional experience.", **body):
    response = client.post("/runs", json={"batch_id": batch_id, "role": ROLE, "plan": plan, **body})
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    assert wait_for(client, f"/runs/{run_id}")["status"] == "complete"
    return run_id


def test_health_reports_the_configured_backend(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert "llm_backend" in body


# --------------------------------------------------------------------------
# Rule checking
# --------------------------------------------------------------------------


def test_rule_check_flags_risky_language_with_a_rewrite(client):
    response = client.post("/rules/check", json={"rules": ["Must be a native English speaker"]})
    assert response.status_code == 200
    body = response.json()
    assert body["flagged"] == 1 and body["applied"] == 0
    finding = body["rules"][0]["findings"][0]
    assert any("Racial Discrimination Act" in s for s in finding["statutes"])
    assert finding["suggested_rewrite"]


def test_rule_check_passes_capability_rules_through(client):
    body = client.post("/rules/check", json={"rules": ["At least 5 years experience"]}).json()
    assert body["applied"] == 1 and body["flagged"] == 0
    assert body["rules"][0]["dsl"] == "REQUIRE years_experience >= 5"


def test_rule_check_rejects_an_empty_request(client):
    assert client.post("/rules/check", json={"rules": []}).status_code == 400


def test_single_rule_endpoint(client):
    response = client.post("/rules/check-one", data={"text": "Recent graduates only"})
    assert response.status_code == 200
    assert response.json()["verdict"] == "risky"


# --------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------


def test_upload_starts_a_batch_immediately(client, upload_files):
    response = client.post("/batches", files=upload_files, data={"name": "Applicants"})
    assert response.status_code == 202
    body = response.json()
    assert body["batch_id"].startswith("batch_")
    assert body["accepted_documents"] == len(upload_files)


def test_batch_detail_reports_ingestion_progress(client, upload_files):
    batch_id = start_batch(client, upload_files, name="Applicants")
    batch = client.get(f"/batches/{batch_id}").json()
    assert batch["name"] == "Applicants"
    assert batch["total"] == len(upload_files) == batch["ready"]
    assert sum(batch["counts"].values()) == batch["total"]
    assert batch["runs"] == []


def test_a_run_over_a_batch_produces_a_shortlist(client, upload_files):
    batch_id = start_batch(client, upload_files)
    run_id = start_run(client, batch_id, plan=PLAN, name="First pass")

    run = client.get(f"/runs/{run_id}").json()
    assert run["batch_id"] == batch_id and run["name"] == "First pass"
    assert run["screened"] == run["total"] == len(upload_files)
    assert sum(run["counts"].values()) == run["screened"]
    assert run["has_shortlist"] and run["has_rules"]
    assert client.get(f"/batches/{batch_id}").json()["runs"][0]["id"] == run_id


def test_shortlist_reattaches_identity_for_human_review(client, upload_files):
    run_id = start_run(client, start_batch(client, upload_files))
    shortlist = client.get(f"/runs/{run_id}/shortlist").json()
    assert shortlist["entries"]
    assert shortlist["entries"][0]["identity"]["full_name"]

    anonymous = client.get(f"/runs/{run_id}/shortlist?reattach_identity=false").json()
    assert "identity" not in anonymous["entries"][0]


def test_run_candidates_carry_outcomes_without_identity(client, upload_files):
    run_id = start_run(client, start_batch(client, upload_files))
    results = client.get(f"/runs/{run_id}/candidates").json()["results"]
    assert results and all(r["candidate_ref"].startswith("Candidate ") for r in results)
    assert all(r["outcome"] in {"eligible", "excluded", "needs_manual_review"} for r in results)
    assert all("identity" not in r and "structured" not in r for r in results)


def test_run_audit_merges_the_batch_trail_by_default(client, upload_files):
    batch_id = start_batch(client, upload_files)
    run_id = start_run(client, batch_id, plan=PLAN)

    merged = {e["event"] for e in client.get(f"/runs/{run_id}/audit").json()["entries"]}
    assert {"batch_created", "extracted", "redaction", "plan_compiled", "scored", "run_complete"} <= merged

    own = {e["event"] for e in client.get(f"/runs/{run_id}/audit?include_batch=false").json()["entries"]}
    assert "extracted" not in own and "rule_risk_flagged" in own


def test_batch_candidates_hide_identity_by_default(client, upload_files):
    batch_id = start_batch(client, upload_files)
    candidates = client.get(f"/batches/{batch_id}/candidates").json()["candidates"]
    assert candidates
    assert all("structured" not in c and "filename" not in c for c in candidates)
    assert all(c["status"] == "ready" for c in candidates)


def test_runs_can_be_listed_and_searched(client, upload_files):
    batch_id = start_batch(client, upload_files[:2])
    first = start_run(client, batch_id, name="Strict screen")
    second = start_run(client, batch_id, name="Wider net")

    runs = client.get("/runs").json()["runs"]
    assert {r["id"] for r in runs} == {first, second}
    assert runs[0]["role_title"] == ROLE["title"] and runs[0]["batch_id"] == batch_id

    assert [r["id"] for r in client.get("/runs?q=Wider").json()["runs"]] == [second]
    assert [r["id"] for r in client.get(f"/runs?batch_id={batch_id}&q=Strict").json()["runs"]] == [first]
    # The role title is searchable too, so a term in it matches both runs.
    assert len(client.get("/runs?q=Backend").json()["runs"]) == 2
    assert client.get("/runs?q=nothing-matches").json()["runs"] == []


def test_a_run_over_an_unfinished_batch_is_a_conflict(client, upload_files, monkeypatch):
    from rescan.api.main import state

    batch_id = client.post("/batches", files=upload_files).json()["batch_id"]
    state.store.update_batch(batch_id, status="running")
    response = client.post("/runs", json={"batch_id": batch_id, "role": ROLE, "plan": PLAN})
    assert response.status_code == 409
    state.store.update_batch(batch_id, status="complete")


def test_unknown_batch_and_run_are_404(client):
    assert client.get("/batches/nope").status_code == 404
    assert client.get("/runs/nope").status_code == 404
    assert client.post("/runs", json={"batch_id": "nope", "role": ROLE}).status_code == 404


def test_invalid_role_is_rejected(client, upload_files):
    batch_id = start_batch(client, upload_files[:1])
    assert client.post("/runs", json={"batch_id": batch_id, "role": {}}).status_code == 422


def test_shortlist_before_completion_is_a_conflict(client, upload_files):
    from rescan.api.main import state

    batch_id = start_batch(client, upload_files[:1])
    run_id = client.post("/runs", json={"batch_id": batch_id, "role": ROLE}).json()["run_id"]
    wait_for(client, f"/runs/{run_id}")
    state.store.update_run(run_id, shortlist_json=None)
    if state.store.get_run(run_id)["shortlist"] is None:
        assert client.get(f"/runs/{run_id}/shortlist").status_code == 409


# --------------------------------------------------------------------------
# Archive uploads
# --------------------------------------------------------------------------


def test_zip_archives_are_expanded(samples):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path in sorted(samples.glob("*.txt")):
            archive.writestr(f"resumes/{path.name}", path.read_bytes())
        archive.writestr("__MACOSX/._junk.txt", b"junk")
        archive.writestr("notes.exe", b"nope")
    expanded = expand_uploads([("batch.zip", buffer.getvalue())])
    names = {name for name, _ in expanded}
    assert "001_priya_nair.txt" in names
    assert not any(name.endswith(".exe") for name in names)
    assert not any("junk" in name for name in names)


def test_zip_upload_ingests_end_to_end(client, samples):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path in sorted(samples.glob("*.txt")):
            archive.writestr(path.name, path.read_bytes())
    response = client.post(
        "/batches", files=[("files", ("batch.zip", buffer.getvalue(), "application/zip"))]
    )
    assert response.status_code == 202
    batch = wait_for(client, f"/batches/{response.json()['batch_id']}")
    assert batch["total"] == 10 and batch["ready"] == 10


def test_unreadable_direct_upload_is_dead_lettered_not_dropped(client):
    # Extension filtering applies to archive members. A directly uploaded file
    # is always accepted and allowed to fail visibly in the pipeline, so a
    # recruiter is never left wondering where a document went.
    response = client.post(
        "/batches", files=[("files", ("notes.exe", b"nope", "application/octet-stream"))]
    )
    assert response.status_code == 202
    batch = wait_for(client, f"/batches/{response.json()['batch_id']}")
    assert batch["counts"]["needs_manual_review"] == 1
    assert batch["counts"]["failed"] == 0


def test_llm_health_reports_the_stub_without_a_network_call(client):
    body = client.get("/health/llm").json()
    assert body["reachable"] is True and body["backend"] == "stub"


def test_llm_health_reports_an_unreachable_server_as_503(client, monkeypatch):
    from rescan.llm.client import LLMError

    monkeypatch.setattr(settings, "llm_backend", "openai")
    monkeypatch.setattr("rescan.llm.client.OpenAICompatClient.served_models", lambda self: (_ for _ in ()).throw(LLMError("down")))
    response = client.get("/health/llm")
    assert response.status_code == 503 and response.json()["reachable"] is False
