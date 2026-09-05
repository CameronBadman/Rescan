"""Rule data contracts.

A recruiter rule is applied automatically only when it compiles into the rule
language (`rescan.dsl`), whose vocabulary is closed and whose forbidden
identifiers carry a statute. That is what makes every exclusion explainable in
terms of a structured value rather than a score.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

from rescan.dsl.ast import Clause
from rescan.rules.statutes import RiskLevel


class RuleVerdict(str, Enum):
    # Compiles into the rule language and is applied automatically.
    APPLICABLE = "applicable"
    # Reads as a proxy for a protected attribute; not applied as written.
    RISKY = "risky"
    # Lawful but not mechanisable; passed to the human reviewer as a note.
    UNMAPPABLE = "unmappable"


class RuleFinding(BaseModel):
    """One legal-risk finding against a recruiter rule."""

    pattern_id: str
    risk: RiskLevel
    matched_text: str | None = None
    protected_attributes: list[str] = Field(default_factory=list)
    statutes: list[str] = Field(default_factory=list)
    explanation: str
    suggested_rewrite: str
    source: Literal["pattern", "model"] = "pattern"


class ClassifiedRule(BaseModel):
    id: str
    source_text: str
    verdict: RuleVerdict
    risk: RiskLevel = RiskLevel.NONE
    findings: list[RuleFinding] = Field(default_factory=list)
    # The compiled rule, in canonical text and as a tree. Absent when the rule
    # was flagged, could not be compiled, or the model was unavailable.
    dsl: str | None = None
    clause: Clause | None = None
    kind: Literal["require", "prefer"] = "require"
    # The model's job-based reason for the requirement, kept for the audit trail.
    justification: str | None = None
    legal_basis: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @property
    def is_applied(self) -> bool:
        """Whether this rule actually filters or ranks candidates."""
        return self.verdict is RuleVerdict.APPLICABLE and self.clause is not None

    @property
    def blocking_findings(self) -> list[RuleFinding]:
        return [f for f in self.findings if f.risk is RiskLevel.HIGH]


class RuleOutcome(BaseModel):
    """Result of evaluating one applied rule against one candidate."""

    rule_id: str
    source_text: str
    dsl: str
    fields: list[str] = Field(default_factory=list)
    # None means "could not be determined" — never an automatic exclusion.
    passed: bool | None
    observed: Any = None
    reason: str


class RuleSet(BaseModel):
    rules: list[ClassifiedRule] = Field(default_factory=list)
    # The model's reasoning over the plan: what the role needs, which phrases
    # were proxies and why. Stored so a reviewer can see how the rules arose.
    reasoning: str | None = None
    source_plan: str | None = None

    @property
    def applied(self) -> list[ClassifiedRule]:
        return [rule for rule in self.rules if rule.is_applied]

    @property
    def requirements(self) -> list[ClassifiedRule]:
        return [rule for rule in self.applied if rule.kind == "require"]

    @property
    def preferences(self) -> list[ClassifiedRule]:
        return [rule for rule in self.applied if rule.kind == "prefer"]

    @property
    def flagged(self) -> list[ClassifiedRule]:
        return [rule for rule in self.rules if rule.verdict is RuleVerdict.RISKY]
