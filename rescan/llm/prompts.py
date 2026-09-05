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
        "projects",
        "licences",
        "security_clearance",
        "management_years",
        "people_managed_max",
        "publications_count",
        "availability_weeks",
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
                "required": ["name", "category", "years", "proficiency", "evidence"],
                "properties": {
                    "name": {"type": "string"},
                    "category": {
                        "type": "string",
                        "enum": ["technical", "domain", "language", "tool", "soft", "other"],
                    },
                    "years": {"type": ["number", "null"]},
                    "proficiency": {
                        "type": ["string", "null"],
                        "enum": ["beginner", "intermediate", "advanced", "expert", None],
                        "description": "Only when the resume states a level.",
                    },
                    "evidence": _str_or_null("Where in the resume this skill was demonstrated."),
                },
            },
        },
        "experience": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "title", "employer", "start", "end", "is_current", "months", "summary",
                    "seniority", "industry", "employment_type", "team_size", "technologies",
                ],
                "properties": {
                    "title": _str_or_null("Role title."),
                    "employer": _str_or_null("Employer name."),
                    "start": _str_or_null("Start date as YYYY-MM when known, else YYYY."),
                    "end": _str_or_null("End date as YYYY-MM, or null if current."),
                    "is_current": {"type": "boolean"},
                    "months": {"type": ["number", "null"], "description": "Duration in months."},
                    "summary": _str_or_null("One line on what the person did."),
                    "seniority": {
                        "type": ["string", "null"],
                        "enum": [
                            "intern", "graduate", "junior", "mid", "senior", "lead", "principal",
                            "manager", "head", "director", "executive", None,
                        ],
                        "description": "Level implied by the title only; null if unclear.",
                    },
                    "industry": _str_or_null("Sector of the employer, e.g. 'banking', 'health', 'SaaS'."),
                    "employment_type": {
                        "type": ["string", "null"],
                        "enum": ["permanent", "contract", "casual", "internship", "freelance", "volunteer", "other", None],
                    },
                    "team_size": {
                        "type": ["integer", "null"],
                        "description": "People managed or led in this role, only when stated.",
                    },
                    "technologies": {"type": "array", "items": {"type": "string"}},
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
        "projects": {
            "type": "array",
            "description": "Named projects, open-source work, theses.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "summary", "technologies", "months"],
                "properties": {
                    "name": {"type": "string"},
                    "summary": _str_or_null("One line on what was built."),
                    "technologies": {"type": "array", "items": {"type": "string"}},
                    "months": {"type": ["number", "null"]},
                },
            },
        },
        "licences": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Licences and registrations, e.g. driver's licence, forklift, AHPRA registration.",
        },
        "security_clearance": _str_or_null("Security clearance held if stated, e.g. 'Baseline', 'NV1'."),
        "management_years": {
            "type": ["number", "null"],
            "description": "Years in roles that managed people, from the role history.",
        },
        "people_managed_max": {
            "type": ["integer", "null"],
            "description": "Largest team the candidate reports managing or leading.",
        },
        "publications_count": {"type": ["integer", "null"]},
        "availability_weeks": {"type": ["number", "null"], "description": "Notice period in weeks, if stated."},
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
- Seniority comes from the title only ('Senior Engineer' -> senior, 'Head of
  Data' -> head); leave it null when the title does not say. team_size,
  management_years and people_managed_max only when the resume states a number.
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
# Pass 3 — compile a recruiter's plan into the rule language
# --------------------------------------------------------------------------

from rescan.dsl.fields import reference_text as _dsl_reference  # noqa: E402
from rescan.rules.statutes import PROTECTED_ATTRIBUTES as _PROTECTED  # noqa: E402
from rescan.rules.statutes import legal_brief as _legal_brief  # noqa: E402


def _protected_attributes() -> tuple[str, ...]:
    return _PROTECTED

COMPILE_DSL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    # `reasoning` comes first so the model thinks before it writes rules.
    "required": ["reasoning", "rules"],
    "properties": {
        "reasoning": {
            "type": "string",
            "description": (
                "Reason step by step before writing any rule: (1) what capability the role "
                "genuinely needs; (2) each requirement in the plan, whether it tests capability "
                "or a protected attribute or a proxy for one, naming the statute engaged; "
                "(3) how each proxy was rewritten as a measurable capability; (4) which "
                "requirements are hard (REQUIRE) and which are preferences (PREFER) with weights."
            ),
        },
        "rules": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "source_text", "source_index", "kind", "dsl", "justification",
                    "risk", "protected_attributes", "explanation", "suggested_rewrite", "legal_basis",
                ],
                "properties": {
                    "source_text": {
                        "type": "string",
                        "description": "The sentence or phrase of the plan this rule comes from, verbatim.",
                    },
                    "source_index": {
                        "type": ["integer", "null"],
                        "description": "When the plan was given as numbered rules, the 1-based number this rule answers. Otherwise null.",
                    },
                    "kind": {"type": "string", "enum": ["require", "prefer"]},
                    "dsl": _str_or_null(
                        "One clause in the rule language, starting with REQUIRE or PREFER. Null when the "
                        "requirement is high risk or cannot be expressed in the language."
                    ),
                    "justification": _str_or_null("The job-based reason this requirement exists, in one sentence."),
                    "risk": {
                        "type": "string",
                        "enum": ["none", "review", "high"],
                        "description": (
                            "'high' if the requirement selects on a protected attribute or a proxy for one; "
                            "'review' if lawful only with a documented job-based justification; "
                            "'none' if it tests capability directly."
                        ),
                    },
                    "protected_attributes": {"type": "array", "items": {"type": "string"}},
                    "explanation": _str_or_null("Why this is or is not a risk, in plain language a recruiter can act on."),
                    "suggested_rewrite": _str_or_null(
                        "A measurable replacement testing the underlying job requirement. Null if the rule is already sound."
                    ),
                    "legal_basis": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Statute codes engaged, e.g. RDA_1975, ADA_2004. Empty when risk is none.",
                    },
                },
            },
        },
    },
}


def compile_dsl_system() -> str:
    return f"""You turn a recruiter's hiring plan into screening rules written in a small
query language, under Australian anti-discrimination law. You reason first,
then write rules. The rules run against anonymized candidate profiles: no
name, institution, suburb, employer name, or graduation year exists in them.

Work in this order and write your reasoning down:

1. What does the role genuinely need? Separate capability (skills, depth of
   experience, qualification level, licences, work rights) from everything
   else.
2. Take each requirement in the plan. Does it test capability, or a
   protected attribute or a proxy for one? A proxy is a neutral-sounding
   criterion a protected group is less able to meet and which is not
   reasonable for the job. Name the statute engaged. 'Native English
   speaker', 'recent graduate', 'cultural fit', 'Australian degree',
   'leading company' and 'no career gaps' are proxies.
3. For each proxy, say what the recruiter most likely needs and express
   that measurably. Never suggest a rewrite that is the same proxy reworded.
   Write the rule for the rewrite, not for the proxy, and mark the original
   text with its risk so the recruiter sees why.
4. Decide which requirements are hard (REQUIRE — a candidate who fails is
   excluded, with the reason shown to them) and which are preferences
   (PREFER — they order candidates, with WEIGHT for importance). When the
   plan says 'must', 'required', 'essential' use REQUIRE; 'nice to have',
   'preferred', 'desirable', 'ideally', 'bonus' use PREFER. If unsure, PREFER.

Rules for writing the language:
- Only the fields, records and operators in the reference below exist. A
  forbidden identifier is rejected by the parser.
- Absence is unknown, never failure: a candidate whose resume is silent goes
  to manual review. Do not write rules that depend on silence.
- Use ANY <record> WHERE ... for per-skill years, role titles, fields of
  study; use COUNT/SUM/MAX for how many or how long.
- Use ASK "..." only for a specific, job-related, yes/no question that no
  structured field can answer (e.g. "Has the candidate led an incident
  response?"). Never ask about a protected attribute or a proxy.
- Qualification requirements use AQF levels: diploma 5, advanced diploma 6,
  bachelor 7, honours or graduate certificate/diploma 8, masters 9, doctorate
  10. Overseas awards are mapped to their equivalent before rules run.
- Work-rights requirements are lawful: `work_rights IS unrestricted`. Require
  citizenship only where a security clearance genuinely requires it, and mark
  it 'review'.
- One rule per requirement. Quote the plan text the rule comes from.

{_legal_brief()}

LANGUAGE REFERENCE
{_dsl_reference()}

Return JSON only."""


def compile_dsl_user_prompt(plan_text: str, role_context: str | None = None) -> str:
    context = f"\n\nRole context: {role_context}" if role_context else ""
    return f"Compile this hiring plan into screening rules.\n\n<plan>\n{plan_text}\n</plan>{context}"


COMPILE_DSL_REPAIR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["dsl"],
    "properties": {
        "dsl": _str_or_null("The corrected clause, or null if the requirement cannot be expressed in the language."),
    },
}


def compile_dsl_repair_system() -> str:
    return f"""You fix a screening rule that did not parse in the rule language. Return the
corrected clause, or null if it cannot be expressed. Never work around a
forbidden identifier by renaming it: the field is forbidden because it is a
proxy for a protected attribute.

LANGUAGE REFERENCE
{_dsl_reference()}

Return JSON only."""


def compile_dsl_repair_user_prompt(source_text: str, dsl: str, errors: list[str]) -> str:
    error_text = "\n".join(f"- {error}" for error in errors)
    return (
        f"Requirement: {source_text}\n\nRejected clause:\n{dsl}\n\nParser errors:\n{error_text}"
    )


# --------------------------------------------------------------------------
# Pass 3b — a check the model makes itself (ASK "...")
# --------------------------------------------------------------------------

JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["reasoning", "answer", "evidence", "declined_reason"],
    "properties": {
        "reasoning": {
            "type": "string",
            "description": "What in the profile bears on the question, before deciding.",
        },
        "answer": {
            "type": "string",
            "enum": ["yes", "no", "unknown"],
            "description": "'unknown' when the profile does not say, or when answering would require inferring a protected attribute.",
        },
        "evidence": _str_or_null(
            "A verbatim quotation from the profile that supports a yes or a no. Null for unknown. "
            "An answer without a quotation will be treated as unknown."
        ),
        "declined_reason": _str_or_null(
            "When you decline to answer because it would require inferring a protected attribute, say which."
        ),
    },
}

JUDGE_SYSTEM = f"""You answer one yes/no question about a candidate from their anonymized profile,
for a recruitment screening rule.

The profile is de-identified: no name, institution, employer name, suburb or
graduation year. Do not speculate about any of them.

Rules:
- Answer from the profile alone. Quote the text that supports your answer,
  verbatim. An answer without a quotation is discarded.
- If the profile does not say, answer "unknown". Silence is not a "no".
- If answering would require inferring a protected attribute — race, national
  origin, sex, age, disability, religion, family responsibilities — or a proxy
  for one, answer "unknown" and give the reason in declined_reason.
- Judge only what is asked. Do not weigh unrelated strengths or weaknesses.

Protected attributes: {", ".join(_protected_attributes())}.

Return JSON only."""


def judge_user_prompt(question: str, profile_text: str) -> str:
    return f"Question: {question}\n\nAnonymized candidate profile:\n{profile_text}"


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
