"""The judge behind `ASK "..."`: a check the model makes itself.

Some requirements have no structured field — "has led a team through a
production incident", "has shipped a customer-facing ML feature". An ASK leaf
puts the question to the model with the anonymized profile and nothing else.

Three guardrails keep this from becoming an unaccountable filter:

* A "no" only counts when the model quotes evidence that is actually in the
  profile. An unverifiable answer is downgraded to unknown, so nobody is
  excluded on an assertion.
* In a REQUIRE clause the question is put to every ensemble member and the
  majority decides; a split vote is unknown and goes to manual review. A
  PREFER clause, which only orders candidates, takes a single answer.
* The model may decline: when answering would require inferring a protected
  attribute it answers unknown and says why. The question text itself was
  already scanned against the statute table when the rule was compiled.

Every call is recorded through the audit callback with question, answer,
evidence and votes, so a model-judged outcome can be read back like any other.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from rescan.dsl.eval import MANUAL_REVIEW, Verdict
from rescan.llm.client import LLMClient, LLMError, LLMRequest
from rescan.llm.prompts import JUDGE_SCHEMA, JUDGE_SYSTEM, judge_user_prompt
from rescan.schemas import AnonymizedProfile

log = logging.getLogger(__name__)

ANSWERS = ("yes", "no", "unknown")


@dataclass
class Vote:
    model: str
    seed: int | None
    answer: str
    evidence: str | None
    reasoning: str
    declined_reason: str | None = None
    evidence_verified: bool = False
    error: str | None = None
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None and v is not False}


@dataclass
class JudgeResult:
    question: str
    value: bool | None
    reason: str
    votes: list[Vote] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "answer": {True: "yes", False: "no", None: "unknown"}[self.value],
            "reason": self.reason,
            "votes": [vote.to_dict() for vote in self.votes],
        }


def profile_text(profile: AnonymizedProfile) -> str:
    """The exact text the model sees, so quoted evidence can be checked against it."""
    return profile.model_dump_json(indent=2, exclude={"redactions", "candidate_ref"})


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\\", "").replace('"', "")).strip().lower()


def evidence_present(evidence: str | None, text: str) -> bool:
    """Whether the quoted evidence appears verbatim (modulo whitespace and quoting) in the profile."""
    if not evidence or len(evidence.strip()) < 3:
        return False
    return _normalise(evidence) in _normalise(text)


class Judge:
    def __init__(
        self,
        client: LLMClient,
        *,
        ensemble: bool = False,
        models: list[str] | None = None,
        on_check: Callable[[JudgeResult, AnonymizedProfile], None] | None = None,
    ) -> None:
        self.client = client
        self.ensemble = ensemble
        self.models = models
        self.on_check = on_check
        self._memo: dict[str, JudgeResult] = {}
        self._lock = threading.Lock()

    # -- members ----------------------------------------------------------

    def _members(self) -> list[tuple[str | None, int | None]]:
        if not self.ensemble:
            return [(None, None)]
        from rescan.pipeline.ensemble import ensemble_members  # local: keeps dsl independent of pipeline at import

        return ensemble_members(self.models)

    # -- one vote ---------------------------------------------------------

    def _vote(self, question: str, text: str, profile: AnonymizedProfile, model: str | None, seed: int | None) -> Vote:
        request = LLMRequest(
            task="judge",
            system=JUDGE_SYSTEM,
            user=judge_user_prompt(question, text),
            schema=JUDGE_SCHEMA,
            model=model,
            seed=seed,
            context={"question": question, "profile": profile.model_dump(mode="json"), "profile_text": text},
        )
        try:
            response = self.client.json_call(request)
        except LLMError as exc:
            return Vote(model=model or "unknown", seed=seed, answer="unknown", evidence=None, reasoning="", error=str(exc))

        data = response.data
        answer = str(data.get("answer") or "unknown").strip().lower()
        if answer not in ANSWERS:
            answer = "unknown"
        evidence = (data.get("evidence") or None) and str(data["evidence"]).strip()
        vote = Vote(
            model=response.model,
            seed=seed,
            answer=answer,
            evidence=evidence,
            reasoning=str(data.get("reasoning") or "").strip(),
            declined_reason=(data.get("declined_reason") or None) and str(data["declined_reason"]).strip(),
        )
        if answer != "unknown":
            vote.evidence_verified = evidence_present(evidence, text)
            if not vote.evidence_verified:
                # An answer the profile does not back is not an answer.
                vote.note = f"model answered '{answer}' without verifiable evidence; treated as unknown"
                vote.answer = "unknown"
        return vote

    # -- combine ----------------------------------------------------------

    @staticmethod
    def _combine(question: str, votes: list[Vote]) -> JudgeResult:
        counted = [vote for vote in votes if vote.error is None]
        yes = sum(1 for vote in counted if vote.answer == "yes")
        no = sum(1 for vote in counted if vote.answer == "no")
        total = len(counted)
        quoted = f'Model check "{question}"'

        if not counted:
            errors = "; ".join(vote.error or "" for vote in votes)
            return JudgeResult(question, None, f"{quoted} could not be made ({errors}). {MANUAL_REVIEW}", votes)

        def evidence_for(answer: str) -> str:
            return next((vote.evidence for vote in counted if vote.answer == answer and vote.evidence), "") or ""

        agreement = f"({max(yes, no)} of {total} votes agree)" if total > 1 else ""
        if yes * 2 > total:
            return JudgeResult(question, True, f'{quoted}: yes — evidence: "{evidence_for("yes")}" {agreement}'.strip() + ".", votes)
        if no * 2 > total:
            return JudgeResult(question, False, f'{quoted}: no — evidence: "{evidence_for("no")}" {agreement}'.strip() + ".", votes)

        declined = next((vote.declined_reason for vote in counted if vote.declined_reason), None)
        if declined:
            why = f"model declined: {declined}"
        elif yes and no:
            why = f"votes split {yes} yes / {no} no of {total}"
        elif any(vote.note for vote in counted):
            why = next(vote.note for vote in counted if vote.note)
        else:
            why = "the profile does not say"
        return JudgeResult(question, None, f"{quoted} could not be determined ({why}). {MANUAL_REVIEW}", votes)

    # -- public -----------------------------------------------------------

    def judge(self, question: str, profile: AnonymizedProfile) -> JudgeResult:
        text = profile_text(profile)
        digest = hashlib.sha256(text.encode()).hexdigest()[:16]
        key = f"{self.ensemble}:{digest}:{question}"
        with self._lock:
            cached = self._memo.get(key)
        if cached is not None:
            return cached

        votes = [self._vote(question, text, profile, model, seed) for model, seed in self._members()]
        result = self._combine(question, votes)
        with self._lock:
            self._memo[key] = result
        if self.on_check is not None:
            self.on_check(result, profile)
        return result

    def ask(self, question: str, profile: AnonymizedProfile) -> Verdict:
        result = self.judge(question, profile)
        return Verdict(result.value, result.reason, result.to_dict())
