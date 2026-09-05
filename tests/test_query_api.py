import json
import time

import pytest
from fastapi.testclient import TestClient

from rescan.api.main import app
from rescan.config import settings

ROLE = {"title": "Senior Data Engineer", "required_skills": ["Python"], "min_years_experience": 5}
PLAN = (
    "Senior data engineer. Must have 5+ years experience, Python and SQL, plus AWS or GCP. "
    "Bachelor's degree or higher. Must be a native English speaker and a recent graduate from a leading company. "
    "Nice to have: Terraform. Should have led an on-call rotation."
)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", tmp_path / "q.db")
    monkeypatch.setattr(settings, "upload_dir", tmp_path / "uploads")
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def upload_files(samples):
    return [("files", (p.name, p.read_bytes(), "text/plain")) for p in sorted(samples.glob("*.txt"))]


def wait_for(client, job_id, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = client.get(f"/jobs/{job_id}/status").json()
        if status["status"] in {"complete", "failed"}:
            return status
        time.sleep(0.05)
    raise AssertionError("job did not finish")


@pytest.fixture()
def finished_job(client, upload_files):
    job_id = client.post(
        "/jobs", files=upload_files, data={"role": json.dumps(ROLE), "plan": PLAN, "rules": "[]"}
    ).json()["job_id"]
    assert wait_for(client, job_id)["status"] == "complete"
    return job_id


# --------------------------------------------------------------------------
# Language reference and parsing
# --------------------------------------------------------------------------


def test_fields_endpoint_documents_the_whole_language(client):
    body = client.get("/dsl/fields").json()
    assert body["counts"]["fields"] >= 60
    assert {"skill", "role", "qualification", "project"} == {r["name"] for r in body["records"]}
    forbidden = {f["name"]: f for f in body["forbidden"]}
    assert forbidden["region"]["statutes"] and forbidden["institution"]["alternative"]
    assert "employer" not in forbidden, "employer names are legitimate data"
    assert "REQUIRE" in body["grammar"] and body["examples"]


def test_parse_endpoint_canonicalises_programs_and_queries(client):
    program = client.post("/dsl/parse", json={"dsl": "require Total_Years_Experience >= 5 ; prefer aqf>=9 weight 2"}).json()
    assert program["ok"] and program["kind"] == "program"
    assert program["canonical"] == "REQUIRE years_experience >= 5\nPREFER aqf >= 9 WEIGHT 2"
    query = client.post("/dsl/parse", json={"dsl": 'skills has any ("Go")'}).json()
    assert query["kind"] == "query" and query["canonical"] == 'skills HAS ANY ("Go")'


def test_parse_endpoint_rejects_forbidden_fields_with_the_statute(client):
    response = client.post("/dsl/parse", json={"dsl": 'REQUIRE region = "Brisbane"'})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["kind"] == "field" and detail["forbidden"] is True
    assert any("Racial Discrimination Act" in s for s in detail["statutes"])
    assert detail["line"] == 1


def test_parse_endpoint_reports_syntax_errors_with_position(client):
    detail = client.post("/dsl/parse", json={"dsl": "REQUIRE aqf >="}).json()["detail"]
    assert detail["kind"] == "syntax" and detail["col"] > 1


# --------------------------------------------------------------------------
# Compilation
# --------------------------------------------------------------------------


def test_compile_endpoint_returns_reasoning_and_rules(client):
    body = client.post("/rules/compile", json={"plan": PLAN, "role_context": "Senior Data Engineer"}).json()
    assert body["reasoning"]
    assert body["requirements"] == 3 and body["preferences"] == 1 and body["flagged"] == 1
    by_text = {r["source_text"]: r for r in body["rules"]}
    assert by_text["Nice to have: Terraform."]["dsl"] == 'PREFER skills HAS ANY ("Terraform")'
    risky = next(r for r in body["rules"] if r["verdict"] == "risky")
    assert {f["pattern_id"] for f in risky["findings"]} >= {"native_speaker", "recent_graduate", "employer_prestige"}


def test_compile_endpoint_accepts_discrete_rules_and_a_plan_together(client):
    body = client.post("/rules/compile", json={"plan": "Ideally Kubernetes.", "rules": ["4+ years experience"]}).json()
    assert body["rules"][0]["id"] == "rule_1" and body["rules"][0]["dsl"] == "REQUIRE years_experience >= 4"
    assert body["rules"][1]["kind"] == "prefer"


def test_compile_endpoint_rejects_an_empty_request(client):
    assert client.post("/rules/compile", json={"plan": "  ", "rules": [""]}).status_code == 400


def test_rule_check_keeps_one_result_per_rule(client):
    body = client.post("/rules/check", json={"rules": ["4+ years", "Recent grads only", "Bachelor degree"]}).json()
    assert [r["id"] for r in body["rules"]] == ["rule_1", "rule_2", "rule_3"]
    assert body["applied"] == 2 and body["flagged"] == 1


# --------------------------------------------------------------------------
# Jobs from a plan, and queries over them
# --------------------------------------------------------------------------


def test_job_compiles_the_plan_and_records_it(client, finished_job):
    rules = client.get(f"/jobs/{finished_job}/rules").json()
    assert rules["reasoning"] and rules["source_plan"] == PLAN
    events = {e["event"] for e in client.get(f"/jobs/{finished_job}/audit").json()["entries"]}
    assert {"plan_compiled", "rule_risk_flagged", "rule_passed", "model_check", "scored"} <= events


def test_query_returns_reasons_and_never_identity(client, finished_job):
    body = client.post(
        f"/jobs/{finished_job}/query",
        json={"dsl": 'years_experience >= 5 AND ANY skill WHERE name = "Python"', "model_checks": False},
    ).json()
    assert body["canonical"] == 'years_experience >= 5 AND ANY skill WHERE name = "Python"'
    assert body["counts"]["matched"] + body["counts"]["not_matched"] + body["counts"]["indeterminate"] >= 9
    assert body["matched"], "some sample resumes have 5+ years and Python"
    for entry in body["matched"] + body["not_matched"] + body["indeterminate"]:
        assert entry["candidate_ref"].startswith("Candidate ")
        assert entry["reason"]
        assert "identity" not in entry and "name" not in entry
    assert body["fields"] == ["years_experience", "skill.name"]


def test_query_is_audited(client, finished_job):
    client.post(f"/jobs/{finished_job}/query", json={"dsl": "aqf >= 9", "model_checks": False})
    entries = [e for e in client.get(f"/jobs/{finished_job}/audit").json()["entries"] if e["event"] == "query_run"]
    assert entries and entries[-1]["detail"]["query"] == "aqf >= 9"
    assert "counts" in entries[-1]["detail"]


def test_query_with_a_model_check_runs_the_judge(client, finished_job):
    body = client.post(
        f"/jobs/{finished_job}/query", json={"dsl": 'ASK "Has the candidate worked with Python?"'}
    ).json()
    assert body["model_checks"] is True
    assert body["counts"]["matched"] >= 1
    off = client.post(
        f"/jobs/{finished_job}/query", json={"dsl": 'ASK "Has the candidate worked with Python?"', "model_checks": False}
    ).json()
    assert off["counts"]["matched"] == 0 and off["counts"]["indeterminate"] >= 9


def test_query_on_a_forbidden_field_is_refused_with_the_statute(client, finished_job):
    response = client.post(f"/jobs/{finished_job}/query", json={"dsl": 'region = "Brisbane"'})
    assert response.status_code == 422
    assert response.json()["detail"]["forbidden"] is True


def test_query_with_a_proxy_literal_is_refused(client, finished_job):
    response = client.post(f"/jobs/{finished_job}/query", json={"dsl": 'ASK "Is the candidate a native English speaker?"'})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["kind"] == "legal"
    assert detail["findings"][0]["pattern_id"] == "native_speaker"


def test_query_on_an_unknown_job_is_404(client):
    assert client.post("/jobs/job_nope/query", json={"dsl": "aqf >= 7"}).status_code == 404
