"""Provider transport and per-command call budgets shared by all operators."""

import asyncio
import json
import logging
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

import httpx

from .agent_config import AgentConfig

log = logging.getLogger("dnd-bot")


class ModelError(RuntimeError):
    pass


class BudgetExceeded(ModelError):
    pass


@dataclass
class TurnContext:
    limit: int
    revision: int
    calls: int = 0
    main_calls: int = 0
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    records: list[dict] = field(default_factory=list)

    @property
    def remaining(self):
        return self.limit - self.calls

    def record(self, role, status, **payload):
        record = {"trace_id": self.trace_id, "source_revision": self.revision,
                  "role": role, "status": status, "call_count": self.calls,
                  "event_ids": [], **payload}
        self.records.append(record)
        return record


_turn: ContextVar[TurnContext | None] = ContextVar("model_turn", default=None)
_reserve: ContextVar[int] = ContextVar("reserved_model_calls", default=0)


@contextmanager
def turn_context(limit: int, revision: int):
    context = TurnContext(limit, revision)
    token = _turn.set(context)
    reserve = _reserve.set(0)
    try:
        yield context
    finally:
        _reserve.reset(reserve)
        _turn.reset(token)


@contextmanager
def reserve_calls(count: int):
    token = _reserve.set(max(count, _reserve.get()))
    try:
        yield
    finally:
        _reserve.reset(token)


def current_turn():
    return _turn.get()


def json_object(text: str) -> dict:
    text = text.strip()
    if text.startswith("```json") and text.endswith("```"):
        text = text[7:-3].strip()
    elif text.startswith("```") and text.endswith("```"):
        text = text[3:-3].strip()
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        raise ModelError("Invalid structured model response") from None
    if type(data) is not dict:
        raise ModelError("Expected a structured object")
    return data


class ModelGateway:
    def __init__(self, config: AgentConfig, transport=None):
        self.config = config
        self.transport = transport

    def _request(self, role, prompt, json_mode, stream, max_tokens):
        spec = self.config.roles[role]
        provider = self.config.providers[spec.provider]
        tokens = min(max_tokens or spec.max_tokens, spec.max_tokens)
        body = {"model": spec.model, "messages": [{"role": "user", "content": prompt}], "stream": stream}
        if provider.api == "ollama":
            url = provider.base_url + "/api/chat"
            body["options"] = {"temperature": spec.temperature, "num_predict": tokens}
            if spec.think is not None:
                body["think"] = spec.think
            if json_mode and provider.json_mode:
                body["format"] = "json"
        else:
            url = provider.base_url + "/chat/completions"
            body["max_completion_tokens" if spec.reasoning_effort else "max_tokens"] = tokens
            if spec.reasoning_effort is None:
                body["temperature"] = spec.temperature
            else:
                body["reasoning_effort"] = spec.reasoning_effort
            if json_mode and provider.json_mode:
                body["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {provider.api_key}"} if provider.api_key else {}
        return spec, provider, url, body, headers

    def _begin(self, role):
        context = current_turn()
        spec = self.config.roles[role]
        if context:
            if context.remaining <= _reserve.get() or (role == "main" and context.main_calls >= 3):
                context.record(role, "budget_exhausted", kind="model_call", provider=spec.provider, model=spec.model)
                raise BudgetExceeded("Model call budget exhausted")
            context.calls += 1
            context.main_calls += role == "main"
            return context.record(role, "running", kind="model_call", provider=spec.provider, model=spec.model)
        return {"role": role, "provider": spec.provider, "model": spec.model, "status": "running"}

    def _finish(self, record, started):
        record["latency_ms"] = round((time.monotonic() - started) * 1000, 2)
        log.info("model | role=%s provider=%s model=%s status=%s error=%s http=%s ms=%.1f calls=%s",
                 record["role"], record["provider"], record["model"], record["status"],
                 record.get("error_code", ""), record.get("http_status", ""), record["latency_ms"],
                 record.get("call_count", 1))

    def _redact(self, text, *, partial=False):
        for provider in self.config.providers.values():
            if provider.api_key:
                text = text.replace(provider.api_key, "[redacted]")
                if partial:
                    # Do not emit the first half of a credential split across chunks.
                    for size in range(min(len(text), len(provider.api_key) - 1), 0, -1):
                        if text.endswith(provider.api_key[:size]):
                            text = text[:-size]
                            break
        return text

    async def complete(self, role: str, prompt: str, *, json_mode=False, max_tokens=None) -> str:
        record, started = self._begin(role), time.monotonic()
        spec, provider, url, body, headers = self._request(role, prompt, json_mode, False, max_tokens)
        try:
            async with asyncio.timeout(spec.timeout):
                async with httpx.AsyncClient(timeout=spec.timeout, transport=self.transport) as client:
                    response = await client.post(url, json=body, headers=headers)
                    response.raise_for_status()
                    data = response.json()
            content = (data["message"]["content"] if provider.api == "ollama"
                       else data["choices"][0]["message"]["content"])
            if not isinstance(content, str):
                raise ValueError()
            content = self._redact(content).strip()
            if json_mode:
                json_object(content)
            reason = (data.get("done_reason") if provider.api == "ollama"
                      else data["choices"][0].get("finish_reason"))
            record["status"] = "partial" if reason in ("length", "content_filter") else "ok"
            return content
        except asyncio.CancelledError:
            record["status"] = "cancelled"
            raise
        except Exception as exc:
            record["status"] = "unavailable"
            if isinstance(exc, httpx.HTTPStatusError):
                record["error_code"] = "http_error"
                record["http_status"] = exc.response.status_code
            elif isinstance(exc, (TimeoutError, httpx.TimeoutException)):
                record["error_code"] = "timeout"
            elif isinstance(exc, httpx.RequestError):
                record["error_code"] = "connection"
            else:
                record["error_code"] = "invalid_response"
            # Never expose response bodies, request headers, or provider errors.
            raise ModelError(f"Model request failed for role {role}") from None
        finally:
            self._finish(record, started)

    async def object(self, role, prompt):
        return json_object(await self.complete(role, prompt, json_mode=True))

    async def stream(self, role: str, prompt: str, on_delta=None, *, max_tokens=None) -> str:
        record, started = self._begin(role), time.monotonic()
        spec, provider, url, body, headers = self._request(role, prompt, False, True, max_tokens)
        text, done, truncated = "", False, False
        try:
            async with asyncio.timeout(spec.timeout):
                timeout = httpx.Timeout(spec.timeout, connect=min(5, spec.timeout), read=min(15, spec.timeout))
                async with httpx.AsyncClient(timeout=timeout, transport=self.transport) as client:
                    async with client.stream("POST", url, json=body, headers=headers) as response:
                        response.raise_for_status()
                        async for line in response.aiter_lines():
                            line = line.strip()
                            if not line or line.startswith(":"):
                                continue
                            if provider.api == "openai":
                                if not line.startswith("data:"):
                                    continue
                                line = line[5:].strip()
                                if line == "[DONE]":
                                    done = True
                                    break
                            chunk = json.loads(line)
                            if "error" in chunk:
                                raise ValueError()
                            if provider.api == "ollama":
                                delta = chunk.get("message", {}).get("content", "")
                                done = chunk.get("done") is True
                                truncated = chunk.get("done_reason") == "length"
                            else:
                                choices = chunk.get("choices", [])
                                if not choices:  # optional usage-only SSE chunk
                                    continue
                                delta = choices[0].get("delta", {}).get("content") or ""
                                reason = choices[0].get("finish_reason")
                                done = reason is not None
                                truncated = reason not in (None, "stop")
                            if not isinstance(delta, str):
                                raise ValueError()
                            text += delta
                            if len(text) > 131072:
                                raise ValueError()
                            if delta and on_delta:
                                on_delta(self._redact(text, partial=True))
                            if done:
                                break
            record["status"] = "ok" if done and not truncated else "partial"
        except asyncio.CancelledError:
            record["status"] = "cancelled"
            raise
        except Exception:
            record["status"] = "partial" if text else "unavailable"
        finally:
            self._finish(record, started)
        return self._redact(text, partial=not done or truncated)
