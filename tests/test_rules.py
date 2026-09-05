import pytest

from rescan.rules.classifier import classify_rule, classify_rules, scan_patterns
from rescan.rules.engine import evaluate_predicate, screen
from rescan.rules.models import Predicate, RuleVerdict
from rescan.rules.statutes import RiskLevel
from rescan.schemas import AnonymizedProfile, Qualification, Skill, WorkRights, WorkRightsStatus


# --------------------------------------------------------------------------
# Legal-risk classification
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,pattern_id",
    [
        ("Must be a native English speaker", "native_speaker"),
        ("Looking for someone who is a good cultural fit", "cultural_fit"),
        ("Recent graduates preferred", "recent_graduate"),
        ("Must hold an Australian university degree", "australian_educated"),
        ("No career gaps in the last 10 years", "career_gap"),
        ("Young and energetic team player", "age_coded"),
        ("Must be clean-shaven", "religion_appearance"),
        ("Must be Australian born", "birthplace"),
        ("Requires 24/7 availability", "availability_always"),
        ("Applicants must have no accent", "unaccented"),
    ],
)
def test_known_risky_phrases_are_caught(text, pattern_id):
    findings = scan_patterns(text)
    assert pattern_id in {f.pattern_id for f in findings}


@pytest.mark.parametrize(
    "text",
    [
        "At least 5 years of backend engineering experience",
        "Experience with Kubernetes and Terraform",
        "Bachelor degree or higher in a technical field",
        "Must hold unrestricted Australian work rights",
    ],
)
def test_capability_rules_are_not_flagged(text):
    assert scan_patterns(text) == []


def test_flagged_rule_cites_statute_and_offers_measurable_rewrite(llm):
    rule = classify_rule("Must be a native English speaker", llm)
    assert rule.verdict is RuleVerdict.RISKY
    assert rule.risk is RiskLevel.HIGH
    finding = rule.findings[0]
    assert any("Racial Discrimination Act 1975" in s for s in finding.statutes)
    assert "national or ethnic origin" in finding.protected_attributes
    assert finding.suggested_rewrite
    # The rewrite must test the capability, not restate the proxy.
    assert "native" not in finding.suggested_rewrite.lower()


def test_a_high_risk_rule_is_never_applied(llm):
    # This one compiles cleanly to an AQF test, so the only thing stopping it
    # from screening candidates is the risk finding.
    rule = classify_rule("Must hold an Australian university degree", llm)
    assert rule.verdict is RuleVerdict.RISKY
    assert rule.predicate is None, "a flagged rule must not survive as a filter"
    assert not rule.is_applied
    assert any("not applied" in note for note in rule.notes)


def test_high_risk_rule_that_does_not_compile_is_still_flagged(llm):
    rule = classify_rule("Only recent graduates need apply", llm)
    assert rule.verdict is RuleVerdict.RISKY
    assert not rule.is_applied


def test_review_level_rule_is_applied_but_requires_justification(llm):
    rule = classify_rule("Australian citizens only", llm)
    assert rule.verdict is RuleVerdict.APPLICABLE
    assert rule.risk is RiskLevel.REVIEW
    assert rule.is_applied
    assert any("justification" in note for note in rule.notes)


def test_multiple_risks_in_one_rule_are_all_reported(llm):
    rule = classify_rule("Recent graduate who is a good cultural fit", llm)
    ids = {f.pattern_id for f in rule.findings}
    assert {"recent_graduate", "cultural_fit"} <= ids


def test_unmappable_rule_goes_to_a_human_not_a_filter(llm):
    rule = classify_rule("Should be a nice person", llm)
    assert rule.verdict is RuleVerdict.UNMAPPABLE
    assert not rule.is_applied
    assert any("human reviewer" in note for note in rule.notes)


def test_classification_failure_does_not_become_a_silent_filter(llm, monkeypatch):
    from rescan.llm.client import LLMError

    def boom(request):
        raise LLMError("inference down")

    monkeypatch.setattr(llm, "json_call", boom)
    rule = classify_rule("At least 5 years experience", llm)
    assert not rule.is_applied
    assert any("not applied" in note for note in rule.notes)


def test_rule_set_separates_applied_from_flagged(llm):
    rule_set = classify_rules(
        [
            "At least 5 years of backend engineering experience",
            "Must be a native English speaker",
            "Bachelor degree or higher",
        ],
        llm,
    )
    assert len(rule_set.applied) == 2
    assert len(rule_set.flagged) == 1


# --------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------


def profile(**kwargs) -> AnonymizedProfile:
    defaults: dict = {"candidate_ref": "Candidate 1"}
    defaults.update(kwargs)
    return AnonymizedProfile(**defaults)


def test_numeric_rule_explains_itself_with_the_observed_value():
    predicate = Predicate(
        field="total_years_experience", operator="gte", value=5, description="At least 5 years."
    )
    passed, observed, reason = evaluate_predicate(predicate, profile(total_years_experience=3.0))
    assert passed is False
    assert observed == 3.0
    assert "3 years of professional experience" in reason
    assert "at least 5 years" in reason


def test_missing_value_routes_to_review_rather_than_rejecting():
    predicate = Predicate(
        field="total_years_experience", operator="gte", value=5, description="At least 5 years."
    )
    passed, _, reason = evaluate_predicate(predicate, profile(total_years_experience=None))
    assert passed is None, "silence in a resume must not count as evidence against a candidate"
    assert "manual review" in reason


def test_skill_matching_is_loose_enough_for_real_resumes():
    predicate = Predicate(
        field="skills", operator="contains_any", value=["AWS"], description="AWS."
    )
    passed, _, _ = evaluate_predicate(predicate, profile(skills=[Skill(name="AWS Solutions Architect")]))
    assert passed is True


def test_contains_all_names_what_was_missing():
    predicate = Predicate(
        field="skills", operator="contains_all", value=["Go", "Rust"], description="Go and Rust."
    )
    passed, _, reason = evaluate_predicate(predicate, profile(skills=[Skill(name="Go")]))
    assert passed is False
    assert "Rust" in reason


def test_aqf_rule_uses_the_deterministic_level():
    predicate = Predicate(field="highest_aqf", operator="gte", value=7, description="Bachelor.")
    candidate = profile(qualifications=[Qualification(title="Master of IT", aqf_level=9)])
    passed, _, reason = evaluate_predicate(predicate, candidate)
    assert passed is True
    assert "AQF level 9" in reason


def test_screening_collects_eligibility_and_review_state(llm):
    rule_set = classify_rules(
        ["At least 5 years experience", "Bachelor degree or higher"], llm
    )
    candidate = profile(
        total_years_experience=6.0, qualifications=[Qualification(title="BSc", aqf_level=7)]
    )
    result = screen(candidate, rule_set)
    assert result.eligible is True
    assert result.needs_manual_review is False
    assert len(result.outcomes) == 2


def test_excluded_candidate_carries_a_plain_language_reason(llm):
    rule_set = classify_rules(["At least 10 years experience"], llm)
    result = screen(profile(total_years_experience=2.0), rule_set)
    assert result.eligible is False
    reasons = result.exclusion_reasons()
    assert reasons and "2 years of professional experience" in reasons[0]


def test_flagged_rules_do_not_screen_anyone(llm):
    rule_set = classify_rules(["Must be a native English speaker"], llm)
    result = screen(profile(total_years_experience=1.0), rule_set)
    assert result.outcomes == []
    assert result.eligible is True


def test_work_rights_rule_applies_to_the_structured_status(llm):
    rule_set = classify_rules(["Must hold unrestricted Australian work rights"], llm)
    sponsored = profile(
        work_rights=WorkRights(status=WorkRightsStatus.REQUIRES_SPONSORSHIP, unrestricted=False)
    )
    assert screen(sponsored, rule_set).eligible is False
    citizen = profile(work_rights=WorkRights(status=WorkRightsStatus.CITIZEN, unrestricted=True))
    assert screen(citizen, rule_set).eligible is True
