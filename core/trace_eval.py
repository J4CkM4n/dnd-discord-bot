"""Offline trace export, replay and evaluation: python -m core.trace_eval --help."""
import argparse
import asyncio
import json
import tempfile
from pathlib import Path

import httpx

from .agent_config import parse_config
from .debug_capture import DebugCapture
from .model_gateway import ModelGateway
from .persistence import CampaignRepository
from .service import V4Service


def read_traces(path):
    path = Path(path)
    paths = sorted(path.glob("trace-*.jsonl")) if path.is_dir() else [path]
    groups = {}
    for source in paths:
        with source.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:  # an interrupted final write is never a valid replay
                    continue
                if row.get("schema_version") == 1 and "trace_id" in row:
                    groups.setdefault(row["trace_id"], []).append(row)
    return {key: sorted(rows, key=lambda r: r["sequence"]) for key, rows in groups.items()}


def evaluate(traces, labels=()):
    """Objective metrics plus explicit human labels; do not pretend humor is automatic."""
    annotated = {row["trace_id"]: row for row in labels}
    results = []
    for trace_id, rows in traces.items():
        end = next((r["payload"] for r in reversed(rows) if r["kind"] == "command_end"), None)
        if end is None:
            continue
        routes = [r["payload"]["record"] for r in rows if r["kind"] == "operator"
                  and r["payload"]["record"].get("role") == "router"
                  and r["payload"]["record"].get("kind") == "report"]
        category = (routes[-1].get("payload", {}).get("answers", {}).get("category", {}).get("choice")
                    if routes else None)
        rendered = json.dumps(end["result"], ensure_ascii=False)
        label = annotated.get(trace_id, {})
        metadata = [r["payload"]["metadata"] for r in rows if r["kind"] == "model_response"]
        results.append({"trace_id": trace_id, "category": category, "calls": end["calls"],
            "model_ms": sum(r.get("latency_ms", 0) for r in metadata),
            "clarification": "❓" in rendered, "hint": "💡" in rendered,
            "routing_correct": category == label["category"] if "category" in label else None,
            "unnecessary_clarification": "❓" in rendered and not label["clarification"]
                if "clarification" in label else None,
            "details_preserved": all(s in rendered for s in label["required_details"])
                if "required_details" in label else None,
            **{k: label[k] for k in ("hint_useful", "canon_consistent", "humor", "enjoyment") if k in label}})
    def average(key):
        values = [r[key] for r in results if type(r.get(key)) in (int, float, bool)]
        return sum(values) / len(values) if values else None
    return {"turns": len(results), "metrics": {key: average(key) for key in (
        "calls", "model_ms", "routing_correct", "unnecessary_clarification", "details_preserved",
        "hint_useful", "canon_consistent", "humor", "enjoyment")}, "results": results}


async def replay(rows):
    """Run a recorded command in a temporary database with a network-free transport."""
    if [r.get("sequence") for r in rows] != list(range(1, len(rows) + 1)):
        raise ValueError("Replay requires every trace record in sequence")
    start = next((r["payload"] for r in rows if r["kind"] == "command_start"), None)
    expected = next((r["payload"] for r in reversed(rows) if r["kind"] == "command_end"), None)
    if not start or not expected:
        raise ValueError("Replay requires a complete command_start and command_end")
    requests = [r["payload"] for r in rows if r["kind"] == "model_request"]
    responses = {r["payload"]["call_id"]: r["payload"] for r in rows if r["kind"] == "model_response"}
    if any(r["call_id"] not in responses for r in requests):
        raise ValueError("Trace has missing model responses")
    native = any(r["provider"] == "typesafe" for r in requests)
    cfg = parse_config(start["config"], {"REPLAY_TYPESAFE_KEY": "offline-placeholder" if native else ""})
    cursor, mismatches = 0, []

    def handler(request):
        nonlocal cursor
        body = json.loads(request.content)
        if cursor >= len(requests):
            mismatches.append("unexpected_model_call")
            return httpx.Response(503)
        original = requests[cursor]
        cursor += 1
        if body != original["request"]:
            mismatches.append(f"request_changed:{original['call_id']}")
        response = responses[original["call_id"]]
        meta, content = response["metadata"], response["content"]
        if meta.get("error_code") in ("timeout", "connection"):
            raise httpx.ReadTimeout("recorded_failure", request=request)
        status = meta.get("http_status", 200)
        if status >= 400:
            return httpx.Response(status)
        if original["provider"] == "typesafe":
            return httpx.Response(200, json=content)
        partial = meta["status"] != "ok"
        if body.get("stream"):
            if original["provider"] == "ollama":
                chunk = {"message": {"content": content}, "done": not partial,
                         "done_reason": "length" if partial else "stop"}
                return httpx.Response(200, text=json.dumps(chunk) + "\n")
            chunk = {"choices": [{"delta": {"content": content}, "finish_reason": "length" if partial else "stop"}]}
            return httpx.Response(200, text="data: " + json.dumps(chunk) + "\n\n")
        if original["provider"] == "ollama":
            return httpx.Response(200, json={"message": {"content": content},
                "done_reason": meta.get("finish_reason", "stop")})
        return httpx.Response(200, json={"choices": [{"message": {"content": content},
                                                     "finish_reason": meta.get("finish_reason", "stop")}]})

    with tempfile.TemporaryDirectory(prefix="dnd-replay-") as directory:
        repository = CampaignRepository(str(Path(directory) / "campaigns.db"))
        repository.commit(start["channel_id"], 0, start["snapshot"], start["entries"],
                          start["unfinished"], [], operator_state=start["operator_state"])
        with repository.connect() as conn:
            conn.execute("UPDATE channels SET revision=? WHERE channel_id=?", (start["revision"], start["channel_id"]))
        gateway = ModelGateway(cfg, httpx.MockTransport(handler))
        service = V4Service(directory, channel_id=start["channel_id"], gateway=gateway)
        service.debug_capture = DebugCapture(directory, cfg, enabled=False)
        service.plot, service.rules = start["plot"], start["rules"]
        # Only commands captured by the serialized service wrapper are eligible.
        allowed = {"handle", "confirm", "settle_roll", "continue_narration", "create_character",
                   "remove_character", "transfer_item", "admin_give", "admin_inspire", "admin_xp",
                   "new_game", "reset_game", "action_plan"}
        if start["method"] not in allowed:
            raise ValueError("Unsupported replay command")
        arguments = dict(start["arguments"])
        if start.get("streaming"):
            arguments["on_delta"] = lambda text: None
        if start.get("resolved_callback"):
            failed = any(r["kind"] == "delivery" and r["payload"].get("phase") == "verdict"
                         and r["payload"].get("status") == "failed" for r in rows)
            async def on_resolved(lines):
                if failed:
                    raise RuntimeError("recorded_delivery_failure")
            arguments["on_resolved"] = on_resolved
        result = await getattr(service, start["method"])(**arguments)
        actual = json.loads(json.dumps(result, ensure_ascii=False))
        commits = [r["payload"] for r in rows if r["kind"] == "commit"]
        snapshot_matches = not commits or json.loads(json.dumps(service._snapshot())) == commits[-1]["snapshot"]
        return {"output_matches": actual == expected["result"], "snapshot_matches": snapshot_matches,
                "calls_consumed": cursor, "all_calls_consumed": cursor == len(requests),
                "request_mismatches": mismatches, "result": actual}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("export", "evaluate", "replay"):
        child = sub.add_parser(name)
        child.add_argument("source", help="Debug directory or exported JSONL")
        child.add_argument("--trace-id")
        if name == "export":
            child.add_argument("--output", required=True)
        if name == "evaluate":
            child.add_argument("--labels", help="JSONL human annotations keyed by trace_id")
    args = parser.parse_args()
    traces = read_traces(args.source)
    if args.trace_id:
        traces = {args.trace_id: traces[args.trace_id]}
    if args.command == "export":
        with Path(args.output).open("w", encoding="utf-8") as stream:
            for rows in traces.values():
                for row in rows:
                    stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    elif args.command == "evaluate":
        labels = [json.loads(line) for line in Path(args.labels).read_text(encoding="utf-8").splitlines() if line] if args.labels else []
        print(json.dumps(evaluate(traces, labels), ensure_ascii=False, indent=2))
    else:
        reports = {key: asyncio.run(replay(rows)) for key, rows in traces.items()}
        print(json.dumps(reports, ensure_ascii=False, indent=2))
        if any(not r["output_matches"] or not r["snapshot_matches"] or not r["all_calls_consumed"]
               or r["request_mismatches"] for r in reports.values()):
            raise SystemExit(1)


if __name__ == "__main__":
    main()
