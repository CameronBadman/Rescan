"""MCP server exposing the rule engine and a job's results.

The legal-risk classifier is useful outside this application: a recruiter
writing a job ad in any MCP-capable client should be able to check the wording
before it becomes a screening rule.

Two modes. In-process (default): the tools run the pipeline code directly
against the local store. Remote: with RESCAN_MCP_REMOTE_URL (and _KEY) set,
every tool calls the deployed HTTP API instead, so an agent on a laptop drives
the real deployment with the same tool surface.

Run it:

    python -m rescan.mcp_server              # stdio, for a local MCP client
    python -m rescan.mcp_server --http       # streamable HTTP
"""

from __future__ import annotations

import argparse
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer

from rescan.aqf import AQF_LABELS, map_to_aqf
from rescan.config import settings
from rescan.dsl import DslError, parse_expr, parse_program
from rescan.dsl.fields import reference as dsl_reference
from rescan.dsl.judge import Judge
from rescan.llm.client import build_client
from rescan.pipeline.query import QueryRejected, run_query
from rescan.rules.classifier import classify_rule, compile_plan
from rescan.rules.models import ClassifiedRule, RuleSet
from rescan.rules.statutes import RISK_PATTERNS, STATUTES, statute_citations

server = MCPServer(
    "rescan",
    instructions=(
        "Reviews recruitment screening rules under Australian anti-discrimination "
        "law and maps qualifications to the Australian Qualifications Framework. "
        "Findings cite a statute and offer a measurable rewrite. This is "
        "decision support, not legal advice."
    ),
)

_client = None
_store = None
_remote = None


class Remote:
    """The deployed API, as the MCP tools see it."""

    def __init__(self, base_url: str, api_key: str = "", client: httpx.Client | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        headers = {"X-API-Key": api_key} if api_key else {}
        self.client = client or httpx.Client(base_url=self.base_url, headers=headers, timeout=300.0)

    def call(self, method: str, path: str, **kwargs: Any) -> tuple[int, Any]:
        response = self.client.request(method, path, **kwargs)
        try:
            body = response.json()
        except ValueError:
            body = {"detail": response.text[:500]}
        return response.status_code, body

    def ok(self, method: str, path: str, **kwargs: Any) -> Any:
        status, body = self.call(method, path, **kwargs)
        if status >= 400:
            raise RemoteError(status, body)
        return body


class RemoteError(RuntimeError):
    def __init__(self, status: int, body: Any) -> None:
        super().__init__(f"API returned {status}")
        self.status = status
        self.body = body

    def to_dict(self) -> dict[str, Any]:
        detail = self.body.get("detail") if isinstance(self.body, dict) else self.body
        if isinstance(detail, dict) and "kind" in detail:
            return {"ok": False, "error": detail}
        kind = {404: "not_found", 401: "unauthorised", 409: "conflict", 422: "invalid"}.get(self.status, "api_error")
        return {"ok": False, "error": {"kind": kind, "status": self.status, "message": detail}}


def _api() -> Remote | None:
    """The remote API when configured, else None (run in-process)."""
    global _remote
    if not settings.mcp_remote_url:
        return None
    if _remote is None:
        _remote = Remote(settings.mcp_remote_url, settings.mcp_remote_key)
    return _remote


def _llm():
    # Built lazily so importing this module does not open a connection.
    global _client
    if _client is None:
        _client = build_client()
    return _client


def _db():
    global _store
    if _store is None:
        from rescan.store import Store

        _store = Store()
    return _store


def _rule_payload(rule) -> dict[str, Any]:
    return {
        "rule": rule.source_text,
        "verdict": rule.verdict.value,
        "risk": rule.risk.value,
        "applied_automatically": rule.is_applied,
        "kind": rule.kind,
        "test": (
            {"dsl": rule.dsl, "clause": rule.clause.model_dump(mode="json")}
            if rule.clause is not None
            else None
        ),
        "justification": rule.justification,
        "legal_basis": rule.legal_basis,
        "findings": [
            {
                "matched_text": finding.matched_text,
                "risk": finding.risk.value,
                "protected_attributes": finding.protected_attributes,
                "statutes": finding.statutes,
                "explanation": finding.explanation,
                "suggested_rewrite": finding.suggested_rewrite,
            }
            for finding in rule.findings
        ],
        "notes": rule.notes,
    }


@server.tool(
    name="check_screening_rule",
    title="Check a recruitment screening rule",
    description=(
        "Review one free-text screening rule under Australian anti-discrimination "
        "law. Returns whether it screens on a protected attribute or a proxy for "
        "one, the statutes engaged, a measurable rewrite, and — when the rule is "
        "sound — the structured test it compiles to. High-risk rules are never "
        "compiled into a filter."
    ),
)
def check_screening_rule(rule_text: str, role_context: str | None = None) -> dict[str, Any]:
    api = _api()
    if api is not None:
        body = api.ok("POST", "/rules/check", json={"rules": [rule_text], "role_context": role_context})
        return _rule_payload(ClassifiedRule.model_validate(body["rules"][0]))
    return _rule_payload(classify_rule(rule_text, _llm(), role_context=role_context))


@server.tool(
    name="check_screening_rules",
    title="Check a set of screening rules",
    description=(
        "Review several screening rules at once, for example every requirement in "
        "a job advertisement. Returns one result per rule plus counts of how many "
        "would be applied and how many were flagged."
    ),
)
def check_screening_rules(rules: list[str], role_context: str | None = None) -> dict[str, Any]:
    api = _api()
    if api is not None:
        body = api.ok("POST", "/rules/check", json={"rules": rules, "role_context": role_context})
        rule_set = RuleSet.model_validate({"rules": body["rules"], "reasoning": body.get("reasoning")})
    else:
        rule_set = compile_plan(None, _llm(), rule_texts=rules, role_context=role_context)
    return {
        "results": [_rule_payload(rule) for rule in rule_set.rules],
        "applied": len(rule_set.applied),
        "flagged": len(rule_set.flagged),
    }


@server.tool(
    name="compile_hiring_plan",
    title="Compile a hiring plan into screening rules",
    description=(
        "Turn a recruiter's whole hiring plan — free text, any length — into rules "
        "in the screening language, under Australian anti-discrimination law. The "
        "model reasons over the plan with the statutes in front of it and returns "
        "that reasoning with one compiled clause per requirement: REQUIRE clauses "
        "screen, PREFER clauses rank. Proxies for protected attributes are flagged "
        "with a statute and a rewrite, never compiled."
    ),
)
def compile_hiring_plan(plan: str, role_context: str | None = None) -> dict[str, Any]:
    api = _api()
    if api is not None:
        body = api.ok("POST", "/rules/compile", json={"plan": plan, "role_context": role_context})
        rule_set = RuleSet.model_validate(
            {"rules": body["rules"], "reasoning": body.get("reasoning"), "source_plan": body.get("source_plan")}
        )
    else:
        rule_set = compile_plan(plan, _llm(), role_context=role_context)
    return {
        "reasoning": rule_set.reasoning,
        "rules": [_rule_payload(rule) for rule in rule_set.rules],
        "program": "\n".join(rule.dsl for rule in rule_set.applied if rule.dsl),
        "applied": len(rule_set.applied),
        "requirements": len(rule_set.requirements),
        "preferences": len(rule_set.preferences),
        "flagged": len(rule_set.flagged),
    }


@server.tool(
    name="describe_query_language",
    title="Describe the screening query language",
    description=(
        "The grammar, every queryable field and record, the aggregates, and every "
        "forbidden identifier with the statute it engages. Read this before writing "
        "a rule or a query by hand."
    ),
)
def describe_query_language() -> dict[str, Any]:
    api = _api()
    if api is not None:
        return api.ok("GET", "/dsl/fields")
    return dsl_reference()


@server.tool(
    name="parse_query",
    title="Validate a rule program or query",
    description=(
        "Parse text in the screening language without running it. Returns the "
        "canonical form, or the error — a forbidden identifier comes back with its "
        "legal basis and the alternative to use instead."
    ),
)
def parse_query(dsl: str) -> dict[str, Any]:
    text = dsl.strip()
    api = _api()
    if api is not None:
        status, body = api.call("POST", "/dsl/parse", json={"dsl": text})
        if status >= 400:
            return RemoteError(status, body).to_dict()
        return {"ok": True, "kind": body["kind"], "canonical": body["canonical"]}
    try:
        if text[:7].upper().startswith(("REQUIRE", "PREFER")):
            program = parse_program(text)
            return {"ok": True, "kind": "program", "canonical": program.to_dsl()}
        expr = parse_expr(text)
        return {"ok": True, "kind": "query", "canonical": expr.to_dsl()}
    except DslError as exc:
        return {"ok": False, "error": exc.to_dict()}


@server.tool(
    name="query_candidates",
    title="Query a job's candidates",
    description=(
        "Run a query in the screening language over the anonymized profiles of a "
        "processed job. Returns matched, not matched and undecided candidates by "
        "reference with a plain-language reason each; never identity. Queries pass "
        "the same legal gate as rules and are written to the job's audit trail."
    ),
)
def query_candidates(job_id: str, dsl: str, model_checks: bool = True) -> dict[str, Any]:
    api = _api()
    if api is not None:
        try:
            return api.ok("POST", f"/jobs/{job_id}/query", json={"dsl": dsl, "model_checks": model_checks})
        except RemoteError as exc:
            return exc.to_dict()
    judge = Judge(_llm(), ensemble=False) if model_checks else None
    try:
        return run_query(_db(), job_id, dsl, judge=judge, actor="mcp")
    except KeyError:
        return {"ok": False, "error": {"kind": "not_found", "message": f"unknown job {job_id!r}"}}
    except DslError as exc:
        return {"ok": False, "error": exc.to_dict()}
    except QueryRejected as exc:
        return {"ok": False, "error": exc.to_dict()}


@server.tool(
    name="list_jobs",
    title="List hiring rounds",
    description="The jobs the service knows about, newest first, with their status.",
)
def list_jobs(limit: int = 20) -> dict[str, Any]:
    api = _api()
    if api is not None:
        return api.ok("GET", "/jobs", params={"limit": limit})
    return {"jobs": _db().list_jobs(limit=limit)}


@server.tool(
    name="start_job_from_bucket",
    title="Start a job from resumes in the bucket",
    description=(
        "Pull the resumes under <prefix>/<job_id>/ in the object store, compile the "
        "hiring plan and/or rules, and run the whole pipeline: extraction, structuring, "
        "anonymization, screening, ranking. Returns immediately with the job id; poll "
        "job_status. The bucket's job id becomes the job id."
    ),
)
def start_job_from_bucket(
    job_id: str, role_title: str, plan: str | None = None, rules: list[str] | None = None,
    role_description: str | None = None,
) -> dict[str, Any]:
    role = {"title": role_title, "description": role_description}
    api = _api()
    if api is not None:
        try:
            return api.ok("POST", "/jobs/from-bucket", json={"job_id": job_id, "role": role, "plan": plan, "rules": rules or []})
        except RemoteError as exc:
            return exc.to_dict()
    from rescan.extract import Extractor
    from rescan.ingest import ObjectStoreError, build_object_store, pull_job_documents
    from rescan.pipeline.runner import PipelineRunner
    from rescan.schemas import RoleSpec

    store = _db()
    if store.get_job(job_id) is not None:
        return {"ok": False, "error": {"kind": "conflict", "message": f"job {job_id!r} already exists"}}
    try:
        pull = pull_job_documents(build_object_store(), job_id)
    except ObjectStoreError as exc:
        return {"ok": False, "error": {"kind": "object_store", "message": str(exc)}}
    if not pull.documents:
        return {"ok": False, "error": {"kind": "not_found", "message": f"no usable documents under {pull.prefix!r}", "skipped": pull.skipped}}
    runner = PipelineRunner(store, _llm(), Extractor())
    runner.create_job(RoleSpec.model_validate(role), pull.documents, job_id=job_id)
    import threading

    threading.Thread(target=lambda: runner.run_job(job_id, rules or [], plan=plan), daemon=True).start()
    return {"job_id": job_id, "prefix": pull.prefix, "accepted_documents": len(pull.documents), "skipped": pull.skipped}


@server.tool(
    name="add_rule_to_job",
    title="Add a screening rule to a round",
    description=(
        "Check a plain-language rule under Australian anti-discrimination law and, if it "
        "is not high risk, compile it, add it to the round and re-screen every candidate "
        "from their stored anonymized profile. A high-risk rule is NOT added: the result "
        "carries the finding, the statute and a measurable rewrite to propose instead."
    ),
)
def add_rule_to_job(job_id: str, text: str) -> dict[str, Any]:
    api = _api()
    if api is not None:
        status, body = api.call("POST", f"/jobs/{job_id}/rules", json={"text": text})
        if status == 422 and isinstance(body, dict) and isinstance(body.get("detail"), dict):
            return {"ok": False, "added": False, "error": body["detail"]}
        if status >= 400:
            return RemoteError(status, body).to_dict()
        return {"ok": True, **body}
    from rescan.pipeline.runner import PipelineRunner, RuleRejected
    from rescan.extract import Extractor

    runner = PipelineRunner(_db(), _llm(), Extractor())
    try:
        rule = runner.add_rule(job_id, text)
    except KeyError as exc:
        return {"ok": False, "error": {"kind": "not_found", "message": str(exc)}}
    except RuleRejected as exc:
        return {"ok": False, "added": False, "error": {"kind": "legal", "rule": _rule_payload(exc.rule)}}
    import threading

    threading.Thread(target=lambda: runner.rescreen(job_id), daemon=True).start()
    return {"ok": True, "rule": _rule_payload(rule), "added": True, "rescreening": True}


@server.tool(
    name="remove_rule_from_job",
    title="Remove a screening rule from a round",
    description="Remove a rule by id and re-screen every candidate without it.",
)
def remove_rule_from_job(job_id: str, rule_id: str) -> dict[str, Any]:
    api = _api()
    if api is not None:
        try:
            return {"ok": True, **api.ok("DELETE", f"/jobs/{job_id}/rules/{rule_id}")}
        except RemoteError as exc:
            return exc.to_dict()
    from rescan.pipeline.runner import PipelineRunner
    from rescan.extract import Extractor

    runner = PipelineRunner(_db(), _llm(), Extractor())
    try:
        runner.remove_rule(job_id, rule_id)
    except KeyError as exc:
        return {"ok": False, "error": {"kind": "not_found", "message": str(exc)}}
    import threading

    threading.Thread(target=lambda: runner.rescreen(job_id), daemon=True).start()
    return {"ok": True, "removed": rule_id, "rescreening": True}


@server.tool(
    name="job_status",
    title="Job progress",
    description="Per-status candidate counts for a job: pending, extracting, ..., complete, needs_manual_review, failed.",
)
def job_status(job_id: str) -> dict[str, Any]:
    api = _api()
    if api is not None:
        try:
            return api.ok("GET", f"/jobs/{job_id}/status")
        except RemoteError as exc:
            return exc.to_dict()
    job = _db().get_job(job_id)
    if job is None:
        return {"ok": False, "error": {"kind": "not_found", "message": f"unknown job {job_id!r}"}}
    counts = _db().status_counts(job_id)
    return {"job_id": job_id, "status": job["status"], "counts": counts, "total": sum(counts.values()), "error": job["error"]}


@server.tool(
    name="job_rules",
    title="A job's compiled rules",
    description="The compiled rule set for a job: each rule's verdict, risk findings with statutes, the clause in the rule language, and the model's reasoning over the plan.",
)
def job_rules(job_id: str) -> dict[str, Any]:
    api = _api()
    if api is not None:
        try:
            return api.ok("GET", f"/jobs/{job_id}/rules")
        except RemoteError as exc:
            return exc.to_dict()
    job = _db().get_job(job_id)
    if job is None:
        return {"ok": False, "error": {"kind": "not_found", "message": f"unknown job {job_id!r}"}}
    return job["rules"] or {"rules": []}


@server.tool(
    name="job_shortlist",
    title="A job's shortlist",
    description=(
        "The ranked shortlist with per-criterion scores, the near-misses below the cutoff, "
        "everyone excluded with the plain-language reason, and everyone sent to manual review. "
        "Anonymized by default; set reattach_identity=true only for the human reviewer step."
    ),
)
def job_shortlist(job_id: str, reattach_identity: bool = False) -> dict[str, Any]:
    api = _api()
    if api is not None:
        try:
            return api.ok("GET", f"/jobs/{job_id}/shortlist", params={"reattach_identity": str(reattach_identity).lower()})
        except RemoteError as exc:
            return exc.to_dict()
    job = _db().get_job(job_id)
    if job is None:
        return {"ok": False, "error": {"kind": "not_found", "message": f"unknown job {job_id!r}"}}
    if job["shortlist"] is None:
        return {"ok": False, "error": {"kind": "conflict", "message": f"job {job_id!r} has no shortlist yet"}}
    shortlist = job["shortlist"]
    if reattach_identity:
        identities = {
            c["candidate_ref"]: (c.get("structured") or {}).get("identity", {})
            for c in _db().list_candidates(job_id) if c.get("candidate_ref")
        }
        for section in ("entries", "below_cutoff", "excluded", "manual_review"):
            for entry in shortlist.get(section, []):
                entry["identity"] = identities.get(entry["candidate_ref"])
    return shortlist


@server.tool(
    name="job_audit",
    title="A job's audit trail",
    description="The decision trail: plan compiled, rules applied and flagged, redactions, model checks, scores, queries. Filter by event name.",
)
def job_audit(job_id: str, event: str | None = None, limit: int = 200) -> dict[str, Any]:
    api = _api()
    if api is not None:
        try:
            body = api.ok("GET", f"/jobs/{job_id}/audit", params={"limit": 2000})
        except RemoteError as exc:
            return exc.to_dict()
        entries = body["entries"]
    else:
        if _db().get_job(job_id) is None:
            return {"ok": False, "error": {"kind": "not_found", "message": f"unknown job {job_id!r}"}}
        entries = _db().audit_trail(job_id, limit=2000)
    if event:
        entries = [e for e in entries if e["event"] == event]
    return {"job_id": job_id, "events": sorted({e["event"] for e in entries}), "entries": entries[-limit:]}


@server.tool(
    name="list_known_risky_phrases",
    title="List known risky recruitment phrasings",
    description=(
        "The phrasings this system flags deterministically, with the protected "
        "attributes each engages, the statutes cited and the suggested rewrite. "
        "Useful for writing an advertisement rather than checking one."
    ),
)
def list_known_risky_phrases() -> dict[str, Any]:
    return {
        "patterns": [
            {
                "id": pattern.id,
                "risk": pattern.risk.value,
                "protected_attributes": list(pattern.attributes),
                "statutes": statute_citations(pattern.statutes),
                "explanation": pattern.explanation,
                "suggested_rewrite": pattern.rewrite,
            }
            for pattern in RISK_PATTERNS
        ],
        "statutes": STATUTES,
        "disclaimer": "Decision support for recruiters, not legal advice.",
    }


@server.tool(
    name="map_qualification_to_aqf",
    title="Map a qualification to an AQF level",
    description=(
        "Map a qualification title, including an overseas one, to its Australian "
        "Qualifications Framework level (1-10). Deterministic: the same title "
        "always returns the same level. A confidence of 0 means no recognised "
        "equivalent was found and a human should check it — not that the "
        "qualification is invalid."
    ),
)
def map_qualification_to_aqf(title: str) -> dict[str, Any]:
    level, label, confidence = map_to_aqf(title)
    return {
        "title": title,
        "aqf_level": level,
        "aqf_label": label,
        "confidence": confidence,
        "needs_human_check": level is None,
        "framework": AQF_LABELS,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rescan MCP server.")
    parser.add_argument("--http", action="store_true", help="Serve over streamable HTTP instead of stdio.")
    args = parser.parse_args(argv)
    server.run(transport="streamable-http" if args.http else "stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
