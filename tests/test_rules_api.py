"""Changing a finished round's rules: the legal gate, and re-screening."""

import json
import time

import pytest
from fastapi.testclient import TestClient

from rescan.api.main import app
from rescan.config import settings

ROLE = {"title": "Senior Data Engineer"}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", tmp_path / "rules.db")
    monkeypatch.setattr(settings, "upload_dir", tmp_path / "uploads")
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "object_store", "local")
    with TestClient(app) as test_client:
        yield test_client


def wait_for(client, job_id, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = client.get(f"/jobs/{job_id}/status").json()
        if status["status"] in {"complete", "failed"}:
            return status
        time.sleep(0.05)
    raise AssertionError("job did not settle")


@pytest.fixture()
def finished(client, samples):
    files = [("files", (p.name, p.read_bytes(), "text/plain")) for p in sorted(samples.glob("*.txt"))]
    job_id = client.post("/jobs", files=files, data={"role": json.dumps(ROLE), "plan": "Must have 2+ years experience.", "rules": "[]"}).json()["job_id"]
    assert wait_for(client, job_id)["status"] == "complete"
    return job_id


def test_a_lawful_rule_is_added_and_the_round_is_rescreened(client, finished):
    before = client.get(f"/jobs/{finished}/shortlist").json()
    response = client.post(f"/jobs/{finished}/rules", json={"text": "At least 8 years of professional experience"})
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["added"] and body["rescreening"]
    assert body["rule"]["dsl"] == "REQUIRE years_experience >= 8"
    assert wait_for(client, finished)["status"] == "complete"

    rules = client.get(f"/jobs/{finished}/rules").json()
    assert [r["source_text"] for r in rules["rules"]][-1] == "At least 8 years of professional experience"
    after = client.get(f"/jobs/{finished}/shortlist").json()
    assert len(after["excluded"]) > len(before["excluded"]), "a stricter rule excludes more people"
    assert any("8 years" in reason for entry in after["excluded"] for reason in entry["reasons"])
    events = [e["event"] for e in client.get(f"/jobs/{finished}/audit").json()["entries"]]
    assert "rule_added" in events and "rescreen_started" in events and "rescreen_complete" in events
    assert "extracted" not in events[events.index("rescreen_started"):], "re-screening never re-extracts"


def test_a_high_risk_rule_is_refused_with_the_rewrite(client, finished):
    rules_before = client.get(f"/jobs/{finished}/rules").json()["rules"]
    response = client.post(f"/jobs/{finished}/rules", json={"text": "Must be a native English speaker"})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["kind"] == "legal"
    finding = detail["rule"]["findings"][0]
    assert any("Racial Discrimination Act" in s for s in finding["statutes"])
    assert "native" not in finding["suggested_rewrite"].lower()
    assert client.get(f"/jobs/{finished}/rules").json()["rules"] == rules_before, "nothing was added"
    events = [e["event"] for e in client.get(f"/jobs/{finished}/audit").json()["entries"]]
    assert "rule_rejected" in events and "rescreen_started" not in events


def test_a_review_level_rule_is_added_with_a_note(client, finished):
    response = client.post(f"/jobs/{finished}/rules", json={"text": "3+ years of experience at a leading company"})
    assert response.status_code == 202
    rule = response.json()["rule"]
    assert rule["risk"] == "review" and rule["dsl"] == "REQUIRE years_experience >= 3"
    assert any("justification" in note for note in rule["notes"])
    wait_for(client, finished)


def test_removing_a_rule_rescreens_without_it(client, finished):
    added = client.post(f"/jobs/{finished}/rules", json={"text": "At least 8 years of professional experience"}).json()["rule"]
    wait_for(client, finished)
    excluded_with = len(client.get(f"/jobs/{finished}/shortlist").json()["excluded"])

    response = client.delete(f"/jobs/{finished}/rules/{added['id']}")
    assert response.status_code == 202
    wait_for(client, finished)
    assert all(r["id"] != added["id"] for r in client.get(f"/jobs/{finished}/rules").json()["rules"])
    assert len(client.get(f"/jobs/{finished}/shortlist").json()["excluded"]) < excluded_with
    assert client.delete(f"/jobs/{finished}/rules/rule_999").status_code == 404


def test_rule_ids_stay_unique_after_removals(client, finished):
    first = client.post(f"/jobs/{finished}/rules", json={"text": "Bachelor degree or higher"}).json()["rule"]
    wait_for(client, finished)
    client.delete(f"/jobs/{finished}/rules/{first['id']}")
    wait_for(client, finished)
    second = client.post(f"/jobs/{finished}/rules", json={"text": "Master degree or higher"}).json()["rule"]
    wait_for(client, finished)
    ids = [r["id"] for r in client.get(f"/jobs/{finished}/rules").json()["rules"]]
    assert len(ids) == len(set(ids)) and second["id"] in ids


def test_unknown_job_and_empty_rule(client):
    assert client.post("/jobs/nope/rules", json={"text": "x"}).status_code == 404
