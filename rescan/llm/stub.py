"""Deterministic stand-in for the inference server.

This is not a model. It is a heuristic implementation of each pass, used so the
pipeline, API and tests run end to end without a GPU, and so tests assert on
fixed output. Point `RESCAN_LLM_BACKEND=openai` at a vLLM or SGLang server to
use the real model; the interface is identical.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Callable

from rescan.llm.client import LLMError, LLMRequest, LLMResponse

# --------------------------------------------------------------------------
# Shared patterns
# --------------------------------------------------------------------------

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE_RE = re.compile(r"\b(?:\+?61\s?)?0?4\d{2}[\s-]?\d{3}[\s-]?\d{3}\b")
AU_STATES = ("QLD", "NSW", "VIC", "SA", "WA", "TAS", "NT", "ACT")
YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")

SECTION_ALIASES: dict[str, tuple[str, ...]] = {
    "summary": ("summary", "profile", "professional summary", "objective", "about"),
    "experience": ("experience", "employment", "work history", "professional experience", "career"),
    "education": ("education", "qualifications", "academic"),
    "skills": ("skills", "technical skills", "technologies"),
    "languages": ("languages",),
    "affiliations": ("affiliations", "interests", "activities", "memberships", "volunteering"),
    "certifications": ("certifications", "certificates", "licences", "licenses"),
    "projects": ("projects", "personal projects", "open source", "selected projects"),
    "publications": ("publications", "papers", "patents"),
}

SENIORITY_RE: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(chief|cto|ceo|cfo|coo|vp|vice president|executive)\b", re.I), "executive"),
    (re.compile(r"\bdirector\b", re.I), "director"),
    (re.compile(r"\bhead of\b|\bhead\b", re.I), "head"),
    (re.compile(r"\bmanager\b|\bmanagement\b", re.I), "manager"),
    (re.compile(r"\bprincipal\b|\bstaff\b|\barchitect\b", re.I), "principal"),
    (re.compile(r"\blead\b|\bteam lead\b|\btech lead\b", re.I), "lead"),
    (re.compile(r"\bsenior\b|\bsr\.?\b", re.I), "senior"),
    (re.compile(r"\bintern\b|\binternship\b", re.I), "intern"),
    (re.compile(r"\bgraduate\b|\bgrad\b", re.I), "graduate"),
    (re.compile(r"\bjunior\b|\bjr\.?\b|\bassociate\b|\bassistant\b", re.I), "junior"),
]
TEAM_SIZE_RE = re.compile(
    r"\b(?:team of|managed|managing|led|leading|supervised|mentored)\s+(?:a\s+)?(?:team\s+of\s+)?(\d{1,3})\b",
    re.I,
)
EMPLOYMENT_TYPE_RE: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bintern(ship)?\b", re.I), "internship"),
    (re.compile(r"\bcontract(or)?\b|\bfixed[- ]term\b", re.I), "contract"),
    (re.compile(r"\bcasual\b", re.I), "casual"),
    (re.compile(r"\bfreelanc\w*\b|\bconsultant\b", re.I), "freelance"),
    (re.compile(r"\bvolunteer\w*\b", re.I), "volunteer"),
]
LICENCE_RE = re.compile(r"\b(licen[cs]e|registration|registered|ahpra|forklift|white card|working with children)\b", re.I)
CLEARANCE_RE = re.compile(r"\b(baseline|nv1|nv2|positive vetting|negative vetting|security clearance)\b", re.I)
NOTICE_RE = re.compile(r"\b(\d{1,2})\s*(weeks?|months?)\s*(?:notice|notice period)\b|\bnotice(?: period)?\s*(?:of|:)?\s*(\d{1,2})\s*(weeks?|months?)\b", re.I)
INDUSTRY_HINTS: dict[str, tuple[str, ...]] = {
    "banking": ("bank", "banking", "finance", "fintech", "payments"),
    "health": ("health", "hospital", "clinic", "medical", "pharma"),
    "government": ("government", "department of", "council", "public service"),
    "education": ("university", "school", "education", "tafe"),
    "retail": ("retail", "ecommerce", "e-commerce", "store"),
    "mining": ("mining", "resources", "energy", "oil", "gas"),
    "consulting": ("consulting", "consultancy", "advisory"),
    "software": ("software", "saas", "technologies", "tech", "labs", "digital", "systems"),
    "telecommunications": ("telco", "telecom", "telstra", "optus"),
}


def _seniority_for(title: str | None) -> str | None:
    if not title:
        return None
    for pattern, level in SENIORITY_RE:
        if pattern.search(title):
            return level
    return None


def _employment_type_for(text: str) -> str | None:
    for pattern, kind in EMPLOYMENT_TYPE_RE:
        if pattern.search(text):
            return kind
    return None


def _industry_for(employer: str | None, summary: str | None) -> str | None:
    haystack = f"{employer or ''} {summary or ''}".lower()
    for industry, hints in INDUSTRY_HINTS.items():
        if any(hint in haystack for hint in hints):
            return industry
    return None


def _team_size_for(text: str) -> int | None:
    sizes = [int(m.group(1)) for m in TEAM_SIZE_RE.finditer(text or "")]
    return max(sizes) if sizes else None

QUALIFICATION_HINT = re.compile(
    r"\b(bachelor|master|phd|doctor|diploma|certificate|graduate|honours|honors"
    r"|b\.?s?c|m\.?s?c|b\.?eng|m\.?eng|mba|llb|b\.?tech|m\.?tech|bca|mca)\b",
    re.IGNORECASE,
)
INSTITUTION_HINT = re.compile(
    r"(\buniversit\w*|\binstitutes?\b|\bcollege\b|\btafe\b|\bpolytechnic\b"
    r"|\bacademy\b|\bschool of\b|\bnit\b|\biit\b)",
    re.IGNORECASE,
)

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
DATE_TOKEN_RE = re.compile(r"([A-Za-z]{3,9})?\s*\b((?:19|20)\d{2})\b")
CURRENT_RE = re.compile(r"\b(present|current|now|ongoing)\b", re.IGNORECASE)

GO8 = (
    "university of queensland",
    "university of melbourne",
    "university of sydney",
    "australian national university",
    "monash university",
    "university of adelaide",
    "university of western australia",
    "unsw",
    "university of new south wales",
)

# Affiliations carrying a protected-attribute signal are dropped when
# anonymizing; anything else is kept only if it reads as professional.
PROTECTED_AFFILIATION = re.compile(
    r"\b(islamic|muslim|christian|catholic|jewish|hindu|sikh|buddhist|church|mosque|temple"
    r"|women|men's|lgbt|queer|pride|disab|veteran|indigenous|aborigin|torres strait"
    r"|malayalee|chinese|indian|african|greek|italian|vietnamese|korean|labor|liberal|greens"
    r"|political|union)\b",
    re.IGNORECASE,
)
PROFESSIONAL_AFFILIATION = re.compile(
    r"\b(ieee|acm|acs\b|engineers australia|institute|professional|association of engineers"
    r"|chartered|society of)\b",
    re.IGNORECASE,
)

REGION_MAP = {
    "QLD": "Greater Brisbane / South East Queensland",
    "NSW": "Greater Sydney / New South Wales",
    "VIC": "Greater Melbourne / Victoria",
    "SA": "South Australia",
    "WA": "Western Australia",
    "TAS": "Tasmania",
    "NT": "Northern Territory",
    "ACT": "Australian Capital Territory",
}


# --------------------------------------------------------------------------
# Section splitting
# --------------------------------------------------------------------------


def _canonical_section(line: str) -> str | None:
    """Return the canonical section name if `line` is a heading."""
    stripped = line.strip().rstrip(":").strip()
    if not stripped or len(stripped) > 40:
        return None
    # Headings in resumes are short and typically uppercase or title case.
    if not (stripped.isupper() or stripped.istitle()):
        return None
    lowered = stripped.lower()
    for canonical, aliases in SECTION_ALIASES.items():
        if lowered in aliases:
            return canonical
    return None


def split_sections(text: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {"header": []}
    current = "header"
    for line in text.splitlines():
        canonical = _canonical_section(line)
        if canonical:
            current = canonical
            sections.setdefault(current, [])
            continue
        if line.strip():
            sections.setdefault(current, []).append(line.rstrip())
    return sections


# --------------------------------------------------------------------------
# Structuring
# --------------------------------------------------------------------------


def _parse_identity(text: str, header: list[str]) -> dict[str, Any]:
    email = EMAIL_RE.search(text)
    phone = PHONE_RE.search(text)

    name = None
    for line in header[:4]:
        candidate = line.strip()
        if not candidate or EMAIL_RE.search(candidate) or "|" in candidate:
            continue
        words = candidate.split()
        if 1 < len(words) <= 5 and (candidate.isupper() or candidate.istitle()):
            name = candidate.title() if candidate.isupper() else candidate
            break

    suburb = state = None
    country = None
    for line in header[:6]:
        for abbreviation in AU_STATES:
            if re.search(rf"\b{abbreviation}\b", line):
                state = abbreviation
                parts = [p.strip() for p in re.split(r"[|,]", line)]
                for index, part in enumerate(parts):
                    if re.fullmatch(rf"{abbreviation}", part) and index > 0:
                        suburb = parts[index - 1]
                        break
                country = "Australia"
                break
        if state:
            break

    links = re.findall(r"\b(?:linkedin\.com|github\.com|gitlab\.com)/[\w./-]+", text)
    return {
        "full_name": name,
        "email": email.group(0) if email else None,
        "phone": phone.group(0) if phone else None,
        "suburb": suburb,
        "state": state,
        "country": country,
        "links": links,
    }


def _parse_skills(lines: list[str]) -> list[dict[str, Any]]:
    skills: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in lines:
        for token in re.split(r"[,;/]|\s{2,}", line):
            name = token.strip(" .-•\t")
            if not name or len(name) > 40 or name.lower() in seen:
                continue
            if len(name) < 2 or name.lower() in {"and", "etc"}:
                continue
            seen.add(name.lower())
            skills.append(
                {"name": name, "category": "technical", "years": None, "evidence": "skills section"}
            )
    return skills


def _parse_date_range(text: str) -> tuple[str | None, str | None, bool, float | None]:
    """Parse 'Mar 2022 - Present' / '2017 - 2021' into ISO-ish bounds."""
    tokens = DATE_TOKEN_RE.findall(text or "")
    is_current = bool(CURRENT_RE.search(text or ""))

    def to_iso(month_word: str, year: str) -> tuple[str, int]:
        month = MONTHS.get((month_word or "")[:3].lower())
        return (f"{year}-{month:02d}" if month else year), (month or 1)

    if not tokens:
        return None, None, is_current, None

    start_iso, start_month = to_iso(*tokens[0])
    start_year = int(tokens[0][1])

    if is_current:
        end_iso, end_year, end_month = None, None, None
    elif len(tokens) > 1:
        end_iso, end_month = to_iso(*tokens[-1])
        end_year = int(tokens[-1][1])
    else:
        end_iso, end_year, end_month = None, None, None

    months = None
    if end_year is not None:
        months = float(max(0, (end_year - start_year) * 12 + (end_month - start_month)))
    return start_iso, end_iso, is_current, months


def _parse_experience(lines: list[str]) -> list[dict[str, Any]]:
    roles: list[dict[str, Any]] = []
    consumed_as_dates: set[int] = set()

    for index, line in enumerate(lines):
        if index in consumed_as_dates:
            continue
        if line.lstrip().startswith(("-", "\u2022", "*")):
            if roles:
                bullet = line.lstrip("-\u2022* ").strip()
                existing = roles[-1]["summary"]
                roles[-1]["summary"] = (f"{existing}; {bullet}" if existing else bullet)[:400]
            continue
        if "\u2014" not in line and " - " not in line and "|" not in line:
            continue

        date_text = line
        if not DATE_TOKEN_RE.search(line) and not CURRENT_RE.search(line):
            # Date range commonly sits on the following line.
            if index + 1 < len(lines):
                lookahead = lines[index + 1]
                if DATE_TOKEN_RE.search(lookahead) or CURRENT_RE.search(lookahead):
                    date_text = lookahead
                    consumed_as_dates.add(index + 1)
                else:
                    continue
            else:
                continue

        separator = "\u2014" if "\u2014" in line else ("|" if "|" in line else " - ")
        head, _, tail = line.partition(separator)
        employer = re.split(r"[|,]", tail)[0].strip() or None
        start_iso, end_iso, is_current, months = _parse_date_range(date_text)

        roles.append(
            {
                "title": head.strip() or None,
                "employer": employer,
                "start": start_iso,
                "end": end_iso,
                "is_current": is_current,
                "months": months,
                "summary": None,
                "seniority": _seniority_for(head.strip()),
                "industry": None,
                "employment_type": _employment_type_for(line),
                "team_size": None,
                "technologies": [],
            }
        )
    for role in roles:
        role["industry"] = _industry_for(role["employer"], role["summary"])
        role["team_size"] = _team_size_for(role["summary"] or "")
        if role["employment_type"] is None:
            role["employment_type"] = _employment_type_for(role["summary"] or "")
    return roles


def _parse_projects(lines: list[str]) -> list[dict[str, Any]]:
    projects: list[dict[str, Any]] = []
    for line in lines:
        text = line.strip(" .-•*\t")
        if not text or len(text) < 4:
            continue
        name, _, rest = text.partition(" - ") if " - " in text else text.partition(": ")
        name = (name or text).strip()[:80]
        technologies = [
            token.strip() for token in re.split(r"[,/]", rest) if 1 < len(token.strip()) <= 30
        ][:8] if rest else []
        projects.append({"name": name, "summary": rest.strip()[:200] or None, "technologies": technologies, "months": None})
    return projects[:10]


def _parse_qualifications(lines: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    qualifications: list[dict[str, Any]] = []
    universities: list[str] = []

    for index, line in enumerate(lines):
        text = line.strip(" .-•")
        if not text:
            continue
        if INSTITUTION_HINT.search(text):
            for part in re.split(r"[,|]", text):
                part = part.strip()
                if INSTITUTION_HINT.search(part) and part not in universities:
                    universities.append(part)
        if not QUALIFICATION_HINT.search(text):
            continue

        # The institution is often on the following line.
        context = text
        if index + 1 < len(lines) and INSTITUTION_HINT.search(lines[index + 1]):
            context = f"{text}, {lines[index + 1].strip()}"

        parts = [p.strip() for p in re.split(r"[,|]", context) if p.strip()]
        title = parts[0] if parts else text
        institution = next((p for p in parts if INSTITUTION_HINT.search(p)), None)
        years = YEAR_RE.findall(context)
        country = None
        for part in parts:
            if part.lower() in {"india", "china", "nigeria", "kenya", "syria", "usa", "uk"}:
                country = part
        if institution and country is None and "australia" not in context.lower():
            country = "Australia" if any(g in institution.lower() for g in ("australia", "queensland", "griffith", "swinburne", "monash", "rmit")) else None

        qualifications.append(
            {
                "title": title,
                "institution": institution,
                "field_of_study": None,
                "completion_year": int(years[-1]) if years else None,
                "country": country,
            }
        )
    return qualifications, universities


def _parse_work_rights(text: str) -> dict[str, Any]:
    lowered = text.lower()
    subclass = re.search(r"subclass\s*(\d{3})", lowered)

    def evidence(pattern: str) -> str | None:
        found = re.search(rf"[^.\n]*{pattern}[^.\n]*", text, re.IGNORECASE)
        return found.group(0).strip() if found else None

    if re.search(r"australian citizen|citizen of australia|citizenship", lowered):
        return {
            "status": "citizen",
            "visa_subclass": None,
            "unrestricted": True,
            "evidence": evidence("citizen"),
        }
    if re.search(r"permanent resident|\bpr\b holder", lowered):
        return {
            "status": "permanent_resident",
            "visa_subclass": None,
            "unrestricted": True,
            "evidence": evidence("permanent resident"),
        }
    if re.search(r"sponsorship", lowered):
        return {
            "status": "requires_sponsorship",
            "visa_subclass": subclass.group(1) if subclass else None,
            "unrestricted": False,
            "evidence": evidence("sponsor"),
        }
    if re.search(r"full work rights|unrestricted work rights|bridging visa", lowered):
        return {
            "status": "visa_unrestricted",
            "visa_subclass": subclass.group(1) if subclass else None,
            "unrestricted": True,
            "evidence": evidence("work rights"),
        }
    if subclass:
        return {
            "status": "visa_restricted",
            "visa_subclass": subclass.group(1),
            "unrestricted": False,
            "evidence": evidence("subclass"),
        }
    return {"status": "unknown", "visa_subclass": None, "unrestricted": None, "evidence": None}


def _estimate_years(text: str, summary: str | None, roles: list[dict[str, Any]]) -> float | None:
    """Prefer the candidate's own stated total, then role durations, then span."""
    # A stated figure in the summary is the most reliable signal.
    if summary:
        stated = re.search(r"\b(\d{1,2})\+?\s*years?\b", summary, re.IGNORECASE)
        if stated:
            return float(stated.group(1))

    stated = re.search(
        r"\b(\d{1,2})\+?\s*years?\s+(?:of\s+)?(?:commercial\s+|professional\s+)?experience",
        text,
        re.IGNORECASE,
    )
    if stated:
        return float(stated.group(1))

    known = [role["months"] for role in roles if role.get("months")]
    if known and len(known) == len(roles):
        return round(sum(known) / 12.0, 1)

    years = [int(y) for y in re.findall(r"\b(?:19|20)\d{2}\b", text)]
    if len(years) >= 2:
        return float(max(years) - min(years))
    return None


def handle_structure(request: LLMRequest) -> dict[str, Any]:
    text = request.context.get("resume_text", "")
    if not text.strip():
        raise LLMError("structure: empty resume text")

    sections = split_sections(text)
    header = sections.get("header", [])
    roles = _parse_experience(sections.get("experience", []))
    qualifications, universities = _parse_qualifications(sections.get("education", []))

    summary_lines = sections.get("summary", [])
    summary = " ".join(summary_lines)[:400] if summary_lines else None

    languages: list[str] = []
    for line in sections.get("languages", []):
        languages.extend(p.strip() for p in re.split(r"[,;]", line) if p.strip())

    affiliations: list[str] = []
    for line in sections.get("affiliations", []):
        affiliations.extend(p.strip(" .-•") for p in re.split(r"[,;]", line) if p.strip())

    certifications = [line.strip(" .-•") for line in sections.get("certifications", []) if line.strip()]
    licences = [item for item in certifications if LICENCE_RE.search(item)]
    clearance = CLEARANCE_RE.search(text)
    notice = NOTICE_RE.search(text)
    availability_weeks = None
    if notice:
        amount = int(notice.group(1) or notice.group(3))
        unit = (notice.group(2) or notice.group(4) or "").lower()
        availability_weeks = float(amount * 4 if unit.startswith("month") else amount)
    managing = [role for role in roles if role["seniority"] in {"lead", "manager", "head", "director", "executive"}]
    management_months = [role["months"] for role in managing if role["months"] is not None]
    team_sizes = [role["team_size"] for role in roles if role["team_size"]] + (
        [_team_size_for(text)] if _team_size_for(text) else []
    )
    publications = [line for line in sections.get("publications", []) if line.strip()]

    notes: list[str] = []
    if not roles:
        notes.append("no employment history parsed; check the document by hand")
    if not qualifications:
        notes.append("no qualifications parsed; check the document by hand")

    return {
        "identity": _parse_identity(text, header),
        "summary": summary,
        "skills": _parse_skills(sections.get("skills", [])),
        "experience": roles,
        "total_years_experience": _estimate_years(text, summary, roles),
        "qualifications": qualifications,
        "universities": universities,
        "work_rights": _parse_work_rights(text),
        "languages": languages,
        "affiliations": affiliations,
        "certifications": certifications,
        "projects": _parse_projects(sections.get("projects", [])),
        "licences": licences,
        "security_clearance": clearance.group(1).title() if clearance else None,
        "management_years": round(sum(management_months) / 12.0, 1) if management_months else None,
        "people_managed_max": max(team_sizes) if team_sizes else None,
        "publications_count": len(publications) if publications else None,
        "availability_weeks": availability_weeks,
        "extraction_notes": notes,
    }


# --------------------------------------------------------------------------
# Anonymization
# --------------------------------------------------------------------------


def _tier_for(institution: str) -> str:
    lowered = institution.lower()
    if any(go8 in lowered for go8 in GO8):
        return "Australian university (Group of Eight)"
    if "tafe" in lowered or "polytechnic" in lowered:
        return "Australian TAFE / VET provider"
    australian_markers = (
        "queensland", "griffith", "swinburne", "rmit", "deakin", "curtin", "macquarie",
        "la trobe", "wollongong", "newcastle", "flinders", "murdoch", "victoria university",
        "charles sturt", "james cook", "bond university", "australian",
    )
    if any(marker in lowered for marker in australian_markers):
        return "Australian university"
    return "Overseas university"


def handle_anonymize(request: LLMRequest) -> dict[str, Any]:
    profile = request.context.get("profile", {})
    identity = profile.get("identity", {}) or {}
    redactions: list[dict[str, str]] = []

    for field_name, reason in (
        ("full_name", "Name is the strongest demographic proxy in a resume."),
        ("email", "Email commonly embeds the candidate's name."),
        ("phone", "Direct identifier."),
        ("links", "Personal profiles re-identify the candidate."),
    ):
        if identity.get(field_name):
            redactions.append({"field": f"identity.{field_name}", "action": "removed", "reason": reason})

    tiers: list[str] = []
    for institution in profile.get("universities", []) or []:
        tier = _tier_for(institution)
        if tier not in tiers:
            tiers.append(tier)
        redactions.append(
            {
                "field": "universities",
                "action": "generalised",
                "reason": "Institution name reintroduces demographic signal after names are removed.",
            }
        )

    region = None
    if identity.get("state"):
        region = REGION_MAP.get(identity["state"], identity["state"])
        redactions.append(
            {
                "field": "identity.suburb",
                "action": "regionalised",
                "reason": "Suburb is a socio-economic and ethnic proxy; generalised to a region.",
            }
        )
    elif identity.get("country"):
        region = identity["country"]

    # The model judges whether an employer's name reveals a protected
    # attribute; this backend approximates that with the affiliation keywords.
    employers_to_remove: list[dict[str, str]] = []
    for role in profile.get("experience", []) or []:
        employer = (role or {}).get("employer") or ""
        if employer and PROTECTED_AFFILIATION.search(employer):
            employers_to_remove.append({
                "employer": employer,
                "reason": "the organisation's name reveals political opinion, religion, ethnicity, union membership or another protected attribute",
            })

    kept_affiliations: list[str] = []
    for affiliation in profile.get("affiliations", []) or []:
        if PROTECTED_AFFILIATION.search(affiliation):
            redactions.append(
                {
                    "field": "affiliations",
                    "action": "removed",
                    "reason": f"{affiliation!r} signals a protected attribute and is not job-relevant.",
                }
            )
        elif PROFESSIONAL_AFFILIATION.search(affiliation):
            kept_affiliations.append(affiliation)
            redactions.append(
                {"field": "affiliations", "action": "kept", "reason": f"{affiliation!r} evidences professional capability."}
            )
        else:
            redactions.append(
                {
                    "field": "affiliations",
                    "action": "removed",
                    "reason": f"{affiliation!r} is not job-relevant and may carry demographic signal.",
                }
            )

    summary = profile.get("summary")
    if summary:
        name = identity.get("full_name")
        if name:
            for part in name.split():
                summary = re.sub(rf"\b{re.escape(part)}\b", "The candidate", summary)
        summary = EMAIL_RE.sub("[redacted]", summary)
        for institution in profile.get("universities", []) or []:
            summary = summary.replace(institution, _tier_for(institution))

    return {
        "summary": summary,
        "institution_tiers": tiers,
        "region": region,
        "job_relevant_affiliations": kept_affiliations,
        "employers_to_remove": employers_to_remove,
        "redactions": redactions,
    }



# --------------------------------------------------------------------------
# Rule compilation
# --------------------------------------------------------------------------

YEARS_MIN_RE = re.compile(
    r"\b(?:at least|minimum(?: of)?|min\.?|no less than|>=)\s*(\d{1,2})\s*\+?\s*years?\b"
    r"|\b(\d{1,2})\s*\+\s*years?\b"
    r"|\b(\d{1,2})\s*(?:or more)\s*years?\b",
    re.IGNORECASE,
)
YEARS_MAX_RE = re.compile(
    r"\b(?:no more than|at most|fewer than|less than|under|up to|max(?:imum)?(?: of)?)\s*(\d{1,2})\s*years?\b",
    re.IGNORECASE,
)
DEGREE_RE = re.compile(
    r"\b(doctorate|phd|masters?|honours|honors|graduate (?:certificate|diploma)"
    r"|bachelor(?:'?s)?|degree|advanced diploma|diploma|certificate\s*(?:iv|4))\b",
    re.IGNORECASE,
)
DEGREE_TO_AQF = {
    "doctorate": 10, "phd": 10,
    "master": 9, "masters": 9,
    "honours": 8, "honors": 8, "graduate certificate": 8, "graduate diploma": 8,
    "bachelor": 7, "bachelor's": 7, "degree": 7,
    "advanced diploma": 6,
    "diploma": 5,
}
WORK_RIGHTS_RE = re.compile(
    r"\b(full|unrestricted|permanent)\s+work(ing)?\s+rights\b|\bright to work\b|\bwork rights\b"
    r"|\bno sponsorship\b|\bwithout sponsorship\b",
    re.IGNORECASE,
)
CITIZEN_ONLY_RE = re.compile(r"\baustralian citizens?\b|\bcitizens? only\b", re.IGNORECASE)
SKILL_LEAD_RE = re.compile(
    r"\b(?:experience (?:with|in|using)|proficient (?:with|in)|proficiency in|knowledge of"
    r"|skilled (?:with|in)|hands[- ]on (?:with|experience with)|must know|familiar(?:ity)? with"
    r"|competent (?:with|in)|background in|expertise in)\b",
    re.IGNORECASE,
)
CERT_RE = re.compile(r"\b(certified|certification|certificate in|accredited)\b", re.IGNORECASE)

# Trailing filler that should not become part of a skill name.
SKILL_STOPWORDS = re.compile(
    r"\b(experience|required|preferred|desirable|essential|skills?|a|an|the|is|are|and|or)\b",
    re.IGNORECASE,
)


def _split_skills(text: str) -> list[str]:
    text = re.split(r"\.|;", text)[0]
    parts = re.split(r",|\band\b|\bor\b|/|\+", text, flags=re.IGNORECASE)
    skills: list[str] = []
    for part in parts:
        cleaned = SKILL_STOPWORDS.sub(" ", part)
        cleaned = re.sub(r"[^\w#+.\- ]", " ", cleaned).strip()
        cleaned = re.sub(r"\s{2,}", " ", cleaned)
        if 1 < len(cleaned) <= 40:
            skills.append(cleaned)
    return skills


def _first_group(match: re.Match[str]) -> int:
    return int(next(group for group in match.groups() if group))


PREFER_RE = re.compile(
    r"\b(prefer(?:red|able)?|desirable|nice[- ]to[- ]have|bonus|ideally|advantage(?:ous)?|a plus|would be great)\b",
    re.IGNORECASE,
)
ASK_RE = re.compile(
    r"\b(?:should|must|ideally|has|have|who has|who have)\s+(?:have\s+)?"
    r"(led|built|shipped|delivered|managed|owned|run|designed|launched|mentored|presented|published|migrated|scaled|operated)\b(.*)",
    re.IGNORECASE,
)
TECH_TOKENS = (
    "python", "sql", "java", "javascript", "typescript", "go", "golang", "rust", "c#", "c++", "scala", "kotlin",
    "aws", "gcp", "azure", "kubernetes", "docker", "terraform", "kafka", "spark", "flink", "airflow", "dbt",
    "snowflake", "postgres", "postgresql", "mysql", "redis", "react", "angular", "vue", "node", "django",
    "flask", "fastapi", "spring", "tensorflow", "pytorch", "linux", "git", "jenkins", "ansible", "figma",
)
_TECH_RE = re.compile(r"(?<![\w#+])(" + "|".join(re.escape(t) for t in TECH_TOKENS) + r")(?![\w#+])", re.IGNORECASE)


def _dsl_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _dsl_list(values: list[str]) -> str:
    return "(" + ", ".join(_dsl_quote(v) for v in values) + ")"


def _skill_groups(text: str) -> tuple[list[str], list[list[str]]]:
    """Split a sentence into skills that are all required and OR-groups."""
    required: list[str] = []
    any_groups: list[list[str]] = []
    for chunk in re.split(r",|\bplus\b|\band also\b|;", text, flags=re.IGNORECASE):
        found = []
        for match in _TECH_RE.finditer(chunk):
            token = match.group(1)
            canonical = next(t for t in TECH_TOKENS if t.lower() == token.lower())
            display = canonical.upper() if canonical in {"sql", "aws", "gcp"} else canonical.capitalize() if canonical.islower() else canonical
            if display not in found:
                found.append(display)
        if not found:
            continue
        if re.search(r"\bor\b", chunk, re.IGNORECASE) and len(found) > 1:
            any_groups.append(found)
        else:
            required.extend(f for f in found if f not in required)
    return required, any_groups


def compile_sentence(sentence: str) -> dict[str, Any] | None:
    """Map one plan sentence onto the rule language, or None.

    Every recognised requirement in the sentence is ANDed, so "5+ years with
    Python and SQL, plus AWS or GCP" becomes one clause with three parts.
    """
    text = sentence.strip()
    if not text:
        return None
    parts: list[str] = []

    match = YEARS_MIN_RE.search(text)
    if match:
        parts.append(f"years_experience >= {_first_group(match)}")
    match = YEARS_MAX_RE.search(text)
    if match:
        parts.append(f"years_experience <= {int(match.group(1))}")

    match = DEGREE_RE.search(text)
    if match:
        token = match.group(1).lower()
        # Longest key first so "graduate certificate" beats "certificate".
        level = next(
            (aqf for key, aqf in sorted(DEGREE_TO_AQF.items(), key=lambda kv: -len(kv[0])) if key in token),
            None,
        )
        if level is None and token.startswith("certificate"):
            level = 4
        if level is not None:
            parts.append(f"aqf >= {level}")

    if CITIZEN_ONLY_RE.search(text):
        parts.append('work_rights IS citizen')
    elif WORK_RIGHTS_RE.search(text):
        parts.append("work_rights IS unrestricted")

    if CERT_RE.search(text):
        certs = _split_skills(CERT_RE.split(text)[-1])
        if certs:
            parts.append(f"certifications HAS ANY {_dsl_list(certs)}")
    else:
        required, any_groups = _skill_groups(text)
        if not required and not any_groups:
            lead = SKILL_LEAD_RE.search(text)
            if lead:
                skills = _split_skills(text[lead.end():])
                if skills:
                    if re.search(r"\bor\b", text, re.IGNORECASE):
                        any_groups.append(skills)
                    else:
                        required = skills
        if required:
            parts.append(f"skills HAS ALL {_dsl_list(required)}" if len(required) > 1 else f"skills HAS ANY {_dsl_list(required)}")
        for group in any_groups:
            parts.append(f"skills HAS ANY {_dsl_list(group)}")

    if not parts:
        ask = ASK_RE.search(text)
        if ask:
            verb, rest = ask.group(1).lower(), ask.group(2).strip(" .")
            if rest:
                parts.append(f'ASK {_dsl_quote(f"Has the candidate {verb} {rest}?")}')

    if not parts:
        return None
    kind = "prefer" if PREFER_RE.search(text) else "require"
    return {"kind": kind, "expr": " AND ".join(parts)}


def handle_compile_dsl(request: LLMRequest) -> dict[str, Any]:
    """Compile a plan by pattern. Legal risk is decided by the deterministic
    statute table in rescan.rules.classifier, which this backend does not
    second-guess, so every rule here reports risk 'none'."""
    from rescan.rules.classifier import split_plan  # local: avoids an import cycle at module load

    plan = request.context.get("plan") or ""
    rule_texts = request.context.get("rule_texts") or []
    if not plan.strip() and not rule_texts:
        raise LLMError("compile_dsl: empty plan")

    sources: list[tuple[int | None, str]] = [(index, text) for index, text in enumerate(rule_texts, start=1)]
    sources += [(None, sentence) for sentence in split_plan(plan)]

    rules = []
    for index, sentence in sources:
        compiled = compile_sentence(sentence)
        kind = compiled["kind"] if compiled else ("prefer" if PREFER_RE.search(sentence) else "require")
        rules.append({
            "source_text": sentence,
            "source_index": index,
            "kind": kind,
            "dsl": f"{kind.upper()} {compiled['expr']}" if compiled else None,
            "justification": None,
            "risk": "none",
            "protected_attributes": [],
            "explanation": None,
            "suggested_rewrite": None,
            "legal_basis": [],
        })
    return {
        "reasoning": (
            "Deterministic backend: each sentence of the plan was mapped onto the rule "
            "language by pattern. Legal risk is decided by the statute table, not here."
        ),
        "rules": rules,
    }


def handle_compile_dsl_repair(request: LLMRequest) -> dict[str, Any]:
    """The stub never emits a clause it cannot parse, so there is nothing to repair."""
    return {"dsl": None}


# --------------------------------------------------------------------------
# Model checks (ASK "...")
# --------------------------------------------------------------------------

JUDGE_STOPWORDS = {
    "candidate", "does", "have", "has", "had", "the", "this", "that", "with", "from", "their",
    "they", "them", "been", "were", "was", "any", "for", "and", "led", "into", "over", "more",
    "than", "year", "years", "ever", "worked", "experience", "experienced", "ability", "able",
}


def _strings_in(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings_in(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings_in(v)]
    return []


def handle_judge(request: LLMRequest) -> dict[str, Any]:
    """Keyword heuristic: 'yes' when the profile plainly mentions what is asked,
    quoting the line it found; otherwise 'unknown'. Never 'no' — a stub must
    not exclude anyone on a guess."""
    question = request.context.get("question", "")
    profile = request.context.get("profile", {})
    if not question.strip():
        raise LLMError("judge: empty question")

    words = [w for w in re.findall(r"[a-z][a-z-]{3,}", question.lower()) if w not in JUDGE_STOPWORDS]
    stems = {w[:-1] if w.endswith("s") else w for w in words}
    best: tuple[int, str] | None = None
    for text in _strings_in(profile):
        lowered = text.lower()
        hits = sum(1 for stem in stems if stem in lowered)
        if hits and (best is None or hits > best[0]):
            best = (hits, text)

    needed = 1 if len(stems) <= 2 else 2
    if best is not None and best[0] >= needed:
        return {
            "reasoning": f"The profile mentions {best[0]} of the terms asked about.",
            "answer": "yes",
            "evidence": best[1][:200],
            "declined_reason": None,
        }
    return {
        "reasoning": "The profile does not mention what was asked.",
        "answer": "unknown",
        "evidence": None,
        "declined_reason": None,
    }


# --------------------------------------------------------------------------
# Ranking
# --------------------------------------------------------------------------


def _skill_names(profile: dict[str, Any]) -> list[str]:
    return [str(skill.get("name", "")).lower() for skill in profile.get("skills") or []]


def _covers(required: str, held: list[str]) -> bool:
    needle = required.strip().lower()
    return bool(needle) and any(needle in name or name in needle for name in held)


def _fraction_present(required: list[str], held: list[str]) -> tuple[float, list[str], list[str]]:
    if not required:
        return 1.0, [], []
    matched = [item for item in required if _covers(item, held)]
    missing = [item for item in required if item not in matched]
    return len(matched) / len(required), matched, missing


def _score_criterion(key: str, profile: dict[str, Any], role: dict[str, Any]) -> tuple[float, str]:
    held = _skill_names(profile)

    if key == "required_skills":
        required = role.get("required_skills") or []
        fraction, matched, missing = _fraction_present(required, held)
        if not required:
            return 0.5, "No required skills specified for the role."
        evidence = f"Holds {len(matched)}/{len(required)} required skills ({', '.join(matched) or 'none'})."
        if missing:
            evidence += f" Missing: {', '.join(missing)}."
        return fraction, evidence

    if key == "desirable_skills":
        desirable = role.get("desirable_skills") or []
        fraction, matched, _ = _fraction_present(desirable, held)
        if not desirable:
            return 0.5, "No desirable skills specified for the role."
        return fraction, f"Holds {len(matched)}/{len(desirable)} desirable skills ({', '.join(matched) or 'none'})."

    if key == "experience_depth":
        years = profile.get("total_years_experience")
        if years is None:
            return 0.0, "Years of professional experience not stated in the resume."
        target = role.get("min_years_experience") or 8.0
        # Meeting the target scores 0.8; the remainder rewards depth beyond it,
        # so seniority is not the only thing that ranks.
        base = min(1.0, float(years) / float(target)) * 0.8
        surplus = min(0.2, max(0.0, (float(years) - float(target)) / 20.0))
        return round(min(1.0, base + surplus), 3), f"{years:g} years of professional experience."

    if key == "qualification":
        level = None
        for qualification in profile.get("qualifications") or []:
            if qualification.get("aqf_level") is not None:
                level = max(level or 0, int(qualification["aqf_level"]))
        if level is None:
            return 0.0, "No qualification with a recognised AQF equivalent."
        target = role.get("min_aqf") or 7
        return round(min(1.0, level / float(target)), 3), f"Highest qualification is AQF level {level}."

    if key == "role_relevance":
        title_words = {
            word for word in re.split(r"\W+", str(role.get("title", "")).lower()) if len(word) > 3
        }
        history = " ".join(
            str(role_entry.get("title") or "") for role_entry in profile.get("experience") or []
        ).lower()
        if not title_words or not history.strip():
            return 0.5, "Insufficient role history to assess relevance."
        overlap = {word for word in title_words if word in history}
        return (
            round(len(overlap) / len(title_words), 3),
            f"Role history matches {len(overlap)}/{len(title_words)} terms in the role title"
            + (f" ({', '.join(sorted(overlap))})." if overlap else "."),
        )

    return 0.5, "Criterion not recognised by this backend; scored neutrally."


def handle_rank(request: LLMRequest) -> dict[str, Any]:
    profile = request.context.get("profile", {})
    role = request.context.get("role", {})
    criteria = request.context.get("criteria", [])
    if not criteria:
        raise LLMError("rank: no criteria supplied")

    scored = []
    for criterion in criteria:
        key = criterion.get("key") if isinstance(criterion, dict) else str(criterion)
        score, evidence = _score_criterion(key, profile, role)
        # A deterministic per-candidate jitter would defeat reproducibility, so
        # ensemble members differ only by the seed applied downstream.
        scored.append({"criterion": key, "score": score, "evidence": evidence})

    strongest = max(scored, key=lambda entry: entry["score"])
    weakest = min(scored, key=lambda entry: entry["score"])
    rationale = f"Strongest on {strongest['criterion']}: {strongest['evidence']} " \
                f"Weakest on {weakest['criterion']}: {weakest['evidence']}"
    return {"criteria": scored, "rationale": rationale}


# ------------------------------------------------------------------
# Dispatch
# ------------------------------------------------------------------

HANDLERS: dict[str, Callable[[LLMRequest], dict[str, Any]]] = {
    "structure": handle_structure,
    "anonymize": handle_anonymize,
    "compile_dsl": handle_compile_dsl,
    "compile_dsl_repair": handle_compile_dsl_repair,
    "judge": handle_judge,
    "rank": handle_rank,
}


class StubClient:
    """Deterministic backend. Same interface as the real inference client."""

    name = "stub"

    def json_call(self, request: LLMRequest) -> LLMResponse:
        started = time.monotonic()
        handler = HANDLERS.get(request.task)
        if handler is None:
            raise LLMError(f"stub backend has no handler for task {request.task!r}")
        data = handler(request)
        return LLMResponse(
            data=data,
            model="stub",
            backend="stub",
            latency_s=time.monotonic() - started,
            raw=json.dumps(data),
        )

    def close(self) -> None:  # parity with the httpx-backed client
        return None
