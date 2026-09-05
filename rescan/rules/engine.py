"""Evaluate compiled rules against anonymized candidate profiles.

Two properties this module exists to guarantee:

* Every outcome names the structured values it was decided on, in plain
  language, so an exclusion can be explained to the candidate and defended by
  the employer.
* A value the resume never stated produces `passed=None`, not a rejection. The
  candidate goes to manual review. Silence in a document is not evidence
  against a person.

The evaluation itself lives in `rescan.dsl.eval`; this module ties it to rules
and collects the result for one candidate.
"""

from __future__ import annotations

from typing import Any

from rescan.dsl.ast import fields_used
from rescan.dsl.eval import JudgeLike, Verdict, evaluate
from rescan.rules.models import ClassifiedRule, RuleOutcome, RuleSet
from rescan.schemas import AnonymizedProfile


def evaluate_rule(
    rule: ClassifiedRule, profile: AnonymizedProfile, judge: JudgeLike | None = None
) -> RuleOutcome | None:
    """Evaluate one requirement, or None if the rule is not applied as a filter.

    PREFER clauses never screen anyone; they are scored by the ranking pass.
    """
    if not rule.is_applied or rule.clause is None or rule.clause.kind != "require":
        return None
    verdict: Verdict = evaluate(rule.clause.expr, profile, judge)
    return RuleOutcome(
        rule_id=rule.id,
        source_text=rule.source_text,
        dsl=rule.dsl or rule.clause.to_dsl(),
        fields=fields_used(rule.clause.expr),
        passed=verdict.value,
        observed=verdict.observed,
        reason=verdict.reason,
    )


class ScreeningResult:
    """Outcome of the whole rule set for one candidate."""

    def __init__(self, candidate_ref: str, outcomes: list[RuleOutcome]) -> None:
        self.candidate_ref = candidate_ref
        self.outcomes = outcomes

    @property
    def failed(self) -> list[RuleOutcome]:
        return [outcome for outcome in self.outcomes if outcome.passed is False]

    @property
    def indeterminate(self) -> list[RuleOutcome]:
        return [outcome for outcome in self.outcomes if outcome.passed is None]

    @property
    def eligible(self) -> bool:
        """Whether the candidate clears every applied rule."""
        return not self.failed

    @property
    def needs_manual_review(self) -> bool:
        return bool(self.indeterminate)

    def exclusion_reasons(self) -> list[str]:
        return [outcome.reason for outcome in self.failed]

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_ref": self.candidate_ref,
            "eligible": self.eligible,
            "needs_manual_review": self.needs_manual_review,
            "outcomes": [outcome.model_dump(mode="json") for outcome in self.outcomes],
        }


def screen(
    profile: AnonymizedProfile, rule_set: RuleSet, judge: JudgeLike | None = None
) -> ScreeningResult:
    outcomes = [
        outcome
        for outcome in (evaluate_rule(rule, profile, judge) for rule in rule_set.rules)
        if outcome is not None
    ]
    return ScreeningResult(profile.candidate_ref, outcomes)
