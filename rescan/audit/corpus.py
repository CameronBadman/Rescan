"""Counterfactual corpora for the bias audit.

A counterfactual pair is the same resume differing only in a demographic
signal — classically the applicant's first name. Any score difference across a
set of variants is attributable to that signal, because nothing else changed.

Two sources:

* `synthetic_corpus` builds variants from the local sample resumes by swapping
  the name (and optionally the institution and suburb, to test proxies beyond
  the name). It runs offline and is the default.
* `huggingface_corpus` pulls the published `nghiemhnlp/bias_resume_public`
  study data, which supplies eight name variants per work history across four
  race groups and two sexes. It needs network access.

Group codes follow that dataset: {w,b,h,a} for the race group paired with
{m,f}. The names are drawn from the audit-study literature, where they are
chosen for how strongly they signal a group in the population being studied.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import httpx

log = logging.getLogger(__name__)

DATASETS_SERVER = "https://datasets-server.huggingface.co"
DATASET = "nghiemhnlp/bias_resume_public"

GROUP_LABELS: dict[str, str] = {
    "wm": "White male-coded name",
    "wf": "White female-coded name",
    "bm": "Black male-coded name",
    "bf": "Black female-coded name",
    "hm": "Hispanic male-coded name",
    "hf": "Hispanic female-coded name",
    "am": "Asian male-coded name",
    "af": "Asian female-coded name",
}

# Used by the offline generator. Kept short and documented rather than
# generated, so the audit is reproducible and reviewable.
SYNTHETIC_NAMES: dict[str, list[str]] = {
    "wm": ["Connor Walsh", "Chase Bradley", "Clay Sutton"],
    "wf": ["Ann Whitfield", "Beth Callahan", "Carole Prescott"],
    "bm": ["Akeem Washington", "Alphonso Jefferson", "Devante Booker"],
    "bf": ["Ashanti Williams", "Lakesha Jefferson", "Latanya Booker"],
    "hm": ["Alvaro Dominguez", "Ezequiel Marquez", "Hipolito Salazar"],
    "hf": ["Marisol Dominguez", "Flor Marquez", "Ivelisse Salazar"],
    "am": ["Cheng Zhao", "Duc Nguyen", "Hoang Tran"],
    "af": ["Ling Zhao", "Huong Nguyen", "Han Tran"],
}

# Proxy substitutions, used when auditing whether removing the name alone is
# enough. Each entry is (institution, suburb) with a contrasting profile.
PROXY_CONTEXTS: dict[str, tuple[str, str]] = {
    "advantaged": ("University of Queensland", "Ascot"),
    "disadvantaged": ("Western Sydney University", "Woodridge"),
}


@dataclass
class Variant:
    variant_id: str
    group: str
    first_name: str
    text: str
    proxy_context: str | None = None


@dataclass
class Case:
    """One work history rendered under several demographic signals."""

    case_id: str
    variants: list[Variant] = field(default_factory=list)
    target_role: str | None = None


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def render_resume(
    name: str,
    jobs: list[dict[str, Any]],
    *,
    institution: str | None = None,
    suburb: str | None = None,
    qualification: str = "Bachelor of Science",
) -> str:
    """Render a structured work history as plain-text resume."""
    handle = re.sub(r"[^a-z]+", ".", name.lower()).strip(".")
    lines = [name.upper(), f"{handle}@email.com | 0400 000 000"]
    if suburb:
        lines[-1] += f" | {suburb}, QLD"
    lines.append("")
    lines.append("EXPERIENCE")

    for job in sorted(jobs, key=lambda entry: entry.get("job_order", 0)):
        title = job.get("job_title") or "Role"
        duration = job.get("duration_str") or ""
        lines.append(f"{title} — {duration}" if duration else title)
        for task in (job.get("tasks") or [])[:5]:
            content = (task or {}).get("content")
            if content:
                lines.append(f"- {content}")
        lines.append("")

    if institution:
        lines += ["EDUCATION", qualification, institution, ""]
    return "\n".join(lines).strip()


# --------------------------------------------------------------------------
# Offline corpus
# --------------------------------------------------------------------------

_NAME_LINE = re.compile(r"^[A-Z][A-Z\s.'-]{3,}$")


_UNIVERSITY_LINE = re.compile(r"^.*\b(universit\w*|institute|college|tafe|polytechnic)\b.*$", re.IGNORECASE | re.MULTILINE)
_SUBURB_IN_HEADER = re.compile(r"\|\s*([A-Z][A-Za-z' -]+),\s*(QLD|NSW|VIC|SA|WA|TAS|NT|ACT)\b")


def _swap_identity(
    text: str,
    original_name: str,
    new_name: str,
    *,
    institution_to: str | None = None,
    suburb_to: str | None = None,
) -> str:
    """Replace the name, and optionally the institution and suburb, in a resume.

    Everything else is left byte-for-byte identical, which is what makes a
    score difference attributable to the substituted signal.
    """
    swapped = text

    original_parts = original_name.split()
    new_parts = new_name.split()
    replacements = {original_name: new_name}
    for index, part in enumerate(original_parts):
        replacements[part] = new_parts[index] if index < len(new_parts) else new_parts[-1]

    # Longest first so the full name is replaced before its parts.
    for part in sorted(replacements, key=len, reverse=True):
        if len(part) < 3:
            continue
        swapped = re.sub(rf"\b{re.escape(part)}\b", replacements[part], swapped, flags=re.IGNORECASE)

    # The email and profile links are derived from the name, so rebuild them.
    # Leaving a stale handle would re-identify the original person and would
    # also hand the model a demographic signal the variant did not intend.
    handle = re.sub(r"[^a-z]+", ".", new_name.lower()).strip(".")
    swapped = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", f"{handle}@email.com", swapped)
    swapped = re.sub(
        r"\b((?:linkedin|github|gitlab)\.com)/[\w./-]+",
        lambda match: f"{match.group(1)}/{handle.replace('.', '')}",
        swapped,
        flags=re.IGNORECASE,
    )

    if institution_to:
        # The institution name runs to the first comma on its line (the rest is
        # country and year); replacing that segment leaves the line's shape and
        # every other field untouched.
        institution_word = re.compile(
            r"\b(universit\w*|institute|college|tafe|polytechnic)\b", re.IGNORECASE
        )

        def _replace_institution(match: re.Match[str]) -> str:
            # Replace only the comma-separated segment naming the institution,
            # so a line like "Master of Computer Science, Tsinghua University,
            # China, 2015" keeps its qualification title, country and year.
            segments = match.group(0).split(",")
            for index, segment in enumerate(segments):
                if institution_word.search(segment):
                    leading = " " if segment.startswith(" ") else ""
                    segments[index] = f"{leading}{institution_to}"
                    break
            return ",".join(segments)

        swapped = _UNIVERSITY_LINE.sub(_replace_institution, swapped, count=1)
    if suburb_to:
        swapped = _SUBURB_IN_HEADER.sub(rf"| {suburb_to}, \2", swapped, count=1)
    return swapped


def synthetic_corpus(
    sample_dir: Path,
    *,
    names_per_group: int = 1,
    include_proxies: bool = False,
) -> list[Case]:
    """Build counterfactual variants from the local sample resumes.

    Each base resume becomes one case; within a case the variants differ only
    in the name, so any score spread is attributable to the name alone.
    """
    cases: list[Case] = []
    for path in sorted(sample_dir.glob("*.txt")):
        text = path.read_text()
        original = next((line.strip() for line in text.splitlines() if _NAME_LINE.match(line.strip())), None)
        if not original:
            log.warning("no name line found in %s; skipping", path.name)
            continue
        original = original.title()

        case = Case(case_id=path.stem)
        for group, names in SYNTHETIC_NAMES.items():
            for name in names[:names_per_group]:
                case.variants.append(
                    Variant(
                        variant_id=f"{path.stem}:{group}:{name.split()[0]}",
                        group=group,
                        first_name=name.split()[0],
                        text=_swap_identity(text, original, name),
                    )
                )
                if include_proxies:
                    # Same name, contrasting institution and suburb: isolates
                    # whether removing the name alone is sufficient.
                    for context, (institution, suburb) in PROXY_CONTEXTS.items():
                        case.variants.append(
                            Variant(
                                variant_id=f"{path.stem}:{group}:{name.split()[0]}:{context}",
                                group=group,
                                first_name=name.split()[0],
                                proxy_context=context,
                                text=_swap_identity(
                                    text, original, name,
                                    institution_to=institution,
                                    suburb_to=suburb,
                                ),
                            )
                        )
        cases.append(case)
    return cases


# --------------------------------------------------------------------------
# Published study corpus
# --------------------------------------------------------------------------


def _fetch_rows(config: str, offset: int, length: int, client: httpx.Client) -> list[dict[str, Any]]:
    response = client.get(
        f"{DATASETS_SERVER}/rows",
        params={
            "dataset": DATASET,
            "config": config,
            "split": "train",
            "offset": offset,
            "length": length,
        },
    )
    response.raise_for_status()
    return [row["row"] for row in response.json().get("rows", [])]


def _fetch_names_for(user_id: str, client: httpx.Client) -> dict[str, str]:
    """Return ``{group: first_name}`` for one work history."""
    response = client.get(
        f"{DATASETS_SERVER}/filter",
        params={
            "dataset": DATASET,
            "config": "summaries",
            "split": "train",
            "where": f"\"user_id\"='{user_id}'",
            "length": 100,
        },
    )
    if response.status_code != 200:
        return {}
    payload = response.json()
    if "error" in payload:
        # The server builds its index lazily; the caller reports and falls back.
        raise RuntimeError(payload["error"])
    return {row["row"]["race"]: row["row"]["first_name"] for row in payload.get("rows", [])}


def huggingface_corpus(
    limit: int = 20,
    *,
    cache_path: Path | None = None,
    timeout_s: float = 60.0,
) -> list[Case]:
    """Build cases from the published study data. Requires network access."""
    import json

    if cache_path and cache_path.exists():
        raw = json.loads(cache_path.read_text())
        return [
            Case(
                case_id=entry["case_id"],
                target_role=entry.get("target_role"),
                variants=[Variant(**variant) for variant in entry["variants"]],
            )
            for entry in raw
        ]

    cases: list[Case] = []
    with httpx.Client(timeout=timeout_s) as client:
        histories = _fetch_rows("resumes", 0, min(limit, 100), client)
        for history in histories[:limit]:
            user_id = history["user_id"]
            try:
                names = _fetch_names_for(user_id, client)
            except RuntimeError as exc:
                log.warning("study corpus unavailable (%s); use synthetic_corpus instead", exc)
                break
            if len(names) < 2:
                continue
            case = Case(case_id=user_id)
            for group, first_name in sorted(names.items()):
                case.variants.append(
                    Variant(
                        variant_id=f"{user_id}:{group}",
                        group=group,
                        first_name=first_name,
                        text=render_resume(first_name, history.get("jobs") or []),
                    )
                )
            cases.append(case)

    if cache_path and cases:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps(
                [
                    {
                        "case_id": case.case_id,
                        "target_role": case.target_role,
                        "variants": [vars(variant) for variant in case.variants],
                    }
                    for case in cases
                ],
                indent=2,
            )
        )
    return cases


def variant_count(cases: Iterable[Case]) -> int:
    return sum(len(case.variants) for case in cases)
