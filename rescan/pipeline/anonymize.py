"""Pass 2 — produce the identity-stripped profile that ranking sees.

Two things happen here, and the split matters:

* The model rewrites free text, generalises institutions to tiers, regionalises
  location and triages affiliations for job-relevance.
* Everything that constitutes *capability* — skills, role history, durations,
  qualification level, work rights, languages — is copied across in code.

The model is never given the opportunity to restate capability, because a
rewrite that strengthens or weakens a candidate would reintroduce exactly the
bias this pass exists to remove. A deterministic scrub and a leak check run
afterwards, so de-identification does not depend on the model behaving.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Iterable

from pydantic import ValidationError

from rescan.llm.client import LLMClient, LLMError, LLMRequest
from rescan.llm.prompts import ANONYMIZE_SCHEMA, ANONYMIZE_SYSTEM, anonymize_user_prompt
from rescan.schemas import AnonymizedProfile, Qualification, Redaction, StructuredResume

log = logging.getLogger(__name__)

REDACTED = "[redacted]"

# Short tokens are skipped when scrubbing: a two-letter name fragment matches
# far too much ordinary text to remove safely.
MIN_SCRUB_TOKEN = 3


class AnonymizationError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Deterministic scrubbing
# --------------------------------------------------------------------------


def identity_tokens(resume: StructuredResume) -> list[str]:
    """Literal strings that must not survive into the anonymized profile."""
    identity = resume.identity
    tokens: list[str] = []

    if identity.full_name:
        tokens.append(identity.full_name)
        tokens.extend(
            part for part in re.split(r"[\s,'-]+", identity.full_name) if len(part) >= MIN_SCRUB_TOKEN
        )
    for value in (identity.email, identity.phone, identity.suburb):
        if value:
            tokens.append(value)
    tokens.extend(identity.links)
    tokens.extend(resume.universities)
    for qualification in resume.qualifications:
        if qualification.institution:
            tokens.append(qualification.institution)

    # Longest first, so "University of Queensland" is removed before "Queensland".
    unique = {token.strip() for token in tokens if token and token.strip()}
    return sorted(unique, key=len, reverse=True)


def scrub_text(text: str | None, tokens: Iterable[str]) -> str | None:
    if not text:
        return text
    scrubbed = text
    for token in tokens:
        scrubbed = re.sub(rf"\b{re.escape(token)}\b", REDACTED, scrubbed, flags=re.IGNORECASE)
    # Collapse runs left behind by adjacent removals.
    scrubbed = re.sub(rf"(?:{re.escape(REDACTED)}[\s,]*){{2,}}", f"{REDACTED} ", scrubbed)
    return scrubbed.strip()


def leak_check(resume: StructuredResume, profile: AnonymizedProfile) -> list[str]:
    """Return every identity token still present in the anonymized profile.

    Runs on the serialised profile so no field can be forgotten as the schema
    grows. An empty list is the only acceptable result before ranking.
    """
    serialised = profile.model_dump_json(exclude={"redactions"})
    leaks = []
    for token in identity_tokens(resume):
        if len(token) < MIN_SCRUB_TOKEN:
            continue
        if re.search(rf"\b{re.escape(token)}\b", serialised, flags=re.IGNORECASE):
            leaks.append(token)
    return leaks


# --------------------------------------------------------------------------
# Main pass
# --------------------------------------------------------------------------


def _anonymized_qualifications(resume: StructuredResume) -> tuple[list[Qualification], list[Redaction]]:
    """Copy qualifications, keeping level and field but dropping identifying detail."""
    qualifications: list[Qualification] = []
    redactions: list[Redaction] = []
    for source in resume.qualifications:
        qualifications.append(
            Qualification(
                title=source.title,
                institution=None,
                field_of_study=source.field_of_study,
                completion_year=None,
                country=None,
                aqf_level=source.aqf_level,
                aqf_label=source.aqf_label,
                aqf_confidence=source.aqf_confidence,
            )
        )
        if source.institution:
            redactions.append(
                Redaction(
                    field="qualifications.institution",
                    action="removed",
                    reason="Institution name is a demographic proxy; retained only as a tier.",
                )
            )
        if source.country:
            redactions.append(
                Redaction(
                    field="qualifications.country",
                    action="removed",
                    reason="Country of award is a proxy for national origin (Racial Discrimination Act 1975).",
                )
            )
        if source.completion_year:
            redactions.append(
                Redaction(
                    field="qualifications.completion_year",
                    action="removed",
                    reason=(
                        "Graduation year is a proxy for age (Age Discrimination Act 2004). "
                        "Recency is represented by total years of experience instead."
                    ),
                )
            )
    return qualifications, redactions


def charged_employers(data: dict[str, Any]) -> dict[str, str]:
    """Employers the model judged to reveal a protected attribute, keyed by
    lower-cased name, with the model's reason. Silence keeps every employer."""
    charged: dict[str, str] = {}
    for entry in data.get("employers_to_remove") or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("employer") or "").strip()
        if not name:
            continue
        reason = str(entry.get("reason") or "").strip() or "signals a protected attribute."
        charged[name.lower()] = reason if reason.endswith(".") else reason + "."
    return charged


def anonymize_resume(
    resume: StructuredResume,
    client: LLMClient,
    *,
    candidate_ref: str,
    model: str | None = None,
) -> AnonymizedProfile:
    """Produce the ranking view of a candidate. Raises if identity survives."""
    # The model sees the profile minus direct identifiers: it needs the
    # institution names to tier them, but never the name, email or phone.
    payload: dict[str, Any] = resume.model_dump(mode="json")
    payload["identity"] = {
        "suburb": resume.identity.suburb,
        "state": resume.identity.state,
        "country": resume.identity.country,
    }

    request = LLMRequest(
        task="anonymize",
        system=ANONYMIZE_SYSTEM,
        user=anonymize_user_prompt(_dump_for_prompt(payload)),
        schema=ANONYMIZE_SCHEMA,
        model=model,
        context={"profile": resume.model_dump(mode="json")},
    )

    try:
        response = client.json_call(request)
    except LLMError as exc:
        raise AnonymizationError(f"anonymization failed: {exc}") from exc

    data = response.data
    tokens = identity_tokens(resume)

    qualifications, qualification_redactions = _anonymized_qualifications(resume)

    # Capability is copied, never restated by the model. Free text within it is
    # still scrubbed, because a role summary can name the candidate.
    skills = [skill.model_copy(deep=True) for skill in resume.skills]
    for skill in skills:
        skill.evidence = scrub_text(skill.evidence, tokens)

    # Employer names are kept: where someone worked is capability-relevant
    # context. The model reviews them and names any whose identity itself
    # reveals a protected attribute — a party, a union, a religious body, an
    # ethnic or advocacy organisation. Those are dropped here, with the reason.
    charged = charged_employers(data)
    experience = [role.model_copy(deep=True) for role in resume.experience]
    for role in experience:
        role.summary = scrub_text(role.summary, tokens)
        if role.employer and role.employer.strip().lower() in charged:
            qualification_redactions.append(
                Redaction(
                    field="experience.employer",
                    action="removed",
                    reason=(
                        f"Employer {role.employer!r} removed: {charged[role.employer.strip().lower()]} "
                        "Industry, title and duration are kept."
                    ),
                )
            )
            role.employer = None
        else:
            role.employer = scrub_text(role.employer, tokens)

    projects = [project.model_copy(deep=True) for project in resume.projects]
    for project in projects:
        project.summary = scrub_text(project.summary, tokens)

    redactions = [Redaction(**entry) for entry in data.get("redactions", []) if isinstance(entry, dict)]
    redactions.extend(qualification_redactions)

    profile = AnonymizedProfile(
        candidate_ref=candidate_ref,
        summary=scrub_text(data.get("summary"), tokens),
        skills=skills,
        experience=experience,
        total_years_experience=resume.total_years_experience,
        qualifications=qualifications,
        institution_tiers=list(data.get("institution_tiers") or []),
        region=data.get("region"),
        work_rights=resume.work_rights.model_copy(deep=True),
        languages=list(resume.languages),
        job_relevant_affiliations=list(data.get("job_relevant_affiliations") or []),
        certifications=list(resume.certifications),
        projects=projects,
        licences=list(resume.licences),
        security_clearance=resume.security_clearance,
        management_years=resume.management_years,
        people_managed_max=resume.people_managed_max,
        publications_count=resume.publications_count,
        availability_weeks=resume.availability_weeks,
        redactions=redactions,
    )

    # Work-rights evidence is quoted verbatim from the resume and routinely
    # contains the candidate's name, so it is scrubbed rather than dropped:
    # the status is a lawful requirement and must stay reviewable.
    profile.work_rights.evidence = scrub_text(profile.work_rights.evidence, tokens)

    leaks = leak_check(resume, profile)
    if leaks:
        # Do not fall through to ranking with identity attached.
        raise AnonymizationError(
            f"identity survived anonymization for {candidate_ref}: {leaks[:5]}"
        )

    return profile


def _dump_for_prompt(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, indent=2, default=str)
