"""Pass 1 — turn extracted resume text into the structured ground truth.

The model produces fields; this module owns everything that must be identical
for every candidate. AQF levels are assigned here from `rescan.aqf`, never by
the model, so two candidates holding the same award always get the same level.
"""

from __future__ import annotations

import logging
import re

from pydantic import ValidationError

from rescan.aqf import map_to_aqf
from rescan.llm.client import LLMClient, LLMError, LLMRequest
from rescan.llm.prompts import STRUCTURE_SCHEMA, STRUCTURE_SYSTEM, structure_user_prompt
from rescan.schemas import ExtractionResult, StructuredResume

log = logging.getLogger(__name__)

# Guards against a truncated or near-empty document reaching the model.
MIN_USABLE_CHARS = 80
# Keeps a pathological document from blowing the context window.
MAX_RESUME_CHARS = 24_000


class StructuringError(RuntimeError):
    pass


# Closed vocabularies the model must land in. A near miss ("fluent" for a
# proficiency, "Senior" with a capital) must not fail the whole resume: the
# value is coerced or dropped and the document goes on. Only a document the
# model could not structure at all is an error.
_ENUMS: dict[str, tuple[tuple[str, ...], str | None]] = {
    "skill.category": (("technical", "domain", "language", "tool", "soft", "other"), "other"),
    "skill.proficiency": (("beginner", "intermediate", "advanced", "expert"), None),
    "role.seniority": (("intern", "graduate", "junior", "mid", "senior", "lead", "principal",
                        "manager", "head", "director", "executive"), None),
    "role.employment_type": (("permanent", "contract", "casual", "internship", "freelance", "volunteer", "other"), None),
    "work_rights.status": (("citizen", "permanent_resident", "visa_unrestricted", "visa_restricted",
                            "requires_sponsorship", "unknown"), "unknown"),
}
_SYNONYMS: dict[str, dict[str, str]] = {
    "skill.proficiency": {"fluent": "advanced", "native": "expert", "proficient": "advanced",
                          "basic": "beginner", "novice": "beginner", "working": "intermediate",
                          "professional": "advanced", "strong": "advanced", "senior": "expert"},
    "role.seniority": {"sr": "senior", "jr": "junior", "staff": "principal", "architect": "principal",
                       "vp": "executive", "chief": "executive", "c-level": "executive", "associate": "junior",
                       "entry": "junior", "entry-level": "junior", "trainee": "intern", "apprentice": "intern"},
    "role.employment_type": {"full-time": "permanent", "full time": "permanent", "part-time": "permanent",
                             "part time": "permanent", "fixed-term": "contract", "contractor": "contract",
                             "consultant": "freelance", "intern": "internship", "temp": "casual", "temporary": "casual"},
    "skill.category": {"programming": "technical", "software": "technical", "framework": "tool",
                       "platform": "tool", "cloud": "tool", "database": "tool", "interpersonal": "soft",
                       "communication": "soft", "spoken": "language", "languages": "language"},
}


def _coerce(key: str, value: object, notes: list[str], where: str) -> object:
    allowed, fallback = _ENUMS[key]
    if value is None:
        return None if fallback != "unknown" else fallback
    raw = str(value).strip()
    lowered = raw.lower()
    snake = re.sub(r"[\s\-]+", "_", lowered)
    kebab = re.sub(r"[\s_]+", "-", lowered)
    if snake in allowed:
        return snake
    if lowered in allowed:
        return lowered
    synonyms = _SYNONYMS.get(key, {})
    mapped = synonyms.get(lowered) or synonyms.get(kebab) or synonyms.get(snake)
    if mapped:
        return mapped
    notes.append(f"{where}: {key.split('.')[-1]} {raw!r} is not a recognised value; recorded as {fallback!r}.")
    return fallback


def normalise_model_output(data: dict) -> dict:
    """Coerce near-miss enum values and drop unusable entries, in place."""
    if not isinstance(data, dict):
        return data
    notes = data.setdefault("extraction_notes", [])
    if not isinstance(notes, list):
        notes = data["extraction_notes"] = []

    skills = [s for s in data.get("skills") or [] if isinstance(s, dict) and str(s.get("name") or "").strip()]
    for skill in skills:
        skill["category"] = _coerce("skill.category", skill.get("category"), notes, f"skill {skill['name']!r}")
        skill["proficiency"] = _coerce("skill.proficiency", skill.get("proficiency"), notes, f"skill {skill['name']!r}")
        if skill.get("years") is not None:
            try:
                skill["years"] = float(skill["years"])
            except (TypeError, ValueError):
                skill["years"] = None
    data["skills"] = skills

    roles = [r for r in data.get("experience") or [] if isinstance(r, dict)]
    for role in roles:
        label = f"role {role.get('title') or '?'!r}"
        role["seniority"] = _coerce("role.seniority", role.get("seniority"), notes, label)
        role["employment_type"] = _coerce("role.employment_type", role.get("employment_type"), notes, label)
        for numeric in ("months", "team_size"):
            if role.get(numeric) is not None:
                try:
                    role[numeric] = float(role[numeric]) if numeric == "months" else int(float(role[numeric]))
                except (TypeError, ValueError):
                    role[numeric] = None
        role["is_current"] = bool(role.get("is_current"))
        role["technologies"] = [str(t) for t in role.get("technologies") or [] if t]
    data["experience"] = roles

    data["qualifications"] = [
        q for q in data.get("qualifications") or [] if isinstance(q, dict) and str(q.get("title") or "").strip()
    ]
    for qualification in data["qualifications"]:
        year = qualification.get("completion_year")
        if year is not None:
            try:
                qualification["completion_year"] = int(float(year))
            except (TypeError, ValueError):
                qualification["completion_year"] = None

    rights = data.get("work_rights")
    if not isinstance(rights, dict):
        rights = data["work_rights"] = {}
    rights["status"] = _coerce("work_rights.status", rights.get("status"), notes, "work rights")

    for numeric in ("total_years_experience", "management_years", "availability_weeks"):
        if data.get(numeric) is not None:
            try:
                data[numeric] = float(data[numeric])
            except (TypeError, ValueError):
                data[numeric] = None
    for integer in ("people_managed_max", "publications_count"):
        if data.get(integer) is not None:
            try:
                data[integer] = int(float(data[integer]))
            except (TypeError, ValueError):
                data[integer] = None
    for listy in ("universities", "languages", "affiliations", "certifications", "licences"):
        data[listy] = [str(v) for v in data.get(listy) or [] if v]
    data["projects"] = [pr for pr in data.get("projects") or [] if isinstance(pr, dict) and str(pr.get("name") or "").strip()]
    return data


def apply_aqf(resume: StructuredResume) -> StructuredResume:
    """Assign AQF level deterministically to every qualification."""
    for qualification in resume.qualifications:
        level, label, confidence = map_to_aqf(qualification.title)
        qualification.aqf_level = level
        qualification.aqf_label = label
        qualification.aqf_confidence = confidence
        if level is None:
            resume.extraction_notes.append(
                f"Qualification {qualification.title!r} has no AQF equivalent mapping; "
                "needs manual confirmation."
            )
    return resume


def structure_resume(
    extraction: ExtractionResult,
    client: LLMClient,
    *,
    model: str | None = None,
) -> StructuredResume:
    """Structure one resume. Raises StructuringError when the text is unusable."""
    text = extraction.text.strip()
    if len(text) < MIN_USABLE_CHARS:
        raise StructuringError(
            f"only {len(text)} characters recovered; document needs manual review"
        )
    if len(text) > MAX_RESUME_CHARS:
        log.warning("resume truncated from %d to %d chars", len(text), MAX_RESUME_CHARS)
        text = text[:MAX_RESUME_CHARS]

    request = LLMRequest(
        task="structure",
        system=STRUCTURE_SYSTEM,
        user=structure_user_prompt(text),
        schema=STRUCTURE_SCHEMA,
        model=model,
        max_tokens=6144,
        context={"resume_text": text},
    )

    try:
        response = client.json_call(request)
    except LLMError as exc:
        raise StructuringError(f"structuring failed: {exc}") from exc

    try:
        resume = StructuredResume.model_validate(normalise_model_output(response.data))
    except ValidationError as exc:
        raise StructuringError(f"model returned data that does not fit the schema: {exc}") from exc

    resume = apply_aqf(resume)

    if extraction.ocr_used:
        resume.extraction_notes.append(
            "Text recovered by OCR; extraction fidelity is lower than a native document."
        )
    for warning in extraction.warnings:
        resume.extraction_notes.append(f"extraction: {warning}")

    return resume
