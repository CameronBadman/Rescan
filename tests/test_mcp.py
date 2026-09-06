import json

import pytest

from rescan.mcp_server import server


def unwrap(result):
    if getattr(result, "structuredContent", None):
        return result.structuredContent
    return json.loads(result.content[0].text)


async def test_all_tools_are_exposed_with_descriptions():
    tools = {tool.name: tool for tool in await server.list_tools()}
    assert {
        "check_screening_rule",
        "check_screening_rules",
        "list_known_risky_phrases",
        "map_qualification_to_aqf",
    } <= set(tools)
    for tool in tools.values():
        assert tool.description and len(tool.description) > 40


async def test_risky_rule_returns_statute_and_rewrite():
    result = unwrap(
        await server.call_tool("check_screening_rule", {"rule_text": "Must be a native English speaker"})
    )
    assert result["verdict"] == "risky"
    assert result["applied_automatically"] is False
    finding = result["findings"][0]
    assert any("Racial Discrimination Act" in s for s in finding["statutes"])
    assert "native" not in finding["suggested_rewrite"].lower()


async def test_sound_rule_returns_the_compiled_test():
    result = unwrap(
        await server.call_tool("check_screening_rule", {"rule_text": "At least 5 years of Python experience"})
    )
    assert result["verdict"] == "applicable"
    assert result["test"]["dsl"].startswith("REQUIRE years_experience >= 5")
    assert "Python" in result["test"]["dsl"]
    assert result["test"]["clause"]["kind"] == "require"


async def test_batch_check_counts_applied_and_flagged():
    result = unwrap(
        await server.call_tool(
            "check_screening_rules",
            {"rules": ["Recent graduates only", "Bachelor degree required"]},
        )
    )
    assert result["applied"] == 1
    assert result["flagged"] == 1
    assert len(result["results"]) == 2


async def test_known_phrase_list_carries_statutes():
    result = unwrap(await server.call_tool("list_known_risky_phrases", {}))
    assert len(result["patterns"]) >= 16
    assert all(p["statutes"] and p["suggested_rewrite"] for p in result["patterns"])
    assert "not legal advice" in result["disclaimer"]


@pytest.mark.parametrize(
    "title,level",
    [("B.Eng (Hons)", 8), ("Bachelor of Science", 7), ("PhD", 10), ("Diploma of Nursing", 5)],
)
async def test_aqf_mapping_tool(title, level):
    result = unwrap(await server.call_tool("map_qualification_to_aqf", {"title": title}))
    assert result["aqf_level"] == level
    assert result["needs_human_check"] is False


async def test_unmappable_qualification_asks_for_a_human_rather_than_failing():
    result = unwrap(await server.call_tool("map_qualification_to_aqf", {"title": "Nanodegree in AI"}))
    assert result["aqf_level"] is None
    assert result["needs_human_check"] is True


# --------------------------------------------------------------------------
# The rule language over MCP
# --------------------------------------------------------------------------


async def test_new_tools_are_exposed():
    tools = {tool.name for tool in await server.list_tools()}
    assert {"compile_hiring_plan", "describe_query_language", "parse_query", "query_candidates"} <= tools


async def test_compile_hiring_plan_returns_reasoning_and_a_program():
    result = unwrap(
        await server.call_tool(
            "compile_hiring_plan",
            {"plan": "Must have 5+ years experience with Python. Nice to have: Kubernetes. Recent graduates preferred."},
        )
    )
    assert result["reasoning"]
    assert result["requirements"] == 1 and result["preferences"] == 1 and result["flagged"] == 1
    assert "REQUIRE years_experience >= 5" in result["program"]
    assert 'PREFER skills HAS ANY ("Kubernetes")' in result["program"]


async def test_describe_and_parse():
    reference = unwrap(await server.call_tool("describe_query_language", {}))
    assert reference["counts"]["fields"] >= 60
    ok = unwrap(await server.call_tool("parse_query", {"dsl": "require aqf >= 7"}))
    assert ok["ok"] and ok["canonical"] == "REQUIRE aqf >= 7"
    bad = unwrap(await server.call_tool("parse_query", {"dsl": 'region = "Brisbane"'}))
    assert bad["ok"] is False and bad["error"]["forbidden"] is True and bad["error"]["statutes"]


async def test_query_candidates_over_a_processed_job(tmp_path, monkeypatch, samples):
    import rescan.mcp_server as mcp_module
    from rescan.config import settings
    from rescan.extract import Extractor
    from rescan.llm.client import build_client
    from rescan.pipeline.runner import PipelineRunner
    from rescan.schemas import RoleSpec
    from rescan.store import Store

    monkeypatch.setattr(settings, "upload_dir", tmp_path / "uploads")
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    store = Store(tmp_path / "mcp.db")
    monkeypatch.setattr(mcp_module, "_store", store)
    runner = PipelineRunner(store, build_client("stub"), Extractor())
    files = [(p.name, p.read_bytes()) for p in sorted(samples.glob("*.txt"))[:5]]
    job_id = runner.create_job(RoleSpec(title="Engineer"), files, job_id="job_mcp")
    runner.run_job(job_id, [])

    result = unwrap(await server.call_tool("query_candidates", {"job_id": "job_mcp", "dsl": "years_experience >= 3", "model_checks": False}))
    assert result["counts"]["matched"] + result["counts"]["not_matched"] + result["counts"]["indeterminate"] == 5
    assert all("reason" in e for e in result["matched"] + result["not_matched"])

    missing = unwrap(await server.call_tool("query_candidates", {"job_id": "nope", "dsl": "aqf >= 7"}))
    assert missing["ok"] is False and missing["error"]["kind"] == "not_found"
    refused = unwrap(await server.call_tool("query_candidates", {"job_id": "job_mcp", "dsl": 'ASK "Is the candidate a recent graduate?"'}))
    assert refused["ok"] is False and refused["error"]["kind"] == "legal"
    store.close()


# --------------------------------------------------------------------------
# Remote mode: the same tools against a deployed API
# --------------------------------------------------------------------------


@pytest.fixture()
def remote(monkeypatch, tmp_path):
    """Point the MCP server at a fake deployed API: our own app under a test client."""
    from fastapi.testclient import TestClient
    import rescan.mcp_server as mcp_module
    from rescan.api.main import app
    from rescan.config import settings

    monkeypatch.setattr(settings, "db_path", tmp_path / "remote.db")
    monkeypatch.setattr(settings, "upload_dir", tmp_path / "uploads")
    monkeypatch.setattr(settings, "object_store", "local")
    monkeypatch.setattr(settings, "local_object_store_dir", tmp_path / "bucket")
    calls = []

    class Recording(TestClient):
        def request(self, method, url, **kwargs):
            calls.append((method, str(url)))
            return super().request(method, url, **kwargs)

    client = Recording(app, base_url="http://deployed", headers={"X-API-Key": "k"})
    client.__enter__()
    monkeypatch.setattr(settings, "mcp_remote_url", "http://deployed")
    monkeypatch.setattr(settings, "mcp_remote_key", "k")
    monkeypatch.setattr(mcp_module, "_remote", mcp_module.Remote("http://deployed", "k", client=client))
    yield calls
    monkeypatch.setattr(mcp_module, "_remote", None)
    client.__exit__(None, None, None)


async def test_remote_mode_routes_rule_tools_through_the_api(remote):
    if True:
        one = unwrap(await server.call_tool("check_screening_rule", {"rule_text": "Must be a native English speaker"}))
        assert one["verdict"] == "risky"
        plan = unwrap(await server.call_tool("compile_hiring_plan", {"plan": "Must have 5+ years experience. Nice to have: Kubernetes."}))
        assert plan["requirements"] == 1 and plan["preferences"] == 1 and plan["reasoning"]
        parsed = unwrap(await server.call_tool("parse_query", {"dsl": 'region = "x"'}))
        assert parsed["ok"] is False and parsed["error"]["forbidden"] is True
        ref = unwrap(await server.call_tool("describe_query_language", {}))
        assert ref["counts"]["fields"] >= 60
    assert [c[1].rsplit("deployed", 1)[-1] for c in remote] == ["/rules/check", "/rules/compile", "/dsl/parse", "/dsl/fields"]


async def test_remote_mode_job_tools(remote, tmp_path, samples):
    from rescan.ingest import LocalObjectStore

    store = LocalObjectStore(tmp_path / "bucket")
    for path in sorted(samples.glob("*.txt"))[:3]:
        store.put_object(f"jobs/agent-1/{path.name}", path.read_bytes())

    if True:
        started = unwrap(await server.call_tool(
            "start_job_from_bucket",
            {"job_id": "agent-1", "role_title": "Engineer", "plan": "Must have 2+ years experience."},
        ))
        assert started["accepted_documents"] == 3
        import time
        for _ in range(200):
            status = unwrap(await server.call_tool("job_status", {"job_id": "agent-1"}))
            if status["status"] in {"complete", "failed"}:
                break
            time.sleep(0.05)
        assert status["status"] == "complete"
        rules = unwrap(await server.call_tool("job_rules", {"job_id": "agent-1"}))
        assert rules["rules"][0]["dsl"] == "REQUIRE years_experience >= 2"
        shortlist = unwrap(await server.call_tool("job_shortlist", {"job_id": "agent-1"}))
        assert "identity" not in (shortlist["entries"] + shortlist["excluded"])[0], "anonymized by default"
        audit = unwrap(await server.call_tool("job_audit", {"job_id": "agent-1", "event": "plan_compiled"}))
        assert audit["entries"] and "plan_compiled" in audit["events"]
        listed = unwrap(await server.call_tool("list_jobs", {}))
        assert any(j["id"] == "agent-1" for j in listed["jobs"])
        missing = unwrap(await server.call_tool("job_status", {"job_id": "nope"}))
        assert missing["ok"] is False and missing["error"]["kind"] == "not_found"
        again = unwrap(await server.call_tool("start_job_from_bucket", {"job_id": "agent-1", "role_title": "Engineer"}))
        assert again["ok"] is False and again["error"]["kind"] == "conflict"


async def test_local_mode_job_tools_report_missing_jobs():
    import rescan.mcp_server as mcp_module
    from rescan.store import Store
    import tempfile, pathlib

    with tempfile.TemporaryDirectory() as tmp:
        store = Store(pathlib.Path(tmp) / "l.db")
        mcp_module._store = store
        try:
            assert unwrap(await server.call_tool("job_status", {"job_id": "nope"}))["ok"] is False
            assert unwrap(await server.call_tool("job_shortlist", {"job_id": "nope"}))["ok"] is False
            assert unwrap(await server.call_tool("list_jobs", {}))["jobs"] == []
        finally:
            mcp_module._store = None
            store.close()
