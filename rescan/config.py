"""Runtime configuration.

Everything is environment-driven so the same code runs against a local stub
backend during development and a real vLLM / SGLang deployment in the demo.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RESCAN_", env_file=".env", extra="ignore")

    # --- storage ---
    data_dir: Path = REPO_ROOT / "data"
    db_path: Path = REPO_ROOT / "data" / "rescan.db"
    upload_dir: Path = REPO_ROOT / "data" / "uploads"

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
    llm_temperature: float = 0.0

    # Ensemble members for the borderline pass. Falls back to `llm_model`
    # repeated with different seeds when only one model is served.
    ensemble_models: list[str] = []

    # --- ranking ---
    # Scores within this band of the shortlist cutoff go to the ensemble.
    borderline_margin: float = 0.08
    shortlist_size: int = 10

    # --- pipeline ---
    pipeline_workers: int = 4
    max_retries: int = 1

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.upload_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
settings.ensure_dirs()
