"""Pass 4 — triage ranking and shortlist construction.

Ranking only ever sees the anonymized profile. It orders candidates who already
cleared the rule set; it never excludes anyone itself. Exclusions come from the
rule engine, where each one is tied to a structured field and stated in plain
language, because "the model scored you 0.41" is not a reason a person can be
given or an employer can defend.

Scores are a weighted sum over named criteria, and every criterion carries the
evidence it was read from, so a ranking can be taken apart after the fact.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from rescan.config import settings
from rescan.llm.client import LLMClient, LLMError, LLMRequest
from rescan.llm.prompts import RANK_SCHEMA, RANK_SYSTEM, rank_user_prompt
from rescan.rules.engine import ScreeningResult
from rescan.schemas import (
    AnonymizedProfile,
    CandidateScore,
    CriterionScore,
    RoleSpec,
    Shortlist,
    ShortlistEntry,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Criterion:
    key: str
    description: str
    weight: float


# Weights are explicit and visible rather than learned, so a recruiter can see
# what the ranking rewards. Required skills and depth of experience dominate.
DEFAULT_CRITERIA: tuple[Criterion, ...] = (
    Criterion("required_skills", "Holds the skills the role requires.", 3.0),
    Criterion("experience_depth", "Depth of relevant professional experience.", 2.0),
    Criterion("role_relevance", "Prior roles resemble the role being filled.", 1.5),
    Criterion("desirable_skills", "Holds skills that are desirable but not required.", 1.0),
    Criterion("qualification", "Qualification level against the role's requirement.", 1.0),
)


class RankingError(RuntimeError):
    pass


def _criteria_payload(criteria: tuple[Criterion, ...]) -> list[dict[str, object]]:
    return [{"key": c.key, "description": c.description, "weight": c.weight} for c in criteria]


def score_candidate(
    profile: AnonymizedProfile,
    role: RoleSpec,
    client: LLMClient,
    *,
    criteria: tuple[Criterion, ...] = DEFAULT_CRITERIA,
    model: str | None = None,
    seed: int | None = None,
    pass_name: str = "triage",
) -> CandidateScore:
    """Score one anonymized candidate against the role."""
    profile_json = profile.model_dump_json(indent=2, exclude={"redactions"})
    role_json = role.model_dump_json(indent=2)
    criteria_payload = _criteria_payload(criteria)

    request = LLMRequest(
        task="rank",
        system=RANK_SYSTEM,
        user=rank_user_prompt(profile_json, role_json, json.dumps(criteria_payload, indent=2)),
        schema=RANK_SCHEMA,
        model=model,
        seed=seed,
        context={
            "profile": profile.model_dump(mode="json"),
            "role": role.model_dump(mode="json"),
            "criteria": criteria_payload,
        },
    )

    try:
        response = client.json_call(request)
    except LLMError as exc:
        raise RankingError(f"ranking failed for {profile.candidate_ref}: {exc}") from exc

    by_key = {c.key: c for c in criteria}
    returned = {
        str(entry.get("criterion")): entry
        for entry in response.data.get("criteria", [])
        if isinstance(entry, dict)
    }

    scores: list[CriterionScore] = []
    for criterion in criteria:
        entry = returned.get(criterion.key)
        if entry is None:
            # A criterion the model skipped scores zero with that stated, rather
            # than being dropped and silently reweighting the others.
            scores.append(
                CriterionScore(
                    criterion=criterion.key,
                    score=0.0,
                    weight=criterion.weight,
                    evidence="Not scored by the model; treated as no evidence.",
                )
            )
            continue
        raw = entry.get("score", 0.0)
        try:
            value = min(1.0, max(0.0, float(raw)))
        except (TypeError, ValueError):
            value = 0.0
        scores.append(
            CriterionScore(
                criterion=criterion.key,
                score=value,
                weight=by_key[criterion.key].weight,
                evidence=str(entry.get("evidence") or "No evidence supplied."),
            )
        )

    total_weight = sum(c.weight for c in criteria) or 1.0
    overall = sum(s.score * s.weight for s in scores) / total_weight

    return CandidateScore(
        candidate_ref=profile.candidate_ref,
        score=round(min(1.0, max(0.0, overall)), 4),
        criteria=scores,
        rationale=str(response.data.get("rationale") or "").strip(),
        model=response.model,
        pass_name=pass_name,  # type: ignore[arg-type]
    )


def triage_rank(
    profiles: list[AnonymizedProfile],
    role: RoleSpec,
    client: LLMClient,
    *,
    criteria: tuple[Criterion, ...] = DEFAULT_CRITERIA,
    model: str | None = None,
) -> list[CandidateScore]:
    """Score every eligible candidate with a single model."""
    scores = [
        score_candidate(profile, role, client, criteria=criteria, model=model)
        for profile in profiles
    ]
    return sorted(scores, key=lambda s: s.score, reverse=True)


def borderline_refs(
    scores: list[CandidateScore],
    *,
    size: int | None = None,
    margin: float | None = None,
) -> set[str]:
    """Candidates close enough to the cutoff that the ordering is not decisive.

    Only these go to the ensemble pass. Running three models over the whole
    batch would cost triple for candidates whose placement is not in doubt.
    """
    size = size if size is not None else settings.shortlist_size
    margin = margin if margin is not None else settings.borderline_margin
    if not scores or size <= 0 or size >= len(scores):
        return set()

    ordered = sorted(scores, key=lambda s: s.score, reverse=True)
    cutoff = ordered[size - 1].score
    return {s.candidate_ref for s in ordered if abs(s.score - cutoff) <= margin}


def build_shortlist(
    scores: list[CandidateScore],
    role: RoleSpec,
    *,
    size: int | None = None,
    margin: float | None = None,
    screening: dict[str, ScreeningResult] | None = None,
) -> Shortlist:
    """Order scored candidates and cut the shortlist, keeping the remainder visible."""
    size = size if size is not None else settings.shortlist_size
    ordered = sorted(scores, key=lambda s: s.score, reverse=True)
    borderline = borderline_refs(ordered, size=size, margin=margin)

    def entry(score: CandidateScore, rank: int) -> ShortlistEntry:
        return ShortlistEntry(
            rank=rank,
            candidate_ref=score.candidate_ref,
            score=score.score,
            rationale=score.rationale,
            borderline=score.candidate_ref in borderline,
            criteria=score.criteria,
        )

    entries = [entry(score, index) for index, score in enumerate(ordered[:size], start=1)]
    # Everyone below the cut stays visible with their reasons; a shortlist that
    # hides the near-misses cannot be reviewed.
    below = [entry(score, index) for index, score in enumerate(ordered[size:], start=size + 1)]

    shortlist = Shortlist(role_title=role.title, entries=entries, below_cutoff=below)

    for result in (screening or {}).values():
        if not result.eligible:
            shortlist.excluded.append(
                {
                    "candidate_ref": result.candidate_ref,
                    "reasons": result.exclusion_reasons(),
                    "failed_rules": [outcome.source_text for outcome in result.failed],
                }
            )
        elif result.needs_manual_review:
            shortlist.manual_review.append(
                {
                    "candidate_ref": result.candidate_ref,
                    "reasons": [outcome.reason for outcome in result.indeterminate],
                }
            )

    return shortlist
