"""Prompts and guided-decoding schemas for each model pass.

Schemas are hand-written rather than derived from the Pydantic models because
the model must not populate every field. AQF levels in particular are excluded:
they are assigned deterministically by `rescan.aqf` so that the same
qualification always maps to the same level for every candidate.
"""

from __future__ import annotations

from typing import Any


def _str_or_null(description: str) -> dict[str, Any]:
    return {"type": ["string", "null"], "description": description}


# --------------------------------------------------------------------------
# Pass 1 — structuring
# --------------------------------------------------------------------------

STRUCTURE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "identity",
        "summary",
        "skills",
        "experience",
        "total_years_experience",
        "qualifications",
        "universities",
        "work_rights",
        "languages",
        "affiliations",
        "certifications",
        "extraction_notes",
    ],
    "properties": {
        "identity": {
            "type": "object",
            "additionalProperties": False,
            "required": ["full_name", "email", "phone", "suburb", "state", "country", "links"],
            "properties": {
                "full_name": _str_or_null("Candidate's full name exactly as written."),
                "email": _str_or_null("Primary email address."),
                "phone": _str_or_null("Primary phone number."),
                "suburb": _str_or_null("Suburb or city of residence."),
                "state": _str_or_null("State or province, e.g. QLD."),
                "country": _str_or_null("Country of residence."),
                "links": {"type": "array", "items": {"type": "string"}},
            },
        },
        "summary": _str_or_null("Two-sentence factual precis of the candidate's background."),
        "skills": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "category", "years", "evidence"],
                "properties": {
                    "name": {"type": "string"},
                    "category": {
                        "type": "string",
                        "enum": ["technical", "domain", "language", "tool", "soft", "other"],
                    },
                    "years": {"type": ["number", "null"]},
                    "evidence": _str_or_null("Where in the resume this skill was demonstrated."),
                },
            },
        },
        "experience": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["title", "employer", "start", "end", "is_current", "months", "summary"],
                "properties": {
                    "title": _str_or_null("Role title."),
                    "employer": _str_or_null("Employer name."),
                    "start": _str_or_null("Start date as YYYY-MM when known, else YYYY."),
                    "end": _str_or_null("End date as YYYY-MM, or null if current."),
                    "is_current": {"type": "boolean"},
                    "months": {"type": ["number", "null"], "description": "Duration in months."},
                    "summary": _str_or_null("One line on what the person did."),
                },
            },
        },
        "total_years_experience": {
            "type": ["number", "null"],
            "description": "Total professional experience in years. Do not count study.",
        },
        "qualifications": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["title", "institution", "field_of_study", "completion_year", "country"],
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Award title verbatim, e.g. 'Bachelor of Engineering (Honours)'.",
                    },
                    "institution": _str_or_null("Awarding institution."),
                    "field_of_study": _str_or_null("Discipline."),
                    "completion_year": {"type": ["integer", "null"]},
                    "country": _str_or_null("Country the award was issued in."),
                },
            },
        },
        "universities": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Every university or tertiary institution named, as its own list.",
        },
        "work_rights": {
            "type": "object",
            "additionalProperties": False,
            "required": ["status", "visa_subclass", "unrestricted", "evidence"],
            "properties": {
                "status": {
                    "type": "string",
                    "enum": [
                        "citizen",
                        "permanent_resident",
                        "visa_unrestricted",
                        "visa_restricted",
                        "requires_sponsorship",
                        "unknown",
                    ],
                },
                "visa_subclass": _str_or_null("Visa subclass number if stated, e.g. '482'."),
                "unrestricted": {"type": ["boolean", "null"]},
                "evidence": _str_or_null("Verbatim text this was inferred from."),
            },
        },
        "languages": {"type": "array", "items": {"type": "string"}},
        "affiliations": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Clubs, societies, memberships, volunteer organisations.",
        },
        "certifications": {"type": "array", "items": {"type": "string"}},
        "extraction_notes": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Anything ambiguous or unreadable a human should check.",
        },
    },
}

STRUCTURE_SYSTEM = """You extract structured data from resumes for an Australian recruitment system.

Rules:
- Extract only what the document states. Never infer, guess or embellish.
- Use null for anything absent. An empty field is correct; an invented one is not.
- Copy qualification titles verbatim. Do not translate them to Australian
  equivalents and do not assign a level — a separate deterministic step does that.
- List every university in the `universities` field as well as on the qualification.
- For work rights, only record a status the document supports. If it is silent,
  use "unknown". Never infer work rights or visa status from a person's name,
  country of education, or country of previous employment.
- Record total_years_experience from employment dates, excluding study.
- Put anything ambiguous or unreadable into extraction_notes.

Return JSON only."""


def structure_user_prompt(resume_text: str) -> str:
    return f"Extract structured data from this resume.\n\n<resume>\n{resume_text}\n</resume>"


# --------------------------------------------------------------------------
# Pass 2 — anonymization
# --------------------------------------------------------------------------

ANONYMIZE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "institution_tiers", "region", "job_relevant_affiliations", "redactions"],
    "properties": {
        "summary": _str_or_null("The candidate summary rewritten with every identity signal removed."),
        "institution_tiers": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "One entry per institution, generalised to a category such as "
                "'Australian university (Group of Eight)', 'Australian university', "
                "'overseas university', 'Australian TAFE / VET provider'. Never a name."
            ),
        },
        "region": _str_or_null(
            "Location generalised to a broad region such as 'Greater Brisbane' or "
            "'regional Queensland'. Never a suburb."
        ),
        "job_relevant_affiliations": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Only affiliations that evidence professional capability, e.g. "
                "'IEEE member'. Drop anything signalling ethnicity, religion, "
                "sex, national origin, disability or political belief."
            ),
        },
        "redactions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["field", "action", "reason"],
                "properties": {
                    "field": {"type": "string"},
                    "action": {
                        "type": "string",
                        "enum": ["removed", "generalised", "regionalised", "kept"],
                    },
                    "reason": {"type": "string"},
                },
            },
        },
    },
}

ANONYMIZE_SYSTEM = """You remove identity and demographic proxies from a candidate profile
before it is ranked, so that ranking sees capability only.

Remove or generalise:
- Names, emails, phone numbers, personal links.
- Institution names -> a tier or category. Research shows university name
  reintroduces demographic signal after names are removed.
- Suburb -> broad region. Suburb is a strong socio-economic and ethnic proxy.
- Affiliations that signal ethnicity, religion, sex, national origin, disability
  or political belief. Keep only affiliations evidencing professional capability.

Preserve exactly, because they are job-relevant:
- Skills, technologies, and the evidence for them.
- Role titles, employer *industry* where it matters, durations and achievements.
- Qualification level and field of study.
- Work rights status — a lawful requirement, not a demographic proxy.
- Languages, which are a job-relevant capability.

Never invent capability the candidate did not demonstrate. Rewriting must not
strengthen or weaken the candidate, only de-identify them. Record one redaction
entry per action taken, with the reason.

Return JSON only."""


def anonymize_user_prompt(profile_json: str) -> str:
    return (
        "De-identify this candidate profile.\n\n"
        f"<profile>\n{profile_json}\n</profile>"
    )
