"""Rule data contracts and the predicate vocabulary.

The predicate vocabulary is deliberately small and closed. A recruiter rule is
only applied automatically when it compiles into one of these fields and
operators, which is what makes every exclusion explainable in terms of a
structured value rather than a score.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

from rescan.rules.statutes import RiskLevel


class RuleVerdict(str, Enum):
    # Maps cleanly to a structured field and is applied automatically.
    APPLICABLE = "applicable"
    # Reads as a proxy for a protected attribute; not applied as written.
    RISKY = "risky"
    # Lawful but not mechanisable; passed to the human reviewer as a note.
    UNMAPPABLE = "unmappable"


# Fields a rule may test. Each maps to a value on the anonymized profile.
PREDICATE_FIELDS: dict[str, str] = {
    "total_years_experience": "Years of professional experience (number).",
    "highest_aqf": "Highest qualification as an AQF level 1-10 (number).",
    "skills": "List of skill names.",
    "languages": "List of languages the candidate uses.",
    "certifications": "List of certifications held.",
    "work_rights_unrestricted": "Whether the candidate holds unrestricted work rights (boolean).",
    "work_rights_status": "Work rights status (citizen, permanent_resident, visa_unrestricted, visa_restricted, requires_sponsorship, unknown).",
}

Operator = Literal["gte", "lte", "eq", "contains_all", "contains_any", "in", "is_true", "is_false"]


class Predicate(BaseModel):
    field: str
    operator: Operator
    value: Any = None
    description: str = Field(description="Plain-language statement of what this tests.")


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
    predicate: Predicate | None = None
    notes: list[str] = Field(default_factory=list)

    @property
    def is_applied(self) -> bool:
        """Whether this rule actually filters candidates."""
        return self.verdict is RuleVerdict.APPLICABLE and self.predicate is not None

    @property
    def blocking_findings(self) -> list[RuleFinding]:
        return [f for f in self.findings if f.risk is RiskLevel.HIGH]


class RuleOutcome(BaseModel):
    """Result of evaluating one applied rule against one candidate."""

    rule_id: str
    source_text: str
    field: str
    # None means "could not be determined" — never an automatic exclusion.
    passed: bool | None
    observed: Any = None
    reason: str


class RuleSet(BaseModel):
    rules: list[ClassifiedRule] = Field(default_factory=list)

    @property
    def applied(self) -> list[ClassifiedRule]:
        return [rule for rule in self.rules if rule.is_applied]

    @property
    def flagged(self) -> list[ClassifiedRule]:
        return [rule for rule in self.rules if rule.verdict is RuleVerdict.RISKY]
