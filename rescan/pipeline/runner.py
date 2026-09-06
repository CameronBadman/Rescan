"""Batch ingestion and analysis runs.

Two orchestrations, deliberately separate.

A **batch** is ingested once: every uploaded document is extracted, structured
and de-identified, and the result is a set of anonymized profiles. No rule and
no role is involved — this work is the same whoever ends up screening it.

An **analysis run** applies one compiled rule set to a finished batch: it
screens every profile, ranks the survivors and cuts a shortlist. A batch can
carry many runs, and each keeps its own outcomes, so a second analysis never
overwrites the first and two rule sets can be compared over the same people.

Failure handling is deliberate. A document that fails extraction is retried
once and then dead-lettered to `needs_manual_review`, never dropped, and never
counted as a rejected candidate. A candidate whose anonymization leaks identity
fails closed rather than reaching any run.
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
from rescan.rules.classifier import classify_rule, compile_plan, scan_patterns
from rescan.rules.engine import ScreeningResult, screen
from rescan.rules.models import ClassifiedRule, RuleSet, RuleVerdict
from rescan.rules.statutes import RiskLevel
from rescan.schemas import (
    AnonymizedProfile,
    BatchStatus,
    CandidateOutcome,
    CandidateStatus,
    RoleSpec,
    RunStatus,
)
from rescan.store import Store

log = logging.getLogger(__name__)


class RuleRejected(ValueError):
    """The rule screens on a protected attribute or a proxy; it was not added."""

    def __init__(self, rule: ClassifiedRule) -> None:
        super().__init__("rule rejected: it screens on a protected attribute or a proxy for one")
        self.rule = rule


class BatchNotReady(RuntimeError):
    """A run was asked for over a batch that has not finished ingesting."""


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

    # ==================================================================
    # Batches: intake and de-identification
    # ==================================================================

    def create_batch(
        self,
        files: Sequence[tuple[str, bytes]],
        *,
        batch_id: str | None = None,
        name: str | None = None,
        source: dict[str, Any] | None = None,
    ) -> str:
        """Register a batch and its documents. Returns immediately with the id."""
        batch_id = batch_id or f"batch_{uuid.uuid4().hex[:12]}"
        self.store.create_batch(batch_id, name=name, source=source)

        batch_dir = settings.upload_dir / batch_id
        batch_dir.mkdir(parents=True, exist_ok=True)

        accepted = duplicates = 0
        for filename, data in files:
            digest = content_hash(data)
            candidate_id = f"cand_{uuid.uuid4().hex[:12]}"
            existing = self.store.add_candidate(candidate_id, batch_id, filename, digest)
            if existing is not None:
                # Same bytes already in this batch: record it, do not reprocess.
                self.store.add_duplicate(candidate_id, batch_id, filename, digest, duplicate_of=existing)
                duplicates += 1
                self.store.audit(
                    batch_id, "ingestion", "duplicate_skipped",
                    candidate_id=candidate_id,
                    detail={"filename": filename, "duplicate_of": existing},
                )
                continue
            (batch_dir / f"{candidate_id}{Path(filename).suffix}").write_bytes(data)
            accepted += 1

        self.store.audit(
            batch_id, "ingestion", "batch_created",
            detail={"accepted": accepted, "duplicates": duplicates, "name": name},
        )
        return batch_id

    def process_batch(self, batch_id: str) -> dict[str, int]:
        """Extract, structure and de-identify every pending document.

        Blocking; callers run it in a background task. Returns the batch's
        stage counts. No rules are involved: the product is a set of anonymized
        profiles that any number of runs can screen.
        """
        batch = self.store.get_batch(batch_id)
        if batch is None:
            raise KeyError(f"unknown batch {batch_id!r}")

        self.store.update_batch(batch_id, status=BatchStatus.RUNNING.value)
        try:
            pending = [
                candidate
                for candidate in self.store.list_candidates(batch_id)
                if candidate["status"] == CandidateStatus.PENDING.value
            ]
            # Refs are assigned up front and by position, so they are stable and
            # carry no information about the candidate.
            start = len([
                c for c in self.store.list_candidates(batch_id)
                if c["candidate_ref"] and c["status"] != CandidateStatus.PENDING.value
            ])
            for index, candidate in enumerate(pending, start=start + 1):
                self.store.update_candidate(candidate["id"], candidate_ref=f"Candidate {index}")
                candidate["candidate_ref"] = f"Candidate {index}"

            batch_dir = settings.upload_dir / batch_id
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                list(pool.map(lambda c: self._ingest_one(batch_id, c, batch_dir), pending))
        except Exception as exc:  # a batch failure must be visible, not silent
            log.exception("batch %s failed", batch_id)
            self.store.update_batch(batch_id, status=BatchStatus.FAILED.value, error=str(exc))
            self.store.audit(batch_id, "ingestion", "batch_failed", detail={"error": str(exc)})
            raise

        counts = self.store.batch_counts(batch_id)
        self.store.update_batch(batch_id, status=BatchStatus.COMPLETE.value)
        self.store.audit(
            batch_id, "ingestion", "batch_complete",
            detail={
                "ready": counts[CandidateStatus.READY.value],
                "needs_manual_review": counts[CandidateStatus.NEEDS_MANUAL_REVIEW.value],
                "failed": counts[CandidateStatus.FAILED.value],
                "duplicate": counts[CandidateStatus.DUPLICATE.value],
            },
        )
        return counts

    def _ingest_one(
        self, batch_id: str, candidate: dict[str, Any], batch_dir: Path
    ) -> AnonymizedProfile | None:
        """Extract, structure and de-identify one document."""
        candidate_id = candidate["id"]
        ref = candidate["candidate_ref"]
        stored = next(batch_dir.glob(f"{candidate_id}*"), None)
        if stored is None:
            self._fail(batch_id, candidate_id, "ingestion", "uploaded file is missing from disk")
            return None

        # --- extract (one retry, then dead-letter) ---
        self.store.update_candidate(candidate_id, status=CandidateStatus.EXTRACTING.value)
        extraction = None
        for attempt in (1, 2):
            extraction = self.extractor.extract_path(stored)
            if extraction.char_count > 0:
                break
            self.store.audit(
                batch_id, "extraction", "extraction_retry",
                candidate_id=candidate_id,
                detail={"attempt": attempt, "warnings": extraction.warnings},
            )
        if extraction is None or extraction.char_count == 0:
            self._dead_letter(
                batch_id, candidate_id, "extraction",
                "No text could be recovered from this document after two attempts.",
                detail={"warnings": extraction.warnings if extraction else []},
            )
            return None

        self.store.update_candidate(candidate_id, extraction_json=extraction.model_dump_json())
        self.store.audit(
            batch_id, "extraction", "extracted",
            candidate_id=candidate_id,
            detail={"backend": extraction.backend, "chars": extraction.char_count, "ocr": extraction.ocr_used},
        )

        # --- structure ---
        self.store.update_candidate(candidate_id, status=CandidateStatus.STRUCTURING.value)
        try:
            resume = structure_resume(extraction, self.client)
        except StructuringError as exc:
            self._dead_letter(batch_id, candidate_id, "structuring", str(exc))
            return None
        self.store.update_candidate(candidate_id, structured_json=resume.model_dump_json())
        self.store.audit(
            batch_id, "structuring", "structured",
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
            # Fail closed: identity must never reach a run.
            self._fail(batch_id, candidate_id, "anonymization", str(exc))
            return None
        self.store.update_candidate(
            candidate_id, anonymized_json=profile.model_dump_json(), status=CandidateStatus.READY.value
        )
        self.store.audit_many([
            (batch_id, None, candidate_id, "anonymization", "redaction",
             {"field": r.field, "action": r.action, "reason": r.reason})
            for r in profile.redactions
        ])
        return profile

    # ==================================================================
    # Runs: rules, screening, ranking
    # ==================================================================

    def create_run(
        self,
        batch_id: str,
        role: RoleSpec,
        *,
        run_id: str | None = None,
        name: str | None = None,
    ) -> str:
        """Register an analysis run over a finished batch."""
        batch = self.store.get_batch(batch_id)
        if batch is None:
            raise KeyError(f"unknown batch {batch_id!r}")
        if batch["status"] != BatchStatus.COMPLETE.value:
            raise BatchNotReady(
                f"batch {batch_id!r} is {batch['status']}; wait for ingestion to finish"
            )
        run_id = run_id or f"run_{uuid.uuid4().hex[:12]}"
        self.store.create_run(run_id, batch_id, role.model_dump(mode="json"), name=name)
        self.store.audit(
            batch_id, "runs", "run_created",
            run_id=run_id,
            detail={"run_id": run_id, "name": name, "role": role.title, "candidates": batch["ready"]},
        )
        return run_id

    def execute_run(
        self,
        run_id: str,
        rule_texts: Sequence[str] = (),
        plan: str | None = None,
        rules_from: str | None = None,
    ) -> dict[str, Any]:
        """Compile the run's rules, screen the batch and cut a shortlist.

        Blocking; callers run it in a background task. `rules_from` reuses
        another run's compiled rule set as-is — the rules a recruiter refined
        on one analysis applied to the next — instead of compiling the plan.
        """
        run = self._require_run(run_id)
        role = RoleSpec.model_validate(run["role"])
        batch_id = run["batch_id"]

        self.store.update_run(run_id, status=RunStatus.RUNNING.value)
        try:
            if rules_from:
                rule_set = self._copy_rules(run_id, batch_id, rules_from)
            else:
                rule_set = self._compile_rules(run_id, batch_id, rule_texts, role, plan)
            profiles, screening = self._screen_run(run_id, batch_id, rule_set)
            shortlist = self._rank(run_id, batch_id, role, profiles, screening, rule_set)
        except Exception as exc:  # a run failure must be visible, not silent
            log.exception("run %s failed", run_id)
            self.store.update_run(run_id, status=RunStatus.FAILED.value, error=str(exc))
            self.store.audit(batch_id, "runs", "run_failed", run_id=run_id, detail={"error": str(exc)})
            raise

        self.store.update_run(run_id, status=RunStatus.COMPLETE.value, error=None)
        self.store.audit(
            batch_id, "runs", "run_complete",
            run_id=run_id,
            detail={"shortlisted": len(shortlist.entries), "excluded": len(shortlist.excluded),
                    "manual_review": len(shortlist.manual_review)},
        )
        return shortlist.model_dump(mode="json")

    def rescreen(self, run_id: str) -> dict[str, Any]:
        """Re-run screening and ranking with the run's current rule set.

        Extraction, structuring and anonymization are not repeated: the batch's
        stored anonymized profiles are screened again in place.
        """
        run = self._require_run(run_id)
        role = RoleSpec.model_validate(run["role"])
        batch_id = run["batch_id"]
        rule_set = self.rule_set_for(run_id)

        self.store.update_run(run_id, status=RunStatus.RUNNING.value)
        self.store.audit(
            batch_id, "runs", "rescreen_started", run_id=run_id,
            detail={"applied": len(rule_set.applied)},
        )
        try:
            profiles, screening = self._screen_run(run_id, batch_id, rule_set)
            shortlist = self._rank(run_id, batch_id, role, profiles, screening, rule_set)
        except Exception as exc:
            log.exception("rescreen of %s failed", run_id)
            self.store.update_run(run_id, status=RunStatus.FAILED.value, error=str(exc))
            self.store.audit(batch_id, "runs", "run_failed", run_id=run_id, detail={"error": str(exc)})
            raise

        self.store.update_run(run_id, status=RunStatus.COMPLETE.value, error=None)
        self.store.audit(
            batch_id, "runs", "rescreen_complete",
            run_id=run_id,
            detail={"candidates": len(profiles), "shortlisted": len(shortlist.entries),
                    "excluded": len(shortlist.excluded)},
        )
        return shortlist.model_dump(mode="json")

    # ------------------------------------------------------------------
    # Rules
    # ------------------------------------------------------------------

    def _compile_rules(
        self, run_id: str, batch_id: str, rule_texts: Sequence[str], role: RoleSpec, plan: str | None
    ) -> RuleSet:
        rule_set = compile_plan(plan, self.client, rule_texts=list(rule_texts), role_context=role.title)
        self.store.update_run(run_id, rules_json=rule_set.model_dump_json())

        entries = [(
            batch_id, run_id, None, "rules", "plan_compiled",
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
                batch_id, run_id, None, "rules",
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
                    batch_id, run_id, None, "rules", "rule_risk_flagged",
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

    def _copy_rules(self, run_id: str, batch_id: str, source_run_id: str) -> RuleSet:
        rule_set = self.rule_set_for(source_run_id)
        self.store.update_run(run_id, rules_json=rule_set.model_dump_json())
        self.store.audit(
            batch_id, "rules", "rules_copied",
            run_id=run_id,
            detail={"from_run": source_run_id, "rules": len(rule_set.rules),
                    "applied": len(rule_set.applied), "flagged": len(rule_set.flagged)},
        )
        return rule_set

    def rule_set_for(self, run_id: str) -> RuleSet:
        run = self._require_run(run_id)
        return RuleSet.model_validate(run["rules"]) if run["rules"] else RuleSet()

    def add_rule(self, run_id: str, text: str) -> ClassifiedRule:
        """Check a rule against the law and, if it is not high risk, add it to the run.

        A high-risk rule is never added: the ClassifiedRule with its findings
        and rewrite is raised inside RuleRejected so the caller can show the
        recommendation. The caller decides when to rescreen.
        """
        run = self._require_run(run_id)
        role = RoleSpec.model_validate(run["role"])
        rule_set = self.rule_set_for(run_id)
        taken = {rule.id for rule in rule_set.rules}
        index = len(rule_set.rules) + 1
        while f"rule_{index}" in taken:
            index += 1
        rule_id = f"rule_{index}"

        # The statute table is authoritative and needs no model. A rule it
        # already rates high risk is refused here, so the recruiter gets the
        # finding and the rewrite immediately instead of waiting on inference
        # for an answer that could not change.
        findings = scan_patterns(text)
        blocking = [f for f in findings if f.risk is RiskLevel.HIGH]
        if blocking:
            rule = ClassifiedRule(
                id=rule_id, source_text=text.strip(), verdict=RuleVerdict.RISKY,
                risk=RiskLevel.HIGH, findings=findings,
                notes=["Refused by the known-phrasings table before any model call."],
            )
        else:
            rule = classify_rule(text, self.client, role_context=role.title, rule_id=rule_id)
        if rule.risk is RiskLevel.HIGH:
            self.store.audit(
                run["batch_id"], "rules", "rule_rejected",
                run_id=run_id,
                detail={"text": rule.source_text, "risk": rule.risk.value,
                        "findings": [f.model_dump(mode="json") for f in rule.findings]},
            )
            raise RuleRejected(rule)
        rule_set.rules.append(rule)
        self.store.update_run(run_id, rules_json=rule_set.model_dump_json())
        self.store.audit(
            run["batch_id"], "rules", "rule_added",
            run_id=run_id,
            detail={"rule_id": rule.id, "text": rule.source_text, "kind": rule.kind,
                    "verdict": rule.verdict.value, "risk": rule.risk.value, "dsl": rule.dsl,
                    "notes": rule.notes},
        )
        return rule

    def remove_rule(self, run_id: str, rule_id: str) -> ClassifiedRule:
        run = self._require_run(run_id)
        rule_set = self.rule_set_for(run_id)
        rule = next((r for r in rule_set.rules if r.id == rule_id), None)
        if rule is None:
            raise KeyError(f"run {run_id!r} has no rule {rule_id!r}")
        rule_set.rules = [r for r in rule_set.rules if r.id != rule_id]
        self.store.update_run(run_id, rules_json=rule_set.model_dump_json())
        self.store.audit(
            run["batch_id"], "rules", "rule_removed",
            run_id=run_id, detail={"rule_id": rule_id, "text": rule.source_text},
        )
        return rule

    # ------------------------------------------------------------------
    # Screening and ranking
    # ------------------------------------------------------------------

    def _screen_run(
        self, run_id: str, batch_id: str, rule_set
    ) -> tuple[list[AnonymizedProfile], dict[str, ScreeningResult]]:
        """Screen every ready profile in the batch against this run's rules."""
        ready = [
            candidate
            for candidate in self.store.list_candidates(batch_id)
            if candidate["status"] == CandidateStatus.READY.value and candidate.get("anonymized")
        ]
        self.store.clear_results(run_id)

        def one(candidate: dict[str, Any]) -> tuple[AnonymizedProfile, ScreeningResult]:
            profile = AnonymizedProfile.model_validate(candidate["anonymized"])
            return profile, self._screen_profile(run_id, batch_id, candidate["id"], profile, rule_set)

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            results = list(pool.map(one, ready))

        profiles = [profile for profile, _ in results]
        screening = {result.candidate_ref: result for _, result in results}
        return profiles, screening

    def _screen_profile(
        self, run_id: str, batch_id: str, candidate_id: str, profile: AnonymizedProfile, rule_set
    ) -> ScreeningResult:
        """Screen one anonymized profile and record the outcome on the run."""
        result = screen(profile, rule_set, self._judge_for(batch_id, run_id, candidate_id))
        if not result.eligible:
            outcome = CandidateOutcome.EXCLUDED
        elif result.needs_manual_review:
            outcome = CandidateOutcome.NEEDS_MANUAL_REVIEW
        else:
            outcome = CandidateOutcome.ELIGIBLE
        self.store.put_result(
            run_id, candidate_id, profile.candidate_ref, outcome.value,
            screening=json.loads(json.dumps(result.to_dict(), default=str)),
        )
        self.store.audit_many([
            (batch_id, run_id, candidate_id, "screening",
             {True: "rule_passed", False: "rule_failed", None: "rule_indeterminate"}[outcome_row.passed],
             {"rule_id": outcome_row.rule_id, "rule": outcome_row.source_text, "dsl": outcome_row.dsl,
              "fields": outcome_row.fields, "observed": outcome_row.observed, "reason": outcome_row.reason})
            for outcome_row in result.outcomes
        ])
        return result

    def _judge_for(
        self, batch_id: str, run_id: str, candidate_id: str | None, *, ensemble: bool | None = None
    ) -> Judge:
        """A judge for ASK clauses that writes every model check to the audit trail.

        Requirements use the ensemble so a model-judged exclusion needs agreement;
        preferences take a single answer because they only order candidates.
        """
        refs = self._refs_to_ids(batch_id) if candidate_id is None else {}

        def record(result, profile):
            self.store.audit(
                batch_id, "screening", "model_check",
                run_id=run_id,
                candidate_id=candidate_id or refs.get(profile.candidate_ref),
                detail={"candidate_ref": profile.candidate_ref, **result.to_dict()},
            )

        return Judge(self.client, ensemble=self.use_ensemble if ensemble is None else ensemble, on_check=record)

    def _rank(self, run_id, batch_id, role, profiles, screening, rule_set=None):
        eligible = [
            profile for profile in profiles
            if screening.get(profile.candidate_ref) and screening[profile.candidate_ref].eligible
        ]
        criteria = criteria_for(rule_set)
        # Preferences only order candidates, so a single answer per ASK suffices.
        judge = self._judge_for(batch_id, run_id, None, ensemble=False)
        self.store.audit(
            batch_id, "ranking", "triage_started",
            run_id=run_id,
            detail={
                "eligible": len(eligible),
                "screened": len(profiles),
                "criteria": [{"key": c.key, "description": c.description, "weight": c.weight} for c in criteria],
            },
        )
        scores = triage_rank(eligible, role, self.client, criteria=criteria, judge=judge) if eligible else []
        scores = self._ensemble(run_id, batch_id, role, scores, eligible, screening, criteria, judge)

        refs = self._refs_to_ids(batch_id)
        for score in scores:
            candidate_id = refs.get(score.candidate_ref)
            if candidate_id:
                self.store.set_result_score(run_id, candidate_id, score.model_dump(mode="json"))
            self.store.audit(
                batch_id, "ranking", "scored",
                run_id=run_id,
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
        self.store.update_run(run_id, shortlist_json=shortlist.model_dump_json())
        return shortlist

    def _ensemble(self, run_id, batch_id, role, scores, eligible, screening, criteria, judge):
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
            self.store.audit(batch_id, "ranking", "ensemble_skipped", run_id=run_id,
                             detail={"reason": "no borderline or ambiguous candidates"})
            return scores

        self.store.audit(
            batch_id, "ranking", "ensemble_started",
            run_id=run_id,
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

        refs = self._refs_to_ids(batch_id)
        for score in rescored:
            if score.pass_name != "ensemble" or not score.ensemble_votes:
                continue
            summary = next(
                (vote["summary"] for vote in score.ensemble_votes if "summary" in vote), {}
            )
            self.store.audit(
                batch_id, "ranking",
                "ensemble_tiebreak" if not summary.get("unanimous", True) else "ensemble_agreed",
                run_id=run_id,
                candidate_id=refs.get(score.candidate_ref),
                detail={
                    "candidate_ref": score.candidate_ref,
                    "votes": [vote for vote in score.ensemble_votes if "summary" not in vote],
                    **summary,
                },
            )
        return rescored

    # ------------------------------------------------------------------
    # Helpers and failure paths
    # ------------------------------------------------------------------

    def _require_run(self, run_id: str) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if run is None:
            raise KeyError(f"unknown run {run_id!r}")
        return run

    def _refs_to_ids(self, batch_id: str) -> dict[str, str]:
        return {
            candidate["candidate_ref"]: candidate["id"]
            for candidate in self.store.list_candidates(batch_id)
            if candidate["candidate_ref"]
        }

    def _dead_letter(
        self, batch_id: str, candidate_id: str, stage: str, reason: str, detail: dict | None = None
    ) -> None:
        """Route to manual review. Not a rejection — nobody has assessed them."""
        self.store.update_candidate(
            candidate_id, status=CandidateStatus.NEEDS_MANUAL_REVIEW.value, error=reason
        )
        self.store.audit(
            batch_id, stage, "dead_lettered",
            candidate_id=candidate_id,
            detail={"reason": reason, **(detail or {})},
        )

    def _fail(self, batch_id: str, candidate_id: str, stage: str, reason: str) -> None:
        self.store.update_candidate(candidate_id, status=CandidateStatus.FAILED.value, error=reason)
        self.store.audit(batch_id, stage, "failed", candidate_id=candidate_id, detail={"reason": reason})
