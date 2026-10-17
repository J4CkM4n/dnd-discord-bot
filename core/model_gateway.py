"""Provider transport and per-command call budgets shared by all operators."""

import asyncio
import json
import logging
import os
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

import httpx

from .agent_config import AgentConfig
from .debug_capture import capture

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
        capture("operator", record=record)
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
    except json.JSONDecodeError as exc:
        raise ModelError(f"Invalid structured model response at line {exc.lineno} column {exc.colno}") from None
    except (ValueError, TypeError):
        raise ModelError("Invalid structured model response") from None
    if type(data) is not dict:
        raise ModelError("Expected a structured object")
    return data


class ModelGateway:
    def __init__(self, config: AgentConfig, transport=None):
        self.config = config
        self.transport = transport

    def diagnostic(self, value, limit=600):
        """Redact known credentials before encoding bounded single-line diagnostics."""
        from .privacy import private_data
        if private_data():
            return "[private campaign data]"
        def clean(item):
            if isinstance(item, str):
                return self._redact(item)
            if isinstance(item, dict):
                return {self._redact(str(k)): clean(v) for k, v in item.items()}
            if isinstance(item, (list, tuple)):
                return [clean(v) for v in item]
            if item is None or type(item) in (bool, int, float):
                return item
            return "<unsupported>"
        value = json.dumps(clean(value), ensure_ascii=False)
        return value[:limit] + ("…" if len(value) > limit else "")

    def log_configuration(self):
        log.info("models configured | supervision=%s max_calls=%d source=%s",
                 self.config.enabled, self.config.max_calls,
                 self.diagnostic(os.environ.get("AGENT_CONFIG", "agents.toml")))
        for role, spec in self.config.roles.items():
            provider = self.config.providers[spec.provider]
            log.info("model config | role=%s provider=%s api=%s endpoint=%s model=%s "
                     "timeout=%s max_tokens=%s think=%s reasoning=%s auth_configured=%s",
                     role, spec.provider, provider.api, self.diagnostic(provider.base_url),
                     self.diagnostic(spec.model), spec.timeout, spec.max_tokens, spec.think,
                     spec.reasoning_effort, bool(provider.api_key))

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

    def _begin(self, role, *, provider=None, model=None):
        context = current_turn()
        if provider is None:
            spec = self.config.roles[role]
            provider, model = spec.provider, spec.model
        if context:
            if context.remaining <= _reserve.get() or (role == "main" and context.main_calls >= 3):
                context.record(role, "budget_exhausted", kind="model_call", provider=provider, model=model)
                log.warning("model | trace=%s role=%s status=budget_exhausted remaining=%d reserved=%d",
                            context.trace_id, role, context.remaining, _reserve.get())
                raise BudgetExceeded("Model call budget exhausted")
            context.calls += 1
            context.main_calls += role == "main"
            return context.record(role, "running", kind="model_call", provider=provider, model=model)
        return {"role": role, "provider": provider, "model": model, "status": "running"}

    def _finish(self, record, started):
        record["latency_ms"] = round((time.monotonic() - started) * 1000, 2)
        log.info("model | trace=%s role=%s provider=%s model=%s status=%s error=%s http=%s "
                 "finish=%s output_chars=%s thinking_chars=%s ms=%.1f calls=%s",
                 record.get("trace_id", "-"), record["role"], record["provider"],
                 self.diagnostic(record["model"]), record["status"],
                 record.get("error_code", ""), record.get("http_status", ""),
                 self.diagnostic(record.get("finish_reason", "")), record.get("output_chars", 0),
                 record.get("thinking_chars", 0), record["latency_ms"],
                 record.get("call_count", 1))
        if record.get("error_detail"):
            log.debug("model failure | trace=%s role=%s detail=%s",
                      record.get("trace_id", "-"), record["role"], record["error_detail"])

    def _redact(self, text, *, partial=False):
        secrets = [p.api_key for p in self.config.providers.values()]
        secrets.append(self.config.routing.api_key)
        secrets.append(os.environ.get("DISCORD_TOKEN", ""))
        for secret in secrets:
            if secret:
                text = text.replace(secret, "[redacted]")
                if partial:
                    # Do not emit the first half of a credential split across chunks.
                    for size in range(min(len(text), len(secret) - 1), 0, -1):
                        if text.endswith(secret[:size]):
                            text = text[:-size]
                            break
        return text

    def _failure(self, record, exc):
        # Exception text from transports or providers may contain credentials.
        record["error_detail"] = type(exc).__name__
        if isinstance(exc, httpx.HTTPStatusError):
            record["error_code"] = "http_error"
            record["http_status"] = exc.response.status_code
        elif isinstance(exc, (TimeoutError, httpx.TimeoutException)):
            record["error_code"] = "timeout"
        elif isinstance(exc, httpx.RequestError):
            record["error_code"] = "connection"
        else:
            record["error_code"] = "invalid_response"
            if isinstance(exc, ModelError):
                record["error_detail"] = self.diagnostic(str(exc))

    async def complete(self, role: str, prompt: str, *, json_mode=False, max_tokens=None) -> str:
        record, started = self._begin(role), time.monotonic()
        spec, provider, url, body, headers = self._request(role, prompt, json_mode, False, max_tokens)
        capture("model_request", call_id=record.get("call_count", 1), role=role,
                provider=provider.api, request=body)
        content = ""
        log.debug("model request | trace=%s role=%s api=%s endpoint=%s timeout=%s "
                  "json=%s stream=false max_tokens=%s think=%s prompt_chars=%d",
                  record.get("trace_id", "-"), role, provider.api, self.diagnostic(url),
                  spec.timeout, json_mode, min(max_tokens or spec.max_tokens, spec.max_tokens), spec.think, len(prompt))
        try:
            async with asyncio.timeout(spec.timeout):
                async with httpx.AsyncClient(timeout=spec.timeout, transport=self.transport) as client:
                    response = await client.post(url, json=body, headers=headers)
                    record["http_status"] = response.status_code
                    response.raise_for_status()
                    data = response.json()
            content = (data["message"]["content"] if provider.api == "ollama"
                       else data["choices"][0]["message"]["content"])
            if not isinstance(content, str):
                raise ValueError()
            record["usage"] = data.get("usage") if provider.api == "openai" else {
                "input_tokens": data.get("prompt_eval_count"), "output_tokens": data.get("eval_count")}
            content = self._redact(content).strip()
            reason = (data.get("done_reason") if provider.api == "ollama"
                      else data["choices"][0].get("finish_reason"))
            record["finish_reason"] = self._redact(str(reason or ""))[:80]
            record["output_chars"] = len(content)
            thinking = data.get("message", {}).get("thinking", "") if provider.api == "ollama" else ""
            record["thinking_chars"] = len(thinking) if isinstance(thinking, str) else 0
            if json_mode:
                json_object(content)
            record["status"] = "partial" if reason in ("length", "content_filter") else "ok"
            return content
        except asyncio.CancelledError:
            record["status"] = "cancelled"
            raise
        except Exception as exc:
            record["status"] = "unavailable"
            self._failure(record, exc)
            # Never expose response bodies, request headers, or provider errors.
            raise ModelError(f"Model request failed for role {role}") from None
        finally:
            self._finish(record, started)
            capture("model_response", call_id=record.get("call_count", 1), role=role,
                    content=content, metadata=record)

    async def object(self, role, prompt):
        return json_object(await self.complete(role, prompt, json_mode=True))

    async def stream(self, role: str, prompt: str, on_delta=None, *, max_tokens=None) -> str:
        record, started = self._begin(role), time.monotonic()
        spec, provider, url, body, headers = self._request(role, prompt, False, True, max_tokens)
        capture("model_request", call_id=record.get("call_count", 1), role=role,
                provider=provider.api, request=body)
        log.debug("model request | trace=%s role=%s api=%s endpoint=%s timeout=%s connect_timeout=%s "
                  "read_timeout=%s stream=true "
                  "max_tokens=%s think=%s prompt_chars=%d",
                  record.get("trace_id", "-"), role, provider.api, self.diagnostic(url),
                  spec.timeout, min(5, spec.timeout), min(15, spec.timeout),
                  min(max_tokens or spec.max_tokens, spec.max_tokens), spec.think, len(prompt))
        text, done, truncated = "", False, False
        try:
            async with asyncio.timeout(spec.timeout):
                timeout = httpx.Timeout(spec.timeout, connect=min(5, spec.timeout), read=min(15, spec.timeout))
                async with httpx.AsyncClient(timeout=timeout, transport=self.transport) as client:
                    async with client.stream("POST", url, json=body, headers=headers) as response:
                        record["http_status"] = response.status_code
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
                            if "usage" in chunk:
                                record["usage"] = chunk["usage"]
                            elif "eval_count" in chunk:
                                record["usage"] = {"input_tokens": chunk.get("prompt_eval_count"),
                                                   "output_tokens": chunk.get("eval_count")}
                            if "error" in chunk:
                                raise ValueError()
                            if provider.api == "ollama":
                                delta = chunk.get("message", {}).get("content", "")
                                done = chunk.get("done") is True
                                truncated = chunk.get("done_reason") == "length"
                                if chunk.get("done_reason"):
                                    record["finish_reason"] = self._redact(str(chunk["done_reason"]))[:80]
                            else:
                                choices = chunk.get("choices", [])
                                if not choices:  # optional usage-only SSE chunk
                                    continue
                                delta = choices[0].get("delta", {}).get("content") or ""
                                reason = choices[0].get("finish_reason")
                                if reason:
                                    record["finish_reason"] = self._redact(str(reason))[:80]
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
        except Exception as exc:
            record["status"] = "partial" if text else "unavailable"
            self._failure(record, exc)
        finally:
            record["output_chars"] = len(text)
            self._finish(record, started)
            capture("model_response", call_id=record.get("call_count", 1), role=role,
                    content=text, metadata=record)
        return self._redact(text, partial=not done or truncated)
