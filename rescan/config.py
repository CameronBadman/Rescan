"""Runtime configuration.

Everything is environment-driven so the same code runs against a local stub
backend during development and a real vLLM / SGLang deployment in the demo.
"""

from __future__ import annotations

from pathlib import Path

from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RESCAN_", env_file=".env", extra="ignore")

    # --- storage ---
    data_dir: Path = REPO_ROOT / "data"
    db_path: Path = REPO_ROOT / "data" / "rescan.db"
    upload_dir: Path = REPO_ROOT / "data" / "uploads"

    # --- object store (where a job's resumes are pulled from) ---
    # "s3" is any S3-compatible endpoint (MinIO, R2, AWS); "local" is a
    # directory with the same <prefix>/<jobId>/ layout, for development.
    object_store: str = "local"
    local_object_store_dir: Path = REPO_ROOT / "data" / "bucket"
    s3_endpoint_url: str = ""
    s3_bucket: str = ""
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""
    s3_region: str = "auto"
    # Objects for a job live under f"{s3_prefix}/{job_id}/".
    s3_prefix: str = "jobs"

    # --- extraction ---
    tika_url: str = "http://localhost:9998"
    tika_timeout_s: float = 60.0
    extract_workers: int = 8
    # Below this many characters we assume the PDF is a scan and try OCR.
    ocr_char_threshold: int = 200
    ocr_enabled: bool = True
    tesseract_bin: str = "tesseract"

    # --- inference ---
    # vLLM and SGLang both expose an OpenAI-compatible /v1 surface.
    llm_backend: str = "stub"  # "openai" | "stub"
    llm_base_url: str = "http://localhost:8000/v1"
    llm_api_key: str = "not-needed"
    llm_model: str = "Qwen/Qwen3.8-27B"
    llm_timeout_s: float = 180.0
    llm_max_concurrency: int = 16
    # Sampling. Structured passes run near-greedy; Qwen's non-thinking
    # recommendation is temperature 0.7 / top_p 0.8 / presence_penalty 1.5,
    # which is what to move towards if greedy decoding starts repeating.
    llm_temperature: float = 0.0
    llm_top_p: float | None = None
    llm_presence_penalty: float | None = None
    # Open reasoning models (Qwen3.x) think by default. Every pass here already
    # carries its reasoning in the schema where it needs it, so thinking is
    # turned off per request via chat_template_kwargs; any <think> block that
    # arrives anyway is stripped before JSON parsing.
    llm_disable_thinking: bool = True
    # Qwen3.8's reasoning_effort dial ("low" | "medium" | "xhigh"); only sent
    # when thinking is left on.
    llm_reasoning_effort: str | None = None

    # Route the plan-compile pass (and its repair round) to a different, usually
    # larger, model. It runs once per job and is where model quality shows;
    # the per-candidate passes stay on `llm_model`. Empty means "same server".
    compile_base_url: str = ""
    compile_model: str = ""
    compile_api_key: str = ""

    # Ensemble members for the borderline pass. Falls back to `llm_model`
    # repeated with different seeds when only one model is served.
    ensemble_models: Annotated[list[str], NoDecode] = []

    # --- ranking ---
    # Scores within this band of the shortlist cutoff go to the ensemble.
    borderline_margin: float = 0.08
    shortlist_size: int = 10

    # --- pipeline ---
    pipeline_workers: int = 4
    max_retries: int = 1

    # --- auth ---
    # Comma-separated in the environment: RESCAN_API_KEYS=key1,key2
    # Empty disables authentication, which is intended for local development
    # only; the API logs a warning at startup when it is left empty.
    api_keys: Annotated[list[str], NoDecode] = []

    @field_validator("api_keys", "ensemble_models", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        """Accept comma-separated env values as well as JSON lists.

        RESCAN_API_KEYS=key1,key2 is the natural thing to write, and
        pydantic-settings would otherwise try to JSON-decode it and fail at
        import time.
        """
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return []
            if stripped.startswith("["):
                import json

                return json.loads(stripped)
            return [part.strip() for part in stripped.split(",") if part.strip()]
        return value

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.upload_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
settings.ensure_dirs()
