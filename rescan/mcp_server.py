"""MCP server exposing the rule engine, batches and analysis runs.

The legal-risk classifier is useful outside this application: a recruiter
writing a job ad in any MCP-capable client should be able to check the wording
before it becomes a screening rule.

Two modes. In-process (default): the tools run the pipeline code directly
against the local store. Remote: with RESCAN_MCP_REMOTE_URL (and _KEY) set,
every tool calls the deployed HTTP API instead, so an agent on a laptop drives
the real deployment with the same tool surface.

Batches and analysis runs are separate: ingest a batch of resumes once, then
run any number of rule sets over it.

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
    title="Query a batch's candidates",
    description=(
        "Run a query in the screening language over the anonymized profiles of an "
        "ingested batch. Returns matched, not matched and undecided candidates by "
        "reference with a plain-language reason each; never identity. A query asks "
        "about the people, independently of any analysis run's verdict. Queries pass "
        "the same legal gate as rules and are written to the batch's audit trail."
    ),
)
def query_candidates(batch_id: str, dsl: str, model_checks: bool = True) -> dict[str, Any]:
    api = _api()
    if api is not None:
        try:
            return api.ok("POST", f"/batches/{batch_id}/query", json={"dsl": dsl, "model_checks": model_checks})
        except RemoteError as exc:
            return exc.to_dict()
    judge = Judge(_llm(), ensemble=False) if model_checks else None
    try:
        return run_query(_db(), batch_id, dsl, judge=judge, actor="mcp")
    except KeyError:
        return {"ok": False, "error": {"kind": "not_found", "message": f"unknown batch {batch_id!r}"}}
    except DslError as exc:
        return {"ok": False, "error": exc.to_dict()}
    except QueryRejected as exc:
        return {"ok": False, "error": exc.to_dict()}


# --------------------------------------------------------------------------
# Batches: resumes in, anonymized profiles out
# --------------------------------------------------------------------------


def _runner():
    from rescan.extract import Extractor
    from rescan.pipeline.runner import PipelineRunner

    return PipelineRunner(_db(), _llm(), Extractor())


def _background(fn) -> None:
    import threading

    threading.Thread(target=fn, daemon=True).start()


@server.tool(
    name="list_batches",
    title="List batches of resumes",
    description="Batches the service has ingested, newest first, with how many documents are ready and how many analysis runs each carries.",
)
def list_batches(limit: int = 20) -> dict[str, Any]:
    api = _api()
    if api is not None:
        return api.ok("GET", "/batches", params={"limit": limit})
    return {"batches": _db().list_batches(limit=limit)}


@server.tool(
    name="start_batch_from_bucket",
    title="Ingest a batch of resumes from the bucket",
    description=(
        "Pull the resumes under <prefix>/<batch_id>/ in the object store and ingest "
        "them: extraction, structuring and de-identification. No rules and no role "
        "are involved — the result is a set of anonymized profiles that any number "
        "of analysis runs can screen. Returns immediately; poll batch_status."
    ),
)
def start_batch_from_bucket(batch_id: str, name: str | None = None) -> dict[str, Any]:
    api = _api()
    if api is not None:
        try:
            return api.ok("POST", "/batches/from-bucket", json={"batch_id": batch_id, "name": name})
        except RemoteError as exc:
            return exc.to_dict()
    from rescan.ingest import ObjectStoreError, build_object_store, pull_batch_documents

    store = _db()
    if store.get_batch(batch_id) is not None:
        return {"ok": False, "error": {"kind": "conflict", "message": f"batch {batch_id!r} already exists"}}
    try:
        pull = pull_batch_documents(build_object_store(), batch_id)
    except ObjectStoreError as exc:
        return {"ok": False, "error": {"kind": "object_store", "message": str(exc)}}
    if not pull.documents:
        return {"ok": False, "error": {"kind": "not_found", "message": f"no usable documents under {pull.prefix!r}", "skipped": pull.skipped}}
    runner = _runner()
    runner.create_batch(pull.documents, batch_id=batch_id, name=name, source={"kind": "bucket", "prefix": pull.prefix})
    _background(lambda: runner.process_batch(batch_id))
    return {"batch_id": batch_id, "prefix": pull.prefix, "accepted_documents": len(pull.documents), "skipped": pull.skipped}


@server.tool(
    name="batch_status",
    title="Batch ingestion progress",
    description="Per-stage document counts for a batch: pending, extracting, structuring, anonymizing, ready, needs_manual_review, failed, duplicate — plus its analysis runs.",
)
def batch_status(batch_id: str) -> dict[str, Any]:
    api = _api()
    if api is not None:
        try:
            return api.ok("GET", f"/batches/{batch_id}")
        except RemoteError as exc:
            return exc.to_dict()
    batch = _db().get_batch(batch_id)
    if batch is None:
        return {"ok": False, "error": {"kind": "not_found", "message": f"unknown batch {batch_id!r}"}}
    return {
        "batch_id": batch_id, "name": batch["name"], "status": batch["status"],
        "counts": batch["counts"], "total": batch["total"], "ready": batch["ready"],
        "runs": batch["runs"], "error": batch["error"],
    }


@server.tool(
    name="batch_audit",
    title="A batch's ingestion trail",
    description="Everything that happened to these documents — ingestion, extraction, structuring, redactions — and, unfiltered, the events of every run over them.",
)
def batch_audit(batch_id: str, event: str | None = None, limit: int = 200) -> dict[str, Any]:
    api = _api()
    if api is not None:
        try:
            body = api.ok("GET", f"/batches/{batch_id}/audit", params={"limit": 2000})
        except RemoteError as exc:
            return exc.to_dict()
        entries = body["entries"]
    else:
        if _db().get_batch(batch_id) is None:
            return {"ok": False, "error": {"kind": "not_found", "message": f"unknown batch {batch_id!r}"}}
        entries = _db().audit_trail(batch_id, limit=2000)
    if event:
        entries = [e for e in entries if e["event"] == event]
    return {"batch_id": batch_id, "events": sorted({e["event"] for e in entries}), "entries": entries[-limit:]}


# --------------------------------------------------------------------------
# Analysis runs: a rule set applied to a batch
# --------------------------------------------------------------------------


@server.tool(
    name="list_runs",
    title="List analysis runs",
    description="Analysis runs, newest first. Filter by batch, or search id, name, role title and batch with `q`.",
)
def list_runs(batch_id: str | None = None, q: str | None = None, limit: int = 20) -> dict[str, Any]:
    api = _api()
    if api is not None:
        params = {"limit": limit}
        if batch_id:
            params["batch_id"] = batch_id
        if q:
            params["q"] = q
        return api.ok("GET", "/runs", params=params)
    return {"runs": _db().list_runs(batch_id=batch_id, query=q, limit=limit)}


@server.tool(
    name="start_run",
    title="Analyse a batch with a hiring plan",
    description=(
        "Apply a rule set to an ingested batch: compile the hiring plan and/or rules "
        "— or reuse another run's compiled rules with rules_from — then screen every "
        "candidate, rank the survivors and cut a shortlist. The batch is not "
        "re-extracted, so many runs over one batch are cheap and independent. "
        "Returns immediately with the run id; poll run_status."
    ),
)
def start_run(
    batch_id: str, role_title: str, plan: str | None = None, rules: list[str] | None = None,
    role_description: str | None = None, rules_from: str | None = None, name: str | None = None,
) -> dict[str, Any]:
    role = {"title": role_title, "description": role_description}
    api = _api()
    if api is not None:
        try:
            return api.ok("POST", "/runs", json={
                "batch_id": batch_id, "role": role, "name": name, "plan": plan,
                "rules": rules or [], "rules_from": rules_from,
            })
        except RemoteError as exc:
            return exc.to_dict()
    from rescan.pipeline.runner import BatchNotReady
    from rescan.schemas import RoleSpec

    runner = _runner()
    try:
        run_id = runner.create_run(batch_id, RoleSpec.model_validate(role), name=name)
    except KeyError as exc:
        return {"ok": False, "error": {"kind": "not_found", "message": str(exc)}}
    except BatchNotReady as exc:
        return {"ok": False, "error": {"kind": "conflict", "message": str(exc)}}
    _background(lambda: runner.execute_run(run_id, rules or [], plan=plan, rules_from=rules_from))
    return {"run_id": run_id, "batch_id": batch_id, "name": name}


@server.tool(
    name="run_status",
    title="Analysis run progress",
    description="Status and outcome counts for one run: how many candidates were screened, and how many are eligible, excluded or sent to manual review.",
)
def run_status(run_id: str) -> dict[str, Any]:
    api = _api()
    if api is not None:
        try:
            return api.ok("GET", f"/runs/{run_id}")
        except RemoteError as exc:
            return exc.to_dict()
    run = _db().get_run(run_id)
    if run is None:
        return {"ok": False, "error": {"kind": "not_found", "message": f"unknown run {run_id!r}"}}
    return {
        "run_id": run_id, "batch_id": run["batch_id"], "name": run["name"], "status": run["status"],
        "role": run["role"], "counts": run["counts"], "screened": run["screened"], "total": run["total"],
        "has_shortlist": run["shortlist"] is not None, "error": run["error"],
    }


@server.tool(
    name="run_rules",
    title="An analysis run's compiled rules",
    description="The rule set for a run: each rule's verdict, risk findings with statutes, the clause in the rule language, and the model's reasoning over the plan.",
)
def run_rules(run_id: str) -> dict[str, Any]:
    api = _api()
    if api is not None:
        try:
            return api.ok("GET", f"/runs/{run_id}/rules")
        except RemoteError as exc:
            return exc.to_dict()
    run = _db().get_run(run_id)
    if run is None:
        return {"ok": False, "error": {"kind": "not_found", "message": f"unknown run {run_id!r}"}}
    return run["rules"] or {"rules": []}


@server.tool(
    name="run_shortlist",
    title="An analysis run's shortlist",
    description=(
        "The ranked shortlist with per-criterion scores, the near-misses below the cutoff, "
        "everyone excluded with the plain-language reason, and everyone sent to manual review. "
        "Anonymized by default; set reattach_identity=true only for the human reviewer step."
    ),
)
def run_shortlist(run_id: str, reattach_identity: bool = False) -> dict[str, Any]:
    api = _api()
    if api is not None:
        try:
            return api.ok("GET", f"/runs/{run_id}/shortlist", params={"reattach_identity": str(reattach_identity).lower()})
        except RemoteError as exc:
            return exc.to_dict()
    run = _db().get_run(run_id)
    if run is None:
        return {"ok": False, "error": {"kind": "not_found", "message": f"unknown run {run_id!r}"}}
    if run["shortlist"] is None:
        return {"ok": False, "error": {"kind": "conflict", "message": f"run {run_id!r} has no shortlist yet"}}
    shortlist = run["shortlist"]
    if reattach_identity:
        identities = {
            c["candidate_ref"]: (c.get("structured") or {}).get("identity", {})
            for c in _db().list_candidates(run["batch_id"]) if c.get("candidate_ref")
        }
        for section in ("entries", "below_cutoff", "excluded", "manual_review"):
            for entry in shortlist.get(section, []):
                entry["identity"] = identities.get(entry["candidate_ref"])
    return shortlist


@server.tool(
    name="run_audit",
    title="An analysis run's decision trail",
    description="The run's events — plan compiled, rules applied and flagged, model checks, screening outcomes, scores — merged by default with the ingestion events of the batch behind them. Filter by event name.",
)
def run_audit(run_id: str, event: str | None = None, include_batch: bool = True, limit: int = 200) -> dict[str, Any]:
    api = _api()
    if api is not None:
        try:
            body = api.ok("GET", f"/runs/{run_id}/audit", params={"limit": 2000, "include_batch": str(include_batch).lower()})
        except RemoteError as exc:
            return exc.to_dict()
        entries = body["entries"]
    else:
        if _db().get_run(run_id) is None:
            return {"ok": False, "error": {"kind": "not_found", "message": f"unknown run {run_id!r}"}}
        entries = _db().audit_trail(run_id=run_id, include_batch=include_batch, limit=2000)
    if event:
        entries = [e for e in entries if e["event"] == event]
    return {"run_id": run_id, "events": sorted({e["event"] for e in entries}), "entries": entries[-limit:]}


@server.tool(
    name="add_rule_to_run",
    title="Add a screening rule to an analysis run",
    description=(
        "Check a plain-language rule under Australian anti-discrimination law and, if it "
        "is not high risk, compile it, add it to the run and re-screen every candidate "
        "from the batch's stored anonymized profiles. A high-risk rule is NOT added: the "
        "result carries the finding, the statute and a measurable rewrite to propose instead."
    ),
)
def add_rule_to_run(run_id: str, text: str) -> dict[str, Any]:
    api = _api()
    if api is not None:
        status, body = api.call("POST", f"/runs/{run_id}/rules", json={"text": text})
        if status == 422 and isinstance(body, dict) and isinstance(body.get("detail"), dict):
            return {"ok": False, "added": False, "error": body["detail"]}
        if status >= 400:
            return RemoteError(status, body).to_dict()
        return {"ok": True, **body}
    from rescan.pipeline.runner import RuleRejected

    runner = _runner()
    try:
        rule = runner.add_rule(run_id, text)
    except KeyError as exc:
        return {"ok": False, "error": {"kind": "not_found", "message": str(exc)}}
    except RuleRejected as exc:
        return {"ok": False, "added": False, "error": {"kind": "legal", "rule": _rule_payload(exc.rule)}}
    _background(lambda: runner.rescreen(run_id))
    return {"ok": True, "rule": _rule_payload(rule), "added": True, "rescreening": True}


@server.tool(
    name="remove_rule_from_run",
    title="Remove a screening rule from an analysis run",
    description="Remove a rule by id and re-screen every candidate without it.",
)
def remove_rule_from_run(run_id: str, rule_id: str) -> dict[str, Any]:
    api = _api()
    if api is not None:
        try:
            return {"ok": True, **api.ok("DELETE", f"/runs/{run_id}/rules/{rule_id}")}
        except RemoteError as exc:
            return exc.to_dict()
    runner = _runner()
    try:
        runner.remove_rule(run_id, rule_id)
    except KeyError as exc:
        return {"ok": False, "error": {"kind": "not_found", "message": str(exc)}}
    _background(lambda: runner.rescreen(run_id))
    return {"ok": True, "removed": rule_id, "rescreening": True}


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
