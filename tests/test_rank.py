import pytest

from rescan.pipeline.rank import (
    DEFAULT_CRITERIA,
    RankingError,
    borderline_refs,
    build_shortlist,
    score_candidate,
    triage_rank,
)
from rescan.schemas import (
    AnonymizedProfile,
    CandidateScore,
    Qualification,
    RoleSpec,
    Skill,
)


ROLE = RoleSpec(
    title="Senior Backend Engineer",
    required_skills=["Python", "AWS"],
    desirable_skills=["Kubernetes", "Go"],
    min_years_experience=5,
    min_aqf=7,
)


def candidate(ref: str, *, years=6.0, skills=("Python", "AWS"), aqf=7) -> AnonymizedProfile:
    return AnonymizedProfile(
        candidate_ref=ref,
        total_years_experience=years,
        skills=[Skill(name=name) for name in skills],
        qualifications=[Qualification(title="Degree", aqf_level=aqf)],
    )


def test_every_criterion_is_scored_with_evidence(llm):
    score = score_candidate(candidate("Candidate 1"), ROLE, llm)
    assert len(score.criteria) == len(DEFAULT_CRITERIA)
    for criterion in score.criteria:
        assert criterion.evidence.strip(), f"{criterion.criterion} scored without evidence"
        assert 0.0 <= criterion.score <= 1.0


def test_overall_score_is_the_declared_weighted_mean(llm):
    score = score_candidate(candidate("Candidate 1"), ROLE, llm)
    total_weight = sum(c.weight for c in score.criteria)
    expected = sum(c.score * c.weight for c in score.criteria) / total_weight
    assert score.score == pytest.approx(expected, abs=1e-4)


def test_stronger_candidate_outranks_weaker(llm):
    strong = score_candidate(candidate("Strong", years=8.0, skills=("Python", "AWS", "Kubernetes", "Go")), ROLE, llm)
    weak = score_candidate(candidate("Weak", years=1.0, skills=("Excel",)), ROLE, llm)
    assert strong.score > weak.score


def test_missing_experience_scores_zero_without_penalising_other_criteria(llm):
    score = score_candidate(candidate("Candidate 1", years=None), ROLE, llm)
    by_key = {c.criterion: c for c in score.criteria}
    assert by_key["experience_depth"].score == 0.0
    assert by_key["required_skills"].score == 1.0


def test_a_criterion_the_model_skips_scores_zero_and_says_so(llm, monkeypatch):
    from rescan.llm.client import LLMResponse

    def partial(request):
        return LLMResponse(
            data={"criteria": [{"criterion": "required_skills", "score": 1.0, "evidence": "all"}],
                  "rationale": "partial"},
            model="stub", backend="stub", latency_s=0.0,
        )

    monkeypatch.setattr(llm, "json_call", partial)
    score = score_candidate(candidate("Candidate 1"), ROLE, llm)
    skipped = [c for c in score.criteria if c.criterion != "required_skills"]
    assert all(c.score == 0.0 for c in skipped)
    assert all("Not scored" in c.evidence for c in skipped)


def test_out_of_range_scores_are_clamped(llm, monkeypatch):
    from rescan.llm.client import LLMResponse

    def wild(request):
        return LLMResponse(
            data={
                "criteria": [
                    {"criterion": c.key, "score": 42.0, "evidence": "x"} for c in DEFAULT_CRITERIA
                ],
                "rationale": "",
            },
            model="stub", backend="stub", latency_s=0.0,
        )

    monkeypatch.setattr(llm, "json_call", wild)
    score = score_candidate(candidate("Candidate 1"), ROLE, llm)
    assert score.score <= 1.0
    assert all(c.score <= 1.0 for c in score.criteria)


def test_ranking_failure_is_raised_not_scored_as_zero(llm, monkeypatch):
    from rescan.llm.client import LLMError

    def boom(request):
        raise LLMError("down")

    monkeypatch.setattr(llm, "json_call", boom)
    with pytest.raises(RankingError):
        score_candidate(candidate("Candidate 1"), ROLE, llm)


def test_triage_returns_candidates_in_descending_order(llm):
    scores = triage_rank(
        [candidate("A", years=8.0), candidate("B", years=1.0, skills=("Excel",)), candidate("C", years=5.0)],
        ROLE,
        llm,
    )
    assert [s.score for s in scores] == sorted((s.score for s in scores), reverse=True)


# --------------------------------------------------------------------------
# Shortlist
# --------------------------------------------------------------------------


def make_scores(values: list[float]) -> list[CandidateScore]:
    return [CandidateScore(candidate_ref=f"Candidate {i}", score=v) for i, v in enumerate(values, 1)]


def test_borderline_is_limited_to_the_band_around_the_cutoff():
    scores = make_scores([0.90, 0.80, 0.72, 0.70, 0.68, 0.40])
    refs = borderline_refs(scores, size=3, margin=0.05)
    # Cutoff is the 3rd score, 0.72; only 0.70 and 0.68 sit within 0.05 of it.
    assert refs == {"Candidate 3", "Candidate 4", "Candidate 5"}
    assert "Candidate 1" not in refs and "Candidate 6" not in refs


def test_no_borderline_when_everyone_fits_on_the_shortlist():
    assert borderline_refs(make_scores([0.9, 0.8]), size=5) == set()


def test_shortlist_keeps_near_misses_visible():
    shortlist = build_shortlist(make_scores([0.9, 0.8, 0.7, 0.6]), ROLE, size=2)
    assert [e.candidate_ref for e in shortlist.entries] == ["Candidate 1", "Candidate 2"]
    assert [e.candidate_ref for e in shortlist.below_cutoff] == ["Candidate 3", "Candidate 4"]
    assert shortlist.below_cutoff[0].rank == 3


def test_shortlist_records_rule_exclusions_with_reasons(llm):
    from rescan.rules.classifier import classify_rules
    from rescan.rules.engine import screen

    rule_set = classify_rules(["At least 10 years experience"], llm)
    excluded = screen(candidate("Candidate 9", years=2.0), rule_set)
    shortlist = build_shortlist(
        make_scores([0.9]), ROLE, size=1, screening={"Candidate 9": excluded}
    )
    assert shortlist.excluded
    assert "2 years of professional experience" in shortlist.excluded[0]["reasons"][0]


def test_shortlist_separates_manual_review_from_exclusion(llm):
    from rescan.rules.classifier import classify_rules
    from rescan.rules.engine import screen

    rule_set = classify_rules(["At least 3 years experience"], llm)
    unknown = screen(candidate("Candidate 8", years=None), rule_set)
    shortlist = build_shortlist(make_scores([0.5]), ROLE, size=1, screening={"Candidate 8": unknown})
    assert shortlist.manual_review and not shortlist.excluded


# --------------------------------------------------------------------------
# PREFER clauses as criteria
# --------------------------------------------------------------------------


def test_prefer_clauses_become_the_criteria(llm):
    from rescan.pipeline.rank import ROLE_RELEVANCE, criteria_for
    from rescan.rules.classifier import compile_plan

    rule_set = compile_plan(
        "Must have 3+ years experience. Nice to have: Kubernetes. Terraform is preferred.", llm
    )
    criteria = criteria_for(rule_set)
    keys = [c.key for c in criteria]
    assert keys[-1] == ROLE_RELEVANCE.key, "the model keeps a holistic read"
    assert len(criteria) == 3
    assert all(c.clause is not None for c in criteria[:-1])
    assert criteria_for(None) == DEFAULT_CRITERIA
    assert criteria_for(compile_plan("Must have 3+ years experience.", llm)) == DEFAULT_CRITERIA


def test_prefer_clauses_are_scored_by_the_rules_not_the_model(llm):
    from rescan.dsl import parse_clause
    from rescan.pipeline.rank import ROLE_RELEVANCE, Criterion

    criteria = (
        Criterion("k8s", "Kubernetes", 2.0, clause=parse_clause('PREFER skills HAS ANY ("Kubernetes")')),
        Criterion("go", "Go", 1.0, clause=parse_clause('PREFER skills HAS ANY ("Go")')),
        ROLE_RELEVANCE,
    )
    score = score_candidate(candidate("Candidate 1", skills=("Python", "Kubernetes")), ROLE, llm, criteria=criteria)
    by_key = {c.criterion: c for c in score.criteria}
    assert by_key["k8s"].score == 1.0 and "Kubernetes" in by_key["k8s"].evidence
    assert by_key["go"].score == 0.0 and "Go" in by_key["go"].evidence
    assert by_key["k8s"].weight == 2.0
    assert "Rule-decided" in score.rationale


def test_unknown_prefer_clause_is_left_to_the_model(llm, monkeypatch):
    from rescan.dsl import parse_clause
    from rescan.pipeline.rank import Criterion

    sent = []
    original = llm.json_call

    def spy(request):
        sent.append([c["key"] for c in request.context["criteria"]])
        return original(request)

    monkeypatch.setattr(llm, "json_call", spy)
    criteria = (
        Criterion("langs", "French", 1.0, clause=parse_clause('PREFER languages HAS ANY ("French")')),
        Criterion("k8s", "Kubernetes", 1.0, clause=parse_clause('PREFER skills HAS ANY ("Kubernetes")')),
    )
    score = score_candidate(candidate("Candidate 1"), ROLE, llm, criteria=criteria)
    assert sent == [["langs"]], "only the undecided criterion reaches the model"
    assert {c.criterion for c in score.criteria} == {"langs", "k8s"}


def test_fully_decided_candidate_needs_no_model_call(llm, monkeypatch):
    from rescan.dsl import parse_clause
    from rescan.pipeline.rank import Criterion

    def boom(request):
        raise AssertionError("the model must not be called")

    monkeypatch.setattr(llm, "json_call", boom)
    criteria = (Criterion("k8s", "Kubernetes", 1.0, clause=parse_clause('PREFER skills HAS ANY ("Kubernetes")')),)
    score = score_candidate(candidate("Candidate 1"), ROLE, llm, criteria=criteria)
    assert score.model == "rules" and score.score == 0.0
