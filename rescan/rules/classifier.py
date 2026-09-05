"""Legal-risk classification and compilation of recruiter rules.

Two sources of judgement, combined deliberately:

* The pattern table in `rescan.rules.statutes` is authoritative. Known risky
  phrasing is caught the same way every time, with a fixed statute basis and a
  fixed rewrite, so the system's advice does not vary between runs.
* The model catches novel phrasing the table does not list, and compiles sound
  rules into predicates.

The model can *add* risk but never remove it: a pattern hit stands regardless of
what the model returns. A rule assessed as high risk is never compiled into a
filter, so flagged language cannot silently keep screening candidates out.
"""

from __future__ import annotations

import logging
import uuid

from pydantic import ValidationError

from rescan.llm.client import LLMClient, LLMError, LLMRequest
from rescan.llm.prompts import (
    CLASSIFY_RULE_SCHEMA,
    CLASSIFY_RULE_SYSTEM,
    classify_rule_user_prompt,
)
from rescan.rules.models import ClassifiedRule, Predicate, RuleFinding, RuleSet, RuleVerdict
from rescan.rules.statutes import RISK_PATTERNS, RiskLevel, statute_citations

log = logging.getLogger(__name__)

_RISK_ORDER = {RiskLevel.NONE: 0, RiskLevel.REVIEW: 1, RiskLevel.HIGH: 2}


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
    return RuleFinding(
        pattern_id="model_assessed",
        risk=risk,
        matched_text=None,
        protected_attributes=list(data.get("protected_attributes") or []),
        statutes=[],
        explanation=explanation,
        suggested_rewrite=rewrite or "Restate the requirement as a measurable capability the role needs.",
        source="model",
    )


def classify_rule(
    rule_text: str,
    client: LLMClient,
    *,
    role_context: str | None = None,
    rule_id: str | None = None,
    model: str | None = None,
) -> ClassifiedRule:
    """Classify one free-text recruiter rule and compile it when it is sound."""
    rule_text = rule_text.strip()
    rule_id = rule_id or f"rule_{uuid.uuid4().hex[:8]}"

    if not rule_text:
        return ClassifiedRule(
            id=rule_id,
            source_text=rule_text,
            verdict=RuleVerdict.UNMAPPABLE,
            notes=["Empty rule ignored."],
        )

    findings = scan_patterns(rule_text)
    notes: list[str] = []
    predicate: Predicate | None = None

    request = LLMRequest(
        task="classify_rule",
        system=CLASSIFY_RULE_SYSTEM,
        user=classify_rule_user_prompt(rule_text, role_context),
        schema=CLASSIFY_RULE_SCHEMA,
        model=model,
        context={"rule_text": rule_text, "role_context": role_context},
    )

    try:
        data = client.json_call(request).data
    except LLMError as exc:
        # A classification failure must not become an unreviewed filter.
        log.warning("rule classification failed for %r: %s", rule_text, exc)
        notes.append(f"Automated review unavailable ({exc}); rule not applied.")
        data = {}

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

    raw_predicate = data.get("predicate")
    if isinstance(raw_predicate, dict):
        try:
            predicate = Predicate.model_validate(raw_predicate)
        except ValidationError as exc:
            notes.append(f"Rule could not be compiled into a testable form: {exc.error_count()} schema errors.")

    risk = max((f.risk for f in findings), key=lambda level: _RISK_ORDER[level], default=RiskLevel.NONE)

    # A high-risk rule is reported, never applied.
    if risk is RiskLevel.HIGH:
        verdict = RuleVerdict.RISKY
        if predicate is not None:
            notes.append(
                "This rule could be tested automatically, but it was not applied "
                "because it screens on a protected attribute or a proxy for one."
            )
            predicate = None
    elif predicate is not None:
        verdict = RuleVerdict.APPLICABLE
        if risk is RiskLevel.REVIEW:
            notes.append(
                "Applied, but record the job-based justification for this requirement."
            )
    else:
        verdict = RuleVerdict.UNMAPPABLE
        if not notes:
            notes.append(
                "No structured field tests this rule, so it is passed to the human "
                "reviewer rather than used to exclude anyone."
            )

    return ClassifiedRule(
        id=rule_id,
        source_text=rule_text,
        verdict=verdict,
        risk=risk,
        findings=findings,
        predicate=predicate,
        notes=notes,
    )


def classify_rules(
    rule_texts: list[str],
    client: LLMClient,
    *,
    role_context: str | None = None,
    model: str | None = None,
) -> RuleSet:
    return RuleSet(
        rules=[
            classify_rule(text, client, role_context=role_context, rule_id=f"rule_{index + 1}", model=model)
            for index, text in enumerate(rule_texts)
            if text and text.strip()
        ]
    )
