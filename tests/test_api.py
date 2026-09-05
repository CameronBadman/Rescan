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
RULES = [
    "At least 4 years of professional experience",
    "Bachelor degree or higher",
    "Must be a native English speaker",
]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", tmp_path / "api.db")
    monkeypatch.setattr(settings, "upload_dir", tmp_path / "uploads")
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def upload_files(samples):
    return [
        ("files", (path.name, path.read_bytes(), "text/plain"))
        for path in sorted(samples.glob("*.txt"))
    ]


def wait_for(client, job_id, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = client.get(f"/jobs/{job_id}/status").json()
        if status["status"] in {"complete", "failed"}:
            return status
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish within {timeout}s")


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
    assert body["rules"][0]["predicate"]["field"] == "total_years_experience"


def test_rule_check_rejects_an_empty_request(client):
    assert client.post("/rules/check", json={"rules": []}).status_code == 400


def test_single_rule_endpoint(client):
    response = client.post("/rules/check-one", data={"text": "Recent graduates only"})
    assert response.status_code == 200
    assert response.json()["verdict"] == "risky"


# --------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------


def test_upload_returns_a_job_id_immediately(client, upload_files):
    response = client.post(
        "/jobs", files=upload_files, data={"role": json.dumps(ROLE), "rules": json.dumps(RULES)}
    )
    assert response.status_code == 202
    body = response.json()
    assert body["job_id"].startswith("job_")
    assert body["accepted_documents"] == len(upload_files)


def test_status_endpoint_reports_progress_counts(client, upload_files):
    job_id = client.post(
        "/jobs", files=upload_files, data={"role": json.dumps(ROLE), "rules": json.dumps(RULES)}
    ).json()["job_id"]
    status = wait_for(client, job_id)
    assert status["status"] == "complete"
    assert status["total"] == len(upload_files)
    assert status["processed"] == status["total"]
    assert sum(status["counts"].values()) == status["total"]


def test_shortlist_reattaches_identity_for_human_review(client, upload_files):
    job_id = client.post(
        "/jobs", files=upload_files, data={"role": json.dumps(ROLE), "rules": json.dumps(RULES)}
    ).json()["job_id"]
    wait_for(client, job_id)

    shortlist = client.get(f"/jobs/{job_id}/shortlist").json()
    assert shortlist["entries"]
    assert shortlist["entries"][0]["identity"]["full_name"]

    anonymous = client.get(f"/jobs/{job_id}/shortlist?reattach_identity=false").json()
    assert "identity" not in anonymous["entries"][0]


def test_audit_endpoint_returns_the_decision_trail(client, upload_files):
    job_id = client.post(
        "/jobs", files=upload_files, data={"role": json.dumps(ROLE), "rules": json.dumps(RULES)}
    ).json()["job_id"]
    wait_for(client, job_id)
    events = {e["event"] for e in client.get(f"/jobs/{job_id}/audit").json()["entries"]}
    assert {"rule_risk_flagged", "redaction", "scored", "job_complete"} <= events


def test_candidates_endpoint_hides_identity_by_default(client, upload_files):
    job_id = client.post(
        "/jobs", files=upload_files, data={"role": json.dumps(ROLE), "rules": json.dumps(RULES)}
    ).json()["job_id"]
    wait_for(client, job_id)
    candidates = client.get(f"/jobs/{job_id}/candidates").json()["candidates"]
    assert candidates
    assert all("structured" not in c and "filename" not in c for c in candidates)


def test_unknown_job_is_a_404(client):
    assert client.get("/jobs/job_nope/status").status_code == 404


def test_invalid_role_is_rejected(client, upload_files):
    response = client.post("/jobs", files=upload_files, data={"role": "{not json", "rules": "[]"})
    assert response.status_code == 400


def test_shortlist_before_completion_is_a_conflict(client, upload_files, monkeypatch):
    # A job with no shortlist yet must not look like a job with an empty one.
    job_id = client.post(
        "/jobs", files=upload_files[:1], data={"role": json.dumps(ROLE), "rules": "[]"}
    ).json()["job_id"]
    from rescan.api.main import state

    state.store.update_job(job_id, shortlist_json=None)
    if state.store.get_job(job_id)["shortlist"] is None:
        assert client.get(f"/jobs/{job_id}/shortlist").status_code == 409


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


def test_zip_upload_runs_end_to_end(client, samples):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path in sorted(samples.glob("*.txt")):
            archive.writestr(path.name, path.read_bytes())
    response = client.post(
        "/jobs",
        files=[("files", ("batch.zip", buffer.getvalue(), "application/zip"))],
        data={"role": json.dumps(ROLE), "rules": json.dumps(RULES)},
    )
    assert response.status_code == 202
    status = wait_for(client, response.json()["job_id"])
    assert status["total"] == 10


def test_unreadable_direct_upload_is_dead_lettered_not_dropped(client):
    # Extension filtering applies to archive members. A directly uploaded file
    # is always accepted and allowed to fail visibly in the pipeline, so a
    # recruiter is never left wondering where a document went.
    response = client.post(
        "/jobs",
        files=[("files", ("notes.exe", b"nope", "application/octet-stream"))],
        data={"role": json.dumps(ROLE), "rules": "[]"},
    )
    assert response.status_code == 202
    status = wait_for(client, response.json()["job_id"])
    assert status["counts"]["needs_manual_review"] == 1
    assert status["counts"]["failed"] == 0
