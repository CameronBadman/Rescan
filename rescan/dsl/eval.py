"""Evaluate an expression against one anonymized profile.

Three-valued. Every leaf yields TRUE, FALSE or UNKNOWN, and the connectives
follow Kleene's tables: UNKNOWN AND FALSE is FALSE, UNKNOWN OR TRUE is TRUE,
and otherwise unknown propagates. That is what makes "silence in a resume is
not evidence against a person" hold across arbitrary composition: a candidate
fails a rule only when the stated evidence definitely fails it.

Every verdict carries a reason written for a person: the observed value, the
requirement, and whether it was met. Composite reasons are built from the
leaves that decided the outcome, so an exclusion can be read back to the
structured values it rested on.

`ASK` leaves are handed to a judge and evaluated last, only when the structured
part of the expression has not already decided it — a model call is never
spent on a candidate a structured clause already excluded.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from rescan.dsl import fields as vocab
from rescan.dsl.ast import (
    Aggregate,
    And,
    AnyRecord,
    Ask,
    Compare,
    Expr,
    HasAll,
    HasAny,
    In,
    Is,
    Not,
    Or,
    has_ask,
)
from rescan.schemas import AnonymizedProfile

MANUAL_REVIEW = "Sent to manual review rather than excluded."


@dataclass
class Verdict:
    # None means "could not be determined" — never an automatic exclusion.
    value: bool | None
    reason: str
    observed: Any = None

    @property
    def unknown(self) -> bool:
        return self.value is None


class JudgeLike(Protocol):
    def ask(self, question: str, profile: AnonymizedProfile) -> Verdict: ...


# --------------------------------------------------------------------------
# Matching helpers (shared with the rule engine)
# --------------------------------------------------------------------------


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


def _join(items: list[str]) -> str:
    return ", ".join(items)


_COMPARATOR_WORDS = {
    ">=": "at least",
    "<=": "no more than",
    ">": "more than",
    "<": "fewer than",
    "=": "exactly",
    "!=": "anything other than",
}


def _compare_numbers(observed: float, cmp: str, target: float) -> bool:
    return {
        ">=": observed >= target,
        "<=": observed <= target,
        ">": observed > target,
        "<": observed < target,
        "=": observed == target,
        "!=": observed != target,
    }[cmp]


# --------------------------------------------------------------------------
# Leaves
# --------------------------------------------------------------------------


def _unknown(spec: vocab.FieldSpec, record: vocab.RecordSpec | None) -> Verdict:
    if record is not None:
        return Verdict(None, f"{spec.label} is not stated for this {record.name}", None)
    return Verdict(
        None,
        f"The resume does not state {spec.label}, so this rule could not be applied. {MANUAL_REVIEW}",
        None,
    )


def _read(spec: vocab.FieldSpec, subject: Any) -> Any:
    observed = spec.reader(subject)
    if spec.kind == "list" and not observed:
        return None
    return observed


def _leaf_compare(node: Compare, spec: vocab.FieldSpec, subject: Any, record: vocab.RecordSpec | None) -> Verdict:
    observed = _read(spec, subject)
    if observed is None:
        return _unknown(spec, record)
    label = spec.label

    if spec.kind == "number":
        try:
            number = float(observed)
        except (TypeError, ValueError):
            return Verdict(None, f"Could not compare {label} ({observed!r}); {MANUAL_REVIEW.lower()}", observed)
        target = float(node.value)
        passed = _compare_numbers(number, node.cmp, target)
        subject_t, target_t = spec.phrasing or (
            (f"{label} is {{observed:g}}" if record else f"Candidate's {label} is {{observed:g}}"),
            "{target:g}",
        )
        subject_text = subject_t.format(observed=number)
        if record is not None:
            subject_text = subject_text[0].upper() + subject_text[1:]
        requirement = target_t.format(target=target)
        verb = "meets" if passed else "does not meet"
        return Verdict(
            passed,
            f"{subject_text}; the rule requires {_COMPARATOR_WORDS[node.cmp]} {requirement}. This {verb} the requirement.",
            number,
        )

    if spec.kind == "text":
        text = str(observed)
        matched = _matches(str(node.value), [text])
        passed = matched if node.cmp == "=" else not matched
        relation = "matches" if matched else "does not match"
        return Verdict(passed, f'{label.capitalize()} "{text}" {relation} "{node.value}".', text)

    # enum: exact, after the parser has normalised the value
    value = str(node.value)
    if spec.name == "work_rights" and value == vocab.WORK_RIGHTS_PSEUDO:
        return _work_rights_unrestricted(subject, node.cmp == "=")
    equal = str(observed) == value
    passed = equal if node.cmp == "=" else not equal
    return Verdict(passed, f"{label.capitalize()} is {observed}; the rule requires {_COMPARATOR_WORDS[node.cmp]} {value}.", observed)


def _work_rights_unrestricted(profile: AnonymizedProfile, expected: bool) -> Verdict:
    observed = profile.work_rights.unrestricted
    if observed is None:
        return Verdict(
            None,
            f"The resume does not state whether work rights are unrestricted, so this rule could not be applied. {MANUAL_REVIEW}",
            None,
        )
    passed = bool(observed) is expected
    state = "does" if observed else "does not"
    return Verdict(
        passed,
        f"Candidate {state} have unrestricted work rights; the rule requires that they {'do' if expected else 'do not'}.",
        observed,
    )


def _leaf_has(node: HasAny | HasAll, spec: vocab.FieldSpec, subject: Any, record: vocab.RecordSpec | None) -> Verdict:
    observed = _read(spec, subject)
    if observed is None:
        return _unknown(spec, record)
    label = spec.label
    want_all = isinstance(node, HasAll)

    if spec.kind == "text":
        text = str(observed)
        matched = [v for v in node.values if v.lower() in text.lower()]
        missing = [v for v in node.values if v not in matched]
        passed = (not missing) if want_all else bool(matched)
        if passed:
            return Verdict(True, f'{label.capitalize()} "{text}" mentions {_join(matched)}.', text)
        if want_all:
            return Verdict(False, f'{label.capitalize()} "{text}" does not mention {_join(missing)}.', text)
        return Verdict(False, f'{label.capitalize()} "{text}" mentions none of {_join(node.values)}.', text)

    held = [str(item) for item in observed]
    matched = [item for item in node.values if _matches(item, held)]
    missing = [item for item in node.values if item not in matched]
    passed = (not missing) if want_all else bool(matched)
    if passed:
        reason = f"Candidate's {label} include {_join(matched)}."
    elif want_all:
        reason = f"Candidate's {label} do not include {_join(missing)}. Listed {label}: {_describe_list(held)}."
    else:
        reason = f"Candidate's {label} include none of {_join(node.values)}. Listed {label}: {_describe_list(held)}."
    return Verdict(passed, reason, held)


def _leaf_in(node: In, spec: vocab.FieldSpec, subject: Any, record: vocab.RecordSpec | None) -> Verdict:
    observed = _read(spec, subject)
    if observed is None:
        return _unknown(spec, record)
    if spec.name == "work_rights" and vocab.WORK_RIGHTS_PSEUDO in node.values:
        unrestricted = _work_rights_unrestricted(subject, True)
        if unrestricted.value:
            return unrestricted
    passed = str(observed) in node.values
    return Verdict(passed, f"{spec.label.capitalize()} is {observed}; the rule accepts {_join(node.values)}.", observed)


def _leaf_is(node: Is, spec: vocab.FieldSpec, subject: Any, record: vocab.RecordSpec | None) -> Verdict:
    observed = spec.reader(subject)
    if observed is None:
        return _unknown(spec, record)
    label = spec.label
    if spec.kind == "bool":
        expected = bool(node.value)
        passed = bool(observed) is expected
        if record is not None:
            return Verdict(passed, f"The {record.name} is {'' if observed else 'not '}{label}; the rule requires {'' if expected else 'not '}{label}.", observed)
        state = "does" if observed else "does not"
        return Verdict(passed, f"Candidate {state} have {label}; the rule requires that they {'do' if expected else 'do not'}.", observed)
    value = str(node.value)
    if spec.name == "work_rights" and value == vocab.WORK_RIGHTS_PSEUDO:
        return _work_rights_unrestricted(subject, True)
    passed = str(observed) == value
    return Verdict(passed, f"{label.capitalize()} is {observed}; the rule requires {value}.", observed)


def _leaf(node: Expr, subject: Any, record: vocab.RecordSpec | None) -> Verdict:
    spec = record.fields[node.field] if record is not None else vocab.FIELDS[node.field]  # type: ignore[attr-defined]
    if isinstance(node, Compare):
        return _leaf_compare(node, spec, subject, record)
    if isinstance(node, (HasAny, HasAll)):
        return _leaf_has(node, spec, subject, record)
    if isinstance(node, In):
        return _leaf_in(node, spec, subject, record)
    if isinstance(node, Is):
        return _leaf_is(node, spec, subject, record)
    raise TypeError(f"not a leaf: {type(node).__name__}")


# --------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------


def _record_verdicts(record: vocab.RecordSpec, where: Expr | None, profile: AnonymizedProfile) -> list[tuple[Any, Verdict]]:
    items = record.reader(profile)
    if where is None:
        return [(item, Verdict(True, "", item)) for item in items]
    return [(item, _evaluate(where, item, record, None)) for item in items]


def _any_record(node: AnyRecord, profile: AnonymizedProfile) -> Verdict:
    record = vocab.RECORDS[node.record]
    items = record.reader(profile)
    if not items:
        return Verdict(None, f"The resume lists no {record.plural}, so this rule could not be applied. {MANUAL_REVIEW}", None)
    verdicts = _record_verdicts(record, node.where, profile)
    where_text = node.where.to_dsl()
    for item, verdict in verdicts:
        if verdict.value is True:
            return Verdict(True, f"Listed {record.name} '{record.summary(item)}' satisfies {where_text}: {verdict.reason}", record.summary(item))
    unknown = [(item, verdict) for item, verdict in verdicts if verdict.value is None]
    listed = _describe_list([record.summary(item) for item in items])
    if unknown:
        item, verdict = unknown[0]
        return Verdict(
            None,
            f"Could not tell whether any listed {record.name} satisfies {where_text} "
            f"({record.summary(item)}: {verdict.reason}). {MANUAL_REVIEW}",
            listed,
        )
    return Verdict(False, f"No listed {record.name} satisfies {where_text}. Listed {record.plural}: {listed}.", listed)


def _aggregate(node: Aggregate, profile: AnonymizedProfile) -> Verdict:
    record = vocab.RECORDS[node.record]
    items = record.reader(profile)
    if not items:
        return Verdict(None, f"The resume lists no {record.plural}, so {node.subject()} could not be computed. {MANUAL_REVIEW}", None)

    verdicts = _record_verdicts(record, node.where, profile)
    matched = [item for item, verdict in verdicts if verdict.value is True]
    uncertain = sum(1 for _, verdict in verdicts if verdict.value is None)
    target = float(node.value)
    words = _COMPARATOR_WORDS[node.cmp]

    if node.func == "count":
        low = len(matched)
        high = low + uncertain
        passed_low = _compare_numbers(low, node.cmp, target)
        passed_high = _compare_numbers(high, node.cmp, target)
        if passed_low == passed_high:
            verb = "meets" if passed_low else "does not meet"
            return Verdict(passed_low, f"{node.subject()} is {low:g}; the rule requires {words} {target:g}. This {verb} the requirement.", low)
        noun = record.name if uncertain == 1 else record.plural
        return Verdict(
            None,
            f"{node.subject()} is between {low:g} and {high:g} because {uncertain} {noun} could not be assessed; "
            f"the rule requires {words} {target:g}. {MANUAL_REVIEW}",
            low,
        )

    spec = record.fields[node.field or ""]
    values = [spec.reader(item) for item in matched]
    known = [float(v) for v in values if v is not None]
    missing = len(values) - len(known)
    uncertain += missing
    if not known:
        if node.func in {"sum"} and not uncertain:
            result = 0.0
        else:
            return Verdict(
                None,
                f"{node.subject()} could not be computed: no matching {record.plural} state {spec.label}. {MANUAL_REVIEW}",
                None,
            )
    else:
        result = {
            "sum": sum(known),
            "max": max(known),
            "min": min(known),
            "avg": sum(known) / len(known),
        }[node.func]
    result = round(result, 2)
    passed = _compare_numbers(result, node.cmp, target)

    # More data can only raise a sum or a max, so a pass on partial data stands
    # for >= and >; anything else with missing values is undecided.
    monotone = node.func in {"sum", "max"} and node.cmp in {">=", ">"}
    if uncertain and not (passed and monotone):
        return Verdict(
            None,
            f"{node.subject()} is {result:g} on the {record.plural} that state {spec.label}, but {uncertain} could not be assessed; "
            f"the rule requires {words} {target:g}. {MANUAL_REVIEW}",
            result,
        )
    verb = "meets" if passed else "does not meet"
    return Verdict(passed, f"{node.subject()} is {result:g}; the rule requires {words} {target:g}. This {verb} the requirement.", result)


# --------------------------------------------------------------------------
# Connectives
# --------------------------------------------------------------------------


def _ordered(args: list[Expr]) -> list[Expr]:
    """Structured arguments first; ASK last so a model call is only made when needed."""
    return sorted(args, key=has_ask)


def _and(node: And, subject: Any, record: vocab.RecordSpec | None, judge: JudgeLike | None) -> Verdict:
    verdicts: list[Verdict] = []
    for arg in _ordered(node.args):
        verdict = _evaluate(arg, subject, record, judge)
        verdicts.append(verdict)
        if verdict.value is False:
            failed = [v for v in verdicts if v.value is False]
            return Verdict(False, " ".join(v.reason for v in failed), [v.observed for v in verdicts])
    unknown = [v for v in verdicts if v.value is None]
    if unknown:
        return Verdict(None, " ".join(v.reason for v in unknown), [v.observed for v in verdicts])
    return Verdict(True, " ".join(v.reason for v in verdicts), [v.observed for v in verdicts])


def _or(node: Or, subject: Any, record: vocab.RecordSpec | None, judge: JudgeLike | None) -> Verdict:
    verdicts: list[Verdict] = []
    for arg in _ordered(node.args):
        verdict = _evaluate(arg, subject, record, judge)
        verdicts.append(verdict)
        if verdict.value is True:
            return Verdict(True, verdict.reason, verdict.observed)
    if any(v.value is None for v in verdicts):
        unknown = [v for v in verdicts if v.value is None]
        return Verdict(None, "Could not decide an alternative: " + " ".join(v.reason for v in unknown), [v.observed for v in verdicts])
    alternatives = "; ".join(f"({chr(97 + i)}) {v.reason}" for i, v in enumerate(verdicts))
    return Verdict(False, f"None of the alternatives were met: {alternatives}", [v.observed for v in verdicts])


def _not(node: Not, subject: Any, record: vocab.RecordSpec | None, judge: JudgeLike | None) -> Verdict:
    inner = _evaluate(node.arg, subject, record, judge)
    if inner.value is None:
        return inner
    if inner.value:
        return Verdict(False, f"{inner.reason} The rule requires the opposite.", inner.observed)
    return Verdict(True, f"{inner.reason} The rule requires exactly that.", inner.observed)


def _ask(node: Ask, profile: AnonymizedProfile, judge: JudgeLike | None) -> Verdict:
    if judge is None:
        return Verdict(
            None,
            f'Model checks are disabled for this evaluation, so "{node.question}" could not be assessed. {MANUAL_REVIEW}',
            None,
        )
    return judge.ask(node.question, profile)


def _evaluate(node: Expr, subject: Any, record: vocab.RecordSpec | None, judge: JudgeLike | None) -> Verdict:
    if isinstance(node, And):
        return _and(node, subject, record, judge)
    if isinstance(node, Or):
        return _or(node, subject, record, judge)
    if isinstance(node, Not):
        return _not(node, subject, record, judge)
    if isinstance(node, AnyRecord):
        return _any_record(node, subject)
    if isinstance(node, Aggregate):
        return _aggregate(node, subject)
    if isinstance(node, Ask):
        return _ask(node, subject, judge)
    return _leaf(node, subject, record)


def evaluate(expr: Expr, profile: AnonymizedProfile, judge: JudgeLike | None = None) -> Verdict:
    """Evaluate `expr` against `profile`. Unknown never rejects."""
    return _evaluate(expr, profile, None, judge)
