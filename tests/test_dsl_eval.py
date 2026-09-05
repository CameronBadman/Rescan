import pytest

from rescan.dsl import parse_expr
from rescan.dsl.eval import Verdict, evaluate
from rescan.schemas import (
    AnonymizedProfile,
    Experience,
    Project,
    Qualification,
    Skill,
    WorkRights,
    WorkRightsStatus,
)


def profile(**kwargs) -> AnonymizedProfile:
    defaults: dict = {"candidate_ref": "Candidate 1"}
    defaults.update(kwargs)
    return AnonymizedProfile(**defaults)


def rich() -> AnonymizedProfile:
    return profile(
        total_years_experience=6.0,
        skills=[
            Skill(name="Python", category="technical", years=4, proficiency="expert"),
            Skill(name="AWS Solutions Architect", category="tool"),
            Skill(name="Stakeholder management", category="soft"),
        ],
        experience=[
            Experience(title="Senior Data Engineer", months=30, is_current=True, seniority="senior",
                       industry="banking", employment_type="permanent", team_size=4, technologies=["Kafka", "Spark"]),
            Experience(title="Data Engineer", months=24, seniority="mid", industry="retail", employment_type="contract"),
            Experience(title="Analyst", months=None),
        ],
        qualifications=[
            Qualification(title="Master of IT", aqf_level=9, field_of_study="Information Technology"),
            Qualification(title="Bachelor of Science", aqf_level=7, field_of_study="Mathematics"),
        ],
        certifications=["AWS Certified Solutions Architect"],
        languages=["English", "Hindi"],
        projects=[Project(name="Streaming ETL", technologies=["Flink"], months=6)],
        work_rights=WorkRights(status=WorkRightsStatus.PERMANENT_RESIDENT, unrestricted=True),
        management_years=2.0,
        people_managed_max=4,
    )


def check(text: str, candidate: AnonymizedProfile | None = None) -> Verdict:
    return evaluate(parse_expr(text), candidate or rich())


# --------------------------------------------------------------------------
# Three-valued logic
# --------------------------------------------------------------------------


def test_unknown_and_false_is_false():
    # languages is unknown here; years fails outright, so the rule fails.
    verdict = check('languages HAS ANY ("French") AND years_experience >= 10', profile(total_years_experience=2.0))
    assert verdict.value is False
    assert "2 years of professional experience" in verdict.reason
    assert "manual review" not in verdict.reason


def test_unknown_or_true_is_true():
    verdict = check('languages HAS ANY ("French") OR years_experience >= 1', profile(total_years_experience=2.0))
    assert verdict.value is True


def test_unknown_propagates_otherwise():
    assert check('languages HAS ANY ("French") AND years_experience >= 1', profile(total_years_experience=2.0)).value is None
    assert check('languages HAS ANY ("French") OR years_experience >= 10', profile(total_years_experience=2.0)).value is None


def test_not_inverts_but_keeps_unknown():
    assert check("NOT years_experience >= 10").value is True
    assert check("NOT years_experience >= 1").value is False
    assert check("NOT years_experience >= 1", profile()).value is None


def test_or_failure_lists_every_alternative():
    verdict = check("years_experience >= 10 OR aqf >= 10")
    assert verdict.value is False
    assert "(a)" in verdict.reason and "(b)" in verdict.reason
    assert "6 years" in verdict.reason and "AQF level 9" in verdict.reason


def test_and_failure_reports_only_what_failed():
    verdict = check('years_experience >= 5 AND skills HAS ALL ("Python", "Rust")')
    assert verdict.value is False
    assert "Rust" in verdict.reason
    assert "6 years" not in verdict.reason, "passing conjuncts are not the reason for an exclusion"


# --------------------------------------------------------------------------
# Leaves
# --------------------------------------------------------------------------


def test_numeric_reason_matches_the_established_wording():
    verdict = check("years_experience >= 8")
    assert verdict.reason == (
        "Candidate has 6 years of professional experience; the rule requires at least 8 years. "
        "This does not meet the requirement."
    )


@pytest.mark.parametrize("cmp,expected", [(">=", True), (">", False), ("<=", True), ("<", False), ("=", True), ("!=", False)])
def test_every_comparator(cmp, expected):
    assert check(f"years_experience {cmp} 6").value is expected


def test_empty_list_is_unknown_not_zero():
    verdict = check('certifications HAS ANY ("AWS")', profile(total_years_experience=3.0))
    assert verdict.value is None
    assert "does not state certifications" in verdict.reason


def test_count_of_empty_list_is_unknown_too():
    assert check("skill_count >= 1", profile()).value is None


def test_work_rights_forms():
    assert check("work_rights IS unrestricted").value is True
    assert check("work_rights IS citizen").value is False
    assert check('work_rights IN ("citizen", "permanent_resident")').value is True
    unknown = check("work_rights IS unrestricted", profile())
    assert unknown.value is None and "manual review" in unknown.reason
    sponsored = profile(work_rights=WorkRights(status=WorkRightsStatus.REQUIRES_SPONSORSHIP, unrestricted=False))
    assert check("work_rights IS unrestricted", sponsored).value is False
    assert check("requires_sponsorship IS TRUE", sponsored).value is True


def test_derived_fields():
    assert check("has_masters IS TRUE").value is True
    assert check("has_doctorate IS TRUE").value is False
    assert check("longest_role_months >= 30").value is True
    assert check("average_role_months >= 27").value is True
    assert check("leadership_role_count >= 1").value is False
    assert check("seniority_level >= 4").value is True
    assert check('highest_seniority IN ("senior", "lead")').value is True
    assert check('industries HAS ANY ("banking")').value is True
    assert check('technologies HAS ALL ("Kafka", "Flink")').value is True, "technologies span roles and projects"
    assert check("multilingual IS TRUE").value is True
    assert check("has_management_experience IS TRUE").value is True
    assert check('expert_skills HAS ANY ("Python")').value is True
    assert check('fields_of_study HAS ANY ("mathematics")').value is True
    assert check('current_role_title = "data engineer"').value is True


def test_text_fields_match_loosely_and_explain():
    verdict = check('latest_role_title = "Data Engineer"')
    assert verdict.value is True
    assert '"Senior Data Engineer"' in verdict.reason


# --------------------------------------------------------------------------
# Records and aggregates
# --------------------------------------------------------------------------


def test_any_record_names_the_record_that_satisfied():
    verdict = check('ANY skill WHERE name = "Python" AND years >= 3')
    assert verdict.value is True
    assert "Python (4 years)" in verdict.reason


def test_any_record_failure_lists_what_was_there():
    verdict = check('ANY role WHERE title HAS ANY ("manager")')
    assert verdict.value is False
    assert "Senior Data Engineer" in verdict.reason


def test_any_record_is_unknown_when_a_record_could_not_be_assessed():
    # The analyst role has no months, so "any role >= 36 months" cannot be decided.
    verdict = check("ANY role WHERE months >= 36")
    assert verdict.value is None
    assert "manual review" in verdict.reason.lower()


def test_any_record_over_an_empty_list_is_unknown():
    assert check('ANY project WHERE name = "x"', profile()).value is None


def test_count_aggregate_with_uncertainty():
    assert check("COUNT(role WHERE months >= 12) >= 2").value is True, "two roles definitely qualify"
    assert check("COUNT(role WHERE months >= 12) >= 3").value is None, "the third depends on an unstated duration"
    assert check("COUNT(role WHERE months >= 12) >= 4").value is False, "even counting the unknown it cannot reach 4"
    assert check('COUNT(role WHERE seniority IN ("senior", "lead", "principal")) >= 1').value is True


def test_sum_and_max_pass_on_partial_data_only_when_monotone():
    assert check("SUM(role.months) >= 50").value is True, "54 known months already clear 50"
    assert check("SUM(role.months) <= 60").value is None, "an unstated duration could push it over"
    assert check('MAX(skill.years WHERE name = "Python") >= 3').value is True
    assert check('MAX(skill.years WHERE name = "Python") >= 5').value is False


def test_avg_and_min_are_undecided_with_missing_values():
    assert check("AVG(role.months) >= 10").value is None
    assert check("MIN(role.months) >= 10").value is None
    assert check('MIN(role.months WHERE title HAS ANY ("engineer")) >= 20').value is True


def test_aggregate_over_no_matches_is_a_real_zero():
    verdict = check('COUNT(role WHERE title HAS ANY ("manager")) >= 1')
    assert verdict.value is False
    assert "is 0" in verdict.reason


def test_aggregate_filter_on_an_unstated_field_is_undecided():
    # The analyst role states no industry, so it might be a mining role.
    verdict = check('COUNT(role WHERE industry = "mining") >= 1')
    assert verdict.value is None
    assert "1 role could not be assessed" in verdict.reason


def test_ask_without_a_judge_is_unknown():
    verdict = check('ASK "Has the candidate led an incident response?"')
    assert verdict.value is None
    assert "disabled" in verdict.reason


def test_ask_is_not_consulted_when_structured_clauses_already_decide():
    calls = []

    class Judge:
        def ask(self, question, profile):
            calls.append(question)
            return Verdict(True, "yes")

    expr = parse_expr('years_experience >= 20 AND ASK "Anything?"')
    assert evaluate(expr, rich(), Judge()).value is False
    assert calls == [], "a model call must not be spent on a candidate already excluded"

    expr = parse_expr('years_experience >= 1 OR ASK "Anything?"')
    assert evaluate(expr, rich(), Judge()).value is True
    assert calls == []

    expr = parse_expr('years_experience >= 1 AND ASK "Anything?"')
    assert evaluate(expr, rich(), Judge()).value is True
    assert calls == ["Anything?"]
