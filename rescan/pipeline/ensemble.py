"""Pass 5 — ensemble re-scoring of borderline candidates only.

Running three models over a whole batch triples cost for candidates whose
placement was never in doubt. This pass fires on two groups:

* candidates whose triage score sits within the borderline band around the
  shortlist cutoff, where the ordering is not decisive; and
* candidates the rule engine could not decide, because a value the resume never
  stated is exactly the case where a second opinion is worth paying for.

Each member votes on inclusion; the majority decides. Ordering uses the median
score rather than the mean, so one outlier cannot move a candidate across the
cutoff on its own. Every vote and the resulting disagreement is recorded, so a
tiebreak can be explained afterwards.
"""

from __future__ import annotations

import logging
import statistics
from concurrent.futures import ThreadPoolExecutor

from rescan.config import settings
from rescan.llm.client import LLMClient
from rescan.pipeline.rank import DEFAULT_CRITERIA, Criterion, RankingError, score_candidate
from rescan.schemas import AnonymizedProfile, CandidateScore, RoleSpec

log = logging.getLogger(__name__)

# Distinct seeds when only one model is served, so members are not identical
# requests. With several models configured, each model is a member instead.
DEFAULT_SEEDS = (11, 22, 33)


def ensemble_members(models: list[str] | None = None) -> list[tuple[str | None, int | None]]:
    """Return ``(model, seed)`` pairs for the ensemble."""
    configured = models if models is not None else settings.ensemble_models
    if configured:
        return [(model, None) for model in configured]
    return [(None, seed) for seed in DEFAULT_SEEDS]


def ensemble_score(
    profile: AnonymizedProfile,
    role: RoleSpec,
    client: LLMClient,
    *,
    cutoff: float,
    criteria: tuple[Criterion, ...] = DEFAULT_CRITERIA,
    models: list[str] | None = None,
) -> CandidateScore:
    """Re-score one candidate with every ensemble member and combine the votes."""
    members = ensemble_members(models)
    votes: list[dict] = []
    scores: list[float] = []
    results: list[CandidateScore] = []

    for model, seed in members:
        try:
            result = score_candidate(
                profile, role, client,
                criteria=criteria, model=model, seed=seed, pass_name="ensemble",
            )
        except RankingError as exc:
            # One member failing must not sink the candidate; record and move on.
            log.warning("ensemble member %s/%s failed for %s: %s", model, seed, profile.candidate_ref, exc)
            votes.append({"model": model or settings.llm_model, "seed": seed, "error": str(exc)})
            continue
        results.append(result)
        scores.append(result.score)
        votes.append({
            "model": result.model,
            "seed": seed,
            "score": result.score,
            "include": result.score >= cutoff,
            "rationale": result.rationale,
        })

    if not scores:
        raise RankingError(f"every ensemble member failed for {profile.candidate_ref}")

    # Median is robust to a single member disagreeing sharply.
    combined = round(statistics.median(scores), 4)
    include_votes = sum(1 for vote in votes if vote.get("include"))
    counted = sum(1 for vote in votes if "include" in vote)
    majority_include = include_votes * 2 > counted
    unanimous = include_votes == counted or include_votes == 0

    # Criteria come from the member nearest the combined score, so the reported
    # evidence matches the score that was actually used.
    representative = min(results, key=lambda result: abs(result.score - combined))

    spread = max(scores) - min(scores)
    rationale = representative.rationale
    if not unanimous:
        rationale = (
            f"Ensemble split {include_votes}/{counted} on inclusion "
            f"(scores {', '.join(f'{s:.3f}' for s in sorted(scores))}). " + rationale
        )

    return CandidateScore(
        candidate_ref=profile.candidate_ref,
        score=combined,
        criteria=representative.criteria,
        rationale=rationale.strip(),
        model=f"ensemble({counted})",
        pass_name="ensemble",
        ensemble_votes=votes + [{
            "summary": {
                "members": counted,
                "include_votes": include_votes,
                "majority_include": majority_include,
                "unanimous": unanimous,
                "score_spread": round(spread, 4),
                "combined_score": combined,
            }
        }],
    )


def ensemble_pass(
    scores: list[CandidateScore],
    profiles: dict[str, AnonymizedProfile],
    role: RoleSpec,
    client: LLMClient,
    refs: set[str],
    *,
    size: int | None = None,
    workers: int | None = None,
    models: list[str] | None = None,
) -> list[CandidateScore]:
    """Re-score `refs` with the ensemble and merge them back into `scores`."""
    if not refs:
        return scores

    size = size if size is not None else settings.shortlist_size
    ordered = sorted(scores, key=lambda score: score.score, reverse=True)
    cutoff = ordered[min(size, len(ordered)) - 1].score if ordered else 0.0

    targets = [ref for ref in refs if ref in profiles]
    if not targets:
        return scores

    def rescore(ref: str) -> tuple[str, CandidateScore | None]:
        try:
            return ref, ensemble_score(
                profiles[ref], role, client, cutoff=cutoff, models=models
            )
        except RankingError as exc:
            log.warning("ensemble pass failed for %s, keeping triage score: %s", ref, exc)
            return ref, None

    with ThreadPoolExecutor(max_workers=workers or settings.pipeline_workers) as pool:
        rescored = dict(pool.map(rescore, targets))

    merged = [rescored.get(score.candidate_ref) or score for score in scores]
    return sorted(merged, key=lambda score: score.score, reverse=True)
