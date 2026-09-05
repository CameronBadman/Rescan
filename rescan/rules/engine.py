"""Evaluate compiled rules against anonymized candidate profiles.

Two properties this module exists to guarantee:

* Every outcome names the structured value it was decided on, in plain
  language, so an exclusion can be explained to the candidate and defended by
  the employer.
* A value the resume never stated produces `passed=None`, not a rejection. The
  candidate goes to manual review. Silence in a document is not evidence
  against a person.
"""

from __future__ import annotations

from typing import Any

from rescan.rules.models import ClassifiedRule, Predicate, RuleOutcome, RuleSet
from rescan.schemas import AnonymizedProfile

# Human-readable names for the predicate fields, used in outcome reasons.
FIELD_LABELS: dict[str, str] = {
    "total_years_experience": "years of professional experience",
    "highest_aqf": "highest qualification (AQF level)",
    "skills": "skills",
    "languages": "languages",
    "certifications": "certifications",
    "work_rights_unrestricted": "unrestricted work rights",
    "work_rights_status": "work rights status",
}

LIST_FIELDS = {"skills", "languages", "certifications"}

# Numeric reasons are phrased per field so they read as a sentence a candidate
# could be shown, rather than a field name with a number appended.
NUMERIC_PHRASING: dict[str, tuple[str, str]] = {
    "total_years_experience": (
        "Candidate has {observed:g} years of professional experience",
        "{target:g} years",
    ),
    "highest_aqf": (
        "Candidate's highest qualification is AQF level {observed:g}",
        "AQF level {target:g}",
    ),
}


def read_field(profile: AnonymizedProfile, field: str) -> Any:
    if field == "total_years_experience":
        return profile.total_years_experience
    if field == "highest_aqf":
        return profile.highest_aqf
    if field == "skills":
        return [skill.name for skill in profile.skills]
    if field == "languages":
        return list(profile.languages)
    if field == "certifications":
        return list(profile.certifications)
    if field == "work_rights_unrestricted":
        return profile.work_rights.unrestricted
    if field == "work_rights_status":
        return profile.work_rights.status.value
    raise KeyError(f"unknown predicate field: {field!r}")


def _matches(required: str, held: list[str]) -> bool:
    """Loose containment so 'AWS' matches 'AWS Solutions Architect'."""
    needle = required.strip().lower()
    if not needle:
        return False
    return any(needle in item.lower() or item.lower() in needle for item in held)


def _describe_list(values: list[str], limit: int = 6) -> str:
    if not values:
        return "none listed"
    shown = ", ".join(values[:limit])
    return shown if len(values) <= limit else f"{shown} (+{len(values) - limit} more)"


def evaluate_predicate(predicate: Predicate, profile: AnonymizedProfile) -> tuple[bool | None, Any, str]:
    """Return ``(passed, observed, reason)`` for one predicate."""
    observed = read_field(profile, predicate.field)
    label = FIELD_LABELS.get(predicate.field, predicate.field)

    # Unknown is never a rejection.
    if observed is None or (predicate.field in LIST_FIELDS and not observed):
        return (
            None,
            observed,
            f"The resume does not state {label}, so this rule could not be applied. "
            "Sent to manual review rather than excluded.",
        )

    operator = predicate.operator
    value = predicate.value

    if operator in {"gte", "lte"}:
        try:
            observed_number = float(observed)
            target = float(value)
        except (TypeError, ValueError):
            return None, observed, f"Could not compare {label} ({observed!r}) with {value!r}; sent to manual review."
        if operator == "gte":
            passed = observed_number >= target
            comparator = "at least"
        else:
            passed = observed_number <= target
            comparator = "no more than"
        subject_template, target_template = NUMERIC_PHRASING.get(
            predicate.field, ("Candidate's " + label + " is {observed:g}", "{target:g}")
        )
        subject = subject_template.format(observed=observed_number)
        requirement = target_template.format(target=target)
        verb = "meets" if passed else "does not meet"
        return (
            passed,
            observed,
            f"{subject}; the rule requires {comparator} {requirement}. This {verb} the requirement.",
        )

    if operator == "eq":
        passed = observed == value
        return passed, observed, f"{label.capitalize()} is {observed!r}; the rule requires {value!r}."

    if operator == "in":
        options = value if isinstance(value, list) else [value]
        passed = observed in options
        return (
            passed,
            observed,
            f"{label.capitalize()} is {observed!r}; the rule accepts {', '.join(map(str, options))}.",
        )

    if operator in {"contains_all", "contains_any"}:
        required = value if isinstance(value, list) else [value]
        held = [str(item) for item in observed]
        matched = [item for item in required if _matches(str(item), held)]
        missing = [item for item in required if item not in matched]
        passed = (not missing) if operator == "contains_all" else bool(matched)
        if passed:
            reason = f"Candidate's {label} include {', '.join(matched)}."
        elif operator == "contains_all":
            reason = (
                f"Candidate's {label} do not include {', '.join(missing)}. "
                f"Listed {label}: {_describe_list(held)}."
            )
        else:
            reason = (
                f"Candidate's {label} include none of {', '.join(map(str, required))}. "
                f"Listed {label}: {_describe_list(held)}."
            )
        return passed, held, reason

    if operator in {"is_true", "is_false"}:
        expected = operator == "is_true"
        passed = bool(observed) is expected
        state = "does" if observed else "does not"
        return passed, observed, f"Candidate {state} have {label}; the rule requires that they {'do' if expected else 'do not'}."

    return None, observed, f"Unsupported operator {operator!r}; sent to manual review."


def evaluate_rule(rule: ClassifiedRule, profile: AnonymizedProfile) -> RuleOutcome | None:
    """Evaluate one rule, or None if the rule is not applied."""
    if not rule.is_applied or rule.predicate is None:
        return None
    passed, observed, reason = evaluate_predicate(rule.predicate, profile)
    return RuleOutcome(
        rule_id=rule.id,
        source_text=rule.source_text,
        field=rule.predicate.field,
        passed=passed,
        observed=observed,
        reason=reason,
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


def screen(profile: AnonymizedProfile, rule_set: RuleSet) -> ScreeningResult:
    outcomes = [
        outcome
        for outcome in (evaluate_rule(rule, profile) for rule in rule_set.rules)
        if outcome is not None
    ]
    return ScreeningResult(profile.candidate_ref, outcomes)
