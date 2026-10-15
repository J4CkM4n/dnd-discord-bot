"""Offline provider contracts, startup validation, and shared call limits."""
import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from core.agent_config import ConfigError, ROLES, load_agent_config, parse_config
from core.model_gateway import BudgetExceeded, ModelError, ModelGateway, reserve_calls, turn_context


def run(coro):
    return asyncio.run(coro)


def config(api="ollama", **overrides):
    provider = {"api": api, "base_url": "http://models.test/v1" if api == "openai" else "http://models.test"}
    provider.update(overrides)
    return parse_config({"providers": {"local": provider},
                         "roles": {r: {"model": r} for r in ROLES}}, {"TEST_KEY": "test-secret-value"})


def test_config_precedence_credentials_and_defaults(tmp_path):
    path = tmp_path / "agents.toml"
    path.write_text('''version = 1
[supervision]
director_evaluate = false
[providers.local]
base_url = "http://explicit.test"
api_key_env = "TEST_KEY"
[roles.narrator]
model = "explicit-model"
think = false
''')
    cfg = load_agent_config(env={"AGENT_CONFIG": str(path), "TEST_KEY": "private-value",
        "LLM_URL": "http://legacy.test", "V4_NARR_MODEL": "legacy-narrator",
        "V4_NARR_THINK": "invalid-but-overridden", "V4_DIGEST_MODEL": "legacy-action",
        "V4_DIRECTOR_EVALUATE": "invalid-but-overridden"})
    assert cfg.roles["narrator"].model == "explicit-model"
    assert cfg.roles["action"].model == "legacy-action"
    assert cfg.providers["local"].base_url == "http://explicit.test"
    assert cfg.providers["local"].api_key == "private-value"
    assert "private-value" not in repr(cfg)
    assert cfg.roles["narrator"].think is False and not cfg.director_evaluate
    defaults = parse_config({}, {})
    assert defaults.roles["narrator"].model == "qwen3.5:35b"
    assert defaults.roles["main"].model == "qwen2.5:7b-instruct"


@pytest.mark.parametrize("data", [
    {"roles": {"unknown": {}}}, {"roles": {"action": {"provider": "missing"}}},
    {"providers": {"local": {"api": "unsupported"}}},
    {"providers": {"local": {"api_key": "DO-NOT-PRINT"}}},
    {"providers": {"local": {"base_url": "https://user:DO-NOT-PRINT@host"}}},
    {"providers": {"local": {"api_key_env": "MISSING"}}},
    {"roles": {"main": {"reasoning_effort": "high"}}},
    {"providers": {"local": {"api": "openai"}}, "roles": {"main": {"think": False}}},
    {"roles": {"action": {"max_tokens": True}}}, {"roles": {"main": {"timeout": 0}}},
    {"supervision": {"max_calls": 7}}, {"supervision": {"enabled": "false"}},
])
def test_config_invalid_values_fail_without_secret(data):
    with pytest.raises(ConfigError) as exc:
        parse_config(data, {})
    assert "DO-NOT-PRINT" not in str(exc.value)


def test_explicit_missing_or_invalid_config(tmp_path):
    with pytest.raises(ConfigError):
        load_agent_config(str(tmp_path / "missing"), {})
    bad = tmp_path / "bad.toml"
    bad.write_text('secret = "DO-NOT-PRINT')
    with pytest.raises(ConfigError) as exc:
        load_agent_config(str(bad), {})
    assert "DO-NOT-PRINT" not in str(exc.value)


@pytest.mark.parametrize("api", ["ollama", "openai"])
def test_provider_request_json_and_auth(api):
    seen = []
    def handler(request):
        seen.append((request, json.loads(request.content)))
        content = '{"ok":true}'
        return httpx.Response(200, json={"message": {"content": content}} if api == "ollama" else
                              {"choices": [{"message": {"content": content}}]})
    cfg = config(api, api_key_env="TEST_KEY")
    gateway = ModelGateway(cfg, httpx.MockTransport(handler))
    assert run(gateway.object("main", "prompt")) == {"ok": True}
    request, body = seen[0]
    assert request.headers["authorization"] == "Bearer test-secret-value"
    assert str(request.url).endswith("/api/chat" if api == "ollama" else "/v1/chat/completions")
    if api == "ollama":
        assert body["format"] == "json" and body["think"] is False
        assert body["options"]["num_predict"] == cfg.roles["main"].max_tokens
    else:
        assert body["response_format"] == {"type": "json_object"}
        assert "think" not in body and body["max_tokens"] == cfg.roles["main"].max_tokens


def test_openai_reasoning_and_json_mode_override():
    seen = []
    cfg = config("openai", json_mode=False)
    cfg = replace(cfg, roles={**cfg.roles, "main": replace(cfg.roles["main"], reasoning_effort="low")})
    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok":true}'}}]})
    run(ModelGateway(cfg, httpx.MockTransport(handler)).object("main", "prompt"))
    body = seen[0]
    assert body["reasoning_effort"] == "low" and "max_completion_tokens" in body
    assert "temperature" not in body and "response_format" not in body


@pytest.mark.parametrize("api", ["ollama", "openai"])
@pytest.mark.parametrize("complete", [True, False])
def test_streaming_keeps_partial_text_and_safe_audit(api, complete):
    def handler(request):
        if api == "ollama":
            chunks = [{"message": {"content": "霧中"}}, {"message": {"content": "有腳步聲。"}}]
            if complete:
                chunks.append({"done": True})
            content = "\n".join(json.dumps(c) for c in chunks)
        else:
            chunks = [{"choices": [{"delta": {"content": s}, "finish_reason": None}]}
                      for s in ("霧中", "有腳步聲。")]
            content = "\n\n".join("data: " + json.dumps(c) for c in chunks)
            if complete:
                content += "\n\ndata: [DONE]\n\n"
        return httpx.Response(200, text=content)
    seen = []
    with turn_context(6, 4) as turn:
        text = run(ModelGateway(config(api), httpx.MockTransport(handler)).stream("narrator", "private prompt", seen.append))
    assert text == "霧中有腳步聲。" and seen[-1] == text
    assert turn.records[0]["status"] == ("ok" if complete else "partial")
    assert "private prompt" not in json.dumps(turn.records)
    assert turn.records[0]["source_revision"] == 4


@pytest.mark.parametrize("response", [httpx.Response(401, text="test-secret-value"),
    httpx.Response(200, json={"message": {"content": "not-json"}})])
def test_provider_failure_is_redacted(response):
    gateway = ModelGateway(config(api_key_env="TEST_KEY"), httpx.MockTransport(lambda _: response))
    with turn_context(6, 1) as turn, pytest.raises(ModelError) as exc:
        run(gateway.object("action", "prompt"))
    assert "test-secret-value" not in str(exc.value) + json.dumps(turn.records)
    assert turn.calls == 1 and turn.records[-1]["status"] == "unavailable"


def test_shared_budget_reserves_final_and_narration_and_caps_main():
    gateway = ModelGateway(config(), httpx.MockTransport(lambda _: httpx.Response(200,
        json={"message": {"content": "{}"}})))
    with turn_context(4, 1) as turn:
        with reserve_calls(2):
            run(gateway.object("main", ""))
            run(gateway.object("action", ""))
            with pytest.raises(BudgetExceeded):
                run(gateway.object("director", ""))
        run(gateway.object("main", ""))
        run(gateway.complete("narrator", ""))
        with pytest.raises(BudgetExceeded):
            run(gateway.object("progress", ""))
    assert turn.calls == 4
    with turn_context(6, 1) as turn:
        for _ in range(3):
            run(gateway.object("main", ""))
        with pytest.raises(BudgetExceeded):
            run(gateway.object("main", ""))
    assert turn.calls == 3


def test_transport_timeout_has_no_retries():
    calls = []
    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("test-secret-value")
    with pytest.raises(ModelError):
        run(ModelGateway(config(), httpx.MockTransport(handler)).complete("action", ""))
    assert len(calls) == 1


def test_channel_budgets_are_isolated_on_shared_gateway():
    async def handler(request):
        await asyncio.sleep(0)
        return httpx.Response(200, json={"message": {"content": "{}"}})
    gateway = ModelGateway(config(), httpx.MockTransport(handler))
    async def command(revision):
        with turn_context(6, revision) as turn:
            await gateway.object("main", "")
            await gateway.object("action", "")
            return turn
    async def scenario():
        return await asyncio.gather(command(1), command(9))
    a, b = run(scenario())
    assert a.calls == b.calls == 2 and a.trace_id != b.trace_id
    assert {r["source_revision"] for r in a.records} == {1}
    assert {r["source_revision"] for r in b.records} == {9}


def test_adapter_loads_config_once_for_all_channels(tmp_path, monkeypatch):
    from adapters.discord_bot import DiscordBot
    path = tmp_path / "agents.toml"
    path.write_text('[roles.main]\nmodel = "startup-model"\n')
    monkeypatch.setenv("AGENT_CONFIG", str(path))
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    bot = DiscordBot()
    path.write_text('[roles.main]\nmodel = "later-model"\n')
    monkeypatch.setenv("V4_NARR_MODEL", "changed-later")
    a, b = bot._v4_service("one"), bot._v4_service("two")
    assert a.gateway is b.gateway is bot.model_gateway
    assert a.agent_config.roles["main"].model == "startup-model"
    assert a.agent_config.roles["narrator"].model != "changed-later"


@pytest.mark.parametrize("api", ["ollama", "openai"])
@pytest.mark.parametrize("stream", [False, True])
def test_token_limit_is_partial_even_with_terminal_punctuation(api, stream):
    content = "海風拂過。"
    def handler(request):
        if api == "ollama":
            body = {"message": {"content": content}, "done": True, "done_reason": "length"}
            return httpx.Response(200, text=json.dumps(body))
        choice = {"finish_reason": "length", "delta" if stream else "message": {"content": content}}
        body = {"choices": [choice]}
        return httpx.Response(200, text="data: " + json.dumps(body) + "\n\n") if stream else httpx.Response(200, json=body)
    gateway = ModelGateway(config(api), httpx.MockTransport(handler))
    with turn_context(6, 1) as turn:
        result = run(gateway.stream("narrator", "") if stream else gateway.complete("narrator", ""))
    assert result == content and turn.records[-1]["status"] == "partial"


def test_stream_credentials_split_across_chunks_never_reach_callback():
    secret = "test-secret-value"
    chunks = [{"message": {"content": text}} for text in ("hello test-sec", "ret-value", " world.")]
    chunks.append({"done": True})
    endpoint = httpx.MockTransport(lambda _: httpx.Response(200, text="\n".join(json.dumps(c) for c in chunks)))
    updates = []
    text = run(ModelGateway(config(api_key_env="TEST_KEY"), endpoint).stream("narrator", "", updates.append))
    assert text == "hello [redacted] world."
    assert all(secret not in s and "test-sec" not in s for s in updates)
