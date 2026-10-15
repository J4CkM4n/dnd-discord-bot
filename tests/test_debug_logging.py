"""Action diagnostics explain LLM decisions and outages without credentials."""
import logging
from dataclasses import replace

import httpx
import pytest

from core.logging_config import configure_logging
from core.model_gateway import ModelError, ModelGateway, turn_context
from test_agent_config_gateway import config
from test_campaign import run
from test_operators import Endpoint, supervised


def test_debug_configuration_keeps_library_wire_logs_disabled(monkeypatch):
    names = ("dnd-bot", "discord_adapter", "httpx", "httpcore", "discord")
    previous = {name: logging.getLogger(name).level for name in names}
    try:
        monkeypatch.setenv("LOG_LEVEL", "DEBUG")
        configure_logging()
        assert logging.getLogger("dnd-bot").getEffectiveLevel() == logging.DEBUG
        assert logging.getLogger("discord_adapter").getEffectiveLevel() == logging.DEBUG
        for name in ("httpx", "httpcore", "discord"):
            assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING
    finally:
        for name, level in previous.items():
            logging.getLogger(name).setLevel(level)


def test_main_clarification_and_fallback_have_distinct_logs(tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger="dnd-bot")
    endpoint = Endpoint(main=[{"consult": ["action"]},
        {"decision": "clarify", "intent": "action", "director": False}],
        action=[{"action": "attack", "target": "船長"}])
    s = supervised(tmp_path, endpoint)
    run(s.handle("想和那位較量一下", user_id="u1"))
    assert '"decision": "clarify"' in caplog.text
    assert "mode=supervised" in caplog.text and "reason=clarification_required" in caplog.text
    assert "status=fallback" not in caplog.text
    trace_records = [r for r in caplog.records if "trace=" in r.getMessage()]
    traces = {r.getMessage().split("trace=", 1)[1].split()[0] for r in trace_records}
    assert len(traces) == 1
    caplog.clear()
    outage = supervised(tmp_path / "outage", Endpoint(main=[httpx.Response(503)]))
    run(outage.handle("與船長決鬥", user_id="u1"))
    assert "status=fallback" in caplog.text and "candidate=parsed" in caplog.text
    assert "http=503" in caplog.text and "action selected" in caplog.text


def test_schema_failure_explains_expected_fields_and_invalid_operator(tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger="dnd-bot")
    s = supervised(tmp_path, Endpoint(main=[{"operators": ["action"]}]))
    run(s.handle("嘗試處理眼前的問題", user_id="u1"))
    assert "Invalid operator response fields" in caplog.text
    assert "expected=['consult']" in caplog.text and "received=['operators']" in caplog.text
    assert "stage=plan" in caplog.text and "status=fallback" in caplog.text


def test_action_schema_failure_logs_the_unsupported_action(tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger="dnd-bot")
    s = supervised(tmp_path, Endpoint(main=[{"consult": ["action"]},
        {"decision": "clarify", "intent": "action", "director": False}],
        action=[{"action": "unsupported_duel", "target": "船長"}]))
    run(s.handle("想和那位較量一下", user_id="u1"))
    assert "action interpretation" in caplog.text and "bad action: unsupported_duel" in caplog.text
    assert "role=action status=invalid" in caplog.text


def test_truncated_json_exposes_finish_reason_and_thinking_length(caplog):
    caplog.set_level(logging.DEBUG, logger="dnd-bot")
    response = {"message": {"content": '{"consult":', "thinking": "reasoning was cut short"},
                "done_reason": "length"}
    gateway = ModelGateway(config(), httpx.MockTransport(lambda _: httpx.Response(200, json=response)))
    with turn_context(6, 1) as turn, pytest.raises(ModelError):
        run(gateway.object("main", "PRIVATE_PROMPT_NOT_FOR_LOGS"))
    assert 'finish="length"' in caplog.text and "thinking_chars=23" in caplog.text
    assert "column" in caplog.text and "error=invalid_response" in caplog.text
    assert turn.records[0]["finish_reason"] == "length"
    assert "PRIVATE_PROMPT_NOT_FOR_LOGS" not in caplog.text


@pytest.mark.parametrize("stream", [False, True])
def test_http_failures_and_startup_config_do_not_expose_credentials(stream, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger="dnd-bot")
    monkeypatch.setenv("DISCORD_TOKEN", "discord-private-value")
    cfg = config(api_key_env="TEST_KEY")
    gateway = ModelGateway(cfg, httpx.MockTransport(lambda _: httpx.Response(
        401, text="PRIVATE_PROVIDER_BODY test-secret-value discord-private-value")))
    gateway.log_configuration()
    with turn_context(6, 1) as turn:
        if stream:
            assert run(gateway.stream("narrator", "PRIVATE_PROMPT")) == ""
        else:
            with pytest.raises(ModelError):
                run(gateway.object("main", "PRIVATE_PROMPT"))
    assert "http=401" in caplog.text and "error=http_error" in caplog.text
    assert "auth_configured=True" in caplog.text and "endpoint=" in caplog.text
    assert "test-secret-value" not in caplog.text and "discord-private-value" not in caplog.text
    assert "PRIVATE_PROVIDER_BODY" not in caplog.text and "PRIVATE_PROMPT" not in caplog.text
    assert turn.records[0]["http_status"] == 401


def test_debug_previews_redact_secrets_before_json_escaping(monkeypatch):
    cfg = config()
    secret = 'private"quoted\\key'
    provider = replace(cfg.providers["local"], api_key=secret)
    gateway = ModelGateway(replace(cfg, providers={"local": provider}))
    monkeypatch.setenv("DISCORD_TOKEN", "discord-secret")
    preview = gateway.diagnostic({"target": secret, "nested": [secret, "discord-secret\nline2"]})
    assert "private" not in preview and "discord-secret" not in preview
    assert "[redacted]" in preview and "\n" not in preview
    assert len(gateway.diagnostic("x" * 1000, 50)) == 51
