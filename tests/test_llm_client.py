"""The real client against fake servers: negotiation, thinking, recovery."""

import json

import httpx
import pytest

from rescan.config import settings
from rescan.llm.client import (
    SCHEMA_MODES,
    LLMError,
    LLMRequest,
    LLMResponse,
    OpenAICompatClient,
    parse_json_lenient,
    strip_thinking,
)

SCHEMA = {"type": "object", "required": ["answer"], "properties": {"answer": {"type": "string"}}}


def request(**kwargs) -> LLMRequest:
    base = dict(task="probe", system="s", user="u", schema=SCHEMA)
    base.update(kwargs)
    return LLMRequest(**base)


def completion(content: str, *, reasoning: str | None = None, usage=None) -> dict:
    message = {"role": "assistant", "content": content}
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    body = {"model": "fake", "choices": [{"message": message}]}
    if usage:
        body["usage"] = usage
    return body


class FakeServer:
    """Scripted OpenAI-compatible server. `policy(payload) -> (status, body)`."""

    def __init__(self, policy):
        self.policy = policy
        self.requests: list[dict] = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "fake"}]})
        payload = json.loads(req.content)
        self.requests.append(payload)
        status, body = self.policy(payload)
        return httpx.Response(status, json=body if isinstance(body, dict) else {"error": body})

    def client(self) -> OpenAICompatClient:
        client = OpenAICompatClient(base_url="http://fake/v1", model="fake")
        client._client = httpx.Client(transport=httpx.MockTransport(self.handler))
        return client


def style_of(payload: dict) -> str:
    fmt = payload.get("response_format") or {}
    if "guided_json" in payload:
        return "guided_json"
    if fmt.get("type") == "json_schema":
        return "json_schema"
    if fmt.get("type") == "json_object" and "schema" in fmt:
        return "json_object_schema"
    if fmt.get("type") == "json_object":
        return "json_object"
    return "none"


# --------------------------------------------------------------------------
# Thinking
# --------------------------------------------------------------------------


def test_think_blocks_are_stripped_before_parsing():
    assert strip_thinking('<think>\nhmm\n</think>\n{"a": 1}') == '{"a": 1}'
    assert strip_thinking('<think>never closed {"a": 1}') == '{"a": 1}'
    assert strip_thinking('{"a": 1}') == '{"a": 1}'
    assert parse_json_lenient('<think>reasoning</think>```json\n{"answer": "x"}\n```') == {"answer": "x"}


def test_thinking_is_disabled_per_request_by_default(monkeypatch):
    monkeypatch.setattr(settings, "llm_disable_thinking", True)
    server = FakeServer(lambda p: (200, completion('{"answer": "x"}')))
    server.client().json_call(request())
    assert server.requests[0]["chat_template_kwargs"] == {"enable_thinking": False}
    assert "reasoning_effort" not in server.requests[0]


def test_reasoning_effort_is_sent_only_when_thinking_is_on(monkeypatch):
    monkeypatch.setattr(settings, "llm_disable_thinking", False)
    monkeypatch.setattr(settings, "llm_reasoning_effort", "low")
    server = FakeServer(lambda p: (200, completion('{"answer": "x"}')))
    server.client().json_call(request())
    assert server.requests[0]["reasoning_effort"] == "low"
    assert "chat_template_kwargs" not in server.requests[0]


def test_reasoning_that_exhausts_the_budget_is_an_error_not_empty_data():
    server = FakeServer(lambda p: (200, completion("", reasoning="I was thinking about...")))
    with pytest.raises(LLMError, match="max_tokens"):
        server.client().json_call(request())


# --------------------------------------------------------------------------
# Negotiation
# --------------------------------------------------------------------------


def test_vllm_style_server_takes_json_schema_with_extras_first_try():
    server = FakeServer(lambda p: (200, completion('{"answer": "x"}', usage={"prompt_tokens": 10, "completion_tokens": 3})))
    client = server.client()
    response = client.json_call(request())
    assert len(server.requests) == 1
    assert style_of(server.requests[0]) == "json_schema"
    assert response.backend == "openai:json_schema"
    assert (response.prompt_tokens, response.completion_tokens) == (10, 3)
    assert client._schema_mode == "json_schema" and client._send_extras is True


def test_llama_cpp_style_500_validation_error_moves_to_the_next_style():
    def policy(payload):
        if style_of(payload) == "json_schema":
            return 500, {"error": {"message": "1 validation error: response_format.type Input should be 'text' or 'json_object'"}}
        return 200, completion('{"answer": "x"}')

    server = FakeServer(policy)
    client = server.client()
    response = client.json_call(request())
    assert response.backend == "openai:json_object_schema", "the enforcing style is preferred over guided_json"
    assert [style_of(p) for p in server.requests] == ["json_schema", "json_schema", "json_object_schema"]
    assert client._schema_mode == "json_object_schema"


def test_server_that_rejects_extra_fields_gets_them_dropped(monkeypatch):
    monkeypatch.setattr(settings, "llm_disable_thinking", True)

    def policy(payload):
        if "chat_template_kwargs" in payload:
            return 422, {"detail": "extra fields not permitted: chat_template_kwargs"}
        return 200, completion('{"answer": "x"}')

    server = FakeServer(policy)
    client = server.client()
    client.json_call(request())
    assert client._send_extras is False
    client.json_call(request())
    assert "chat_template_kwargs" not in server.requests[-1]
    assert len(server.requests) == 3, "the negotiated combination is remembered"


def test_probe_stops_at_the_first_working_style_and_remembers_it():
    def policy(payload):
        style = style_of(payload)
        if style in ("json_schema", "json_object_schema"):
            return 400, {"error": f"{style} not supported"}
        return 200, completion('{"answer": "x"}')

    server = FakeServer(policy)
    client = server.client()
    client.json_call(request())
    client.json_call(request())
    styles = [style_of(p) for p in server.requests]
    assert styles[-2:] == ["guided_json", "guided_json"]
    assert client._schema_mode == "guided_json"


def test_a_real_server_error_after_negotiation_is_raised_not_retried_as_a_probe():
    calls = {"n": 0}

    def policy(payload):
        calls["n"] += 1
        if calls["n"] == 1:
            return 200, completion('{"answer": "x"}')
        return 500, {"error": {"message": "CUDA out of memory"}}

    server = FakeServer(policy)
    client = server.client()
    client.json_call(request())
    with pytest.raises(LLMError, match="500"):
        client.json_call(request())
    assert calls["n"] == 2


def test_no_style_accepted_is_a_clear_error():
    server = FakeServer(lambda p: (400, {"error": "no"}))
    with pytest.raises(LLMError, match="no supported JSON mode"):
        server.client().json_call(request())


def test_every_style_is_a_distinct_request_shape():
    seen = set()
    client = FakeServer(lambda p: (200, completion("{}"))).client()
    for mode in SCHEMA_MODES:
        seen.add(style_of(client._payload(request(), mode, with_extras=False)))
    assert seen == set(SCHEMA_MODES)


def test_served_models_lists_the_server_catalogue():
    assert FakeServer(lambda p: (200, {})).client().served_models() == ["fake"]


def test_truncated_response_is_a_clear_error():
    body = completion('{"answer": "x", "long": "aaaa')
    body["choices"][0]["finish_reason"] = "length"
    server = FakeServer(lambda p: (200, body))
    with pytest.raises(LLMError, match="truncated at max_tokens"):
        server.client().json_call(request(max_tokens=64))


def test_nullable_enums_use_the_anyof_form():
    from rescan.llm.prompts import STRUCTURE_SCHEMA

    skill = STRUCTURE_SCHEMA["properties"]["skills"]["items"]["properties"]
    assert "anyOf" in skill["proficiency"] and None not in skill["proficiency"]["anyOf"][0]["enum"]
    role = STRUCTURE_SCHEMA["properties"]["experience"]["items"]["properties"]
    assert "anyOf" in role["seniority"] and "anyOf" in role["employment_type"]


def test_schemas_avoid_keywords_grammar_decoders_cannot_handle():
    """llama.cpp's grammar converter and xgrammar expand length bounds and
    patterns into huge rules (a maxLength of 2000 crashed the server)."""
    from rescan.llm import prompts

    banned = {"maxLength", "minLength", "pattern", "format", "minimum", "maximum", "multipleOf"}

    def walk(node, path):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in banned:
                    found.append(f"{path}.{key}")
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, f"{path}[{i}]")

    found = []
    for name in dir(prompts):
        if name.endswith("_SCHEMA"):
            walk(getattr(prompts, name), name)
    assert found == [], found


# --------------------------------------------------------------------------
# Per-pass routing
# --------------------------------------------------------------------------


def test_compile_tasks_are_routed_to_the_compile_server(monkeypatch):
    from rescan.llm.client import COMPILE_TASKS, RoutingClient

    class Recorder:
        def __init__(self, name):
            self.name, self.tasks = name, []

        def json_call(self, req):
            self.tasks.append(req.task)
            return LLMResponse(data={}, model=self.name, backend="fake", latency_s=0.0)

    bulk, big = Recorder("bulk"), Recorder("big")
    client = RoutingClient(bulk, {task: big for task in COMPILE_TASKS})
    for task in ("structure", "compile_dsl", "judge", "compile_dsl_repair", "rank"):
        client.json_call(request(task=task))
    assert bulk.tasks == ["structure", "judge", "rank"]
    assert big.tasks == ["compile_dsl", "compile_dsl_repair"]


def test_build_client_routes_only_when_a_compile_server_is_configured(monkeypatch):
    from rescan.llm.client import RetryingClient, RoutingClient, build_client

    monkeypatch.setattr(settings, "compile_base_url", "")
    monkeypatch.setattr(settings, "compile_model", "")
    plain = build_client("openai")
    assert isinstance(plain, RetryingClient) and isinstance(plain.inner, OpenAICompatClient)

    monkeypatch.setattr(settings, "compile_base_url", "http://big-host:8000/v1")
    monkeypatch.setattr(settings, "compile_model", "Qwen/Qwen3-235B-A22B-Instruct-2507-FP8")
    routed = build_client("openai")
    assert isinstance(routed.inner, RoutingClient)
    compile_client = routed.inner.routes["compile_dsl"]
    assert compile_client.base_url == "http://big-host:8000/v1"
    assert compile_client.model == "Qwen/Qwen3-235B-A22B-Instruct-2507-FP8"
    assert routed.inner.default.model == settings.llm_model
    routed.close()
