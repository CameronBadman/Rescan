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
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


class LLMError(RuntimeError):
    pass


class LLMClient(Protocol):
    def json_call(self, request: LLMRequest) -> LLMResponse: ...


# --------------------------------------------------------------------------
# JSON recovery
# --------------------------------------------------------------------------

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)
_OPEN_THINK = re.compile(r"^\s*<think>.*?(?=\{)", re.DOTALL)


def strip_thinking(text: str) -> str:
    """Remove a reasoning model's <think> block, closed or not.

    With thinking disabled the server should not send one, and with a reasoning
    parser configured it arrives in a separate field, but a model that emits
    one anyway must not break JSON parsing.
    """
    if "<think>" not in text:
        return text
    stripped = _THINK.sub("", text)
    if "<think>" in stripped and "</think>" not in stripped:
        # Truncated or unterminated block: drop everything up to the JSON.
        stripped = _OPEN_THINK.sub("", stripped)
    return stripped.strip()


def parse_json_lenient(text: str) -> dict[str, Any]:
    """Parse model output that should be JSON but may carry fences or preamble.

    Guided decoding makes this rare, but a server without schema support falls
    back to plain JSON mode and the occasional wrapper still slips through.
    """
    text = strip_thinking((text or "").strip())
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


# Constraint styles in probe order. `json_schema` is the OpenAI form vLLM,
# SGLang and current llama.cpp all accept. `json_object_schema` is llama.cpp's
# older grammar-from-schema form and is tried before `guided_json` (vLLM /
# SGLang's native field) because a server that lacks json_schema is far more
# likely to enforce the former and silently ignore the latter. `json_object`
# enforces nothing and relies on the lenient parser.
SCHEMA_MODES = ("json_schema", "json_object_schema", "guided_json", "json_object")


# Request fields a server may not know. A complaint naming one of these is a
# rejection of the request style, not a server fault.
_NEGOTIABLE = ("response_format", "json_schema", "guided_json", "schema",
               "chat_template_kwargs", "reasoning_effort", "top_p", "presence_penalty")


def _is_rejection(status: int, text: str) -> bool:
    """Whether an error response means "I don't accept that field", so the
    probe should try the next style rather than fail.

    vLLM and SGLang answer 400; FastAPI-based servers answer 422; llama.cpp's
    server wraps its request validation error in a 500. The body is the tell.
    """
    if status in (400, 422):
        return True
    if status == 500:
        lowered = text.lower()
        return "validation error" in lowered or any(key in lowered for key in _NEGOTIABLE)
    return False


class OpenAICompatClient:
    """Client for any OpenAI-compatible server: vLLM, SGLang, llama.cpp.

    Two things are negotiated once and then remembered: which structured-output
    style the server accepts, and whether it tolerates the extra request fields
    used to control thinking. Servers that reject an unknown field do so with a
    4xx, so a rejection during the probe moves to the next style rather than
    failing the batch.
    """

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
        self._send_extras: bool | None = None

    def _extras(self) -> dict[str, Any]:
        """Request fields beyond the OpenAI core: thinking control and sampling."""
        extras: dict[str, Any] = {}
        if settings.llm_disable_thinking:
            extras["chat_template_kwargs"] = {"enable_thinking": False}
        elif settings.llm_reasoning_effort:
            extras["reasoning_effort"] = settings.llm_reasoning_effort
        if settings.llm_top_p is not None:
            extras["top_p"] = settings.llm_top_p
        if settings.llm_presence_penalty is not None:
            extras["presence_penalty"] = settings.llm_presence_penalty
        return extras

    def _payload(self, request: LLMRequest, schema_mode: str, with_extras: bool) -> dict[str, Any]:
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
        if with_extras:
            payload.update(self._extras())

        if schema_mode == "json_schema":
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": request.task, "schema": request.schema, "strict": True},
            }
        elif schema_mode == "guided_json":
            payload["guided_json"] = request.schema
        elif schema_mode == "json_object_schema":
            payload["response_format"] = {"type": "json_object", "schema": request.schema}
        else:  # "json_object" — no schema enforcement, lenient parse catches drift
            payload["response_format"] = {"type": "json_object"}
        return payload

    def _post(self, payload: dict[str, Any]) -> httpx.Response:
        with self._sem:
            return self._client.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}"},
            )

    def json_call(self, request: LLMRequest) -> LLMResponse:
        # Probe schema support once, then reuse the mode that worked.
        modes = [self._schema_mode] if self._schema_mode else list(SCHEMA_MODES)
        extras_options = [self._send_extras] if self._send_extras is not None else [True, False]
        last_error: Exception | None = None

        for mode in modes:
            for with_extras in extras_options:
                if with_extras and not self._extras():
                    # Nothing extra to send; the bare attempt is the same request.
                    if False in extras_options:
                        continue
                started = time.monotonic()
                try:
                    resp = self._post(self._payload(request, mode, with_extras))
                except httpx.HTTPError as exc:
                    raise LLMError(f"inference server unreachable at {self.base_url}: {exc}") from exc

                probing = self._schema_mode is None or self._send_extras is None
                if probing and _is_rejection(resp.status_code, resp.text):
                    # Rejected this constraint style or an extra field; try the next.
                    last_error = LLMError(f"{mode}{' +extras' if with_extras else ''} rejected: {resp.text[:200]}")
                    continue
                if resp.status_code >= 400:
                    raise LLMError(f"inference server returned {resp.status_code}: {resp.text[:300]}")

                body = resp.json()
                message = body["choices"][0]["message"]
                content = message.get("content") or ""
                if not content.strip() and message.get("reasoning_content"):
                    # A thinking model that spent its whole budget reasoning.
                    raise LLMError("model returned reasoning but no answer; raise max_tokens or disable thinking")
                if self._schema_mode != mode or self._send_extras != with_extras:
                    log.info("inference server %s: structured output via %s, extras %s",
                             self.base_url, mode, "accepted" if with_extras else "rejected")
                self._schema_mode = mode
                self._send_extras = with_extras
                usage = body.get("usage") or {}
                return LLMResponse(
                    data=parse_json_lenient(content),
                    model=body.get("model", request.model or self.model),
                    backend=f"openai:{mode}",
                    latency_s=time.monotonic() - started,
                    raw=content,
                    prompt_tokens=usage.get("prompt_tokens"),
                    completion_tokens=usage.get("completion_tokens"),
                )

        raise LLMError(f"no supported JSON mode on this server: {last_error}")

    def served_models(self) -> list[str]:
        """Model ids the server advertises, for health checks and smoke tests."""
        try:
            resp = self._client.get(f"{self.base_url}/models", headers={"Authorization": f"Bearer {self.api_key}"})
            resp.raise_for_status()
            return [str(item.get("id")) for item in resp.json().get("data", [])]
        except (httpx.HTTPError, ValueError) as exc:
            raise LLMError(f"inference server unreachable at {self.base_url}: {exc}") from exc

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
