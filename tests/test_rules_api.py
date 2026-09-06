"""Changing an analysis run's rules: the legal gate, and re-screening."""

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
    monkeypatch.setattr(settings, "local_object_store_dir", tmp_path / "bucket")
    with TestClient(app) as test_client:
        yield test_client


def wait_for(client, path, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(path).json()
        if body["status"] in {"complete", "failed"}:
            return body
        time.sleep(0.05)
    raise AssertionError(f"{path} did not settle")


@pytest.fixture()
def batch(client, samples):
    files = [("files", (p.name, p.read_bytes(), "text/plain")) for p in sorted(samples.glob("*.txt"))]
    batch_id = client.post("/batches", files=files).json()["batch_id"]
    assert wait_for(client, f"/batches/{batch_id}")["status"] == "complete"
    return batch_id


@pytest.fixture()
def run(client, batch):
    """An analysis run over an ingested batch, already screened once."""
    run_id = client.post(
        "/runs", json={"batch_id": batch, "role": ROLE, "plan": "Must have 2+ years experience."}
    ).json()["run_id"]
    assert wait_for(client, f"/runs/{run_id}")["status"] == "complete"
    return run_id


def test_a_lawful_rule_is_added_and_the_run_is_rescreened(client, run):
    before = client.get(f"/runs/{run}/shortlist").json()
    response = client.post(f"/runs/{run}/rules", json={"text": "At least 8 years of professional experience"})
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["added"] and body["rescreening"]
    assert body["rule"]["dsl"] == "REQUIRE years_experience >= 8"
    assert wait_for(client, f"/runs/{run}")["status"] == "complete"

    rules = client.get(f"/runs/{run}/rules").json()
    assert [r["source_text"] for r in rules["rules"]][-1] == "At least 8 years of professional experience"
    after = client.get(f"/runs/{run}/shortlist").json()
    assert len(after["excluded"]) > len(before["excluded"]), "a stricter rule excludes more people"
    assert any("8 years" in reason for entry in after["excluded"] for reason in entry["reasons"])
    events = [e["event"] for e in client.get(f"/runs/{run}/audit?include_batch=false").json()["entries"]]
    assert "rule_added" in events and "rescreen_started" in events and "rescreen_complete" in events
    assert "extracted" not in events, "re-screening never re-extracts"


def test_a_high_risk_rule_is_refused_with_the_rewrite(client, run):
    rules_before = client.get(f"/runs/{run}/rules").json()["rules"]
    response = client.post(f"/runs/{run}/rules", json={"text": "Must be a native English speaker"})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["kind"] == "legal"
    finding = detail["rule"]["findings"][0]
    assert any("Racial Discrimination Act" in s for s in finding["statutes"])
    assert "native" not in finding["suggested_rewrite"].lower()
    assert client.get(f"/runs/{run}/rules").json()["rules"] == rules_before, "nothing was added"
    events = [e["event"] for e in client.get(f"/runs/{run}/audit?include_batch=false").json()["entries"]]
    assert "rule_rejected" in events and "rescreen_started" not in events


def test_a_review_level_rule_is_added_with_a_note(client, run):
    response = client.post(f"/runs/{run}/rules", json={"text": "3+ years of experience at a leading company"})
    assert response.status_code == 202
    rule = response.json()["rule"]
    assert rule["risk"] == "review" and rule["dsl"] == "REQUIRE years_experience >= 3"
    assert any("justification" in note for note in rule["notes"])
    wait_for(client, f"/runs/{run}")


def test_removing_a_rule_rescreens_without_it(client, run):
    added = client.post(f"/runs/{run}/rules", json={"text": "At least 8 years of professional experience"}).json()["rule"]
    wait_for(client, f"/runs/{run}")
    excluded_with = len(client.get(f"/runs/{run}/shortlist").json()["excluded"])

    response = client.delete(f"/runs/{run}/rules/{added['id']}")
    assert response.status_code == 202
    wait_for(client, f"/runs/{run}")
    assert all(r["id"] != added["id"] for r in client.get(f"/runs/{run}/rules").json()["rules"])
    assert len(client.get(f"/runs/{run}/shortlist").json()["excluded"]) < excluded_with
    assert client.delete(f"/runs/{run}/rules/rule_999").status_code == 404


def test_rule_ids_stay_unique_after_removals(client, run):
    first = client.post(f"/runs/{run}/rules", json={"text": "Bachelor degree or higher"}).json()["rule"]
    wait_for(client, f"/runs/{run}")
    client.delete(f"/runs/{run}/rules/{first['id']}")
    wait_for(client, f"/runs/{run}")
    second = client.post(f"/runs/{run}/rules", json={"text": "Master degree or higher"}).json()["rule"]
    wait_for(client, f"/runs/{run}")
    ids = [r["id"] for r in client.get(f"/runs/{run}/rules").json()["rules"]]
    assert len(ids) == len(set(ids)) and second["id"] in ids


def test_unknown_run_and_empty_rule(client, run):
    assert client.post("/runs/nope/rules", json={"text": "x"}).status_code == 404
    assert client.post(f"/runs/{run}/rules", json={"text": "   "}).status_code == 400


def test_a_new_run_can_reuse_a_tuned_rule_set(client, batch, run):
    client.post(f"/runs/{run}/rules", json={"text": "At least 8 years of professional experience"})
    wait_for(client, f"/runs/{run}")
    tuned = client.get(f"/runs/{run}/rules").json()

    response = client.post("/runs", json={"batch_id": batch, "role": ROLE, "rules_from": run, "name": "Copy"})
    assert response.status_code == 202, response.text
    copy_id = response.json()["run_id"]
    wait_for(client, f"/runs/{copy_id}")

    copied = client.get(f"/runs/{copy_id}/rules").json()
    assert [r["dsl"] for r in copied["rules"]] == [r["dsl"] for r in tuned["rules"]]
    events = [e["event"] for e in client.get(f"/runs/{copy_id}/audit?include_batch=false").json()["entries"]]
    assert "rules_copied" in events and "plan_compiled" not in events
    assert client.post("/runs", json={"batch_id": batch, "role": ROLE, "rules_from": "nope"}).status_code == 404


def test_editing_one_run_leaves_the_other_alone(client, batch, run):
    other = client.post(
        "/runs", json={"batch_id": batch, "role": ROLE, "plan": "Must have 2+ years experience."}
    ).json()["run_id"]
    wait_for(client, f"/runs/{other}")
    before = client.get(f"/runs/{other}/shortlist?reattach_identity=false").json()

    client.post(f"/runs/{run}/rules", json={"text": "At least 12 years of professional experience"})
    wait_for(client, f"/runs/{run}")

    assert client.get(f"/runs/{other}/shortlist?reattach_identity=false").json() == before
    assert len(client.get(f"/runs/{other}/rules").json()["rules"]) < len(
        client.get(f"/runs/{run}/rules").json()["rules"]
    )


def test_a_known_proxy_is_refused_without_calling_the_model(client, run, monkeypatch):
    """The statute table is authoritative, so the recruiter waits on nothing."""
    from rescan.api.main import state

    def boom(request):
        raise AssertionError("no model call should be needed to refuse a known proxy")

    monkeypatch.setattr(state.runner.client, "json_call", boom)
    response = client.post(f"/runs/{run}/rules", json={"text": "Must be a recent graduate"})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["rule"]["findings"][0]["pattern_id"] == "recent_graduate"
    assert any("Age Discrimination Act" in s for s in detail["rule"]["findings"][0]["statutes"])
