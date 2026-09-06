import json

import pytest
from fastapi.testclient import TestClient

from rescan.api.main import app
from rescan.config import Settings, settings

KEY = "test-key-abc123"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", tmp_path / "auth.db")
    monkeypatch.setattr(settings, "upload_dir", tmp_path / "uploads")
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def secured(client, monkeypatch):
    monkeypatch.setattr(settings, "api_keys", [KEY])
    return client


def test_health_stays_public_for_load_balancer_probes(secured):
    assert secured.get("/health").status_code == 200


def test_protected_route_rejects_a_missing_key(secured):
    response = secured.get("/jobs")
    assert response.status_code == 401
    assert response.headers.get("www-authenticate") == "Bearer"


def test_protected_route_rejects_a_wrong_key(secured):
    assert secured.get("/jobs", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_bearer_token_is_accepted(secured):
    assert secured.get("/jobs", headers={"Authorization": f"Bearer {KEY}"}).status_code == 200


def test_x_api_key_header_is_accepted(secured):
    assert secured.get("/jobs", headers={"X-API-Key": KEY}).status_code == 200


def test_upload_is_protected(secured, samples):
    files = [("files", ("a.txt", (samples / "001_priya_nair.txt").read_bytes(), "text/plain"))]
    unauthorised = secured.post("/jobs", files=files, data={"role": json.dumps({"title": "X"}), "rules": "[]"})
    assert unauthorised.status_code == 401


def test_auth_is_disabled_when_no_keys_are_configured(client, monkeypatch):
    monkeypatch.setattr(settings, "api_keys", [])
    assert client.get("/jobs").status_code == 200


# --------------------------------------------------------------------------
# Configuration parsing
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("k1,k2", ["k1", "k2"]),
        ("k1, k2 ", ["k1", "k2"]),
        ('["k1","k2"]', ["k1", "k2"]),
        ("", []),
        ("single", ["single"]),
    ],
)
def test_api_keys_accept_csv_and_json(monkeypatch, raw, expected):
    # RESCAN_API_KEYS=key1,key2 is the natural thing to write and must not
    # crash at import time.
    monkeypatch.setenv("RESCAN_API_KEYS", raw)
    assert Settings().api_keys == expected


def test_ensemble_models_accept_csv(monkeypatch):
    monkeypatch.setenv("RESCAN_ENSEMBLE_MODELS", "m1,m2,m3")
    assert Settings().ensemble_models == ["m1", "m2", "m3"]


# --------------------------------------------------------------------------
# CORS for the browser frontend
# --------------------------------------------------------------------------


def test_preflight_from_an_allowed_origin_succeeds_without_a_key(secured):
    response = secured.options(
        "/rules/compile",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-api-key,content-type",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert "x-api-key" in response.headers["access-control-allow-headers"].lower()


def test_actual_request_still_needs_the_key_and_gets_cors_headers(secured):
    denied = secured.get("/jobs", headers={"Origin": "http://localhost:3000"})
    assert denied.status_code == 401
    allowed = secured.get("/jobs", headers={"Origin": "http://localhost:3000", "X-API-Key": KEY})
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "http://localhost:3000"


def test_unlisted_origin_gets_no_cors_headers(secured):
    response = secured.get("/jobs", headers={"Origin": "https://evil.example", "X-API-Key": KEY})
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


def test_cors_origins_accept_csv(monkeypatch):
    monkeypatch.setenv("RESCAN_CORS_ORIGINS", "https://app.example, http://localhost:3000")
    assert Settings().cors_origins == ["https://app.example", "http://localhost:3000"]
