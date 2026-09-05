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
