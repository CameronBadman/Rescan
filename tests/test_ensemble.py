import itertools

import pytest

from rescan.config import settings
from rescan.llm.client import LLMError, LLMResponse
from rescan.pipeline.ensemble import DEFAULT_SEEDS, ensemble_members, ensemble_pass, ensemble_score
from rescan.pipeline.rank import DEFAULT_CRITERIA, RankingError
from rescan.schemas import AnonymizedProfile, CandidateScore, Qualification, RoleSpec, Skill

ROLE = RoleSpec(title="Senior Backend Engineer", required_skills=["Python"], min_years_experience=5)


def candidate(ref="Candidate 1", years=5.0):
    return AnonymizedProfile(
        candidate_ref=ref,
        total_years_experience=years,
        skills=[Skill(name="Python")],
        qualifications=[Qualification(title="Degree", aqf_level=7)],
    )


def scripted(scores):
    """An LLM client whose successive calls return the given overall scores."""
    values = itertools.cycle(scores)

    def json_call(request):
        value = next(values)
        return LLMResponse(
            data={
                "criteria": [
                    {"criterion": c.key, "score": value, "evidence": f"scripted {value}"}
                    for c in DEFAULT_CRITERIA
                ],
                "rationale": f"scripted {value}",
            },
            model="scripted",
            backend="stub",
            latency_s=0.0,
        )

    class Scripted:
        pass

    client = Scripted()
    client.json_call = json_call
    return client


def test_default_members_use_distinct_seeds():
    assert ensemble_members([]) == [(None, seed) for seed in DEFAULT_SEEDS]


def test_configured_models_become_the_members():
    assert ensemble_members(["a", "b", "c"]) == [("a", None), ("b", None), ("c", None)]


def test_all_members_are_recorded_as_votes(llm):
    score = ensemble_score(candidate(), ROLE, llm, cutoff=0.5)
    votes = [v for v in score.ensemble_votes if "summary" not in v]
    assert len(votes) == len(DEFAULT_SEEDS)
    assert all("score" in v and "include" in v for v in votes)


def test_unanimous_agreement_is_recorded_as_such(llm):
    score = ensemble_score(candidate(), ROLE, llm, cutoff=0.0)
    summary = next(v["summary"] for v in score.ensemble_votes if "summary" in v)
    assert summary["unanimous"] is True
    assert summary["include_votes"] == summary["members"]


def test_median_is_used_so_one_outlier_cannot_decide():
    # Two members near 0.8, one wild outlier at 0.1.
    client = scripted([0.8, 0.82, 0.1])
    score = ensemble_score(candidate(), ROLE, client, cutoff=0.5)
    assert score.score == pytest.approx(0.8, abs=0.01)
    summary = next(v["summary"] for v in score.ensemble_votes if "summary" in v)
    assert summary["unanimous"] is False
    assert summary["include_votes"] == 2
    assert summary["majority_include"] is True


def test_a_split_is_explained_in_the_rationale():
    client = scripted([0.8, 0.82, 0.1])
    score = ensemble_score(candidate(), ROLE, client, cutoff=0.5)
    assert "split 2/3" in score.rationale


def test_minority_cannot_include_a_candidate():
    client = scripted([0.2, 0.25, 0.9])
    score = ensemble_score(candidate(), ROLE, client, cutoff=0.5)
    summary = next(v["summary"] for v in score.ensemble_votes if "summary" in v)
    assert summary["include_votes"] == 1
    assert summary["majority_include"] is False


def test_one_failing_member_does_not_sink_the_candidate():
    calls = {"n": 0}

    class Flaky:
        def json_call(self, request):
            calls["n"] += 1
            if calls["n"] == 2:
                raise LLMError("member unavailable")
            return LLMResponse(
                data={
                    "criteria": [
                        {"criterion": c.key, "score": 0.7, "evidence": "ok"} for c in DEFAULT_CRITERIA
                    ],
                    "rationale": "ok",
                },
                model="m", backend="stub", latency_s=0.0,
            )

    score = ensemble_score(candidate(), ROLE, Flaky(), cutoff=0.5)
    assert score.score == pytest.approx(0.7, abs=0.01)
    assert any("error" in v for v in score.ensemble_votes)


def test_every_member_failing_raises():
    class Dead:
        def json_call(self, request):
            raise LLMError("cluster down")

    with pytest.raises(RankingError):
        ensemble_score(candidate(), ROLE, Dead(), cutoff=0.5)


# --------------------------------------------------------------------------
# Pass-level behaviour
# --------------------------------------------------------------------------


def make_scores(values):
    return [CandidateScore(candidate_ref=f"Candidate {i}", score=v) for i, v in enumerate(values, 1)]


def test_only_targeted_candidates_are_rescored(llm):
    scores = make_scores([0.9, 0.8, 0.7, 0.6])
    profiles = {f"Candidate {i}": candidate(f"Candidate {i}") for i in range(1, 5)}
    merged = ensemble_pass(scores, profiles, ROLE, llm, {"Candidate 3"}, size=2)
    by_ref = {s.candidate_ref: s for s in merged}
    assert by_ref["Candidate 3"].pass_name == "ensemble"
    assert by_ref["Candidate 1"].pass_name == "triage"
    assert by_ref["Candidate 1"].score == 0.9, "untouched candidates keep their triage score"


def test_empty_target_set_is_a_no_op(llm):
    scores = make_scores([0.9, 0.8])
    assert ensemble_pass(scores, {}, ROLE, llm, set()) == scores


def test_results_come_back_ordered(llm):
    scores = make_scores([0.9, 0.2, 0.7])
    profiles = {f"Candidate {i}": candidate(f"Candidate {i}") for i in range(1, 4)}
    merged = ensemble_pass(scores, profiles, ROLE, llm, {"Candidate 2"}, size=2)
    assert [s.score for s in merged] == sorted((s.score for s in merged), reverse=True)


def test_ensemble_only_runs_on_borderline_candidates_in_a_run(tmp_path, monkeypatch, samples):
    from rescan.extract import Extractor
    from rescan.llm.client import build_client
    from rescan.pipeline.runner import PipelineRunner
    from rescan.store import Store

    monkeypatch.setattr(settings, "upload_dir", tmp_path / "uploads")
    monkeypatch.setattr(settings, "shortlist_size", 4)
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    store = Store(tmp_path / "e.db")
    runner = PipelineRunner(store, build_client("stub"), Extractor())

    files = [(p.name, p.read_bytes()) for p in sorted(samples.glob("*.txt"))]
    batch_id = runner.create_batch(files)
    runner.process_batch(batch_id)
    run_id = runner.create_run(batch_id, RoleSpec(title="Senior Backend Engineer", min_years_experience=5))
    runner.execute_run(run_id, ["At least 2 years of professional experience"])

    trail = store.audit_trail(run_id=run_id)
    started = [e for e in trail if e["event"] == "ensemble_started"]
    assert started, "ensemble should have run"
    rescored = started[0]["detail"]["candidates"]
    assert 0 < len(rescored) < started[0]["detail"]["of_total"], (
        "the ensemble must fire on a subset, not the whole batch"
    )
    store.close()


def test_ensemble_can_be_disabled(tmp_path, monkeypatch, samples):
    from rescan.extract import Extractor
    from rescan.llm.client import build_client
    from rescan.pipeline.runner import PipelineRunner
    from rescan.store import Store

    monkeypatch.setattr(settings, "upload_dir", tmp_path / "uploads")
    monkeypatch.setattr(settings, "shortlist_size", 4)
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    store = Store(tmp_path / "d.db")
    runner = PipelineRunner(store, build_client("stub"), Extractor(), use_ensemble=False)
    files = [(p.name, p.read_bytes()) for p in sorted(samples.glob("*.txt"))]
    batch_id = runner.create_batch(files)
    runner.process_batch(batch_id)
    run_id = runner.create_run(batch_id, RoleSpec(title="Engineer"))
    runner.execute_run(run_id, [])
    assert not [e for e in store.audit_trail(batch_id) if e["event"].startswith("ensemble")]
    store.close()
