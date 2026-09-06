import itertools

import pytest

from rescan.dsl import parse_expr
from rescan.dsl.eval import evaluate
from rescan.dsl.judge import Judge, evidence_present, profile_text
from rescan.llm.client import LLMError, LLMResponse
from rescan.schemas import AnonymizedProfile, Experience, Skill

QUESTION = "Has the candidate led an on-call rotation?"


def profile() -> AnonymizedProfile:
    return AnonymizedProfile(
        candidate_ref="Candidate 1",
        total_years_experience=6.0,
        skills=[Skill(name="Python")],
        experience=[
            Experience(title="Senior Engineer", months=30, summary="Led the on-call rotation for the payments platform."),
        ],
    )


def scripted(answers):
    """A client whose successive judge calls return the given (answer, evidence, declined) tuples."""
    cycle = itertools.cycle(answers)
    calls = []

    class Client:
        def json_call(self, request):
            calls.append(request)
            answer, evidence, declined = next(cycle)
            if answer == "error":
                raise LLMError("member down")
            return LLMResponse(
                data={"reasoning": "scripted", "answer": answer, "evidence": evidence, "declined_reason": declined},
                model="scripted", backend="stub", latency_s=0.0,
            )

    client = Client()
    client.calls = calls
    return client


REAL = "Led the on-call rotation for the payments platform."


# --------------------------------------------------------------------------
# Single-vote judge
# --------------------------------------------------------------------------


def test_stub_answers_yes_with_quoted_evidence(llm):
    verdict = Judge(llm).ask(QUESTION, profile())
    assert verdict.value is True
    assert "on-call rotation" in verdict.reason
    assert verdict.observed["votes"][0]["evidence_verified"] is True


def test_stub_says_unknown_rather_than_no_when_the_profile_is_silent(llm):
    verdict = Judge(llm).ask("Has the candidate published a peer-reviewed paper?", profile())
    assert verdict.value is None
    assert "manual review" in verdict.reason.lower()


def test_a_no_without_verifiable_evidence_becomes_unknown():
    client = scripted([("no", "Never did on-call.", None)])
    verdict = Judge(client).ask(QUESTION, profile())
    assert verdict.value is None, "an exclusion cannot rest on text the profile does not contain"
    assert "without verifiable evidence" in verdict.reason


def test_a_no_with_real_evidence_counts():
    client = scripted([("no", "Senior Engineer", None)])
    verdict = Judge(client).ask("Has the candidate managed a budget?", profile())
    assert verdict.value is False
    assert '"Senior Engineer"' in verdict.reason


def test_declining_is_unknown_with_the_reason_shown():
    client = scripted([("unknown", None, "would require inferring age")])
    verdict = Judge(client).ask("Is the candidate early in their career?", profile())
    assert verdict.value is None
    assert "declined" in verdict.reason and "age" in verdict.reason


def test_inference_failure_is_unknown_not_false():
    client = scripted([("error", None, None)])
    verdict = Judge(client).ask(QUESTION, profile())
    assert verdict.value is None
    assert "member down" in verdict.reason


def test_evidence_check_tolerates_whitespace_and_quoting():
    text = profile_text(profile())
    assert evidence_present("led the on-call   rotation", text)
    assert evidence_present('"Senior Engineer"', text)
    assert not evidence_present("Ran the SRE team", text)
    assert not evidence_present("", text) and not evidence_present("ab", text)


# --------------------------------------------------------------------------
# Ensemble judge (REQUIRE clauses)
# --------------------------------------------------------------------------


def test_unanimous_ensemble_answer_stands():
    client = scripted([("yes", REAL, None)] * 3)
    verdict = Judge(client, ensemble=True).ask(QUESTION, profile())
    assert verdict.value is True
    assert "3 of 3 votes agree" in verdict.reason
    assert len(client.calls) == 3


def test_majority_decides_and_a_split_goes_to_review():
    majority = scripted([("no", REAL, None), ("no", REAL, None), ("yes", REAL, None)])
    assert Judge(majority, ensemble=True).ask(QUESTION, profile()).value is False

    split = scripted([("yes", REAL, None), ("no", REAL, None), ("unknown", None, None)])
    verdict = Judge(split, ensemble=True).ask(QUESTION, profile())
    assert verdict.value is None
    assert "split" in verdict.reason


def test_one_failing_member_does_not_block_a_majority():
    client = scripted([("yes", REAL, None), ("error", None, None), ("yes", REAL, None)])
    verdict = Judge(client, ensemble=True).ask(QUESTION, profile())
    assert verdict.value is True
    assert "2 of 2 votes agree" in verdict.reason


def test_unverified_votes_do_not_count_toward_a_majority():
    client = scripted([("no", "made up", None), ("no", "also made up", None), ("yes", REAL, None)])
    verdict = Judge(client, ensemble=True).ask(QUESTION, profile())
    # Two unverifiable "no"s collapse to unknown; one verified yes of three is not a majority.
    assert verdict.value is None


def test_results_are_memoised_per_question_and_profile():
    client = scripted([("yes", REAL, None)])
    judge = Judge(client)
    judge.ask(QUESTION, profile())
    judge.ask(QUESTION, profile())
    assert len(client.calls) == 1
    judge.ask("A different question?", profile())
    assert len(client.calls) == 2


def test_on_check_receives_every_judgement():
    seen = []
    client = scripted([("yes", REAL, None)])
    Judge(client, on_check=lambda result, prof: seen.append((result.question, prof.candidate_ref))).ask(QUESTION, profile())
    assert seen == [(QUESTION, "Candidate 1")]


# --------------------------------------------------------------------------
# Through the language and the pipeline
# --------------------------------------------------------------------------


def test_ask_composes_with_structured_clauses(llm):
    expr = parse_expr('years_experience >= 5 AND ASK "Has the candidate led an on-call rotation?"')
    verdict = evaluate(expr, profile(), Judge(llm))
    assert verdict.value is True
    assert "on-call" in verdict.reason and "6 years" in verdict.reason


def test_model_checks_are_audited_in_a_run(tmp_path, monkeypatch, samples):
    from rescan.config import settings
    from rescan.extract import Extractor
    from rescan.llm.client import build_client
    from rescan.pipeline.runner import PipelineRunner
    from rescan.schemas import RoleSpec
    from rescan.store import Store

    monkeypatch.setattr(settings, "upload_dir", tmp_path / "uploads")
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    store = Store(tmp_path / "j.db")
    runner = PipelineRunner(store, build_client("stub"), Extractor())
    files = [(p.name, p.read_bytes()) for p in sorted(samples.glob("*.txt"))[:4]]
    batch_id = runner.create_batch(files)
    runner.process_batch(batch_id)
    run_id = runner.create_run(batch_id, RoleSpec(title="Engineer"))
    runner.execute_run(run_id, [], plan="Should have led a team.")

    checks = [e for e in store.audit_trail(run_id=run_id) if e["event"] == "model_check"]
    assert checks, "every ASK evaluation is written to the audit trail"
    detail = checks[0]["detail"]
    assert detail["question"] == "Has the candidate led a team?"
    assert detail["answer"] in {"yes", "unknown"}
    assert len(detail["votes"]) == 3, "REQUIRE clauses put the question to the ensemble"
    assert checks[0]["candidate_id"] and checks[0]["run_id"] == run_id
    store.close()
