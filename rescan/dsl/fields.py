"""The query vocabulary: every field a rule may test, and every one it may not.

This is the single source of truth. The parser validates identifiers against
it, the evaluator reads values through it, the prompt renders it for the model,
and the API serves it to an editor. Adding a field here is the only step needed
to make it queryable everywhere.

Three layers give the language its reach:

* Named fields — dozens of values and derived measures on the profile
  (`years_experience`, `longest_role_months`, `technical_skills`, `has_degree`).
* Records — `skill`, `role`, `qualification`, `project`, quantified with
  `ANY <record> WHERE ...` and aggregated with `COUNT(...)`, `SUM(...)`,
  `MAX(...)`, `MIN(...)`, `AVG(...)`, so every record field is a queryable
  quantity in combination with any filter.
* `ASK "..."` — a question the model answers from the anonymized profile.

Absence is unknown, never zero: a reader returns None when the resume does not
state the value, and the evaluator turns None into "sent to manual review".

Forbidden identifiers are listed deliberately, with their legal basis, so that a
rule naming one fails at parse time with a citation rather than at evaluation
time with a KeyError. The model cannot smuggle a proxy through a field name.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal

from rescan.rules.statutes import STATUTES
from rescan.schemas import AnonymizedProfile, Experience, Skill, WorkRightsStatus

Kind = Literal["number", "text", "enum", "bool", "list"]


@dataclass(frozen=True)
class FieldSpec:
    name: str
    kind: Kind
    label: str
    description: str
    reader: Callable[[Any], Any]
    # (subject, target) templates so numeric reasons read as a sentence.
    phrasing: tuple[str, str] | None = None
    enum_values: tuple[str, ...] = ()
    group: str = "general"


@dataclass(frozen=True)
class RecordSpec:
    """A list of structured records a rule can quantify over or aggregate."""

    name: str
    plural: str
    description: str
    reader: Callable[[AnonymizedProfile], list[Any]]
    fields: dict[str, FieldSpec]
    summary: Callable[[Any], str]


@dataclass(frozen=True)
class ForbiddenField:
    name: str
    reason: str
    statutes: tuple[str, ...]
    alternative: str

    def citations(self) -> list[str]:
        return [STATUTES[code] for code in self.statutes if code in STATUTES]


# --------------------------------------------------------------------------
# Shared readers
# --------------------------------------------------------------------------

SENIORITY_LADDER = (
    "intern", "graduate", "junior", "mid", "senior", "lead", "principal",
    "manager", "head", "director", "executive",
)
LEADERSHIP_LEVELS = {"lead", "principal", "manager", "head", "director", "executive"}
SKILL_CATEGORIES = ("technical", "domain", "language", "tool", "soft", "other")
PROFICIENCIES = ("beginner", "intermediate", "advanced", "expert")
EMPLOYMENT_TYPES = ("permanent", "contract", "casual", "internship", "freelance", "volunteer", "other")

WORK_RIGHTS_VALUES = tuple(status.value for status in WorkRightsStatus if status is not WorkRightsStatus.UNKNOWN)
# `unrestricted` is not a status but a property of one; the evaluator reads it
# from `work_rights.unrestricted` when a rule says `work_rights IS unrestricted`.
WORK_RIGHTS_PSEUDO = "unrestricted"


def _none_if_empty(values: list[Any]) -> list[Any] | None:
    """An empty list means the resume did not say, which is unknown, not zero."""
    return values or None


def _count(values: list[Any]) -> int | None:
    return len(values) if values else None


def _work_rights_status(profile: AnonymizedProfile) -> str | None:
    status = profile.work_rights.status
    return None if status is WorkRightsStatus.UNKNOWN else status.value


def _months(roles: list[Experience]) -> list[float]:
    return [role.months for role in roles if role.months is not None]


def _latest_role(profile: AnonymizedProfile) -> Experience | None:
    """The current role if there is one, else the first listed (resumes lead with the latest)."""
    for role in profile.experience:
        if role.is_current:
            return role
    return profile.experience[0] if profile.experience else None


def _skills_in(profile: AnonymizedProfile, *categories: str) -> list[str]:
    return [skill.name for skill in profile.skills if skill.category in categories]


def _seniority_index(value: str | None) -> int | None:
    return SENIORITY_LADDER.index(value) if value in SENIORITY_LADDER else None


def _highest_seniority(profile: AnonymizedProfile) -> str | None:
    levels = [_seniority_index(role.seniority) for role in profile.experience]
    known = [level for level in levels if level is not None]
    return SENIORITY_LADDER[max(known)] if known else None


def _highest_qualification(profile: AnonymizedProfile) -> str | None:
    ranked = [q for q in profile.qualifications if q.aqf_level is not None]
    if ranked:
        return max(ranked, key=lambda q: q.aqf_level).title
    return profile.qualifications[0].title if profile.qualifications else None


def _technologies(profile: AnonymizedProfile) -> list[str]:
    seen: list[str] = []
    sources = (
        [tech for role in profile.experience for tech in role.technologies]
        + [tech for project in profile.projects for tech in project.technologies]
        + _skills_in(profile, "tool", "technical")
    )
    for item in sources:
        if item and item.lower() not in {s.lower() for s in seen}:
            seen.append(item)
    return seen


def _aqf_at_least(level: int) -> Callable[[AnonymizedProfile], bool | None]:
    return lambda p: None if p.highest_aqf is None else p.highest_aqf >= level


def _bool_from_number(reader: Callable[[AnonymizedProfile], float | None]) -> Callable[[AnonymizedProfile], bool | None]:
    def read(profile: AnonymizedProfile) -> bool | None:
        value = reader(profile)
        return None if value is None else value > 0

    return read


def _has_management(profile: AnonymizedProfile) -> bool | None:
    if profile.management_years or profile.people_managed_max:
        return True
    if any(role.seniority in LEADERSHIP_LEVELS or role.team_size for role in profile.experience):
        return True
    return False if profile.experience else None


# --------------------------------------------------------------------------
# Named fields
# --------------------------------------------------------------------------


def _n(name: str, label: str, description: str, reader, phrasing=None, group="experience") -> FieldSpec:
    return FieldSpec(name, "number", label, description, reader, phrasing=phrasing, group=group)


def _l(name: str, label: str, description: str, reader, group="skills") -> FieldSpec:
    return FieldSpec(name, "list", label, description, lambda p: _none_if_empty(reader(p)), group=group)


def _b(name: str, label: str, description: str, reader, group="derived") -> FieldSpec:
    return FieldSpec(name, "bool", label, description, reader, group=group)


def _t(name: str, label: str, description: str, reader, group="experience") -> FieldSpec:
    return FieldSpec(name, "text", label, description, reader, group=group)


_FIELD_LIST: list[FieldSpec] = [
    # --- experience: totals and durations ---
    _n("years_experience", "years of professional experience",
       "Total years of professional experience, from employment dates, excluding study.",
       lambda p: p.total_years_experience,
       ("Candidate has {observed:g} years of professional experience", "{target:g} years")),
    _n("role_count", "number of roles listed", "How many employment entries the resume lists.",
       lambda p: _count(p.experience), ("Candidate lists {observed:g} roles", "{target:g}")),
    _n("current_role_count", "number of current roles", "How many roles are marked current.",
       lambda p: sum(1 for r in p.experience if r.is_current) if p.experience else None,
       ("Candidate holds {observed:g} current roles", "{target:g}")),
    _n("total_role_months", "total months across listed roles", "Sum of role durations in months.",
       lambda p: sum(_months(p.experience)) if _months(p.experience) else None,
       ("Candidate's listed roles total {observed:g} months", "{target:g} months")),
    _n("longest_role_months", "longest role in months", "Duration of the longest single role.",
       lambda p: max(_months(p.experience)) if _months(p.experience) else None,
       ("Candidate's longest role lasted {observed:g} months", "{target:g} months")),
    _n("shortest_role_months", "shortest role in months", "Duration of the shortest single role.",
       lambda p: min(_months(p.experience)) if _months(p.experience) else None,
       ("Candidate's shortest role lasted {observed:g} months", "{target:g} months")),
    _n("average_role_months", "average role length in months", "Mean role duration; a tenure measure.",
       lambda p: round(sum(_months(p.experience)) / len(_months(p.experience)), 1) if _months(p.experience) else None,
       ("Candidate's roles average {observed:g} months", "{target:g} months")),
    _n("current_role_months", "months in the current role", "Duration of the role marked current.",
       lambda p: next((r.months for r in p.experience if r.is_current and r.months is not None), None),
       ("Candidate has {observed:g} months in the current role", "{target:g} months")),
    _n("latest_role_months", "months in the latest role", "Duration of the most recent role.",
       lambda p: (_latest_role(p).months if _latest_role(p) else None),
       ("Candidate's latest role lasted {observed:g} months", "{target:g} months")),
    _n("leadership_role_count", "number of leadership roles",
       "Roles at lead, principal, manager, head, director or executive level.",
       lambda p: sum(1 for r in p.experience if r.seniority in LEADERSHIP_LEVELS) if p.experience else None,
       ("Candidate lists {observed:g} leadership roles", "{target:g}")),
    _n("contract_role_count", "number of contract roles", "Roles marked contract or freelance.",
       lambda p: sum(1 for r in p.experience if r.employment_type in {"contract", "freelance"}) if p.experience else None,
       ("Candidate lists {observed:g} contract roles", "{target:g}")),
    _n("permanent_role_count", "number of permanent roles", "Roles marked permanent.",
       lambda p: sum(1 for r in p.experience if r.employment_type == "permanent") if p.experience else None,
       ("Candidate lists {observed:g} permanent roles", "{target:g}")),
    _n("management_years", "years managing people", "Years spent in roles that managed people.",
       lambda p: p.management_years, ("Candidate has {observed:g} years managing people", "{target:g} years")),
    _n("people_managed_max", "largest team led", "Largest team the candidate reports leading.",
       lambda p: p.people_managed_max, ("Candidate has led a team of {observed:g}", "{target:g}")),
    _n("seniority_level", "seniority level (0 intern - 10 executive)",
       "Highest seniority reached, as a rank on the ladder intern(0) graduate junior mid senior(4) lead principal manager head director executive(10).",
       lambda p: _seniority_index(_highest_seniority(p)),
       ("Candidate's highest seniority is level {observed:g}", "level {target:g}")),
    _n("industry_count", "number of industries worked in", "Distinct industries across roles.",
       lambda p: _count(sorted({r.industry.lower() for r in p.experience if r.industry})),
       ("Candidate has worked in {observed:g} industries", "{target:g}")),
    # --- qualifications ---
    _n("aqf", "highest qualification (AQF level)",
       "Highest qualification as an AQF level 1-10, assigned deterministically: diploma 5, advanced diploma 6, "
       "bachelor 7, honours / graduate certificate or diploma 8, masters 9, doctorate 10. Overseas awards map to their equivalent.",
       lambda p: p.highest_aqf,
       ("Candidate's highest qualification is AQF level {observed:g}", "AQF level {target:g}"), group="qualifications"),
    _n("qualification_count", "number of qualifications", "How many awards are listed.",
       lambda p: _count(p.qualifications), ("Candidate lists {observed:g} qualifications", "{target:g}"), group="qualifications"),
    _n("degree_count", "number of degrees (AQF 7+)", "Awards at bachelor level or above.",
       lambda p: sum(1 for q in p.qualifications if (q.aqf_level or 0) >= 7) if p.qualifications else None,
       ("Candidate holds {observed:g} degrees", "{target:g}"), group="qualifications"),
    _n("publications_count", "number of publications", "Papers or patents listed.",
       lambda p: p.publications_count, ("Candidate lists {observed:g} publications", "{target:g}"), group="qualifications"),
    # --- skills: counts ---
    _n("skill_count", "number of skills", "How many skills are listed.",
       lambda p: _count(p.skills), ("Candidate lists {observed:g} skills", "{target:g}"), group="skills"),
    _n("technical_skill_count", "number of technical skills", "Skills in the technical or tool categories.",
       lambda p: _count(_skills_in(p, "technical", "tool")), ("Candidate lists {observed:g} technical skills", "{target:g}"), group="skills"),
    _n("max_skill_years", "most years with any one skill", "Highest per-skill years figure stated.",
       lambda p: max((s.years for s in p.skills if s.years is not None), default=None),
       ("Candidate's deepest skill has {observed:g} years", "{target:g} years"), group="skills"),
    _n("certification_count", "number of certifications", "Certifications held.",
       lambda p: _count(p.certifications), ("Candidate holds {observed:g} certifications", "{target:g}"), group="skills"),
    _n("licence_count", "number of licences", "Licences and registrations held.",
       lambda p: _count(p.licences), ("Candidate holds {observed:g} licences", "{target:g}"), group="skills"),
    _n("language_count", "number of languages", "Languages the candidate uses.",
       lambda p: _count(p.languages), ("Candidate lists {observed:g} languages", "{target:g}"), group="skills"),
    _n("project_count", "number of projects", "Projects listed.",
       lambda p: _count(p.projects), ("Candidate lists {observed:g} projects", "{target:g}"), group="skills"),
    _n("technology_count", "number of technologies", "Distinct technologies across roles, projects and skills.",
       lambda p: _count(_technologies(p)), ("Candidate names {observed:g} technologies", "{target:g}"), group="skills"),
    _n("availability_weeks", "notice period in weeks", "Notice period, when stated.",
       lambda p: p.availability_weeks, ("Candidate's notice period is {observed:g} weeks", "{target:g} weeks"), group="general"),
    # --- lists ---
    _l("skills", "skills", "Skill names the resume evidences. Matching is loose: \"AWS\" matches \"AWS Solutions Architect\".",
       lambda p: [s.name for s in p.skills]),
    _l("technical_skills", "technical skills", "Skills in the technical category.", lambda p: _skills_in(p, "technical")),
    _l("tool_skills", "tools", "Skills in the tool category.", lambda p: _skills_in(p, "tool")),
    _l("domain_skills", "domain skills", "Skills in the domain category, e.g. 'payments', 'clinical trials'.",
       lambda p: _skills_in(p, "domain")),
    _l("soft_skills", "soft skills", "Skills in the soft category.", lambda p: _skills_in(p, "soft")),
    _l("language_skills", "language skills", "Skills categorised as languages (human or programming, as the resume framed them).",
       lambda p: _skills_in(p, "language")),
    _l("expert_skills", "skills at advanced or expert level", "Skills the resume rates advanced or expert.",
       lambda p: [s.name for s in p.skills if s.proficiency in {"advanced", "expert"}]),
    _l("skills_with_years", "skills with a stated duration", "Skills for which the resume gives years of use.",
       lambda p: [s.name for s in p.skills if s.years is not None]),
    _l("technologies", "technologies", "Union of technologies named on roles, projects, and tool/technical skills.",
       _technologies),
    _l("languages", "languages", "Languages the candidate uses.", lambda p: list(p.languages)),
    _l("certifications", "certifications", "Certifications held.", lambda p: list(p.certifications)),
    _l("licences", "licences", "Licences and registrations held, e.g. driver's, forklift, AHPRA.", lambda p: list(p.licences)),
    _l("role_titles", "role titles", "Titles of every listed role.", lambda p: [r.title for r in p.experience if r.title], group="experience"),
    _l("current_role_titles", "current role titles", "Titles of roles marked current.",
       lambda p: [r.title for r in p.experience if r.is_current and r.title], group="experience"),
    _l("industries", "industries", "Industries across listed roles.", lambda p: [r.industry for r in p.experience if r.industry], group="experience"),
    _l("employment_types", "employment types", "Employment types across listed roles.",
       lambda p: [r.employment_type for r in p.experience if r.employment_type], group="experience"),
    _l("seniorities", "seniority levels held", "Seniority of every listed role.",
       lambda p: [r.seniority for r in p.experience if r.seniority], group="experience"),
    _l("qualification_titles", "qualification titles", "Titles of every award.", lambda p: [q.title for q in p.qualifications], group="qualifications"),
    _l("fields_of_study", "fields of study", "Disciplines of every award.",
       lambda p: [q.field_of_study for q in p.qualifications if q.field_of_study], group="qualifications"),
    _l("aqf_labels", "AQF labels", "AQF level labels of every award, e.g. 'Bachelor Degree'.",
       lambda p: [q.aqf_label for q in p.qualifications if q.aqf_label], group="qualifications"),
    _l("project_names", "project names", "Names of listed projects.", lambda p: [pr.name for pr in p.projects]),
    _l("project_technologies", "project technologies", "Technologies named on projects.",
       lambda p: [t for pr in p.projects for t in pr.technologies]),
    # --- text ---
    _t("current_role_title", "current role title", "Title of the role marked current.",
       lambda p: next((r.title for r in p.experience if r.is_current and r.title), None)),
    _t("latest_role_title", "latest role title", "Title of the most recent role.",
       lambda p: (_latest_role(p).title if _latest_role(p) else None)),
    _t("highest_qualification", "highest qualification", "Title of the highest-level award.",
       _highest_qualification, group="qualifications"),
    _t("security_clearance", "security clearance", "Clearance held if stated, e.g. 'Baseline', 'NV1'. Lawful only where the role genuinely needs it.",
       lambda p: p.security_clearance, group="general"),
    # --- enums ---
    FieldSpec("work_rights", "enum", "work rights status",
              "Work rights as stated in the resume. Use `work_rights IS unrestricted` for the lawful 'holds full Australian work "
              "rights' requirement; only use a specific status such as `citizen` where a security clearance genuinely requires it.",
              _work_rights_status, enum_values=WORK_RIGHTS_VALUES + (WORK_RIGHTS_PSEUDO,), group="general"),
    FieldSpec("highest_seniority", "enum", "highest seniority held", "Highest seniority reached across roles.",
              _highest_seniority, enum_values=SENIORITY_LADDER, group="experience"),
    FieldSpec("latest_seniority", "enum", "seniority of the latest role", "Seniority of the most recent role.",
              lambda p: (_latest_role(p).seniority if _latest_role(p) else None), enum_values=SENIORITY_LADDER, group="experience"),
    FieldSpec("latest_employment_type", "enum", "employment type of the latest role", "Employment type of the most recent role.",
              lambda p: (_latest_role(p).employment_type if _latest_role(p) else None), enum_values=EMPLOYMENT_TYPES, group="experience"),
    # --- booleans ---
    _b("has_current_role", "a current role", "Whether any role is marked current.",
       lambda p: any(r.is_current for r in p.experience) if p.experience else None),
    _b("has_degree", "a degree (AQF 7+)", "Bachelor level or above.", _aqf_at_least(7)),
    _b("has_postgraduate", "a postgraduate award (AQF 8+)", "Honours, graduate certificate/diploma or above.", _aqf_at_least(8)),
    _b("has_masters", "a masters (AQF 9+)", "Masters or above.", _aqf_at_least(9)),
    _b("has_doctorate", "a doctorate (AQF 10)", "Doctorate.", _aqf_at_least(10)),
    _b("has_certifications", "certifications", "Whether any certification is listed.",
       lambda p: True if p.certifications else None),
    _b("has_licence", "a licence", "Whether any licence or registration is listed.",
       lambda p: True if p.licences else None),
    _b("has_clearance", "a security clearance", "Whether a clearance is stated.",
       lambda p: True if p.security_clearance else None),
    _b("has_management_experience", "management experience", "Managed people, or held a lead-or-above role.",
       _has_management),
    _b("has_projects", "listed projects", "Whether any project is listed.", lambda p: True if p.projects else None),
    _b("has_publications", "publications", "Whether any publication is listed.",
       _bool_from_number(lambda p: p.publications_count)),
    _b("multilingual", "two or more languages", "Whether two or more languages are listed.",
       lambda p: (len(p.languages) >= 2) if p.languages else None),
    _b("work_rights_unrestricted", "unrestricted work rights", "Whether the resume states unrestricted Australian work rights.",
       lambda p: p.work_rights.unrestricted, group="general"),
    _b("requires_sponsorship", "a sponsorship requirement", "Whether the resume states sponsorship is required.",
       lambda p: None if _work_rights_status(p) is None else p.work_rights.status is WorkRightsStatus.REQUIRES_SPONSORSHIP,
       group="general"),
]

FIELDS: dict[str, FieldSpec] = {spec.name: spec for spec in _FIELD_LIST}

# Names the previous predicate vocabulary and models tend to reach for.
ALIASES: dict[str, str] = {
    "total_years_experience": "years_experience",
    "years": "years_experience",
    "experience": "years_experience",
    "experience_years": "years_experience",
    "years_of_experience": "years_experience",
    "highest_aqf": "aqf",
    "aqf_level": "aqf",
    "qualification_level": "aqf",
    "work_rights_status": "work_rights",
    "certs": "certifications",
    "skill": "skills",
    "language": "languages",
    "certification": "certifications",
    "licenses": "licences",
    "licence": "licences",
    "license": "licences",
    "tools": "tool_skills",
    "titles": "role_titles",
    "roles": "role_titles",
    "positions": "role_titles",
    "degrees": "qualification_titles",
    "qualifications": "qualification_titles",
    "seniority": "highest_seniority",
    "team_size": "people_managed_max",
    "clearance": "security_clearance",
    "notice_period_weeks": "availability_weeks",
    "projects": "project_names",
    "tech": "technologies",
    "tech_stack": "technologies",
    "stack": "technologies",
}

# --------------------------------------------------------------------------
# Records for ANY ... WHERE and aggregates
# --------------------------------------------------------------------------


def _rf(name: str, kind: Kind, label: str, description: str, reader, phrasing=None, enum_values=()) -> FieldSpec:
    return FieldSpec(name, kind, label, description, reader, phrasing=phrasing, enum_values=enum_values)


RECORDS: dict[str, RecordSpec] = {
    "skill": RecordSpec(
        name="skill",
        plural="skills",
        description="One skill entry: `name` (text), `category` (technical | domain | language | tool | soft | other), "
                    "`years` (number, often absent), `proficiency` (beginner | intermediate | advanced | expert), `evidence` (text).",
        reader=lambda p: list(p.skills),
        fields={
            "name": _rf("name", "text", "skill name", "Skill name.", lambda s: s.name),
            "category": _rf("category", "enum", "skill category", "Skill category.", lambda s: s.category, enum_values=SKILL_CATEGORIES),
            "years": _rf("years", "number", "years using the skill", "Years of experience with this skill.",
                         lambda s: s.years, ("{observed:g} years with the skill", "{target:g} years")),
            "proficiency": _rf("proficiency", "enum", "stated proficiency", "Proficiency the resume states.",
                               lambda s: s.proficiency, enum_values=PROFICIENCIES),
            "evidence": _rf("evidence", "text", "evidence for the skill", "Where the skill was demonstrated.", lambda s: s.evidence),
        },
        summary=lambda s: s.name + (f" ({s.years:g} years)" if s.years is not None else ""),
    ),
    "role": RecordSpec(
        name="role",
        plural="roles",
        description="One employment entry: `title` (text), `months` (number), `is_current` (boolean), `seniority` "
                    "(intern … executive), `industry` (text), `employment_type` (permanent | contract | casual | internship | "
                    "freelance | volunteer | other), `team_size` (number), `technologies` (list), `summary` (text).",
        reader=lambda p: list(p.experience),
        fields={
            "title": _rf("title", "text", "role title", "Job title as written.", lambda r: r.title),
            "months": _rf("months", "number", "months in the role", "Duration of the role in months.",
                          lambda r: r.months, ("{observed:g} months in the role", "{target:g} months")),
            "is_current": _rf("is_current", "bool", "currently held", "Whether the role is current.", lambda r: r.is_current),
            "seniority": _rf("seniority", "enum", "seniority", "Seniority implied by the title.", lambda r: r.seniority, enum_values=SENIORITY_LADDER),
            "industry": _rf("industry", "text", "industry", "Sector of the employer.", lambda r: r.industry),
            "employment_type": _rf("employment_type", "enum", "employment type", "Employment type.",
                                   lambda r: r.employment_type, enum_values=EMPLOYMENT_TYPES),
            "team_size": _rf("team_size", "number", "team size led", "People managed or led in the role.",
                             lambda r: r.team_size, ("led a team of {observed:g}", "{target:g}")),
            "technologies": _rf("technologies", "list", "technologies used", "Technologies named for the role.",
                                lambda r: _none_if_empty(list(r.technologies))),
            "summary": _rf("summary", "text", "role summary", "What the person did in the role.", lambda r: r.summary),
        },
        summary=lambda r: (r.title or "untitled role") + (f" ({r.months:g} months)" if r.months is not None else ""),
    ),
    "qualification": RecordSpec(
        name="qualification",
        plural="qualifications",
        description="One qualification: `title` (text), `field_of_study` (text), `aqf` (number 1-10), `aqf_label` (text).",
        reader=lambda p: list(p.qualifications),
        fields={
            "title": _rf("title", "text", "qualification title", "Award title as written.", lambda q: q.title),
            "field_of_study": _rf("field_of_study", "text", "field of study", "Discipline of the award.", lambda q: q.field_of_study),
            "aqf": _rf("aqf", "number", "AQF level", "AQF level of this award.",
                       lambda q: q.aqf_level, ("AQF level {observed:g}", "AQF level {target:g}")),
            "aqf_label": _rf("aqf_label", "text", "AQF label", "AQF label, e.g. 'Bachelor Degree'.", lambda q: q.aqf_label),
        },
        summary=lambda q: q.title + (f" (AQF {q.aqf_level})" if q.aqf_level is not None else ""),
    ),
    "project": RecordSpec(
        name="project",
        plural="projects",
        description="One project: `name` (text), `summary` (text), `technologies` (list), `months` (number).",
        reader=lambda p: list(p.projects),
        fields={
            "name": _rf("name", "text", "project name", "Project name.", lambda pr: pr.name),
            "summary": _rf("summary", "text", "project summary", "What was built.", lambda pr: pr.summary),
            "technologies": _rf("technologies", "list", "technologies used", "Technologies named for the project.",
                                lambda pr: _none_if_empty(list(pr.technologies))),
            "months": _rf("months", "number", "months on the project", "Duration in months.",
                          lambda pr: pr.months, ("{observed:g} months on the project", "{target:g} months")),
        },
        summary=lambda pr: pr.name,
    ),
}

RECORD_ALIASES: dict[str, str] = {
    "skills": "skill",
    "roles": "role",
    "experience": "role",
    "job": "role",
    "jobs": "role",
    "position": "role",
    "positions": "role",
    "employment": "role",
    "qualifications": "qualification",
    "degree": "qualification",
    "degrees": "qualification",
    "education": "qualification",
    "award": "qualification",
    "projects": "project",
}

AGGREGATES = ("COUNT", "SUM", "MAX", "MIN", "AVG")

# --------------------------------------------------------------------------
# Forbidden identifiers
# --------------------------------------------------------------------------

FORBIDDEN: dict[str, ForbiddenField] = {
    name: ForbiddenField(name, reason, statutes, alternative)
    for name, reason, statutes, alternative in [
        ("region", "location is a proxy for race and social origin; suburb and region correlate strongly with ethnicity and socio-economic background",
         ("RDA_1975", "ADA_QLD_1991"), "State the attendance requirement in the role description; it is not a screening field."),
        ("suburb", "location is a proxy for race and social origin", ("RDA_1975", "ADA_QLD_1991"), "Not a screening field."),
        ("postcode", "location is a proxy for race and social origin", ("RDA_1975", "ADA_QLD_1991"), "Not a screening field."),
        ("location", "location is a proxy for race and social origin", ("RDA_1975", "ADA_QLD_1991"), "Not a screening field."),
        ("city", "location is a proxy for race and social origin", ("RDA_1975", "ADA_QLD_1991"), "Not a screening field."),
        ("state", "location is a proxy for race and social origin", ("RDA_1975", "ADA_QLD_1991"), "Not a screening field."),
        ("institution", "institution name reintroduces demographic signal after names are removed, and prefers local over overseas study",
         ("RDA_1975", "ADA_QLD_1991"), "aqf >= 7, or ANY qualification WHERE field_of_study HAS ANY (...)"),
        ("institution_tiers", "institution tier is a prestige proxy that tracks social origin and country of study", ("RDA_1975", "ADA_QLD_1991"), "aqf >= 7"),
        ("institution_tier", "institution tier is a prestige proxy that tracks social origin and country of study", ("RDA_1975", "ADA_QLD_1991"), "aqf >= 7"),
        ("university", "institution name reintroduces demographic signal and prefers local over overseas study", ("RDA_1975", "ADA_QLD_1991"), "aqf >= 7"),
        ("universities", "institution name reintroduces demographic signal and prefers local over overseas study", ("RDA_1975", "ADA_QLD_1991"), "aqf >= 7"),
        ("employer", "employer prestige is an unvalidated proxy for social origin and penalises overseas experience",
         ("RDA_1975", "ADA_QLD_1991"), "Test the capability instead, e.g. ANY role WHERE months >= 24 AND industry = \"banking\", or ASK \"...\" for a specific achievement."),
        ("employers", "employer prestige is an unvalidated proxy for social origin and penalises overseas experience", ("RDA_1975", "ADA_QLD_1991"), "ANY role WHERE months >= 24"),
        ("company", "employer prestige is an unvalidated proxy for social origin and penalises overseas experience", ("RDA_1975", "ADA_QLD_1991"), "ANY role WHERE months >= 24"),
        ("companies", "employer prestige is an unvalidated proxy for social origin and penalises overseas experience", ("RDA_1975", "ADA_QLD_1991"), "ANY role WHERE months >= 24"),
        ("completion_year", "graduation year is a proxy for age", ("ADA_2004", "ADA_QLD_1991"), "years_experience >= N"),
        ("graduation_year", "graduation year is a proxy for age", ("ADA_2004", "ADA_QLD_1991"), "years_experience >= N"),
        ("start", "role start dates reveal age when read across a career", ("ADA_2004", "ADA_QLD_1991"), "months, or years_experience"),
        ("end", "role end dates reveal age and career breaks", ("ADA_2004", "SDA_1984", "ADA_QLD_1991"), "months, or is_current"),
        ("start_year", "role start dates reveal age when read across a career", ("ADA_2004", "ADA_QLD_1991"), "years_experience >= N"),
        ("age", "age is a protected attribute", ("ADA_2004", "FWA_351", "ADA_QLD_1991"), "years_experience >= N"),
        ("date_of_birth", "age is a protected attribute", ("ADA_2004", "PRIVACY_1988"), "Not a screening field."),
        ("name", "names carry race, national origin and sex; the profile is anonymized before any rule runs", ("RDA_1975", "SDA_1984", "FWA_351"), "Not a screening field."),
        ("full_name", "names carry race, national origin and sex", ("RDA_1975", "SDA_1984", "FWA_351"), "Not a screening field."),
        ("email", "direct identifier", ("PRIVACY_1988",), "Not a screening field."),
        ("phone", "direct identifier", ("PRIVACY_1988",), "Not a screening field."),
        ("gender", "sex and gender identity are protected attributes", ("SDA_1984", "FWA_351", "ADA_QLD_1991"), "Not a screening field."),
        ("sex", "sex is a protected attribute", ("SDA_1984", "FWA_351", "ADA_QLD_1991"), "Not a screening field."),
        ("nationality", "national origin is a protected attribute; work rights are the lawful test", ("RDA_1975", "FWA_351"), "work_rights IS unrestricted"),
        ("citizenship", "citizenship tracks national origin; require it only where a clearance genuinely needs it", ("RDA_1975", "FWA_351"),
         "work_rights IS unrestricted, or work_rights IS citizen with a documented clearance basis"),
        ("country", "country of residence, birth or study is a marker of national origin", ("RDA_1975", "FWA_351"), "work_rights IS unrestricted"),
        ("visa", "visa subclass tracks national origin; the lawful test is whether work rights are unrestricted", ("RDA_1975", "FWA_351"), "work_rights IS unrestricted"),
        ("visa_subclass", "visa subclass tracks national origin", ("RDA_1975", "FWA_351"), "work_rights IS unrestricted"),
        ("religion", "religion is a protected attribute", ("ADA_QLD_1991", "FWA_351"), "Not a screening field."),
        ("disability", "disability is a protected attribute; only inherent requirements may be tested", ("DDA_1992", "FWA_351"), "State the inherent requirement as a capability."),
        ("marital_status", "marital status is a protected attribute", ("SDA_1984", "FWA_351", "ADA_QLD_1991"), "Not a screening field."),
        ("children", "family responsibilities are a protected attribute", ("SDA_1984", "FWA_351", "ADA_QLD_1991"), "Not a screening field."),
        ("summary", "free-text profile matching is unexplainable and re-admits proxies removed elsewhere", ("PRIVACY_1988",),
         "Use a structured field, or ASK \"...\" for a specific, job-related question."),
        ("affiliations", "affiliations signal religion, ethnicity, sex and political belief", ("ADA_QLD_1991", "FWA_351"), "Not a screening field."),
        ("job_relevant_affiliations", "affiliations signal religion, ethnicity, sex and political belief", ("ADA_QLD_1991", "FWA_351"), "certifications HAS ANY (...)"),
        ("career_gap", "career breaks are taken disproportionately by women, carers and people managing a disability", ("SDA_1984", "DDA_1992", "FWA_351"),
         "has_current_role IS TRUE, or skills current within a period"),
        ("gap", "career breaks are taken disproportionately by women, carers and people managing a disability", ("SDA_1984", "DDA_1992", "FWA_351"), "has_current_role IS TRUE"),
        ("photo", "appearance invites demographic bias", ("RDA_1975", "ADA_2004", "PRIVACY_1988"), "Not a screening field."),
        ("accent", "accent is a direct marker of national origin", ("RDA_1975", "FWA_351"), "languages HAS ANY (\"English\")"),
        ("native_language", "language of origin is a marker of national origin", ("RDA_1975", "FWA_351"), "languages HAS ANY (\"English\")"),
    ]
}


def resolve_field(name: str) -> FieldSpec | None:
    key = name.lower()
    return FIELDS.get(ALIASES.get(key, key))


def resolve_record(name: str) -> RecordSpec | None:
    key = name.lower()
    return RECORDS.get(RECORD_ALIASES.get(key, key))


def forbidden(name: str) -> ForbiddenField | None:
    return FORBIDDEN.get(name.lower())


# --------------------------------------------------------------------------
# Reference material for the model and the API
# --------------------------------------------------------------------------

GRAMMAR = """program   := clause (';' clause)*
clause    := ('REQUIRE' | 'PREFER') expr ['WEIGHT' number] ['BECAUSE' "text"]
expr      := and_expr ('OR' and_expr)*
and_expr  := not_expr ('AND' not_expr)*
not_expr  := 'NOT' not_expr | primary
primary   := '(' expr ')'
           | 'ANY' record 'WHERE' expr
           | aggregate '(' record ['.' field] ['WHERE' expr] ')' cmp number
           | field cmp (number | "text")
           | field 'HAS' ('ANY' | 'ALL') '(' "text" (',' "text")* ')'
           | field 'IN' '(' "text" (',' "text")* ')'
           | field 'IS' (identifier | 'TRUE' | 'FALSE')
           | 'ASK' "a yes/no question the model answers from the anonymized profile"
cmp       := '>=' | '<=' | '>' | '<' | '=' | '!='
aggregate := 'COUNT' | 'SUM' | 'MAX' | 'MIN' | 'AVG'
record    := 'skill' | 'role' | 'qualification' | 'project'
comment   := '--' to end of line"""

EXAMPLES = """REQUIRE years_experience >= 5
REQUIRE skills HAS ALL ("Python", "SQL") AND skills HAS ANY ("AWS", "GCP", "Azure")
REQUIRE aqf >= 7 OR years_experience >= 8
REQUIRE work_rights IS unrestricted
REQUIRE ANY qualification WHERE field_of_study HAS ANY ("computer science", "software", "engineering") AND aqf >= 7
REQUIRE ANY role WHERE title HAS ANY ("engineer", "developer") AND months >= 24
REQUIRE COUNT(role WHERE seniority IN ("senior", "lead", "principal")) >= 1
REQUIRE SUM(role.months WHERE industry = "banking") >= 24
REQUIRE MAX(skill.years WHERE name = "Python") >= 3
REQUIRE longest_role_months >= 18 AND has_current_role IS TRUE
PREFER ANY skill WHERE name = "Python" AND years >= 3 WEIGHT 2 BECAUSE "The team's main language."
PREFER certifications HAS ANY ("AWS Certified", "CKA")
PREFER people_managed_max >= 5 OR management_years >= 2
PREFER technologies HAS ANY ("Kafka", "Flink") WEIGHT 1.5
REQUIRE ASK "Has the candidate led an on-call rotation or incident response?"
"""


def _operators_for(kind: Kind) -> str:
    return {
        "number": ">= <= > < = !=",
        "text": "= != HAS ANY HAS ALL",
        "enum": "IS IN = !=",
        "bool": "IS TRUE / IS FALSE",
        "list": "HAS ANY HAS ALL",
    }[kind]


def _field_entry(spec: FieldSpec) -> dict[str, Any]:
    return {
        "name": spec.name,
        "kind": spec.kind,
        "group": spec.group,
        "operators": _operators_for(spec.kind),
        "description": spec.description,
        "values": list(spec.enum_values) or None,
    }


def reference() -> dict[str, Any]:
    """Structured description of the language, for the API and the MCP tool."""
    return {
        "grammar": GRAMMAR,
        "examples": EXAMPLES.strip().splitlines(),
        "fields": [_field_entry(spec) for spec in FIELDS.values()],
        "aliases": dict(ALIASES),
        "records": [
            {
                "name": record.name,
                "description": record.description,
                "fields": [_field_entry(spec) for spec in record.fields.values()],
            }
            for record in RECORDS.values()
        ],
        "aggregates": list(AGGREGATES),
        "forbidden": [
            {"name": item.name, "reason": item.reason, "statutes": item.citations(), "alternative": item.alternative}
            for item in FORBIDDEN.values()
        ],
        "semantics": (
            "Three-valued: a value the resume does not state is unknown, and unknown "
            "never fails a candidate on its own. UNKNOWN AND FALSE is FALSE; UNKNOWN "
            "OR TRUE is TRUE; otherwise unknown propagates and the candidate goes to "
            "manual review. Empty lists are unknown, not zero."
        ),
        "counts": {"fields": len(FIELDS), "records": len(RECORDS),
                   "record_fields": sum(len(r.fields) for r in RECORDS.values()), "forbidden": len(FORBIDDEN)},
    }


def reference_text() -> str:
    """The same reference rendered for a prompt."""
    lines = ["GRAMMAR", GRAMMAR, "", "FIELDS (name (kind; operators): description)"]
    for group in ("experience", "qualifications", "skills", "general", "derived"):
        lines.append(f"  [{group}]")
        for spec in FIELDS.values():
            if spec.group != group:
                continue
            values = f" Values: {', '.join(spec.enum_values)}." if spec.enum_values else ""
            lines.append(f"  - {spec.name} ({spec.kind}; {_operators_for(spec.kind)}): {spec.description}{values}")
    lines.append("")
    lines.append("RECORDS (ANY <record> WHERE <expr>, or COUNT/SUM/MAX/MIN/AVG(<record>[.<field>] WHERE <expr>) <cmp> <number>;"
                 " only the record's own fields are valid inside WHERE)")
    for record in RECORDS.values():
        lines.append(f"- {record.name}: {record.description}")
    lines.append("")
    lines.append("FORBIDDEN IDENTIFIERS (a rule naming one is rejected; use the alternative)")
    for item in FORBIDDEN.values():
        lines.append(f"- {item.name}: {item.reason}. Instead: {item.alternative}")
    lines.append("")
    lines.append("EXAMPLES")
    lines.append(EXAMPLES.strip())
    return "\n".join(lines)
