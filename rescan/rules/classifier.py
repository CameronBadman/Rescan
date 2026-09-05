"""Compile a recruiter's plan into the rule language, under legal review.

Two sources of judgement, combined deliberately:

* The pattern table in `rescan.rules.statutes` is authoritative. Known risky
  phrasing is caught the same way every time, with a fixed statute basis and a
  fixed rewrite, so the system's advice does not vary between runs. It runs
  over the recruiter's text *and* over every string literal in the compiled
  rule, so a proxy cannot be smuggled through a skill name or an ASK question.
* The model reads the whole plan, reasons over it with the legal standing in
  front of it, and writes each requirement as a clause. Its reasoning is kept.

The model can *add* risk but never remove it: a pattern hit stands regardless
of what the model returns. A rule assessed as high risk is never compiled into
a filter, so flagged language cannot silently keep screening candidates out.
The parser is the last gate: a clause naming a forbidden field does not parse.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from rescan.dsl import DslError, parse_clause
from rescan.dsl.ast import Clause, string_literals
from rescan.llm.client import LLMClient, LLMError, LLMRequest
from rescan.llm.prompts import (
    COMPILE_DSL_REPAIR_SCHEMA,
    COMPILE_DSL_SCHEMA,
    compile_dsl_repair_system,
    compile_dsl_repair_user_prompt,
    compile_dsl_system,
    compile_dsl_user_prompt,
)
from rescan.rules.models import ClassifiedRule, RuleFinding, RuleSet, RuleVerdict
from rescan.rules.statutes import RISK_PATTERNS, STATUTES, RiskLevel, statute_citations

log = logging.getLogger(__name__)

_RISK_ORDER = {RiskLevel.NONE: 0, RiskLevel.REVIEW: 1, RiskLevel.HIGH: 2}

_SENTENCE_SPLIT = re.compile(r"(?<=[.;!?])\s+|\n+")
_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s*")


def split_plan(plan: str) -> list[str]:
    """Deterministic sentence/bullet split, used when the model is unavailable
    and by the stub backend."""
    sentences = []
    for chunk in _SENTENCE_SPLIT.split(plan or ""):
        text = _BULLET.sub("", chunk).strip()
        if len(text) >= 3:
            sentences.append(text)
    return sentences


# --------------------------------------------------------------------------
# Deterministic legal scan
# --------------------------------------------------------------------------


def scan_patterns(rule_text: str) -> list[RuleFinding]:
    """Deterministic pass over the known risky-phrase table."""
    findings: list[RuleFinding] = []
    for pattern in RISK_PATTERNS:
        match = pattern.pattern.search(rule_text)
        if not match:
            continue
        findings.append(
            RuleFinding(
                pattern_id=pattern.id,
                risk=pattern.risk,
                matched_text=match.group(0),
                protected_attributes=list(pattern.attributes),
                statutes=statute_citations(pattern.statutes),
                explanation=pattern.explanation,
                suggested_rewrite=pattern.rewrite,
                source="pattern",
            )
        )
    return findings


def _model_finding(data: dict) -> RuleFinding | None:
    """Turn a model risk assessment into a finding, if it claims one."""
    try:
        risk = RiskLevel(data.get("risk") or "none")
    except ValueError:
        return None
    if risk is RiskLevel.NONE:
        return None
    explanation = (data.get("explanation") or "").strip()
    rewrite = (data.get("suggested_rewrite") or "").strip()
    if not explanation:
        return None
    codes = [str(code) for code in data.get("legal_basis") or [] if str(code) in STATUTES]
    return RuleFinding(
        pattern_id="model_assessed",
        risk=risk,
        matched_text=None,
        protected_attributes=list(data.get("protected_attributes") or []),
        statutes=statute_citations(codes),
        explanation=explanation,
        suggested_rewrite=rewrite or "Restate the requirement as a measurable capability the role needs.",
        source="model",
    )


def _merge_findings(findings: list[RuleFinding], extra: list[RuleFinding]) -> None:
    seen = {f.pattern_id for f in findings}
    for finding in extra:
        if finding.pattern_id not in seen:
            findings.append(finding)
            seen.add(finding.pattern_id)


# --------------------------------------------------------------------------
# Compilation
# --------------------------------------------------------------------------


def _parse_with_repair(
    source_text: str, dsl: str, client: LLMClient, model: str | None, notes: list[str]
) -> Clause | None:
    """Parse the model's clause; on failure give the model one chance to fix it."""
    try:
        return parse_clause(dsl)
    except DslError as first:
        errors = [str(first)]

    request = LLMRequest(
        task="compile_dsl_repair",
        system=compile_dsl_repair_system(),
        user=compile_dsl_repair_user_prompt(source_text, dsl, errors),
        schema=COMPILE_DSL_REPAIR_SCHEMA,
        model=model,
        context={"source_text": source_text, "dsl": dsl, "errors": errors},
    )
    try:
        repaired = client.json_call(request).data.get("dsl")
    except LLMError as exc:
        notes.append(f"Rule could not be compiled ({errors[0]}); repair unavailable ({exc}).")
        return None
    if not repaired or not str(repaired).strip():
        notes.append(f"Rule could not be compiled into the rule language: {errors[0]}")
        return None
    try:
        return parse_clause(str(repaired))
    except DslError as second:
        notes.append(
            f"Rule could not be compiled into the rule language: {errors[0]}; after repair: {second}"
        )
        return None


def _build_rule(
    rule_id: str,
    source_text: str,
    data: dict[str, Any] | None,
    client: LLMClient,
    model: str | None,
    unavailable: str | None = None,
) -> ClassifiedRule:
    findings = scan_patterns(source_text)
    notes: list[str] = []
    clause: Clause | None = None
    justification = None
    legal_basis: list[str] = []
    kind = "require"

    if unavailable:
        notes.append(f"Automated review unavailable ({unavailable}); rule not applied.")
    elif data is None:
        notes.append("The model returned no rule for this text; passed to the human reviewer.")
    else:
        model_finding = _model_finding(data)
        if model_finding:
            already_covered = any(
                model_finding.risk is existing.risk
                and model_finding.protected_attributes
                and set(model_finding.protected_attributes) & set(existing.protected_attributes)
                for existing in findings
            )
            if not already_covered:
                findings.append(model_finding)
        justification = (data.get("justification") or None) and str(data["justification"]).strip()
        legal_basis = statute_citations([str(code) for code in data.get("legal_basis") or []])
        if data.get("kind") in {"require", "prefer"}:
            kind = str(data["kind"])

        dsl = data.get("dsl")
        if isinstance(dsl, str) and dsl.strip():
            clause = _parse_with_repair(source_text, dsl.strip(), client, model, notes)
            if clause is not None:
                kind = clause.kind
                # The compiled text is scanned too: a proxy inside a string
                # literal or an ASK question is still a proxy.
                for literal in string_literals(clause.expr):
                    _merge_findings(findings, scan_patterns(literal))

    risk = max((f.risk for f in findings), key=lambda level: _RISK_ORDER[level], default=RiskLevel.NONE)

    # A high-risk rule is reported, never applied.
    if risk is RiskLevel.HIGH:
        verdict = RuleVerdict.RISKY
        if clause is not None:
            notes.append(
                "This rule could be tested automatically, but it was not applied "
                f"because it screens on a protected attribute or a proxy for one. (Compiled form: {clause.to_dsl()})"
            )
            clause = None
    elif clause is not None:
        verdict = RuleVerdict.APPLICABLE
        if risk is RiskLevel.REVIEW:
            notes.append("Applied, but record the job-based justification for this requirement.")
    else:
        verdict = RuleVerdict.UNMAPPABLE
        if not notes:
            notes.append(
                "No structured field tests this rule, so it is passed to the human "
                "reviewer rather than used to exclude anyone."
            )

    return ClassifiedRule(
        id=rule_id,
        source_text=source_text,
        verdict=verdict,
        risk=risk,
        findings=findings,
        dsl=clause.to_dsl() if clause else None,
        clause=clause,
        kind=kind,  # type: ignore[arg-type]
        justification=justification,
        legal_basis=legal_basis,
        notes=notes,
    )


def compile_plan(
    plan: str | None,
    client: LLMClient,
    *,
    rule_texts: list[str] | None = None,
    role_context: str | None = None,
    model: str | None = None,
) -> RuleSet:
    """Compile a free-text plan and/or a list of discrete rules into a RuleSet.

    With `rule_texts`, the result has one rule per input text in order (the
    model answers each by number); anything else the model finds in the plan is
    appended after them.
    """
    texts = [text.strip() for text in (rule_texts or []) if text and text.strip()]
    plan = (plan or "").strip()
    if not texts and not plan:
        return RuleSet(rules=[], source_plan=None)

    parts = []
    if plan:
        parts.append(plan)
    if texts:
        numbered = "\n".join(f"{index}. {text}" for index, text in enumerate(texts, start=1))
        parts.append("Specific rules (answer each by number):\n" + numbered if plan else numbered)
    plan_text = "\n\n".join(parts)

    request = LLMRequest(
        task="compile_dsl",
        system=compile_dsl_system(),
        user=compile_dsl_user_prompt(plan_text, role_context),
        schema=COMPILE_DSL_SCHEMA,
        model=model,
        # Reasoning plus one clause per requirement; a long plan needs room.
        max_tokens=8192,
        context={"plan": plan, "rule_texts": texts, "role_context": role_context},
    )

    try:
        data = client.json_call(request).data
    except LLMError as exc:
        # Inference down: the statute table still runs, nothing is applied.
        log.warning("plan compilation failed: %s", exc)
        sources = texts or split_plan(plan)
        return RuleSet(
            rules=[
                _build_rule(f"rule_{index}", text, None, client, model, unavailable=str(exc))
                for index, text in enumerate(sources, start=1)
            ],
            reasoning=None,
            source_plan=plan_text,
        )

    returned = [item for item in data.get("rules") or [] if isinstance(item, dict)]
    rules: list[ClassifiedRule] = []

    if texts:
        by_index: dict[int, dict[str, Any]] = {}
        extras: list[dict[str, Any]] = []
        for item in returned:
            index = item.get("source_index")
            if isinstance(index, int) and 1 <= index <= len(texts) and index not in by_index:
                by_index[index] = item
            else:
                extras.append(item)
        for index, text in enumerate(texts, start=1):
            rules.append(_build_rule(f"rule_{index}", text, by_index.get(index), client, model))
        for offset, item in enumerate(extras, start=len(texts) + 1):
            source = str(item.get("source_text") or "").strip() or f"(rule {offset})"
            rules.append(_build_rule(f"rule_{offset}", source, item, client, model))
    else:
        for index, item in enumerate(returned, start=1):
            source = str(item.get("source_text") or "").strip() or f"(rule {index})"
            rules.append(_build_rule(f"rule_{index}", source, item, client, model))

    reasoning = data.get("reasoning")
    return RuleSet(
        rules=rules,
        reasoning=str(reasoning).strip() if reasoning else None,
        source_plan=plan_text,
    )


def classify_rule(
    rule_text: str,
    client: LLMClient,
    *,
    role_context: str | None = None,
    rule_id: str | None = None,
    model: str | None = None,
) -> ClassifiedRule:
    """Classify and compile one free-text recruiter rule."""
    if not rule_text or not rule_text.strip():
        return ClassifiedRule(
            id=rule_id or "rule_1",
            source_text="",
            verdict=RuleVerdict.UNMAPPABLE,
            notes=["Empty rule ignored."],
        )
    rule_set = compile_plan(None, client, rule_texts=[rule_text], role_context=role_context, model=model)
    rule = rule_set.rules[0]
    if rule_id:
        rule = rule.model_copy(update={"id": rule_id})
    return rule


def classify_rules(
    rule_texts: list[str],
    client: LLMClient,
    *,
    role_context: str | None = None,
    model: str | None = None,
) -> RuleSet:
    return compile_plan(None, client, rule_texts=list(rule_texts), role_context=role_context, model=model)
