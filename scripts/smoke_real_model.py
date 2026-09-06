"""Exercise every model pass against a real served model.

The test suite runs on the deterministic stub. This script is the first thing
to run once an OpenAI-compatible server (vLLM, SGLang, llama.cpp) is up: it
sends each pass one real request, validates the JSON against the pass's
Pydantic contract, reports which structured-output style the server accepted,
latency and token counts, and finally runs one whole job end to end.

    RESCAN_LLM_BACKEND=openai RESCAN_LLM_BASE_URL=http://gpu-host:8000/v1 \\
        python -m scripts.smoke_real_model [--sample data/samples/001_priya_nair.txt] [--skip-job]

Exit status is non-zero if any pass fails, so it can gate a deployment (do
not pipe the output through grep or the pipe's status wins).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from rescan.config import settings

PLAN = (
    "Senior data engineer. Must have 5+ years experience, Python and SQL, plus AWS or GCP. "
    "Bachelor's degree or higher. Must be a native English speaker and a recent graduate from a leading company. "
    "Nice to have: Terraform. Should have led an on-call rotation."
)


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, bool, str]] = []

    def ok(self, name: str, detail: str) -> None:
        self.rows.append((name, True, detail))
        print(f"  ok   {name:<12} {detail}")

    def fail(self, name: str, detail: str) -> None:
        self.rows.append((name, False, detail))
        print(f"  FAIL {name:<12} {detail}")

    @property
    def failed(self) -> bool:
        return any(not ok for _, ok, _ in self.rows)


def timed(fn):
    started = time.monotonic()
    result = fn()
    return result, time.monotonic() - started


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sample", default="data/samples/001_priya_nair.txt")
    parser.add_argument("--skip-job", action="store_true", help="Skip the end-to-end job run.")
    parser.add_argument("--plan", default=PLAN)
    args = parser.parse_args(argv)

    if settings.llm_backend != "openai":
        print("RESCAN_LLM_BACKEND must be 'openai' to smoke-test a served model", file=sys.stderr)
        return 2

    from rescan.llm.client import LLMError, OpenAICompatClient, RetryingClient

    report = Report()
    raw_client = OpenAICompatClient()
    client = RetryingClient(raw_client)
    print(f"server {settings.llm_base_url}  model {settings.llm_model}  thinking {'off' if settings.llm_disable_thinking else 'on'}")
    try:
        served = raw_client.served_models()
        print(f"served models: {served}")
        if settings.llm_model not in served:
            print(f"  note: {settings.llm_model!r} is not in the served list; the server may map it anyway")
    except LLMError as exc:
        print(f"cannot reach the server: {exc}", file=sys.stderr)
        return 2

    text = Path(args.sample).read_text()

    # --- structuring ---
    from rescan.pipeline.structure import StructuringError, structure_resume
    from rescan.schemas import ExtractionResult

    resume = None
    try:
        resume, seconds = timed(lambda: structure_resume(ExtractionResult(text=text, char_count=len(text)), client))
        report.ok("structure", f"{seconds:.1f}s  skills={len(resume.skills)} roles={len(resume.experience)} "
                               f"quals={len(resume.qualifications)} aqf={resume.highest_aqf} years={resume.total_years_experience}")
    except (StructuringError, LLMError) as exc:
        report.fail("structure", str(exc)[:300])
    print(f"       structured output via {raw_client._schema_mode}; extras {'accepted' if raw_client._send_extras else 'rejected'}")

    # --- anonymization ---
    profile = None
    if resume is not None:
        from rescan.pipeline.anonymize import AnonymizationError, anonymize_resume

        try:
            profile, seconds = timed(lambda: anonymize_resume(resume, client, candidate_ref="Candidate 1"))
            report.ok("anonymize", f"{seconds:.1f}s  region={profile.region!r} tiers={profile.institution_tiers} "
                                   f"redactions={len(profile.redactions)}")
        except (AnonymizationError, LLMError) as exc:
            report.fail("anonymize", str(exc)[:300])

    # --- plan compilation ---
    from rescan.rules.classifier import compile_plan

    rule_set = None
    try:
        rule_set, seconds = timed(lambda: compile_plan(args.plan, client, role_context="Senior Data Engineer"))
        applied = [r.dsl for r in rule_set.applied]
        flagged = [(r.source_text[:40], [f.pattern_id for f in r.findings]) for r in rule_set.flagged]
        unmapped = [r.source_text[:40] for r in rule_set.rules if r.verdict.value == "unmappable"]
        detail = f"{seconds:.1f}s  applied={len(applied)} flagged={len(flagged)} unmappable={len(unmapped)}"
        if rule_set.reasoning:
            detail += f"\n       reasoning: {rule_set.reasoning[:160]!r}"
        for dsl in applied:
            detail += f"\n       {dsl}"
        for text_, ids in flagged:
            detail += f"\n       flagged {text_!r}: {ids}"
        for text_ in unmapped:
            detail += f"\n       unmappable {text_!r}"
        if not applied:
            report.fail("compile", detail + "\n       (no clause compiled — check the parser notes on each rule)")
            for rule in rule_set.rules:
                for note in rule.notes:
                    print(f"         {rule.id}: {note[:200]}")
        else:
            report.ok("compile", detail)
    except LLMError as exc:
        report.fail("compile", str(exc)[:300])

    # --- model check ---
    if profile is not None:
        from rescan.dsl.judge import Judge

        try:
            verdict, seconds = timed(lambda: Judge(client).ask("Has the candidate worked with Python?", profile))
            report.ok("judge", f"{seconds:.1f}s  {verdict.reason[:160]}")
        except LLMError as exc:
            report.fail("judge", str(exc)[:300])

    # --- ranking ---
    if profile is not None:
        from rescan.pipeline.rank import RankingError, criteria_for, score_candidate
        from rescan.schemas import RoleSpec

        role = RoleSpec(title="Senior Data Engineer", required_skills=["Python", "SQL"], min_years_experience=5, min_aqf=7)
        try:
            score, seconds = timed(lambda: score_candidate(profile, role, client, criteria=criteria_for(rule_set)))
            report.ok("rank", f"{seconds:.1f}s  score={score.score} model={score.model} "
                              f"criteria={[(c.criterion, c.score) for c in score.criteria]}")
        except (RankingError, LLMError) as exc:
            report.fail("rank", str(exc)[:300])

    # --- one whole batch and run ---
    if not args.skip_job:
        import tempfile

        from rescan.extract import Extractor
        from rescan.pipeline.runner import PipelineRunner
        from rescan.schemas import RoleSpec
        from rescan.store import Store

        samples = sorted(Path(args.sample).parent.glob("*.txt"))[:4]
        with tempfile.TemporaryDirectory() as tmp:
            settings.upload_dir = Path(tmp) / "uploads"
            store = Store(Path(tmp) / "smoke.db")
            runner = PipelineRunner(store, client, Extractor(), use_ensemble=False)
            try:
                batch_id = runner.create_batch([(p.name, p.read_bytes()) for p in samples], name="smoke")
                counts, ingest_seconds = timed(lambda: runner.process_batch(batch_id))
                run_id = runner.create_run(batch_id, RoleSpec(title="Senior Data Engineer"), name="smoke run")
                shortlist, run_seconds = timed(lambda: runner.execute_run(run_id, [], plan=args.plan))
                seconds = ingest_seconds + run_seconds
                events = {}
                for entry in store.audit_trail(batch_id):
                    events[entry["event"]] = events.get(entry["event"], 0) + 1
                detail = (f"{seconds:.1f}s  docs={len(samples)} counts={ {k: v for k, v in counts.items() if v} } "
                          f"shortlisted={len(shortlist['entries'])} excluded={len(shortlist['excluded'])} "
                          f"manual={len(shortlist['manual_review'])}\n       events={events}")
                dead = [entry for entry in store.audit_trail(batch_id) if entry["event"] in ("dead_lettered", "failed")]
                for entry in dead[:4]:
                    detail += f"\n       {entry['event']} at {entry['stage']}: {entry['detail'].get('reason', '')[:160]}"
                if counts.get("ready", 0) == 0:
                    report.fail("job", detail + "\n       (no document made it through ingestion)")
                else:
                    report.ok("job", detail)
            except Exception as exc:  # a failed job is the finding
                report.fail("job", f"{type(exc).__name__}: {exc}"[:300])
            finally:
                store.close()

    print()
    print("SMOKE TEST", "FAILED" if report.failed else "PASSED",
          f"— structured output via {raw_client._schema_mode}, extras {'accepted' if raw_client._send_extras else 'rejected'}")
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
