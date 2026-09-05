"""Pass 1 — turn extracted resume text into the structured ground truth.

The model produces fields; this module owns everything that must be identical
for every candidate. AQF levels are assigned here from `rescan.aqf`, never by
the model, so two candidates holding the same award always get the same level.
"""

from __future__ import annotations

import logging

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
        context={"resume_text": text},
    )

    try:
        response = client.json_call(request)
    except LLMError as exc:
        raise StructuringError(f"structuring failed: {exc}") from exc

    try:
        resume = StructuredResume.model_validate(response.data)
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
