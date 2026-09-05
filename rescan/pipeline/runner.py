"""Bulk job orchestration.

A job is: classify the recruiter's rules once, run every uploaded document
through extraction, structuring, anonymization and screening concurrently, then
rank the survivors and cut a shortlist.

Failure handling is deliberate. A document that fails extraction is retried
once and then dead-lettered to `needs_manual_review`, never dropped, and never
counted as a rejected candidate. A candidate whose anonymization leaks identity
fails closed rather than reaching the ranking pass.
"""

from __future__ import annotations

import json
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Sequence

from rescan.config import settings
from rescan.dsl.judge import Judge
from rescan.extract import Extractor, content_hash
from rescan.llm.client import LLMClient
from rescan.pipeline.anonymize import AnonymizationError, anonymize_resume
from rescan.pipeline.ensemble import ensemble_pass
from rescan.pipeline.rank import borderline_refs, build_shortlist, criteria_for, triage_rank
from rescan.pipeline.structure import StructuringError, structure_resume
from rescan.rules.classifier import compile_plan
from rescan.rules.engine import ScreeningResult, screen
from rescan.schemas import (
    AnonymizedProfile,
    CandidateStatus,
    JobStatus,
    RoleSpec,
)
from rescan.store import Store

log = logging.getLogger(__name__)


class PipelineRunner:
    def __init__(
        self,
        store: Store,
        client: LLMClient,
        extractor: Extractor | None = None,
        workers: int | None = None,
        use_ensemble: bool = True,
    ) -> None:
        self.store = store
        self.client = client
        self.extractor = extractor if extractor is not None else Extractor()
        self.workers = workers or settings.pipeline_workers
        self.use_ensemble = use_ensemble

    # ------------------------------------------------------------------
    # Intake
    # ------------------------------------------------------------------

    def create_job(
        self,
        role: RoleSpec,
        files: Sequence[tuple[str, bytes]],
        *,
        job_id: str | None = None,
    ) -> str:
        """Register a job and its documents. Returns immediately with the job id."""
        job_id = job_id or f"job_{uuid.uuid4().hex[:12]}"
        self.store.create_job(job_id, role.model_dump(mode="json"))

        job_dir = settings.upload_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=True)

        accepted = duplicates = 0
        for filename, data in files:
            digest = content_hash(data)
            candidate_id = f"cand_{uuid.uuid4().hex[:12]}"
            existing = self.store.add_candidate(candidate_id, job_id, filename, digest)
            if existing is not None:
                # Same bytes already in this job: record it, do not reprocess.
                self.store.add_duplicate(candidate_id, job_id, filename, digest, duplicate_of=existing)
                duplicates += 1
                self.store.audit(
                    job_id, "ingestion", "duplicate_skipped",
                    candidate_id=candidate_id,
                    detail={"filename": filename, "duplicate_of": existing},
                )
                continue
            (job_dir / f"{candidate_id}{Path(filename).suffix}").write_bytes(data)
            accepted += 1

        self.store.audit(
            job_id, "ingestion", "job_created",
            detail={"accepted": accepted, "duplicates": duplicates, "role": role.title},
        )
        return job_id

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def run_job(
        self, job_id: str, rule_texts: Sequence[str], plan: str | None = None
    ) -> dict[str, Any]:
        """Run a job to completion. Blocking; callers run it in a background task."""
        job = self.store.get_job(job_id)
        if job is None:
            raise KeyError(f"unknown job {job_id!r}")
        role = RoleSpec.model_validate(job["role"])

        self.store.update_job(job_id, status=JobStatus.RUNNING.value)

        try:
            rule_set = self._classify_rules(job_id, rule_texts, role, plan)
            profiles, screening = self._process_candidates(job_id, rule_set)
            shortlist = self._rank(job_id, role, profiles, screening, rule_set)
        except Exception as exc:  # a job failure must be visible, not silent
            log.exception("job %s failed", job_id)
            self.store.update_job(job_id, status=JobStatus.FAILED.value, error=str(exc))
            self.store.audit(job_id, "job", "job_failed", detail={"error": str(exc)})
            raise

        self.store.update_job(job_id, status=JobStatus.COMPLETE.value)
        self.store.audit(
            job_id, "job", "job_complete",
            detail={"shortlisted": len(shortlist.entries), "excluded": len(shortlist.excluded)},
        )
        return shortlist.model_dump(mode="json")

    def _classify_rules(
        self, job_id: str, rule_texts: Sequence[str], role: RoleSpec, plan: str | None = None
    ):
        rule_set = compile_plan(plan, self.client, rule_texts=list(rule_texts), role_context=role.title)
        self.store.update_job(job_id, rules_json=rule_set.model_dump_json())

        entries = [(
            job_id, None, "rules", "plan_compiled",
            {
                "plan": rule_set.source_plan,
                "reasoning": rule_set.reasoning,
                "rules": len(rule_set.rules),
                "applied": len(rule_set.applied),
                "flagged": len(rule_set.flagged),
            },
        )]
        for rule in rule_set.rules:
            entries.append((
                job_id, None, "rules",
                f"rule_{rule.verdict.value}",
                {
                    "rule_id": rule.id,
                    "text": rule.source_text,
                    "kind": rule.kind,
                    "risk": rule.risk.value,
                    "dsl": rule.dsl,
                    "clause": rule.clause.model_dump(mode="json") if rule.clause else None,
                    "justification": rule.justification,
                    "notes": rule.notes,
                },
            ))
            for finding in rule.findings:
                entries.append((
                    job_id, None, "rules", "rule_risk_flagged",
                    {
                        "rule_id": rule.id,
                        "text": rule.source_text,
                        "pattern": finding.pattern_id,
                        "risk": finding.risk.value,
                        "protected_attributes": finding.protected_attributes,
                        "statutes": finding.statutes,
                        "explanation": finding.explanation,
                        "suggested_rewrite": finding.suggested_rewrite,
                    },
                ))
        self.store.audit_many(entries)
        return rule_set

    def _process_candidates(
        self, job_id: str, rule_set
    ) -> tuple[list[AnonymizedProfile], dict[str, ScreeningResult]]:
        pending = [
            candidate
            for candidate in self.store.list_candidates(job_id)
            if candidate["status"] == CandidateStatus.PENDING.value
        ]
        # Refs are assigned up front and by position, so they are stable and
        # carry no information about the candidate.
        for index, candidate in enumerate(pending, start=1):
            self.store.update_candidate(candidate["id"], candidate_ref=f"Candidate {index}")
            candidate["candidate_ref"] = f"Candidate {index}"

        job_dir = settings.upload_dir / job_id
        results: list[tuple[AnonymizedProfile | None, ScreeningResult | None]] = []

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            results = list(
                pool.map(
                    lambda candidate: self._process_one(job_id, candidate, job_dir, rule_set),
                    pending,
                )
            )

        profiles = [profile for profile, _ in results if profile is not None]
        screening = {
            result.candidate_ref: result for _, result in results if result is not None
        }
        return profiles, screening

    def _process_one(
        self, job_id: str, candidate: dict[str, Any], job_dir: Path, rule_set
    ) -> tuple[AnonymizedProfile | None, ScreeningResult | None]:
        candidate_id = candidate["id"]
        ref = candidate["candidate_ref"]
        stored = next(job_dir.glob(f"{candidate_id}*"), None)
        if stored is None:
            self._fail(job_id, candidate_id, "ingestion", "uploaded file is missing from disk")
            return None, None

        # --- extract (one retry, then dead-letter) ---
        self.store.update_candidate(candidate_id, status=CandidateStatus.EXTRACTING.value)
        extraction = None
        for attempt in (1, 2):
            extraction = self.extractor.extract_path(stored)
            if extraction.char_count > 0:
                break
            self.store.audit(
                job_id, "extraction", "extraction_retry",
                candidate_id=candidate_id,
                detail={"attempt": attempt, "warnings": extraction.warnings},
            )
        if extraction is None or extraction.char_count == 0:
            self._dead_letter(
                job_id, candidate_id, "extraction",
                "No text could be recovered from this document after two attempts.",
                detail={"warnings": extraction.warnings if extraction else []},
            )
            return None, None

        self.store.update_candidate(
            candidate_id, extraction_json=extraction.model_dump_json()
        )
        self.store.audit(
            job_id, "extraction", "extracted",
            candidate_id=candidate_id,
            detail={"backend": extraction.backend, "chars": extraction.char_count, "ocr": extraction.ocr_used},
        )

        # --- structure ---
        self.store.update_candidate(candidate_id, status=CandidateStatus.STRUCTURING.value)
        try:
            resume = structure_resume(extraction, self.client)
        except StructuringError as exc:
            self._dead_letter(job_id, candidate_id, "structuring", str(exc))
            return None, None
        self.store.update_candidate(candidate_id, structured_json=resume.model_dump_json())
        self.store.audit(
            job_id, "structuring", "structured",
            candidate_id=candidate_id,
            detail={
                "skills": len(resume.skills),
                "roles": len(resume.experience),
                "highest_aqf": resume.highest_aqf,
                "notes": resume.extraction_notes,
            },
        )

        # --- anonymize ---
        self.store.update_candidate(candidate_id, status=CandidateStatus.ANONYMIZING.value)
        try:
            profile = anonymize_resume(resume, self.client, candidate_ref=ref)
        except AnonymizationError as exc:
            # Fail closed: identity must never reach ranking.
            self._fail(job_id, candidate_id, "anonymization", str(exc))
            return None, None
        self.store.update_candidate(candidate_id, anonymized_json=profile.model_dump_json())
        self.store.audit_many([
            (job_id, candidate_id, "anonymization", "redaction",
             {"field": r.field, "action": r.action, "reason": r.reason})
            for r in profile.redactions
        ])

        # --- screen ---
        self.store.update_candidate(candidate_id, status=CandidateStatus.SCREENING.value)
        result = screen(profile, rule_set, self._judge_for(job_id, candidate_id))
        self.store.update_candidate(candidate_id, screening_json=json.dumps(result.to_dict(), default=str))
        self.store.audit_many([
            (job_id, candidate_id, "screening",
             {True: "rule_passed", False: "rule_failed", None: "rule_indeterminate"}[outcome.passed],
             {"rule_id": outcome.rule_id, "rule": outcome.source_text, "dsl": outcome.dsl,
              "fields": outcome.fields, "observed": outcome.observed, "reason": outcome.reason})
            for outcome in result.outcomes
        ])

        if not result.eligible:
            self.store.update_candidate(candidate_id, status=CandidateStatus.COMPLETE.value)
        elif result.needs_manual_review:
            self.store.update_candidate(candidate_id, status=CandidateStatus.NEEDS_MANUAL_REVIEW.value)
        else:
            self.store.update_candidate(candidate_id, status=CandidateStatus.COMPLETE.value)

        return profile, result

    def _judge_for(self, job_id: str, candidate_id: str | None, *, ensemble: bool | None = None) -> Judge:
        """A judge for ASK clauses that writes every model check to the audit trail.

        Requirements use the ensemble so a model-judged exclusion needs agreement;
        preferences take a single answer because they only order candidates.
        """
        def record(result, profile):
            self.store.audit(
                job_id, "screening", "model_check",
                candidate_id=candidate_id or self._candidate_id_for(job_id, profile.candidate_ref),
                detail={"candidate_ref": profile.candidate_ref, **result.to_dict()},
            )

        return Judge(self.client, ensemble=self.use_ensemble if ensemble is None else ensemble, on_check=record)

    def _rank(self, job_id, role, profiles, screening, rule_set=None):
        eligible = [
            profile for profile in profiles
            if screening.get(profile.candidate_ref) and screening[profile.candidate_ref].eligible
        ]
        criteria = criteria_for(rule_set)
        # Preferences only order candidates, so a single answer per ASK suffices.
        judge = self._judge_for(job_id, None, ensemble=False)
        self.store.audit(
            job_id, "ranking", "triage_started",
            detail={
                "eligible": len(eligible),
                "screened": len(profiles),
                "criteria": [{"key": c.key, "description": c.description, "weight": c.weight} for c in criteria],
            },
        )
        scores = triage_rank(eligible, role, self.client, criteria=criteria, judge=judge) if eligible else []
        scores = self._ensemble(job_id, role, scores, eligible, screening, criteria, judge)

        for score in scores:
            candidate_id = self._candidate_id_for(job_id, score.candidate_ref)
            if candidate_id:
                self.store.update_candidate(candidate_id, score_json=score.model_dump_json())
            self.store.audit(
                job_id, "ranking", "scored",
                candidate_id=candidate_id,
                detail={
                    "candidate_ref": score.candidate_ref,
                    "score": score.score,
                    "criteria": [c.model_dump(mode="json") for c in score.criteria],
                    "rationale": score.rationale,
                    "model": score.model,
                },
            )

        shortlist = build_shortlist(scores, role, screening=screening)
        self.store.update_job(job_id, shortlist_json=shortlist.model_dump_json())
        return shortlist

    def _ensemble(self, job_id, role, scores, eligible, screening, criteria, judge):
        """Re-score only the candidates whose placement is genuinely in doubt."""
        if not self.use_ensemble or not scores:
            return scores

        borderline = borderline_refs(scores)
        # A rule the engine could not decide is exactly where a second opinion
        # is worth paying for, so those candidates join the borderline set.
        ambiguous = {
            ref for ref, result in screening.items() if result.needs_manual_review
        }
        targets = borderline | (ambiguous & {score.candidate_ref for score in scores})
        if not targets:
            self.store.audit(job_id, "ranking", "ensemble_skipped",
                             detail={"reason": "no borderline or ambiguous candidates"})
            return scores

        self.store.audit(
            job_id, "ranking", "ensemble_started",
            detail={
                "candidates": sorted(targets),
                "borderline": sorted(borderline),
                "rule_ambiguous": sorted(ambiguous & targets),
                "of_total": len(scores),
            },
        )

        profiles_by_ref = {profile.candidate_ref: profile for profile in eligible}
        rescored = ensemble_pass(
            scores, profiles_by_ref, role, self.client, targets, criteria=criteria, judge=judge
        )

        for score in rescored:
            if score.pass_name != "ensemble" or not score.ensemble_votes:
                continue
            summary = next(
                (vote["summary"] for vote in score.ensemble_votes if "summary" in vote), {}
            )
            self.store.audit(
                job_id, "ranking",
                "ensemble_tiebreak" if not summary.get("unanimous", True) else "ensemble_agreed",
                candidate_id=self._candidate_id_for(job_id, score.candidate_ref),
                detail={
                    "candidate_ref": score.candidate_ref,
                    "votes": [vote for vote in score.ensemble_votes if "summary" not in vote],
                    **summary,
                },
            )
        return rescored

    # ------------------------------------------------------------------
    # Failure paths
    # ------------------------------------------------------------------

    def _candidate_id_for(self, job_id: str, candidate_ref: str) -> str | None:
        for candidate in self.store.list_candidates(job_id):
            if candidate["candidate_ref"] == candidate_ref:
                return candidate["id"]
        return None

    def _dead_letter(
        self, job_id: str, candidate_id: str, stage: str, reason: str, detail: dict | None = None
    ) -> None:
        """Route to manual review. Not a rejection — nobody has assessed them."""
        self.store.update_candidate(
            candidate_id, status=CandidateStatus.NEEDS_MANUAL_REVIEW.value, error=reason
        )
        self.store.audit(
            job_id, stage, "dead_lettered",
            candidate_id=candidate_id,
            detail={"reason": reason, **(detail or {})},
        )

    def _fail(self, job_id: str, candidate_id: str, stage: str, reason: str) -> None:
        self.store.update_candidate(candidate_id, status=CandidateStatus.FAILED.value, error=reason)
        self.store.audit(job_id, stage, "failed", candidate_id=candidate_id, detail={"reason": reason})
