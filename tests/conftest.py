from pathlib import Path

import pytest

from rescan.extract import Extractor
from rescan.llm.client import build_client

SAMPLES = Path(__file__).resolve().parent.parent / "data" / "samples"


@pytest.fixture(scope="session")
def samples() -> Path:
    return SAMPLES


@pytest.fixture(scope="session")
def extractor() -> Extractor:
    ex = Extractor()
    yield ex
    ex.close()


@pytest.fixture(scope="session")
def llm():
    return build_client("stub")


@pytest.fixture(autouse=True)
def _in_process_mcp(monkeypatch):
    """Tests run the MCP server in-process unless they opt into remote mode;
    a developer's .env pointing at a deployment must not redirect them."""
    import rescan.mcp_server as mcp_module
    from rescan.config import settings

    monkeypatch.setattr(settings, "mcp_remote_url", "")
    monkeypatch.setattr(settings, "mcp_remote_key", "")
    monkeypatch.setattr(mcp_module, "_remote", None)
    yield


@pytest.fixture(autouse=True)
def _stub_inference(monkeypatch):
    """Tests never call a served model: a developer's .env may point
    RESCAN_LLM_BACKEND at a GPU pod, and the suite must not follow it."""
    from rescan.config import settings

    monkeypatch.setattr(settings, "llm_backend", "stub")
    monkeypatch.setattr(settings, "compile_base_url", "")
    monkeypatch.setattr(settings, "compile_model", "")
    yield
