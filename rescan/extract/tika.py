"""Apache Tika server client.

Tika is run in server mode so a single JVM handles the whole batch and we can
drive it from a thread pool. `scripts/tika_server.sh` starts it.
"""

from __future__ import annotations

import logging

import httpx

from rescan.config import settings

log = logging.getLogger(__name__)


class TikaUnavailable(RuntimeError):
    """Raised when the Tika server cannot be reached or refuses the document."""


class TikaClient:
    def __init__(self, base_url: str | None = None, timeout_s: float | None = None) -> None:
        self.base_url = (base_url or settings.tika_url).rstrip("/")
        self.timeout_s = timeout_s or settings.tika_timeout_s
        # One client shared across threads; httpx.Client is thread-safe and
        # pools connections, which is the point of running Tika as a server.
        self._client = httpx.Client(timeout=self.timeout_s)

    def is_available(self) -> bool:
        try:
            resp = self._client.get(f"{self.base_url}/tika", timeout=3.0)
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    def extract_text(self, data: bytes, content_type: str | None = None) -> str:
        headers = {"Accept": "text/plain"}
        if content_type:
            headers["Content-Type"] = content_type
        try:
            resp = self._client.put(f"{self.base_url}/tika", content=data, headers=headers)
        except httpx.HTTPError as exc:
            raise TikaUnavailable(f"tika unreachable: {exc}") from exc
        if resp.status_code == 422:
            raise TikaUnavailable("tika could not parse the document (422)")
        if resp.status_code >= 400:
            raise TikaUnavailable(f"tika returned {resp.status_code}")
        return resp.text or ""

    def metadata(self, data: bytes, content_type: str | None = None) -> dict:
        headers = {"Accept": "application/json"}
        if content_type:
            headers["Content-Type"] = content_type
        try:
            resp = self._client.put(f"{self.base_url}/meta", content=data, headers=headers)
            if resp.status_code >= 400:
                return {}
            return resp.json()
        except (httpx.HTTPError, ValueError):
            return {}

    def close(self) -> None:
        self._client.close()
