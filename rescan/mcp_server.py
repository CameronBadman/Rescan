"""MCP server exposing the rule engine.

The legal-risk classifier is useful outside this application: a recruiter
writing a job ad in any MCP-capable client should be able to check the wording
before it becomes a screening rule. These tools are read-only and stateless.

Run it:

    python -m rescan.mcp_server              # stdio, for a local MCP client
    python -m rescan.mcp_server --http       # streamable HTTP
"""

from __future__ import annotations

import argparse
from typing import Any

from mcp.server.mcpserver import MCPServer

from rescan.aqf import AQF_LABELS, map_to_aqf
from rescan.llm.client import build_client
from rescan.rules.classifier import classify_rule, classify_rules
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


def _llm():
    # Built lazily so importing this module does not open a connection.
    global _client
    if _client is None:
        _client = build_client()
    return _client


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
    rule_set = classify_rules(rules, _llm(), role_context=role_context)
    return {
        "results": [_rule_payload(rule) for rule in rule_set.rules],
        "applied": len(rule_set.applied),
        "flagged": len(rule_set.flagged),
    }


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
