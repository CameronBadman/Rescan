"""Deterministic AQF-equivalence mapping.

Qualification level drives eligibility decisions, so it is resolved by an
explicit, reviewable table rather than left to the model. The LLM's job is only
to pull the qualification *title* out of the document; the level assigned to
that title is decided here and is identical for every candidate.

Australian Qualifications Framework levels:
    1-4  Certificate I-IV
    5    Diploma
    6    Advanced Diploma, Associate Degree
    7    Bachelor Degree
    8    Bachelor Honours Degree, Graduate Certificate, Graduate Diploma
    9    Masters Degree
    10   Doctoral Degree
"""

from __future__ import annotations

import re

AQF_LABELS: dict[int, str] = {
    1: "Certificate I",
    2: "Certificate II",
    3: "Certificate III",
    4: "Certificate IV",
    5: "Diploma",
    6: "Advanced Diploma / Associate Degree",
    7: "Bachelor Degree",
    8: "Bachelor Honours Degree / Graduate Certificate / Graduate Diploma",
    9: "Masters Degree",
    10: "Doctoral Degree",
}

# Ordered most-specific first: the first pattern to match wins, so
# "Master of Philosophy" must be tested before a bare "Bachelor".
_RULES: list[tuple[str, int, float]] = [
    # --- doctoral ---
    (r"\b(ph\.?\s?d|d\.?\s?phil|doctor(ate|al)?\b|doctor of philosophy)\b", 10, 0.97),
    (r"\b(ed\.?d|sc\.?d|dba|md\b|juris doctor|j\.?d\.?)\b", 10, 0.85),
    # --- masters ---
    (r"\b(m\.?\s?phil|master of philosophy)\b", 9, 0.95),
    (r"\bmaster(s|'s)?\b", 9, 0.95),
    (r"\b(m\.?\s?sc|m\.?\s?eng|m\.?\s?ba|m\.?\s?a\.?\b|m\.?\s?tech|m\.?\s?com|mca|m\.?\s?ed)\b", 9, 0.9),
    # --- level 8 ---
    (r"\bgraduate (certificate|diploma)\b|\bgrad\.? ?(cert|dip)\b", 8, 0.95),
    (r"\b(post ?graduate) (certificate|diploma)\b|\bpg ?(cert|dip)\b", 8, 0.9),
    (r"\b(honours|honors)\b|\bb\.?[a-z]{0,4}\.?\s*\(hons\)|\bhons\b", 8, 0.9),
    # --- bachelor ---
    (r"\bbachelor(s|'s)?\b", 7, 0.95),
    (r"\b(b\.?\s?sc|b\.?\s?eng|b\.?\s?a\.?\b|b\.?\s?com|b\.?\s?tech|b\.?\s?bus|bca|llb|b\.?\s?ed)\b", 7, 0.9),
    (r"\b(undergraduate degree|first degree)\b", 7, 0.75),
    # --- level 6 ---
    (r"\badvanced diploma\b|\badv\.? ?dip\b", 6, 0.95),
    (r"\bassociate (degree|diploma)\b", 6, 0.9),
    (r"\b(foundation degree|associate of (arts|science))\b", 6, 0.75),
    # --- level 5 ---
    (r"\bdiploma\b|\bdip\.\b", 5, 0.9),
    # --- certificates ---
    (r"\bcertificate\s*(iv|4)\b|\bcert\.?\s*(iv|4)\b", 4, 0.95),
    (r"\bcertificate\s*(iii|3)\b|\bcert\.?\s*(iii|3)\b", 3, 0.95),
    (r"\bcertificate\s*(ii|2)\b|\bcert\.?\s*(ii|2)\b", 2, 0.95),
    (r"\bcertificate\s*(i|1)\b|\bcert\.?\s*(i|1)\b", 1, 0.95),
]

_COMPILED = [(re.compile(pat, re.IGNORECASE), level, conf) for pat, level, conf in _RULES]

# Titles that look like qualifications but carry no AQF level. Matched before
# the rules so a "Certificate of Completion" is not read as a Certificate I.
_NON_AQF = re.compile(
    r"\b(certificate of (completion|attendance|participation|achievement)"
    r"|micro-?credential|nanodegree|bootcamp|short course|online course"
    r"|udemy|coursera|edx|badge)\b",
    re.IGNORECASE,
)


def map_to_aqf(title: str | None) -> tuple[int | None, str | None, float]:
    """Return ``(aqf_level, aqf_label, confidence)`` for a qualification title.

    Confidence is 0.0 when nothing matched, which is the signal for the
    reviewer to check the qualification by hand rather than a reason to
    exclude the candidate.
    """
    if not title or not title.strip():
        return None, None, 0.0

    text = title.strip()
    if _NON_AQF.search(text):
        return None, None, 0.0

    for pattern, level, confidence in _COMPILED:
        if pattern.search(text):
            return level, AQF_LABELS[level], confidence
    return None, None, 0.0


def meets_minimum(level: int | None, minimum: int) -> bool:
    """Whether an (possibly unknown) AQF level clears a required minimum.

    Unknown levels are treated as *not proven* rather than failing: callers
    route these to manual review instead of auto-rejecting.
    """
    return level is not None and level >= minimum
