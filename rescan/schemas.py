"""Canonical data contracts.

`StructuredResume` is the ground truth for every downstream decision. Rules,
ranking and audit entries all reference fields on this object, so a decision can
always be traced back to a specific extracted value.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# Structuring pass output
# --------------------------------------------------------------------------


class WorkRightsStatus(str, Enum):
    CITIZEN = "citizen"
    PERMANENT_RESIDENT = "permanent_resident"
    VISA_UNRESTRICTED = "visa_unrestricted"
    VISA_RESTRICTED = "visa_restricted"
    REQUIRES_SPONSORSHIP = "requires_sponsorship"
    UNKNOWN = "unknown"


class Identity(BaseModel):
    """Direct identifiers. Never reaches the ranking passes."""

    full_name: str | None = None
    email: str | None = None
    phone: str | None = None
    suburb: str | None = None
    state: str | None = None
    country: str | None = None
    links: list[str] = Field(default_factory=list)


class Skill(BaseModel):
    name: str
    category: Literal["technical", "domain", "language", "tool", "soft", "other"] = "other"
    years: float | None = None
    evidence: str | None = Field(
        default=None, description="Where in the resume this skill was demonstrated."
    )


class Experience(BaseModel):
    title: str | None = None
    employer: str | None = None
    start: str | None = Field(default=None, description="ISO-ish date as written, e.g. 2021-03.")
    end: str | None = None
    is_current: bool = False
    months: float | None = None
    summary: str | None = None


class Qualification(BaseModel):
    title: str
    institution: str | None = None
    field_of_study: str | None = None
    completion_year: int | None = None
    country: str | None = None
    # Filled deterministically by rescan.aqf, never by the model.
    aqf_level: int | None = None
    aqf_label: str | None = None
    aqf_confidence: float = 0.0


class WorkRights(BaseModel):
    status: WorkRightsStatus = WorkRightsStatus.UNKNOWN
    visa_subclass: str | None = None
    unrestricted: bool | None = None
    evidence: str | None = Field(
        default=None, description="Verbatim text the status was inferred from."
    )


class StructuredResume(BaseModel):
    """Full, identified structuring of one resume."""

    identity: Identity = Field(default_factory=Identity)
    summary: str | None = None
    skills: list[Skill] = Field(default_factory=list)
    experience: list[Experience] = Field(default_factory=list)
    total_years_experience: float | None = None
    qualifications: list[Qualification] = Field(default_factory=list)
    # Kept as its own field so anonymization can generalise it without
    # disturbing the qualification level it is attached to.
    universities: list[str] = Field(default_factory=list)
    work_rights: WorkRights = Field(default_factory=WorkRights)
    languages: list[str] = Field(default_factory=list)
    affiliations: list[str] = Field(
        default_factory=list,
        description="Clubs, societies, memberships. Reviewed for job-relevance when anonymizing.",
    )
    certifications: list[str] = Field(default_factory=list)
    extraction_notes: list[str] = Field(default_factory=list)

    @property
    def highest_aqf(self) -> int | None:
        levels = [q.aqf_level for q in self.qualifications if q.aqf_level is not None]
        return max(levels) if levels else None


# --------------------------------------------------------------------------
# Anonymization pass output
# --------------------------------------------------------------------------


class Redaction(BaseModel):
    """One anonymization action, recorded so the audit log can explain it."""

    field: str
    action: Literal["removed", "generalised", "regionalised", "kept"]
    reason: str
    original_present: bool = True


class AnonymizedProfile(BaseModel):
    """Identity-stripped copy. This is the only view the ranking passes see."""

    candidate_ref: str = Field(description="Opaque handle, e.g. 'Candidate 7'.")
    summary: str | None = None
    skills: list[Skill] = Field(default_factory=list)
    experience: list[Experience] = Field(default_factory=list)
    total_years_experience: float | None = None
    qualifications: list[Qualification] = Field(default_factory=list)
    # e.g. "Australian university (Group of Eight)" — never the institution name.
    institution_tiers: list[str] = Field(default_factory=list)
    region: str | None = Field(default=None, description="Generalised location, e.g. 'Greater Brisbane'.")
    work_rights: WorkRights = Field(default_factory=WorkRights)
    languages: list[str] = Field(default_factory=list)
    job_relevant_affiliations: list[str] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)
    redactions: list[Redaction] = Field(default_factory=list)

    @property
    def highest_aqf(self) -> int | None:
        levels = [q.aqf_level for q in self.qualifications if q.aqf_level is not None]
        return max(levels) if levels else None


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------


class ExtractionResult(BaseModel):
    text: str = ""
    content_type: str | None = None
    page_count: int | None = None
    backend: str = Field(default="unknown", description="tika | pypdf | docx | plaintext | ocr")
    ocr_used: bool = False
    char_count: int = 0
    warnings: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Job / candidate lifecycle
# --------------------------------------------------------------------------


class CandidateStatus(str, Enum):
    PENDING = "pending"
    EXTRACTING = "extracting"
    STRUCTURING = "structuring"
    ANONYMIZING = "anonymizing"
    SCREENING = "screening"
    RANKING = "ranking"
    COMPLETE = "complete"
    FAILED = "failed"
    NEEDS_MANUAL_REVIEW = "needs_manual_review"
    DUPLICATE = "duplicate"


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"


class AuditEntry(BaseModel):
    at: datetime = Field(default_factory=_now)
    stage: str
    event: str
    detail: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------
# Role specification and ranking
# --------------------------------------------------------------------------


class RoleSpec(BaseModel):
    """What the recruiter is hiring for. Rules are held separately."""

    title: str
    description: str | None = None
    required_skills: list[str] = Field(default_factory=list)
    desirable_skills: list[str] = Field(default_factory=list)
    min_years_experience: float | None = None
    min_aqf: int | None = None


class CriterionScore(BaseModel):
    criterion: str
    score: float = Field(ge=0.0, le=1.0)
    weight: float = 1.0
    evidence: str = Field(description="The structured values this score was read from.")


class CandidateScore(BaseModel):
    candidate_ref: str
    score: float = Field(ge=0.0, le=1.0)
    criteria: list[CriterionScore] = Field(default_factory=list)
    rationale: str = ""
    model: str = "unknown"
    pass_name: Literal["triage", "ensemble"] = "triage"
    # Populated only for candidates the ensemble pass re-scored.
    ensemble_votes: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def weighted_breakdown(self) -> list[tuple[str, float]]:
        return [(c.criterion, c.score * c.weight) for c in self.criteria]


class ShortlistEntry(BaseModel):
    rank: int
    candidate_ref: str
    score: float
    rationale: str
    borderline: bool = False
    criteria: list[CriterionScore] = Field(default_factory=list)


class Shortlist(BaseModel):
    role_title: str
    entries: list[ShortlistEntry] = Field(default_factory=list)
    below_cutoff: list[ShortlistEntry] = Field(default_factory=list)
    excluded: list[dict[str, Any]] = Field(default_factory=list)
    manual_review: list[dict[str, Any]] = Field(default_factory=list)
