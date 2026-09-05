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


# --------------------------------------------------------------------------
# Pass 3 — recruiter rule classification and compilation
# --------------------------------------------------------------------------

CLASSIFY_RULE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["risk", "protected_attributes", "explanation", "suggested_rewrite", "predicate"],
    "properties": {
        "risk": {
            "type": "string",
            "enum": ["none", "review", "high"],
            "description": (
                "'high' if the rule selects on a protected attribute or a proxy for one; "
                "'review' if lawful only with a documented job-based justification; "
                "'none' if it tests capability directly."
            ),
        },
        "protected_attributes": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Protected attributes the rule engages, e.g. 'age', 'national or ethnic origin'.",
        },
        "explanation": _str_or_null(
            "Why this is or is not a risk, in plain language a recruiter can act on."
        ),
        "suggested_rewrite": _str_or_null(
            "A measurable replacement testing the underlying job requirement. Null if the rule is already sound."
        ),
        "predicate": {
            "type": ["object", "null"],
            "additionalProperties": False,
            "required": ["field", "operator", "value", "description"],
            "description": "How to test the rule against a candidate, or null if it cannot be mechanised.",
            "properties": {
                "field": {
                    "type": "string",
                    "enum": [
                        "total_years_experience",
                        "highest_aqf",
                        "skills",
                        "languages",
                        "certifications",
                        "work_rights_unrestricted",
                        "work_rights_status",
                    ],
                },
                "operator": {
                    "type": "string",
                    "enum": ["gte", "lte", "eq", "contains_all", "contains_any", "in", "is_true", "is_false"],
                },
                "value": {
                    "type": ["number", "string", "boolean", "array", "null"],
                    "items": {"type": "string"},
                },
                "description": {"type": "string"},
            },
        },
    },
}

CLASSIFY_RULE_SYSTEM = """You review a recruiter's screening rule under Australian
anti-discrimination law, and where the rule is sound you compile it into a test.

Protected attributes include race, colour, national or ethnic origin, sex,
sexual orientation, gender identity, age, disability, marital or relationship
status, pregnancy, family or carer's responsibilities, religion, political
opinion and social origin.

Judge two things:

1. Risk. A rule is high risk when it selects on a protected attribute or on a
   proxy for one — a neutral-sounding criterion a protected group is less able
   to meet and which is not reasonable for the job. It is 'review' when it may
   be lawful with a documented job-based justification, such as a citizenship
   requirement for a role needing a security clearance. It is 'none' when it
   tests a capability the job actually needs.

2. The underlying requirement. When you flag a rule, say what the recruiter
   most likely needs and express it measurably. Replace 'recent graduate' with a
   band of years of experience; replace 'native English speaker' with a standard
   of communication. Never suggest a rewrite that is the same proxy reworded.

Compile a predicate only when the rule tests capability. Never compile a rule
you rated high risk. Requirements about work rights are lawful and should be
compiled. Qualification requirements compile to an AQF level: a bachelor degree
is 7, honours or a graduate certificate or diploma is 8, a masters is 9, a
doctorate is 10, a diploma is 5.

Return JSON only."""


def classify_rule_user_prompt(rule_text: str, role_context: str | None = None) -> str:
    context = f"\n\nRole context: {role_context}" if role_context else ""
    return f"Review this screening rule.\n\n<rule>\n{rule_text}\n</rule>{context}"


# --------------------------------------------------------------------------
# Pass 4 — triage ranking
# --------------------------------------------------------------------------

RANK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["criteria", "rationale"],
    "properties": {
        "criteria": {
            "type": "array",
            "description": "One entry per criterion supplied, in the same order.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["criterion", "score", "evidence"],
                "properties": {
                    "criterion": {"type": "string"},
                    "score": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 1,
                        "description": "How well the candidate meets this criterion.",
                    },
                    "evidence": {
                        "type": "string",
                        "description": "The structured values this score was read from. Never a bare assertion.",
                    },
                },
            },
        },
        "rationale": {
            "type": "string",
            "description": "Two sentences a reviewer can act on, citing the evidence above.",
        },
    },
}

RANK_SYSTEM = """You score an anonymized candidate against a role, one criterion at a time.

You are given a de-identified profile. It contains no name, no institution name,
no suburb and no graduation year, by design. Do not speculate about any of them,
and do not treat their absence as a negative.

Rules:
- Score each criterion from 0 to 1 on the evidence in the profile alone.
- Every score must cite the structured values it came from: skills listed, years
  of experience, AQF level, role history. Never assert a judgement without
  naming what you read.
- Absence of evidence is a low score for that criterion, not a penalty applied
  across the others.
- Do not reward or penalise work rights status, language background, region, or
  the tier of an institution. Those are either lawful requirements handled
  elsewhere or demographic proxies.
- Career breaks, non-linear histories and overseas experience are not defects.

Return JSON only."""


def rank_user_prompt(profile_json: str, role_json: str, criteria_json: str) -> str:
    return (
        f"Role:\n{role_json}\n\n"
        f"Criteria to score:\n{criteria_json}\n\n"
        f"Anonymized candidate profile:\n{profile_json}"
    )
