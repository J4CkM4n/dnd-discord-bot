"""Opt-in capture, redaction, bounded storage and network-free replay."""
import json
import os
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from core.agent_config import parse_config
from core.debug_capture import DebugCapture, capture, capture_context
from core.model_gateway import ModelGateway, turn_context
from core.trace_eval import evaluate, read_traces, replay
from test_campaign import run
from test_operators import Endpoint
from test_system_one import routed, router_answer


def test_capture_off_never_creates_storage(tmp_path):
    sink = DebugCapture(tmp_path, parse_config({}, {}), enabled=False)
    with capture_context(sink, "t"):
        capture("input", text="private words")
    assert not (tmp_path / "debug").exists()


def test_capture_redacts_before_serialization_and_rotates_and_expires(tmp_path, monkeypatch):
    secret = 'quoted"secret\\value'
    monkeypatch.setenv("DISCORD_TOKEN", secret)
    sink = DebugCapture(tmp_path, parse_config({}, {}), enabled=True, max_file=400, max_total=900, retention=10)
    for i in range(8):
        sink.write("t", i, "input", {"text": secret + " x" * 30})
    files = sorted((tmp_path / "debug").glob("*.jsonl"))
    assert sum(p.stat().st_size for p in files) <= 900 and len(files) > 1
    for p in files:
        assert secret not in p.read_text(encoding="utf-8")
        assert "[redacted]" in p.read_text(encoding="utf-8")
        os.utime(p, (time.time() - 20, time.time() - 20))
    sink.write("next", 1, "input", {})
    assert len(list((tmp_path / "debug").glob("*.jsonl"))) == 1


def test_disk_failure_does_not_interrupt_gameplay(tmp_path, monkeypatch):
    sink = DebugCapture(tmp_path, parse_config({}, {}), enabled=True)
    monkeypatch.setattr(Path, "mkdir", lambda *a, **k: (_ for _ in ()).throw(OSError("full")))
    with capture_context(sink, "trace"):
        capture("input", text="test")


def test_stream_capture_stitches_split_secrets_and_marks_partial(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "split-secret")
    cfg = parse_config({}, {})
    payload = '\n'.join(json.dumps({"message": {"content": text}}) for text in ("split-", "secret"))
    gateway = ModelGateway(cfg, httpx.MockTransport(lambda _: httpx.Response(200, text=payload)))
    sink = DebugCapture(tmp_path, cfg, enabled=True)
    with turn_context(6, 1) as turn, capture_context(sink, turn.trace_id):
        run(gateway.stream("narrator", "not a secret"))
    rows = next(iter(read_traces(tmp_path / "debug").values()))
    response = next(r["payload"] for r in rows if r["kind"] == "model_response")
    assert response["metadata"]["status"] == "partial"
    assert response["content"] == "[redacted]"


def test_full_turn_replays_with_isolated_database_and_no_live_requests(tmp_path):
    endpoint = Endpoint(router=[router_answer()], action=[{"action": "search"}])
    s, _ = routed(tmp_path, endpoint)
    s.game.world.here.hidden_items = [("破舊木雕", 1)]
    s.debug_capture = DebugCapture(tmp_path, s.agent_config, enabled=True)
    run(s.handle("搜索", user_id="u1"))
    traces = read_traces(tmp_path / "debug")
    rows = next(iter(traces.values()))
    calls = len(endpoint.calls)
    report = run(replay(rows))
    assert report["output_matches"] and report["snapshot_matches"] and report["all_calls_consumed"]
    assert report["request_mismatches"] == []
    assert len(endpoint.calls) == calls
    key = next(iter(traces))
    metrics = evaluate(traces, [{"trace_id": key, "category": "search", "clarification": False,
        "humor": 3, "enjoyment": 4, "canon_consistent": True}])
    assert metrics["metrics"]["routing_correct"] == 1 and metrics["metrics"]["enjoyment"] == 4


def test_native_trace_redacts_key_and_replays_probabilities(tmp_path):
    s, _ = routed(tmp_path, Endpoint(), native=True)
    s.game.world.here.hidden_items = [("破舊木雕", 1)]
    s.debug_capture = DebugCapture(tmp_path, s.agent_config, enabled=True)
    run(s.handle("搜索", user_id="u1"))
    paths = list((tmp_path / "debug").glob("*.jsonl"))
    assert "native-private" not in paths[0].read_text(encoding="utf-8")
    rows = next(iter(read_traces(tmp_path / "debug").values()))
    report = run(replay(rows))
    assert report["output_matches"] and report["snapshot_matches"]
    assert not report["request_mismatches"]


@pytest.mark.parametrize("partial", [False, True])
def test_streamed_command_records_delivered_text_and_replays_offline(tmp_path, partial):
    def narration(body):
        assert body["stream"] is True
        chunks = [{"message": {"content": "霧裡有鹽味，"}},
                  {"message": {"content": "木板在腳下呻吟。"}, "done": not partial}]
        return httpx.Response(200, text="\n".join(json.dumps(c) for c in chunks))
    s, _ = routed(tmp_path, Endpoint(narrator=[narration]), native=True)
    s.game.world.here.hidden_items = [("破紙", 1)]
    s.debug_capture = DebugCapture(tmp_path, s.agent_config, enabled=True)
    delivered = []
    run(s.handle("搜索", user_id="u1", on_delta=delivered.append))
    rows = next(iter(read_traces(tmp_path / "debug").values()))
    callbacks = [r["payload"]["text"] for r in rows if r["kind"] == "delivery"
                 and r["payload"]["phase"] == "stream"]
    assert callbacks == delivered and len(delivered) == 2
    report = run(replay(rows))
    assert report["output_matches"] and report["snapshot_matches"] and report["all_calls_consumed"]
    assert not report["request_mismatches"]


def test_committed_verdict_delivery_failure_replays_without_narrating(tmp_path):
    s, _ = routed(tmp_path, Endpoint(), native=True)
    s.game.world.here.hidden_items = [("破紙", 1)]
    s.debug_capture = DebugCapture(tmp_path, s.agent_config, enabled=True)
    async def fail_delivery(lines):
        raise RuntimeError("Discord temporarily unavailable")
    run(s.handle("搜索", user_id="u1", on_resolved=fail_delivery))
    rows = next(iter(read_traces(tmp_path / "debug").values()))
    assert s._unfinished
    report = run(replay(rows))
    assert report["output_matches"] and report["snapshot_matches"] and report["all_calls_consumed"]
    assert not report["request_mismatches"]


def test_export_evaluate_and_replay_cli_and_incomplete_trace_rejection(tmp_path):
    s, _ = routed(tmp_path, Endpoint(), native=True)
    s.game.world.here.hidden_items = [("破紙", 1)]
    s.debug_capture = DebugCapture(tmp_path, s.agent_config, enabled=True)
    run(s.handle("搜索", user_id="u1"))
    trace_id, rows = next(iter(read_traces(tmp_path / "debug").items()))
    exported = tmp_path / "export.jsonl"
    labels = tmp_path / "labels.jsonl"
    labels.write_text(json.dumps({"trace_id": trace_id, "category": "search"}), encoding="utf-8")
    def cli(*arguments):
        completed = subprocess.run([sys.executable, "-X", "utf8", "-m", "core.trace_eval", *map(str, arguments)],
                                   capture_output=True, text=True, encoding="utf-8", check=True)
        return completed.stdout
    cli("export", tmp_path / "debug", "--output", exported, "--trace-id", trace_id)
    assert json.loads(cli("evaluate", exported, "--labels", labels))["metrics"]["routing_correct"] == 1
    assert json.loads(cli("replay", exported))[trace_id]["snapshot_matches"]
    with pytest.raises(ValueError, match="every trace record"):
        run(replay(rows[:1] + rows[2:]))
