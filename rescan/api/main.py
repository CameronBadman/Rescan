"""HTTP API.

Backend only — this exposes the pipeline for a separate frontend to drive. The
upload endpoint returns a job id immediately and the work continues in a
background worker, so a bulk upload never blocks the caller. `/jobs/{id}/status`
is the polling endpoint behind a progress view.
"""

from __future__ import annotations

import json
import logging
import secrets
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
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
    pull_job_documents,
)
from rescan.llm.client import build_client
from rescan.pipeline.query import QueryRejected, run_query
from rescan.pipeline.runner import PipelineRunner
from rescan.rules.classifier import classify_rule, compile_plan
from rescan.schemas import RoleSpec
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


# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------

# Health is public so a load balancer can probe it without a credential.
PUBLIC_PATHS = {"/health", "/docs", "/redoc", "/openapi.json"}


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
    if not settings.api_keys or request.url.path in PUBLIC_PATHS:
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


class BucketJobRequest(BaseModel):
    job_id: str = Field(description="The job's folder in the bucket: objects under <prefix>/<job_id>/ are pulled.")
    role: RoleSpec
    plan: str | None = Field(default=None, description="The recruiter's hiring plan, free text.")
    rules: list[str] = Field(default_factory=list, description="Discrete rules, in addition to or instead of the plan.")
    prefix: str | None = Field(default=None, description="Override the configured bucket prefix for this job.")


class QueryRequest(BaseModel):
    dsl: str = Field(description="A bare expression in the rule language, e.g. years_experience >= 5 AND skills HAS ANY (\"Python\").")
    model_checks: bool = Field(default=True, description="Whether ASK clauses are put to the model. Off, they evaluate to unknown.")


class JobStatusResponse(BaseModel):
    job_id: str
    status: str
    counts: dict[str, int]
    total: int
    processed: int
    created_at: str
    updated_at: str
    error: str | None = None


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
    candidates here — pass the same plan to POST /jobs to screen a batch.
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


@app.post("/jobs", status_code=202)
async def create_job(
    files: list[UploadFile] = File(..., description="Resumes, or a zip archive of them."),
    role: str = Form(..., description="RoleSpec as JSON."),
    rules: str = Form("[]", description="Recruiter rules as a JSON array of strings."),
    plan: str | None = Form(None, description="The recruiter's hiring plan, free text; compiled into rules."),
) -> dict[str, Any]:
    """Accept a bulk upload and start processing. Returns a job id immediately."""
    try:
        role_spec = RoleSpec.model_validate_json(role)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid role: {exc}") from exc
    try:
        rule_texts = json.loads(rules)
        if not isinstance(rule_texts, list):
            raise ValueError("rules must be a JSON array of strings")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid rules: {exc}") from exc

    uploads = [(upload.filename or "unnamed", await upload.read()) for upload in files]
    documents = expand_uploads(uploads)
    if not documents:
        raise HTTPException(status_code=400, detail="no usable documents in the upload")

    job_id = state.runner.create_job(role_spec, documents)
    state.pool.submit(_run_job_safely, job_id, rule_texts, plan)

    return {
        "job_id": job_id,
        "accepted_documents": len(documents),
        "status_url": f"/jobs/{job_id}/status",
    }


@app.post("/jobs/from-bucket", status_code=202)
def create_job_from_bucket(request: BucketJobRequest) -> dict[str, Any]:
    """Start a job from resumes already in the object store.

    Objects under `<prefix>/<job_id>/` are pulled, archives expanded, and the
    job runs exactly as an upload would. The bucket's job id becomes the
    Rescan job id so the frontend can correlate the two without a mapping.
    """
    job_id = request.job_id.strip().strip("/")
    if not job_id or "/" in job_id or job_id.startswith("."):
        raise HTTPException(status_code=400, detail="job_id must be a single path segment")
    if state.store.get_job(job_id) is not None:
        raise HTTPException(status_code=409, detail=f"job {job_id!r} already exists")

    try:
        pull = pull_job_documents(state.object_store, job_id, prefix=request.prefix)
    except ObjectStoreError as exc:
        raise HTTPException(status_code=502, detail=f"object store error: {exc}") from exc
    if not pull.documents:
        raise HTTPException(
            status_code=404,
            detail={"message": f"no usable documents under {pull.prefix!r}", "skipped": pull.skipped},
        )

    state.runner.create_job(request.role, pull.documents, job_id=job_id)
    state.store.audit(
        job_id, "ingestion", "bucket_pulled",
        detail={
            "store": getattr(state.object_store, "name", settings.object_store),
            "prefix": pull.prefix,
            "keys": pull.keys,
            "documents": len(pull.documents),
            "bytes": pull.bytes_pulled,
            "skipped": pull.skipped,
        },
    )
    state.pool.submit(_run_job_safely, job_id, request.rules, request.plan)
    return {
        "job_id": job_id,
        "prefix": pull.prefix,
        "accepted_documents": len(pull.documents),
        "skipped": pull.skipped,
        "status_url": f"/jobs/{job_id}/status",
    }


def _run_job_safely(job_id: str, rule_texts: list[str], plan: str | None = None) -> None:
    try:
        state.runner.run_job(job_id, rule_texts, plan=plan)
    except Exception:
        # run_job already recorded the failure on the job and in the audit log.
        log.exception("background job %s failed", job_id)


@app.get("/jobs")
def list_jobs(limit: int = 50) -> dict[str, Any]:
    return {"jobs": state.store.list_jobs(limit=limit)}


@app.get("/jobs/{job_id}/status", response_model=JobStatusResponse)
def job_status(job_id: str) -> JobStatusResponse:
    """Poll target for a progress view: per-status counts as they change."""
    job = _require_job(job_id)
    counts = state.store.status_counts(job_id)
    total = sum(counts.values())
    settled = {"complete", "failed", "needs_manual_review", "duplicate"}
    return JobStatusResponse(
        job_id=job_id,
        status=job["status"],
        counts=counts,
        total=total,
        processed=sum(count for status, count in counts.items() if status in settled),
        created_at=job["created_at"],
        updated_at=job["updated_at"],
        error=job["error"],
    )


@app.get("/jobs/{job_id}/rules")
def job_rules(job_id: str) -> dict[str, Any]:
    job = _require_job(job_id)
    return job["rules"] or {"rules": []}


@app.get("/jobs/{job_id}/shortlist")
def job_shortlist(job_id: str, reattach_identity: bool = True) -> dict[str, Any]:
    """The shortlist, with identity re-attached for the human reviewer.

    Full anonymization is correct for the machine passes but backfires for human
    reviewers, so names come back at this step. Pass reattach_identity=false to
    review the anonymized view.
    """
    job = _require_job(job_id)
    shortlist = job["shortlist"]
    if shortlist is None:
        raise HTTPException(status_code=409, detail=f"job {job_id} has no shortlist yet")
    if not reattach_identity:
        return shortlist

    identities = {
        candidate["candidate_ref"]: (candidate.get("structured") or {}).get("identity", {})
        for candidate in state.store.list_candidates(job_id)
        if candidate.get("candidate_ref")
    }
    for section in ("entries", "below_cutoff"):
        for entry in shortlist.get(section, []):
            entry["identity"] = identities.get(entry["candidate_ref"])
    for section in ("excluded", "manual_review"):
        for entry in shortlist.get(section, []):
            entry["identity"] = identities.get(entry["candidate_ref"])
    return shortlist


@app.get("/jobs/{job_id}/candidates")
def job_candidates(job_id: str, include_identity: bool = False) -> dict[str, Any]:
    _require_job(job_id)
    candidates = state.store.list_candidates(job_id)
    if not include_identity:
        for candidate in candidates:
            candidate.pop("structured", None)
            candidate.pop("filename", None)
    return {"candidates": candidates}


@app.post("/jobs/{job_id}/query")
def job_query(job_id: str, request: QueryRequest) -> dict[str, Any]:
    """Run a query in the rule language over the job's anonymized profiles.

    Returns matched / not matched / indeterminate candidates, each with the
    plain-language reason. Identity is never returned here. The query passes
    the same legal gate as a rule and is written to the audit trail.
    """
    _require_job(job_id)
    judge = state.runner._judge_for(job_id, None, ensemble=False) if request.model_checks else None
    try:
        return run_query(state.store, job_id, request.dsl, judge=judge)
    except DslError as exc:
        raise HTTPException(status_code=422, detail=exc.to_dict()) from exc
    except QueryRejected as exc:
        raise HTTPException(status_code=422, detail=exc.to_dict()) from exc


@app.get("/jobs/{job_id}/audit")
def job_audit(job_id: str, candidate_id: str | None = None, limit: int = 2000) -> dict[str, Any]:
    """Full decision trail: rules applied, rules flagged, redactions, scores."""
    _require_job(job_id)
    return {"entries": state.store.audit_trail(job_id, candidate_id=candidate_id, limit=limit)}


def _require_job(job_id: str) -> dict[str, Any]:
    job = state.store.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"unknown job {job_id!r}")
    return job
