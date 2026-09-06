"""HTTP API.

Backend only — this exposes the pipeline for a separate frontend to drive.

Two resources, because they are two different things. A **batch** is a set of
resumes: uploaded once, extracted, structured and de-identified once. An
**analysis run** applies one rule set to a batch and owns the screening
outcomes, the scores and the shortlist, so a batch can carry many runs and a
second analysis never overwrites the first.

Creating a batch or a run returns immediately and the work continues in a
background worker; `GET /batches/{id}` and `GET /runs/{id}` are the polling
endpoints behind the progress views.
"""

from __future__ import annotations

import json
import logging
import secrets
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Any

import mimetypes

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from rescan.config import settings
from rescan.dsl import DslError, parse_expr, parse_program
from rescan.dsl.fields import reference as dsl_reference
from rescan.extract import Extractor
from rescan.ingest import (
    ALLOWED_SUFFIXES,  # noqa: F401 - re-exported for callers of this module
    MAX_UNPACKED_BYTES,  # noqa: F401
    ObjectStore,
    ObjectStoreError,
    build_object_store,
    expand_uploads,
    pull_batch_documents,
)
from rescan.llm.client import build_client
from rescan.pipeline.query import QueryRejected, run_query
from rescan.pipeline.runner import BatchNotReady, PipelineRunner, RuleRejected
from rescan.rules.classifier import classify_rule, compile_plan
from rescan.schemas import BatchStatus, RoleSpec, RunStatus
from rescan.store import Store

log = logging.getLogger(__name__)

class AppState:
    store: Store
    runner: PipelineRunner
    pool: ThreadPoolExecutor
    object_store: ObjectStore


state = AppState()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.ensure_dirs()
    state.store = Store()
    client = build_client()
    state.runner = PipelineRunner(state.store, client, Extractor())
    state.object_store = build_object_store()
    state.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="rescan-job")
    if not settings.api_keys:
        log.warning(
            "RESCAN_API_KEYS is empty: the API is unauthenticated. Set it before "
            "exposing this service beyond localhost."
        )
    log.info(
        "rescan API ready (llm backend=%s, object store=%s)",
        settings.llm_backend, getattr(state.object_store, "name", settings.object_store),
    )
    yield
    state.pool.shutdown(wait=False, cancel_futures=True)
    state.store.close()


app = FastAPI(
    title="Rescan",
    description="Bias-aware applicant triage with an auditable rule engine.",
    version="0.1.0",
    lifespan=lifespan,
)


# The frontend is a separate app on its own origin. CORS is added before the
# auth middleware so a preflight (which carries no key) is answered by the
# browser handshake rather than rejected with a 401.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "X-API-Key", "Content-Type"],
    allow_credentials=False,
    max_age=600,
)


# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------

# Health is public so a load balancer can probe it without a credential.
PUBLIC_PATHS = {"/health", "/health/llm", "/docs", "/redoc", "/openapi.json"}


def _presented_key(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return request.headers.get("x-api-key")


@app.middleware("http")
async def require_api_key(request: Request, call_next):
    """Reject unauthenticated requests when API keys are configured.

    Candidate data is sensitive personal information, so this is a deny-by-
    default check across every route rather than a per-endpoint dependency that
    a new endpoint could forget to declare.
    """
    if not settings.api_keys or request.url.path in PUBLIC_PATHS or request.method == "OPTIONS":
        return await call_next(request)

    presented = _presented_key(request)
    if not presented or not any(
        secrets.compare_digest(presented, configured) for configured in settings.api_keys
    ):
        return JSONResponse(
            status_code=401,
            content={"detail": "missing or invalid API key"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    return await call_next(request)

# --------------------------------------------------------------------------
# Request / response models
# --------------------------------------------------------------------------


class RuleCheckRequest(BaseModel):
    rules: list[str] = Field(description="Free-text screening rules to review.")
    role_context: str | None = None


class PlanCompileRequest(BaseModel):
    plan: str | None = Field(default=None, description="The recruiter's hiring plan, free text.")
    rules: list[str] = Field(default_factory=list, description="Discrete rules, answered one each in order.")
    role_context: str | None = None


class DslParseRequest(BaseModel):
    dsl: str = Field(description="A rule program (REQUIRE/PREFER clauses) or a bare query expression.")


class BucketBatchRequest(BaseModel):
    batch_id: str = Field(description="The batch's folder in the bucket: objects under <prefix>/<batch_id>/ are pulled.")
    name: str | None = Field(default=None, description="A human label for the batch.")
    prefix: str | None = Field(default=None, description="Override the configured bucket prefix.")


class RunRequest(BaseModel):
    batch_id: str = Field(description="The batch of resumes to analyse.")
    role: RoleSpec
    name: str | None = Field(default=None, description="A human label for this analysis run.")
    plan: str | None = Field(default=None, description="The recruiter's hiring plan, free text; compiled into rules.")
    rules: list[str] = Field(default_factory=list, description="Discrete rules, in addition to or instead of the plan.")
    rules_from: str | None = Field(default=None, description="Reuse another run's compiled rule set instead of compiling the plan.")


class RuleAddRequest(BaseModel):
    text: str = Field(description="A screening rule in plain language.")


class QueryRequest(BaseModel):
    dsl: str = Field(description="A bare expression in the rule language, e.g. years_experience >= 5 AND skills HAS ANY (\"Python\").")
    model_checks: bool = Field(default=True, description="Whether ASK clauses are put to the model. Off, they evaluate to unknown.")




# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "llm_backend": settings.llm_backend,
        "llm_model": settings.llm_model if settings.llm_backend != "stub" else "stub",
        "tika_url": settings.tika_url,
    }


def _rule_set_payload(rule_set) -> dict[str, Any]:
    return {
        "rules": [rule.model_dump(mode="json") for rule in rule_set.rules],
        "reasoning": rule_set.reasoning,
        "source_plan": rule_set.source_plan,
        "applied": len(rule_set.applied),
        "requirements": len(rule_set.requirements),
        "preferences": len(rule_set.preferences),
        "flagged": len(rule_set.flagged),
    }


@app.get("/health/llm")
def health_llm() -> dict[str, Any]:
    """Whether the inference server is reachable and serving the configured model.

    Kept separate from /health so a load balancer probe never waits on the GPU
    host. Public, like /health.
    """
    if settings.llm_backend != "openai":
        return {"backend": settings.llm_backend, "reachable": True, "model": "stub", "served": ["stub"]}
    from rescan.llm.client import LLMError, OpenAICompatClient

    inner = getattr(state.runner.client, "inner", state.runner.client)
    probe = inner if isinstance(inner, OpenAICompatClient) else OpenAICompatClient()
    try:
        served = probe.served_models()
    except LLMError as exc:
        return JSONResponse(
            status_code=503,
            content={"backend": "openai", "reachable": False, "model": settings.llm_model, "error": str(exc)},
        )
    return {
        "backend": "openai",
        "reachable": True,
        "model": settings.llm_model,
        "served": served,
        "model_served": settings.llm_model in served,
        "structured_output": probe._schema_mode,
        "thinking_disabled": settings.llm_disable_thinking,
    }


@app.post("/rules/check")
def check_rules(request: RuleCheckRequest) -> dict[str, Any]:
    """Review screening rules without running a batch.

    This is the fast path a recruiter uses while writing rules: it returns the
    legal-risk finding, the statute basis, a measurable rewrite and the compiled
    clause immediately, one result per rule in order.
    """
    if not request.rules:
        raise HTTPException(status_code=400, detail="no rules supplied")
    rule_set = compile_plan(None, state.runner.client, rule_texts=request.rules, role_context=request.role_context)
    return _rule_set_payload(rule_set)


@app.post("/rules/compile")
def compile_hiring_plan(request: PlanCompileRequest) -> dict[str, Any]:
    """Compile a recruiter's whole plan into the rule language.

    The model reasons over the plan with the legal standing in front of it and
    writes one clause per requirement; its reasoning comes back alongside the
    rules so a reviewer can see how each arose. Nothing is run against
    candidates here — pass the same plan to POST /runs to screen a batch.
    """
    if not (request.plan and request.plan.strip()) and not any(r.strip() for r in request.rules):
        raise HTTPException(status_code=400, detail="no plan or rules supplied")
    rule_set = compile_plan(
        request.plan, state.runner.client, rule_texts=request.rules, role_context=request.role_context
    )
    return _rule_set_payload(rule_set)


# --------------------------------------------------------------------------
# The rule language
# --------------------------------------------------------------------------


@app.get("/dsl/fields")
def dsl_fields() -> dict[str, Any]:
    """The language reference: grammar, every queryable field and record, the
    aggregates, and every forbidden identifier with the statute it engages."""
    return dsl_reference()


@app.post("/dsl/parse")
def dsl_parse(request: DslParseRequest) -> dict[str, Any]:
    """Validate a program or query without running it — for a live editor.

    A forbidden field comes back as 422 with the legal basis, so an editor can
    show why the identifier is rejected rather than merely that it is.
    """
    text = request.dsl.strip()
    is_program = text[:7].upper().startswith(("REQUIRE", "PREFER"))
    try:
        if is_program:
            program = parse_program(text)
            return {
                "ok": True,
                "kind": "program",
                "canonical": program.to_dsl(),
                "program": program.model_dump(mode="json"),
            }
        expr = parse_expr(text)
        return {"ok": True, "kind": "query", "canonical": expr.to_dsl(), "expr": expr.model_dump(mode="json")}
    except DslError as exc:
        raise HTTPException(status_code=422, detail=exc.to_dict()) from exc


@app.post("/rules/check-one")
def check_one_rule(text: str = Form(...), role_context: str | None = Form(None)) -> dict[str, Any]:
    return classify_rule(text, state.runner.client, role_context=role_context).model_dump(mode="json")


# --------------------------------------------------------------------------
# Batches: resumes in, anonymized profiles out
# --------------------------------------------------------------------------


@app.post("/batches", status_code=202)
async def create_batch(
    files: list[UploadFile] = File(..., description="Resumes, or a zip archive of them."),
    name: str | None = Form(None, description="A human label for the batch."),
) -> dict[str, Any]:
    """Accept a bulk upload and start ingesting. Returns a batch id immediately."""
    uploads = [(upload.filename or "unnamed", await upload.read()) for upload in files]
    documents = expand_uploads(uploads)
    if not documents:
        raise HTTPException(status_code=400, detail="no usable documents in the upload")

    batch_id = state.runner.create_batch(documents, name=name, source={"kind": "upload"})
    state.pool.submit(_process_batch_safely, batch_id)
    return {"batch_id": batch_id, "accepted_documents": len(documents), "status_url": f"/batches/{batch_id}"}


@app.post("/batches/from-bucket", status_code=202)
def create_batch_from_bucket(request: BucketBatchRequest) -> dict[str, Any]:
    """Start a batch from resumes already in the object store.

    Objects under `<prefix>/<batch_id>/` are pulled and ingested. The bucket's
    folder name becomes the batch id so the frontend can correlate the two
    without a mapping.
    """
    batch_id = request.batch_id.strip().strip("/")
    if not batch_id or "/" in batch_id or batch_id.startswith("."):
        raise HTTPException(status_code=400, detail="batch_id must be a single path segment")
    if state.store.get_batch(batch_id) is not None:
        raise HTTPException(status_code=409, detail=f"batch {batch_id!r} already exists")

    try:
        pull = pull_batch_documents(state.object_store, batch_id, prefix=request.prefix)
    except ObjectStoreError as exc:
        raise HTTPException(status_code=502, detail=f"object store error: {exc}") from exc
    if not pull.documents:
        raise HTTPException(
            status_code=404,
            detail={"message": f"no usable documents under {pull.prefix!r}", "skipped": pull.skipped},
        )

    state.runner.create_batch(
        pull.documents, batch_id=batch_id, name=request.name,
        source={"kind": "bucket", "prefix": pull.prefix},
    )
    state.store.audit(
        batch_id, "ingestion", "bucket_pulled",
        detail={
            "store": getattr(state.object_store, "name", settings.object_store),
            "prefix": pull.prefix,
            "keys": pull.keys,
            "documents": len(pull.documents),
            "bytes": pull.bytes_pulled,
            "skipped": pull.skipped,
        },
    )
    state.pool.submit(_process_batch_safely, batch_id)
    return {
        "batch_id": batch_id,
        "prefix": pull.prefix,
        "accepted_documents": len(pull.documents),
        "skipped": pull.skipped,
        "status_url": f"/batches/{batch_id}",
    }


def _process_batch_safely(batch_id: str) -> None:
    try:
        state.runner.process_batch(batch_id)
    except Exception:
        # process_batch already recorded the failure on the batch and in the audit log.
        log.exception("background ingestion of batch %s failed", batch_id)


@app.get("/batches")
def list_batches(limit: int = 50) -> dict[str, Any]:
    return {"batches": state.store.list_batches(limit=limit)}


@app.get("/batches/{batch_id}")
def batch_detail(batch_id: str) -> dict[str, Any]:
    """Ingestion progress and the analysis runs over this batch."""
    batch = _require_batch(batch_id)
    return {
        "batch_id": batch_id,
        "name": batch["name"],
        "status": batch["status"],
        "counts": batch["counts"],
        "total": batch["total"],
        "ready": batch["ready"],
        "source": batch["source"],
        "runs": batch["runs"],
        "created_at": batch["created_at"],
        "updated_at": batch["updated_at"],
        "error": batch["error"],
    }


@app.get("/batches/{batch_id}/candidates")
def batch_candidates(batch_id: str, include_identity: bool = False) -> dict[str, Any]:
    """Per-document ingestion state. Identity is withheld by default."""
    _require_batch(batch_id)
    candidates = state.store.list_candidates(batch_id)
    if not include_identity:
        for candidate in candidates:
            candidate.pop("structured", None)
            candidate.pop("filename", None)
    return {"candidates": candidates}


@app.post("/batches/{batch_id}/query")
def batch_query(batch_id: str, request: QueryRequest) -> dict[str, Any]:
    """Run a query in the rule language over the batch's anonymized profiles.

    Returns matched / not matched / indeterminate candidates, each with the
    plain-language reason. Identity is never returned here. The query passes
    the same legal gate as a rule and is written to the audit trail.
    """
    _require_batch(batch_id)
    judge = state.runner._judge_for(batch_id, None, None, ensemble=False) if request.model_checks else None
    try:
        return run_query(state.store, batch_id, request.dsl, judge=judge)
    except DslError as exc:
        raise HTTPException(status_code=422, detail=exc.to_dict()) from exc
    except QueryRejected as exc:
        raise HTTPException(status_code=422, detail=exc.to_dict()) from exc


@app.get("/batches/{batch_id}/audit")
def batch_audit(batch_id: str, candidate_id: str | None = None, limit: int = 2000) -> dict[str, Any]:
    """Everything that happened to these documents, every run included."""
    _require_batch(batch_id)
    return {"entries": state.store.audit_trail(batch_id, candidate_id=candidate_id, limit=limit)}


# --------------------------------------------------------------------------
# Analysis runs: a rule set applied to a batch
# --------------------------------------------------------------------------


@app.post("/runs", status_code=202)
def create_run(request: RunRequest) -> dict[str, Any]:
    """Start an analysis run over a finished batch. Returns a run id immediately."""
    if state.store.get_batch(request.batch_id) is None:
        raise HTTPException(status_code=404, detail=f"unknown batch {request.batch_id!r}")
    if request.rules_from and state.store.get_run(request.rules_from) is None:
        raise HTTPException(status_code=404, detail=f"unknown run {request.rules_from!r} to copy rules from")
    try:
        run_id = state.runner.create_run(request.batch_id, request.role, name=request.name)
    except BatchNotReady as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    state.pool.submit(_execute_run_safely, run_id, request.rules, request.plan, request.rules_from)
    return {"run_id": run_id, "batch_id": request.batch_id, "status_url": f"/runs/{run_id}"}


def _execute_run_safely(
    run_id: str, rule_texts: list[str], plan: str | None = None, rules_from: str | None = None
) -> None:
    try:
        state.runner.execute_run(run_id, rule_texts, plan=plan, rules_from=rules_from)
    except Exception:
        # execute_run already recorded the failure on the run and in the audit log.
        log.exception("background run %s failed", run_id)


@app.get("/runs")
def list_runs(batch_id: str | None = None, q: str | None = None, limit: int = 50) -> dict[str, Any]:
    """Run summaries, newest first. `q` searches id, name, role title and batch."""
    return {"runs": state.store.list_runs(batch_id=batch_id, query=q, limit=limit)}


@app.get("/runs/{run_id}")
def run_detail(run_id: str) -> dict[str, Any]:
    """Progress and outcome counts for one analysis run."""
    run = _require_run(run_id)
    batch = state.store.get_batch(run["batch_id"]) or {}
    return {
        "run_id": run_id,
        "batch_id": run["batch_id"],
        "batch_name": batch.get("name"),
        "batch_status": batch.get("status"),
        "name": run["name"],
        "status": run["status"],
        "role": run["role"],
        "counts": run["counts"],
        "screened": run["screened"],
        "total": run["total"],
        "has_shortlist": run["shortlist"] is not None,
        "has_rules": run["rules"] is not None,
        "created_at": run["created_at"],
        "updated_at": run["updated_at"],
        "error": run["error"],
    }


@app.get("/runs/{run_id}/rules")
def run_rules(run_id: str) -> dict[str, Any]:
    run = _require_run(run_id)
    return run["rules"] or {"rules": []}


@app.post("/runs/{run_id}/rules", status_code=202)
def add_run_rule(run_id: str, request: RuleAddRequest) -> dict[str, Any]:
    """Add a rule to a run and re-screen it.

    The rule is checked under the same legal review as the plan. If it is
    high risk it is not added: the response is a 422 carrying the finding,
    the statute and a measurable rewrite to use instead. Otherwise it is
    compiled, added, and the run is re-screened and re-ranked from the batch's
    stored anonymized profiles — nothing is re-extracted.
    """
    run = _require_run(run_id)
    if run["status"] == RunStatus.RUNNING.value:
        raise HTTPException(status_code=409, detail=f"run {run_id!r} is still running")
    if not request.text.strip():
        raise HTTPException(status_code=400, detail="empty rule")
    try:
        rule = state.runner.add_rule(run_id, request.text)
    except RuleRejected as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "kind": "legal",
                "message": "This rule screens on a protected attribute or a proxy for one and was not added.",
                "rule": exc.rule.model_dump(mode="json"),
            },
        ) from exc
    # Queue it before returning, so a client polling straight after the 202
    # cannot read the previous screening's "complete".
    state.store.update_run(run_id, status=RunStatus.QUEUED.value)
    state.pool.submit(_rescreen_safely, run_id)
    return {"rule": rule.model_dump(mode="json"), "added": True, "rescreening": True, "status_url": f"/runs/{run_id}"}


@app.delete("/runs/{run_id}/rules/{rule_id}", status_code=202)
def remove_run_rule(run_id: str, rule_id: str) -> dict[str, Any]:
    """Remove a rule from a run and re-screen it."""
    run = _require_run(run_id)
    if run["status"] == RunStatus.RUNNING.value:
        raise HTTPException(status_code=409, detail=f"run {run_id!r} is still running")
    try:
        state.runner.remove_rule(run_id, rule_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    state.store.update_run(run_id, status=RunStatus.QUEUED.value)
    state.pool.submit(_rescreen_safely, run_id)
    return {"removed": rule_id, "rescreening": True, "status_url": f"/runs/{run_id}"}


def _rescreen_safely(run_id: str) -> None:
    try:
        state.runner.rescreen(run_id)
    except Exception:
        log.exception("rescreen of run %s failed", run_id)


@app.get("/runs/{run_id}/shortlist")
def run_shortlist(run_id: str, reattach_identity: bool = True) -> dict[str, Any]:
    """The shortlist, with identity re-attached for the human reviewer.

    Full anonymization is correct for the machine passes but backfires for human
    reviewers, so names come back at this step. Pass reattach_identity=false to
    review the anonymized view.
    """
    run = _require_run(run_id)
    shortlist = run["shortlist"]
    if shortlist is None:
        raise HTTPException(status_code=409, detail=f"run {run_id!r} has no shortlist yet")
    identities = {}
    candidate_ids = {}
    for candidate in state.store.list_candidates(run["batch_id"]):
        ref = candidate.get("candidate_ref")
        if not ref:
            continue
        identities[ref] = (candidate.get("structured") or {}).get("identity", {})
        candidate_ids[ref] = candidate["id"]
    # The candidate id is an opaque handle to the document, not identity, so it
    # is attached either way: a reviewer can open the resume without a name.
    for section in ("entries", "below_cutoff", "excluded", "manual_review"):
        for entry in shortlist.get(section, []):
            entry["candidate_id"] = candidate_ids.get(entry["candidate_ref"])
            if reattach_identity:
                entry["identity"] = identities.get(entry["candidate_ref"])
    return shortlist


@app.get("/candidates/{candidate_id}/document")
def candidate_document(candidate_id: str, format: str = "auto") -> Any:
    """The resume behind a candidate, for a reviewer who wants to read it.

    `auto` returns the original document when it is still on disk — the PDF the
    recruiter can read as it was submitted — and falls back to text. `text`
    always returns text: the extracted document if there is one, otherwise a
    summary rendered from the profile, so a candidate is never a dead link.
    """
    candidate = state.store.get_candidate(candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail=f"unknown candidate {candidate_id!r}")

    stored = next((settings.upload_dir / candidate["batch_id"]).glob(f"{candidate_id}*"), None)
    if format != "text" and stored is not None and stored.is_file():
        return FileResponse(
            stored,
            media_type=mimetypes.guess_type(stored.name)[0] or "application/octet-stream",
            filename=candidate["filename"],
            content_disposition_type="inline",
        )

    extraction = candidate.get("extraction") or {}
    text = extraction.get("text") or _profile_summary(candidate)
    return {
        "candidate_id": candidate_id,
        "candidate_ref": candidate["candidate_ref"],
        "filename": candidate["filename"],
        "source": "extracted" if extraction.get("text") else "profile_summary",
        "has_document": stored is not None,
        "text": text,
    }


def _profile_summary(candidate: dict[str, Any]) -> str:
    """A readable summary of a candidate whose document is no longer on disk."""
    profile = candidate.get("anonymized") or candidate.get("structured") or {}
    lines = [f"{candidate.get('candidate_ref') or candidate['id']} — summary rendered from the "
             "structured profile; the original document is not stored on this instance.", ""]
    if profile.get("summary"):
        lines += [profile["summary"], ""]
    years = profile.get("total_years_experience")
    if years is not None:
        lines.append(f"Total experience: {years} years")
    if profile.get("region"):
        lines.append(f"Region: {profile['region']}")
    rights = profile.get("work_rights") or {}
    if rights.get("status"):
        lines.append(f"Work rights: {rights['status']}")
    for label, key in (("Skills", "skills"), ("Certifications", "certifications"),
                       ("Licences", "licences"), ("Languages", "languages")):
        values = profile.get(key) or []
        rendered = [v.get("name", "") if isinstance(v, dict) else str(v) for v in values]
        if rendered:
            lines.append(f"{label}: " + ", ".join(filter(None, rendered)))
    if profile.get("experience"):
        lines += ["", "Experience:"]
        for role in profile["experience"]:
            months = role.get("months")
            span = f" ({months:.0f} months)" if isinstance(months, (int, float)) else ""
            lines.append(f"  - {role.get('title') or 'Role'} at {role.get('employer') or 'employer'}{span}")
            if role.get("summary"):
                lines.append(f"    {role['summary']}")
    if profile.get("qualifications"):
        lines += ["", "Qualifications:"]
        for qual in profile["qualifications"]:
            lines.append(f"  - {qual.get('title')} ({qual.get('aqf_label') or 'level unknown'})")
    return "\n".join(lines)


@app.get("/runs/{run_id}/candidates")
def run_candidates(run_id: str) -> dict[str, Any]:
    """What this run concluded about each candidate. Identity is never included."""
    _require_run(run_id)
    return {"results": state.store.list_results(run_id)}


@app.get("/runs/{run_id}/audit")
def run_audit(
    run_id: str, include_batch: bool = True, candidate_id: str | None = None, limit: int = 2000
) -> dict[str, Any]:
    """This run's decision trail, by default merged with its batch's ingestion events."""
    _require_run(run_id)
    return {
        "entries": state.store.audit_trail(
            run_id=run_id, include_batch=include_batch, candidate_id=candidate_id, limit=limit
        )
    }


def _require_batch(batch_id: str) -> dict[str, Any]:
    batch = state.store.get_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"unknown batch {batch_id!r}")
    return batch


def _require_run(run_id: str) -> dict[str, Any]:
    run = state.store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"unknown run {run_id!r}")
    return run
