import pytest

from rescan.llm.client import LLMError, LLMResponse
from rescan.rules.classifier import compile_plan, split_plan
from rescan.rules.models import RuleVerdict
from rescan.rules.statutes import RiskLevel

PLAN = (
    "Senior data engineer. Must have 5+ years experience, Python and SQL, plus AWS or GCP. "
    "Bachelor's degree or higher. Must be a native English speaker and a recent graduate from a leading company. "
    "Nice to have: Terraform. Should have led an on-call rotation."
)


def by_text(rule_set, fragment):
    return next(rule for rule in rule_set.rules if fragment in rule.source_text)


def test_plan_is_split_into_requirements_and_preferences(llm):
    rule_set = compile_plan(PLAN, llm, role_context="Senior Data Engineer")
    assert rule_set.reasoning, "the model's reasoning is kept for the audit trail"
    assert rule_set.source_plan == PLAN

    core = by_text(rule_set, "5+ years")
    assert core.verdict is RuleVerdict.APPLICABLE and core.kind == "require"
    assert core.dsl == 'REQUIRE years_experience >= 5 AND skills HAS ALL ("Python", "SQL") AND skills HAS ANY ("AWS", "GCP")'

    degree = by_text(rule_set, "Bachelor")
    assert degree.dsl == "REQUIRE aqf >= 7"

    terraform = by_text(rule_set, "Terraform")
    assert terraform.kind == "prefer" and terraform.dsl == 'PREFER skills HAS ANY ("Terraform")'
    assert terraform in rule_set.preferences and terraform not in rule_set.requirements

    on_call = by_text(rule_set, "on-call")
    assert on_call.dsl == 'REQUIRE ASK "Has the candidate led an on-call rotation?"'


def test_proxies_in_the_plan_are_flagged_with_statutes_and_never_applied(llm):
    rule_set = compile_plan(PLAN, llm)
    flagged = by_text(rule_set, "native English")
    assert flagged.verdict is RuleVerdict.RISKY and flagged.risk is RiskLevel.HIGH
    ids = {finding.pattern_id for finding in flagged.findings}
    assert {"native_speaker", "recent_graduate", "employer_prestige"} <= ids
    assert flagged.clause is None and flagged.dsl is None
    assert all(finding.statutes for finding in flagged.findings)
    assert len(rule_set.flagged) == 1


def test_leading_company_is_the_frontends_example(llm):
    rule_set = compile_plan("3+ years of experience at a leading company", llm)
    rule = rule_set.rules[0]
    assert rule.risk is RiskLevel.REVIEW
    finding = next(f for f in rule.findings if f.pattern_id == "employer_prestige")
    assert "relevant" in finding.suggested_rewrite
    # Review-level: the measurable part is still applied, with a note to justify.
    assert rule.is_applied and rule.dsl == "REQUIRE years_experience >= 3"
    assert any("justification" in note for note in rule.notes)


def test_discrete_rules_come_back_one_per_input_in_order(llm):
    rule_set = compile_plan(None, llm, rule_texts=["At least 4 years experience", "", "Recent graduates only", "Bachelor degree"])
    assert [rule.id for rule in rule_set.rules] == ["rule_1", "rule_2", "rule_3"]
    assert [rule.verdict for rule in rule_set.rules] == [
        RuleVerdict.APPLICABLE, RuleVerdict.RISKY, RuleVerdict.APPLICABLE,
    ]


def test_proxy_inside_a_compiled_string_literal_is_still_caught(llm, monkeypatch):
    def sneaky(request):
        return LLMResponse(
            data={
                "reasoning": "x",
                "rules": [{
                    "source_text": "Strong communicator", "source_index": 1, "kind": "require",
                    "dsl": 'REQUIRE skills HAS ANY ("native English speaker")',
                    "justification": None, "risk": "none", "protected_attributes": [],
                    "explanation": None, "suggested_rewrite": None, "legal_basis": [],
                }],
            },
            model="m", backend="stub", latency_s=0.0,
        )

    monkeypatch.setattr(llm, "json_call", sneaky)
    rule = compile_plan(None, llm, rule_texts=["Strong communicator"]).rules[0]
    assert rule.verdict is RuleVerdict.RISKY
    assert not rule.is_applied
    assert any(f.pattern_id == "native_speaker" for f in rule.findings)


def test_proxy_inside_an_ask_question_is_caught(llm, monkeypatch):
    def sneaky(request):
        return LLMResponse(
            data={
                "reasoning": "x",
                "rules": [{
                    "source_text": "Good communicator", "source_index": 1, "kind": "require",
                    "dsl": 'REQUIRE ASK "Is the candidate a native English speaker?"',
                    "justification": None, "risk": "none", "protected_attributes": [],
                    "explanation": None, "suggested_rewrite": None, "legal_basis": [],
                }],
            },
            model="m", backend="stub", latency_s=0.0,
        )

    monkeypatch.setattr(llm, "json_call", sneaky)
    rule = compile_plan(None, llm, rule_texts=["Good communicator"]).rules[0]
    assert rule.verdict is RuleVerdict.RISKY and rule.clause is None


def test_forbidden_field_from_the_model_does_not_parse_and_is_not_applied(llm, monkeypatch):
    calls = []

    def forbidden(request):
        calls.append(request.task)
        if request.task == "compile_dsl_repair":
            return LLMResponse(data={"dsl": 'REQUIRE region = "Brisbane"'}, model="m", backend="stub", latency_s=0.0)
        return LLMResponse(
            data={
                "reasoning": "x",
                "rules": [{
                    "source_text": "Lives in Brisbane", "source_index": 1, "kind": "require",
                    "dsl": 'REQUIRE region = "Brisbane"',
                    "justification": None, "risk": "none", "protected_attributes": [],
                    "explanation": None, "suggested_rewrite": None, "legal_basis": [],
                }],
            },
            model="m", backend="stub", latency_s=0.0,
        )

    monkeypatch.setattr(llm, "json_call", forbidden)
    rule = compile_plan(None, llm, rule_texts=["Lives in Brisbane"]).rules[0]
    assert calls == ["compile_dsl", "compile_dsl_repair"], "one repair round, then give up"
    assert rule.verdict is RuleVerdict.UNMAPPABLE
    assert not rule.is_applied
    assert any("not queryable" in note for note in rule.notes)


def test_repair_round_can_rescue_a_typo(llm, monkeypatch):
    def flaky(request):
        if request.task == "compile_dsl_repair":
            return LLMResponse(data={"dsl": "REQUIRE years_experience >= 5"}, model="m", backend="stub", latency_s=0.0)
        return LLMResponse(
            data={
                "reasoning": "x",
                "rules": [{
                    "source_text": "5 years", "source_index": 1, "kind": "require",
                    "dsl": "REQUIRE years_experience >== 5",
                    "justification": "Depth of experience.", "risk": "none", "protected_attributes": [],
                    "explanation": None, "suggested_rewrite": None, "legal_basis": [],
                }],
            },
            model="m", backend="stub", latency_s=0.0,
        )

    monkeypatch.setattr(llm, "json_call", flaky)
    rule = compile_plan(None, llm, rule_texts=["5 years"]).rules[0]
    assert rule.is_applied and rule.dsl == "REQUIRE years_experience >= 5"
    assert rule.justification == "Depth of experience."


def test_model_can_add_risk_but_not_remove_it(llm, monkeypatch):
    def lenient(request):
        return LLMResponse(
            data={
                "reasoning": "x",
                "rules": [{
                    "source_text": "Must be a native English speaker", "source_index": 1, "kind": "require",
                    "dsl": 'REQUIRE languages HAS ANY ("English")',
                    "justification": None, "risk": "none", "protected_attributes": [],
                    "explanation": "Fine.", "suggested_rewrite": None, "legal_basis": [],
                }],
            },
            model="m", backend="stub", latency_s=0.0,
        )

    monkeypatch.setattr(llm, "json_call", lenient)
    rule = compile_plan(None, llm, rule_texts=["Must be a native English speaker"]).rules[0]
    assert rule.risk is RiskLevel.HIGH and not rule.is_applied
    assert any("not applied" in note for note in rule.notes)


def test_model_added_risk_is_recorded_with_its_legal_basis(llm, monkeypatch):
    def strict(request):
        return LLMResponse(
            data={
                "reasoning": "x",
                "rules": [{
                    "source_text": "Must be able to lift heavy loads all day", "source_index": 1, "kind": "require",
                    "dsl": None, "justification": None, "risk": "high", "protected_attributes": ["disability"],
                    "explanation": "General fitness screens on disability.", "suggested_rewrite": "Lift up to 15kg with adjustments.",
                    "legal_basis": ["DDA_1992"],
                }],
            },
            model="m", backend="stub", latency_s=0.0,
        )

    monkeypatch.setattr(llm, "json_call", strict)
    rule = compile_plan(None, llm, rule_texts=["Must be able to lift heavy loads all day"]).rules[0]
    finding = next(f for f in rule.findings if f.source == "model")
    assert rule.verdict is RuleVerdict.RISKY
    assert any("Disability Discrimination Act" in s for s in finding.statutes)


def test_inference_failure_still_flags_but_applies_nothing(llm, monkeypatch):
    def boom(request):
        raise LLMError("inference down")

    monkeypatch.setattr(llm, "json_call", boom)
    rule_set = compile_plan(PLAN, llm)
    assert rule_set.reasoning is None
    assert rule_set.applied == []
    assert rule_set.flagged, "the statute table runs without a model"
    assert all(any("not applied" in note for note in rule.notes) for rule in rule_set.rules)


def test_empty_plan_compiles_to_nothing(llm):
    assert compile_plan("", llm).rules == []
    assert compile_plan(None, llm, rule_texts=["", "  "]).rules == []


def test_split_plan_handles_bullets_and_numbers():
    assert split_plan("- one\n2. two\n• three. Four; five") == ["one", "two", "three.", "Four;", "five"]


def test_compiled_rules_serialise_and_reload(llm):
    from rescan.rules.models import RuleSet

    rule_set = compile_plan(PLAN, llm)
    reloaded = RuleSet.model_validate_json(rule_set.model_dump_json())
    assert reloaded == rule_set
    assert [r.dsl for r in reloaded.applied] == [r.dsl for r in rule_set.applied]


def test_stale_predicate_rows_still_load():
    from rescan.rules.models import RuleSet

    legacy = {"rules": [{"id": "rule_1", "source_text": "x", "verdict": "applicable", "risk": "none",
                         "predicate": {"field": "total_years_experience", "operator": "gte", "value": 5}}]}
    rule_set = RuleSet.model_validate(legacy)
    assert rule_set.rules[0].clause is None and not rule_set.rules[0].is_applied
