"""Legal reference data for the rule classifier.

Every risk finding cites a statute and offers a measurable rewrite, so a
recruiter is told what to write instead, not merely that they were wrong.

This encodes the anti-discrimination provisions that bear on recruitment
advertising and screening in Queensland. It is decision-support for recruiters,
not legal advice, and the citations are provided so a human can check them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class RiskLevel(str, Enum):
    NONE = "none"
    # Lawful in some contexts but needs a documented, job-based justification.
    REVIEW = "review"
    # Reads as a proxy for a protected attribute; should not be applied as written.
    HIGH = "high"


STATUTES: dict[str, str] = {
    "RDA_1975": "Racial Discrimination Act 1975 (Cth) ss 9, 15 — race, colour, national or ethnic origin",
    "FWA_351": "Fair Work Act 2009 (Cth) s 351 — adverse action on a protected attribute",
    "SDA_1984": "Sex Discrimination Act 1984 (Cth) ss 5, 7A, 14 — sex, pregnancy, family responsibilities",
    "ADA_2004": "Age Discrimination Act 2004 (Cth) ss 14, 18 — age",
    "DDA_1992": "Disability Discrimination Act 1992 (Cth) ss 5, 15 — disability",
    "ADA_QLD_1991": "Anti-Discrimination Act 1991 (Qld) ss 7, 14, 15 — protected attributes in work",
    "PRIVACY_1988": "Privacy Act 1988 (Cth) — collection limited to what is reasonably necessary",
}

# Indirect discrimination is the common failure mode here: a criterion that is
# neutral on its face but that a protected group is less able to comply with,
# and that is not reasonable for the job.
INDIRECT_DISCRIMINATION_NOTE = (
    "Indirect discrimination: a neutral-sounding requirement that a protected "
    "group is less able to meet, and which is not reasonable for the role."
)


@dataclass(frozen=True)
class RiskPattern:
    id: str
    pattern: re.Pattern[str]
    risk: RiskLevel
    attributes: tuple[str, ...]
    statutes: tuple[str, ...]
    explanation: str
    rewrite: str
    field_alternative: str | None = None


def _compile(source: str) -> re.Pattern[str]:
    return re.compile(source, re.IGNORECASE)


RISK_PATTERNS: list[RiskPattern] = [
    RiskPattern(
        id="native_speaker",
        pattern=_compile(r"\b(native|mother[- ]tongue)\s+(english\s+)?(speaker|level|fluency)\b|\bnative english\b"),
        risk=RiskLevel.HIGH,
        attributes=("race", "national or ethnic origin"),
        statutes=("RDA_1975", "FWA_351", "ADA_QLD_1991"),
        explanation=(
            "'Native speaker' selects on where and how someone learned English, "
            "which tracks national origin, rather than on how well they "
            "communicate. Fluent speakers who learned English as a second "
            "language are excluded for a reason unrelated to the job."
        ),
        rewrite="Communicates in written and spoken English at a professional standard, evidenced in the application.",
        field_alternative="languages",
    ),
    RiskPattern(
        id="unaccented",
        pattern=_compile(r"\b(no|without)\s+(an\s+)?accent\b|\bunaccented\b|\bneutral accent\b|\bclear accent\b"),
        risk=RiskLevel.HIGH,
        attributes=("race", "national or ethnic origin"),
        statutes=("RDA_1975", "FWA_351"),
        explanation=(
            "Accent is a direct marker of national origin and is not a measure "
            "of whether someone can be understood."
        ),
        rewrite="Able to be clearly understood by customers and colleagues in spoken English.",
    ),
    RiskPattern(
        id="cultural_fit",
        pattern=_compile(r"\bculture?(al)?\s+fit\b|\bfits? (our|the) culture\b|\bone of us\b"),
        risk=RiskLevel.HIGH,
        attributes=("race", "national or ethnic origin", "religion", "age", "sex"),
        statutes=("FWA_351", "ADA_QLD_1991"),
        explanation=(
            "'Cultural fit' has no measurable definition, so it absorbs the "
            "assessor's assumptions about background. It is the most common "
            "route by which demographic preference enters a screening decision."
        ),
        rewrite="Name the behaviour you actually need, e.g. 'has worked in cross-functional teams and given and received code review'.",
    ),
    RiskPattern(
        id="recent_graduate",
        pattern=_compile(r"\b(recent|new|fresh|current)\s+grad(uate)?s?\b|\bgraduating (this|next) year\b"),
        risk=RiskLevel.HIGH,
        attributes=("age",),
        statutes=("ADA_2004", "FWA_351", "ADA_QLD_1991"),
        explanation=(
            "'Recent graduate' is a proxy for age: it excludes career changers "
            "and people who returned to study later. If the real requirement is "
            "a junior level of experience, measure experience directly."
        ),
        rewrite="0-2 years of professional experience since completing the qualification.",
        field_alternative="total_years_experience",
    ),
    RiskPattern(
        id="australian_educated",
        pattern=_compile(
            r"\baustralian\s+(university|uni|degree|qualification|educated|education)\b"
            r"|\bstudied in australia\b|\bdegree from an? australian\b"
        ),
        risk=RiskLevel.HIGH,
        attributes=("national or ethnic origin", "race"),
        statutes=("RDA_1975", "FWA_351", "ADA_QLD_1991"),
        explanation=(
            "Requiring an Australian institution screens on country of origin "
            "when the underlying need is a qualification level. Overseas awards "
            "have recognised Australian equivalents."
        ),
        rewrite="Qualification at AQF level 7 or above, or an assessed overseas equivalent.",
        field_alternative="highest_aqf",
    ),
    RiskPattern(
        id="local_experience",
        pattern=_compile(r"\b(australian|local)\s+(work\s+)?experience\b|\bexperience in the australian market\b"),
        risk=RiskLevel.REVIEW,
        attributes=("national or ethnic origin", "race"),
        statutes=("RDA_1975", "FWA_351"),
        explanation=(
            "'Australian experience' is lawful only where something specific to "
            "the Australian context is genuinely required, such as knowledge of "
            "a local regulatory regime. As a blanket requirement it screens on "
            "national origin. " + INDIRECT_DISCRIMINATION_NOTE
        ),
        rewrite="Name the specific local knowledge required, e.g. 'working knowledge of the Australian Privacy Principles'.",
    ),
    RiskPattern(
        id="age_coded",
        pattern=_compile(
            r"\b(young|youthful|energetic|digital native|recent school leaver|mature[- ]aged"
            r"|over \d{2}|under \d{2}|aged? \d{2}\s*(-|to)\s*\d{2})\b"
        ),
        risk=RiskLevel.HIGH,
        attributes=("age",),
        statutes=("ADA_2004", "FWA_351", "ADA_QLD_1991"),
        explanation=(
            "Age-coded language screens on age rather than on capability. "
            "'Energetic' and 'digital native' are read by applicants as age limits."
        ),
        rewrite="State the capability, e.g. 'comfortable learning new tooling quickly' or a specific tool proficiency.",
    ),
    RiskPattern(
        id="career_gap",
        pattern=_compile(
            r"\b(no|without)\s+(career\s+)?(gaps?|breaks?)\b"
            r"|\bcontinuous (employment|work history)\b|\buninterrupted (career|employment)\b"
        ),
        risk=RiskLevel.HIGH,
        attributes=("sex", "family or carer's responsibilities", "disability"),
        statutes=("SDA_1984", "DDA_1992", "FWA_351"),
        explanation=(
            "Career breaks are taken disproportionately by women, carers and "
            "people managing a disability. Excluding gaps screens on those "
            "attributes rather than on current capability. "
            + INDIRECT_DISCRIMINATION_NOTE
        ),
        rewrite="Skills current within the last 3 years, evidenced by recent work or projects.",
    ),
    RiskPattern(
        id="availability_always",
        pattern=_compile(
            r"\b24/7 availability\b|\balways available\b|\bno family commitments\b"
            r"|\bno dependants?\b|\bable to work any hours\b"
        ),
        risk=RiskLevel.HIGH,
        attributes=("sex", "family or carer's responsibilities"),
        statutes=("SDA_1984", "FWA_351", "ADA_QLD_1991"),
        explanation=(
            "Open-ended availability requirements exclude carers, who are "
            "predominantly women, without measuring the job's actual hours."
        ),
        rewrite="State the actual roster, e.g. 'participates in an on-call rotation of one week in six'.",
    ),
    RiskPattern(
        id="gendered_language",
        pattern=_compile(
            r"\b(salesman|salesmen|foreman|handyman|waitress|hostess|he/his required"
            r"|\bmale\b|\bfemale\b|\bmen only\b|\bwomen only\b)\b"
        ),
        risk=RiskLevel.REVIEW,
        attributes=("sex", "gender identity"),
        statutes=("SDA_1984", "FWA_351", "ADA_QLD_1991"),
        explanation=(
            "Gendered role language signals a sex preference. A sex requirement "
            "is lawful only where it is a genuine occupational requirement, "
            "which is narrow and must be documented."
        ),
        rewrite="Use the neutral role title, e.g. 'salesperson', and state the task requirement instead.",
    ),
    RiskPattern(
        id="physical_ability",
        pattern=_compile(
            r"\b(able[- ]bodied|physically fit|no (health|medical) (issues|conditions)"
            r"|must be able to (stand|lift)(?! up to \d))\b"
        ),
        risk=RiskLevel.REVIEW,
        attributes=("disability",),
        statutes=("DDA_1992", "FWA_351", "ADA_QLD_1991"),
        explanation=(
            "A general fitness requirement screens on disability. Only the "
            "inherent requirements of the role may be applied, and reasonable "
            "adjustments must be considered."
        ),
        rewrite="State the inherent physical requirement with a measure, e.g. 'lift up to 15kg with reasonable adjustments available'.",
    ),
    RiskPattern(
        id="religion_appearance",
        pattern=_compile(r"\bclean[- ]shaven\b|\bno (head\s*)?(covering|scarf|turban)\b|\bchristian values\b|\bno visible tattoos or religious\b"),
        risk=RiskLevel.HIGH,
        attributes=("religion", "race"),
        statutes=("FWA_351", "ADA_QLD_1991", "RDA_1975"),
        explanation=(
            "Appearance rules of this kind exclude people whose religion "
            "requires a beard or head covering, without a job-based reason. "
            + INDIRECT_DISCRIMINATION_NOTE
        ),
        rewrite="State the genuine safety requirement, e.g. 'must maintain a seal on respiratory PPE', and consider adjustments.",
    ),
    RiskPattern(
        id="citizenship_only",
        pattern=_compile(r"\baustralian citizens? only\b|\bmust be an? australian citizen\b|\bcitizens? only\b"),
        risk=RiskLevel.REVIEW,
        attributes=("national or ethnic origin",),
        statutes=("RDA_1975", "FWA_351"),
        explanation=(
            "Citizenship requirements are lawful where genuinely required, for "
            "example a role needing a security clearance. Applied without that "
            "basis they exclude permanent residents and visa holders who hold "
            "full work rights, which tracks national origin. Record the basis."
        ),
        rewrite="If the role needs clearance, say so: 'eligible for an Australian Government security clearance'. Otherwise: 'holds unrestricted Australian work rights'.",
        field_alternative="work_rights",
    ),
    RiskPattern(
        id="birthplace",
        pattern=_compile(r"\bborn in australia\b|\baustralian[- ]born\b|\bbirthplace\b|\bcountry of birth\b"),
        risk=RiskLevel.HIGH,
        attributes=("national or ethnic origin", "race"),
        statutes=("RDA_1975", "FWA_351", "ADA_QLD_1991"),
        explanation="Place of birth is a direct marker of national origin and has no bearing on capability.",
        rewrite="Holds unrestricted Australian work rights.",
        field_alternative="work_rights",
    ),
    RiskPattern(
        id="name_or_photo",
        pattern=_compile(
            r"\b(anglo|western|english)[- ](sounding\s+)?name\b|\beasy to pronounce\b"
            r"|\bphoto (required|attached)\b|\bdate of birth\b|\bmarital status\b"
        ),
        risk=RiskLevel.HIGH,
        attributes=("race", "national or ethnic origin", "age", "marital status"),
        statutes=("RDA_1975", "ADA_2004", "FWA_351", "PRIVACY_1988"),
        explanation=(
            "Collecting or screening on name style, photo, date of birth or "
            "marital status invites demographic bias and collects more personal "
            "information than the decision reasonably requires."
        ),
        rewrite="Remove the requirement. Screen on the skills and experience the role needs.",
    ),
    RiskPattern(
        id="postcode_or_suburb",
        pattern=_compile(r"\b(lives? in|located in|resident of|from)\s+(the\s+)?(inner|northern|southern|eastern|western)\b|\bpostcode\b|\bgood suburb\b"),
        risk=RiskLevel.REVIEW,
        attributes=("race", "national or ethnic origin", "social origin"),
        statutes=("RDA_1975", "FWA_351"),
        explanation=(
            "Suburb and postcode correlate strongly with ethnicity and "
            "socio-economic background. A commute requirement should be stated "
            "as one. " + INDIRECT_DISCRIMINATION_NOTE
        ),
        rewrite="State the actual constraint, e.g. 'able to attend the Brisbane CBD office two days per week'.",
    ),
]


def statute_citations(codes: tuple[str, ...] | list[str]) -> list[str]:
    return [STATUTES[code] for code in codes if code in STATUTES]
