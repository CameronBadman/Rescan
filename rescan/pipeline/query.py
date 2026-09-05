"""Ad-hoc queries over a job's anonymized profiles.

The rule language is the query base for the resumes: a recruiter (or a
frontend, or an MCP client) can ask "who has 5+ years and Python" of a job
that has already been processed, and get back matched / not matched /
undecided with the same plain-language reasons the screening pass produces.

A query goes through the same legal gate as a compiled rule: forbidden fields
fail at parse time with their statute, and every string literal — including an
ASK question — is scanned against the risky-phrase table. Every query is
written to the audit trail, because a query that never excludes anyone
formally can still shape who a recruiter looks at.
"""

from __future__ import annotations

from typing import Any

from rescan.dsl import DslError, parse_expr
from rescan.dsl.ast import fields_used, has_ask, string_literals
from rescan.dsl.eval import JudgeLike, evaluate
from rescan.rules.classifier import scan_patterns
from rescan.rules.models import RuleFinding
from rescan.rules.statutes import RiskLevel
from rescan.schemas import AnonymizedProfile
from rescan.store import Store


class QueryRejected(ValueError):
    """The query names a proxy for a protected attribute."""

    def __init__(self, findings: list[RuleFinding]) -> None:
        super().__init__("query rejected: it screens on a protected attribute or a proxy for one")
        self.findings = findings

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "legal",
            "message": str(self),
            "findings": [finding.model_dump(mode="json") for finding in self.findings],
        }


def check_query(dsl: str):
    """Parse and legally vet a query. Raises DslError or QueryRejected."""
    expr = parse_expr(dsl)
    findings: list[RuleFinding] = []
    seen: set[str] = set()
    for literal in string_literals(expr):
        for finding in scan_patterns(literal):
            if finding.pattern_id not in seen:
                findings.append(finding)
                seen.add(finding.pattern_id)
    if any(finding.risk is RiskLevel.HIGH for finding in findings):
        raise QueryRejected(findings)
    return expr, findings


def run_query(
    store: Store,
    job_id: str,
    dsl: str,
    *,
    judge: JudgeLike | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    """Evaluate a query against every anonymized profile of a job.

    Raises KeyError for an unknown job, DslError for a bad query, and
    QueryRejected when the query engages a protected attribute.
    """
    if store.get_job(job_id) is None:
        raise KeyError(job_id)
    expr, warnings = check_query(dsl)

    matched: list[dict[str, Any]] = []
    not_matched: list[dict[str, Any]] = []
    indeterminate: list[dict[str, Any]] = []
    skipped = 0
    for candidate in store.list_candidates(job_id):
        raw = candidate.get("anonymized")
        if not raw:
            skipped += 1
            continue
        profile = AnonymizedProfile.model_validate(raw)
        verdict = evaluate(expr, profile, judge)
        entry = {
            "candidate_ref": profile.candidate_ref,
            "candidate_id": candidate["id"],
            "reason": verdict.reason,
        }
        if verdict.value is True:
            matched.append(entry)
        elif verdict.value is False:
            not_matched.append(entry)
        else:
            indeterminate.append(entry)

    result = {
        "job_id": job_id,
        "query": dsl,
        "canonical": expr.to_dsl(),
        "fields": fields_used(expr),
        "model_checks": has_ask(expr) and judge is not None,
        "matched": matched,
        "not_matched": not_matched,
        "indeterminate": indeterminate,
        "counts": {
            "matched": len(matched),
            "not_matched": len(not_matched),
            "indeterminate": len(indeterminate),
            "not_evaluated": skipped,
        },
        "warnings": [finding.model_dump(mode="json") for finding in warnings],
    }
    store.audit(
        job_id, "query", "query_run",
        detail={
            "query": dsl,
            "canonical": result["canonical"],
            "fields": result["fields"],
            "counts": result["counts"],
            "warnings": [f.pattern_id for f in warnings],
            "matched": [entry["candidate_ref"] for entry in matched],
            **({"actor": actor} if actor else {}),
        },
    )
    return result


__all__ = ["DslError", "QueryRejected", "check_query", "run_query"]
