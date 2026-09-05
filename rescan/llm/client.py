"""Inference client.

The real backend is any OpenAI-compatible server — vLLM and SGLang both expose
one, and both do continuous batching behind it, which is what makes a bulk
upload tractable. The stub backend is a deterministic local implementation used
for tests and for running the pipeline without a GPU.

Every call is schema-guided: we ask for JSON matching an explicit schema rather
than parsing prose, because downstream stages index into fixed fields.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from rescan.config import settings

log = logging.getLogger(__name__)


@dataclass
class LLMRequest:
    task: str
    system: str
    user: str
    schema: dict[str, Any]
    model: str | None = None
    seed: int | None = None
    temperature: float | None = None
    max_tokens: int = 4096
    # Structured inputs the stub backend reads directly. The real backend
    # ignores these; they are already rendered into `user`.
    context: dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMResponse:
    data: dict[str, Any]
    model: str
    backend: str
    latency_s: float
    raw: str | None = None


class LLMError(RuntimeError):
    pass


class LLMClient(Protocol):
    def json_call(self, request: LLMRequest) -> LLMResponse: ...


# --------------------------------------------------------------------------
# JSON recovery
# --------------------------------------------------------------------------

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def parse_json_lenient(text: str) -> dict[str, Any]:
    """Parse model output that should be JSON but may carry fences or preamble.

    Guided decoding makes this rare, but a server without schema support falls
    back to plain JSON mode and the occasional wrapper still slips through.
    """
    text = (text or "").strip()
    if not text:
        raise LLMError("empty model response")

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    fenced = _FENCE.search(text)
    if fenced:
        try:
            return json.loads(fenced.group(1).strip())
        except json.JSONDecodeError:
            pass

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass

    raise LLMError(f"could not parse JSON from model response: {text[:200]!r}")


# --------------------------------------------------------------------------
# OpenAI-compatible backend (vLLM / SGLang)
# --------------------------------------------------------------------------


class OpenAICompatClient:
    """Client for a vLLM or SGLang OpenAI-compatible server."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout_s: float | None = None,
        max_concurrency: int | None = None,
    ) -> None:
        self.base_url = (base_url or settings.llm_base_url).rstrip("/")
        self.api_key = api_key or settings.llm_api_key
        self.model = model or settings.llm_model
        self._client = httpx.Client(timeout=timeout_s or settings.llm_timeout_s)
        # Bounds in-flight requests so a large batch does not overrun the
        # server's queue; the server still batches continuously behind this.
        self._sem = threading.Semaphore(max_concurrency or settings.llm_max_concurrency)
        self._schema_mode: str | None = None

    def _payload(self, request: LLMRequest, schema_mode: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": request.model or self.model,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.user},
            ],
            "temperature": (
                request.temperature if request.temperature is not None else settings.llm_temperature
            ),
            "max_tokens": request.max_tokens,
        }
        if request.seed is not None:
            payload["seed"] = request.seed

        if schema_mode == "json_schema":
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": request.task, "schema": request.schema, "strict": True},
            }
        elif schema_mode == "guided_json":
            payload["guided_json"] = request.schema
        else:  # "json_object" — no schema enforcement, lenient parse catches drift
            payload["response_format"] = {"type": "json_object"}
        return payload

    def json_call(self, request: LLMRequest) -> LLMResponse:
        # Probe schema support once, then reuse the mode that worked.
        modes = [self._schema_mode] if self._schema_mode else ["json_schema", "guided_json", "json_object"]
        last_error: Exception | None = None

        for mode in modes:
            started = time.monotonic()
            try:
                with self._sem:
                    resp = self._client.post(
                        f"{self.base_url}/chat/completions",
                        json=self._payload(request, mode),
                        headers={"Authorization": f"Bearer {self.api_key}"},
                    )
                if resp.status_code == 400 and self._schema_mode is None:
                    # Server rejected this constraint style; try the next one.
                    last_error = LLMError(f"{mode} rejected: {resp.text[:200]}")
                    continue
                if resp.status_code >= 400:
                    raise LLMError(f"inference server returned {resp.status_code}: {resp.text[:300]}")

                body = resp.json()
                content = body["choices"][0]["message"]["content"]
                self._schema_mode = mode
                return LLMResponse(
                    data=parse_json_lenient(content),
                    model=body.get("model", request.model or self.model),
                    backend=f"openai:{mode}",
                    latency_s=time.monotonic() - started,
                    raw=content,
                )
            except httpx.HTTPError as exc:
                raise LLMError(f"inference server unreachable at {self.base_url}: {exc}") from exc

        raise LLMError(f"no supported JSON mode on this server: {last_error}")

    def close(self) -> None:
        self._client.close()


# --------------------------------------------------------------------------
# Retry wrapper
# --------------------------------------------------------------------------


class RetryingClient:
    """Retries transient inference failures once, then gives up loudly."""

    def __init__(self, inner: LLMClient, attempts: int | None = None, backoff_s: float = 1.0) -> None:
        self.inner = inner
        self.attempts = (attempts if attempts is not None else settings.max_retries) + 1
        self.backoff_s = backoff_s

    def json_call(self, request: LLMRequest) -> LLMResponse:
        last: Exception | None = None
        for attempt in range(self.attempts):
            try:
                return self.inner.json_call(request)
            except LLMError as exc:
                last = exc
                log.warning("llm task=%s attempt %d/%d failed: %s", request.task, attempt + 1, self.attempts, exc)
                if attempt + 1 < self.attempts:
                    time.sleep(self.backoff_s * (attempt + 1))
        raise LLMError(f"task {request.task} failed after {self.attempts} attempts: {last}")

    def close(self) -> None:
        close = getattr(self.inner, "close", None)
        if close:
            close()


# --------------------------------------------------------------------------
# Factory
# --------------------------------------------------------------------------


def build_client(backend: str | None = None) -> LLMClient:
    backend = backend or settings.llm_backend
    if backend == "openai":
        return RetryingClient(OpenAICompatClient())
    if backend == "stub":
        from rescan.llm.stub import StubClient

        return RetryingClient(StubClient(), attempts=0)
    raise ValueError(f"unknown llm backend: {backend!r}")
