"""Opt-in local traces. Diagnostic I/O must never decide a gameplay outcome."""

import json
import logging
import os
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

log = logging.getLogger("dnd-bot")
_active = ContextVar("debug_capture", default=None)
_write_lock = threading.Lock()


def clean(value, secrets):
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[redacted]")
        return value
    if isinstance(value, dict):
        return {clean(str(k), secrets): clean(v, secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v, secrets) for v in value]
    if value is None or type(value) in (bool, int, float):
        return value
    return "<unsupported>"


class DebugCapture:
    def __init__(self, data_dir, config, *, enabled=None, max_file=25 * 1024**2,
                 max_total=250 * 1024**2, retention=7 * 86400):
        self.enabled = (os.getenv("AI_DEBUG_CAPTURE", "off").lower() == "full"
                        if enabled is None else enabled)
        self.path = Path(data_dir) / "debug"
        self.secrets = [p.api_key for p in config.providers.values()]
        self.secrets += [config.routing.api_key, os.getenv("DISCORD_TOKEN", "")]
        self.max_file, self.max_total, self.retention = max_file, max_total, retention

    def write(self, trace_id, sequence, kind, payload):
        if not self.enabled:
            return
        try:
            row = clean({"schema_version": 1, "trace_id": trace_id, "sequence": sequence,
                         "timestamp": time.time(), "kind": kind, "payload": payload}, self.secrets)
            encoded = (json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
            with _write_lock:
                self.path.mkdir(parents=True, exist_ok=True)
                files = sorted(self.path.glob("trace-*.jsonl"))
                now = time.time()
                for path in files:
                    if now - path.stat().st_mtime > self.retention:
                        path.unlink()
                files = sorted(self.path.glob("trace-*.jsonl"))
                total = sum(p.stat().st_size for p in files)
                while files and total + len(encoded) > self.max_total:
                    old = files.pop(0)
                    total -= old.stat().st_size
                    old.unlink()
                if len(encoded) > min(self.max_total, self.max_file):
                    log.warning("debug capture record exceeds storage limit | kind=%s", kind)
                    return
                target = files[-1] if files else None
                if target is None or target.stat().st_size + len(encoded) > self.max_file:
                    target = self.path / f"trace-{time.time_ns()}.jsonl"
                fd = os.open(target, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                with os.fdopen(fd, "ab") as stream:
                    stream.write(encoded)
        except (OSError, ValueError, TypeError):
            log.warning("debug capture unavailable | kind=%s", kind)


@contextmanager
def capture_context(sink, trace_id):
    token = _active.set([sink, trace_id, 0] if sink is not None else None)
    try:
        yield
    except BaseException as exc:
        capture("command_error", error=type(exc).__name__)
        raise
    finally:
        _active.reset(token)


def capture(kind, **payload):
    active = _active.get()
    if active:
        active[2] += 1
        active[0].write(active[1], active[2], kind, payload)


def replay_config(config):
    """Only nonsecret, supported configuration fields enter the replay artifact."""
    from dataclasses import asdict
    providers = {name: {"api": p.api, "base_url": p.base_url, "json_mode": p.json_mode}
                 for name, p in config.providers.items()}
    roles = {name: {k: v for k, v in asdict(role).items() if v is not None}
             for name, role in config.roles.items()}
    routing = asdict(config.routing)
    routing.pop("api_key")
    routing["api_key_env"] = "REPLAY_TYPESAFE_KEY"
    return {"providers": providers, "roles": roles, "routing": routing,
            "hints": asdict(config.hints),
            "supervision": {"enabled": config.enabled, "max_calls": config.max_calls,
                            "progress_interval": config.progress_interval,
                            "director_evaluate": config.director_evaluate}}
