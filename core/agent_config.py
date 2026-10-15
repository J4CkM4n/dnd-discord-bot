"""Startup-only model configuration. Credentials are resolved from environment."""

import math
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

ROLES = ("main", "action", "progress", "director", "evaluator", "narrator", "router")


class ConfigError(ValueError):
    """Messages describe fields, never secret values."""


@dataclass(frozen=True)
class ProviderConfig:
    api: str
    base_url: str
    api_key_env: str = ""
    api_key: str = field(default="", repr=False)
    json_mode: bool = True


@dataclass(frozen=True)
class RoleConfig:
    provider: str
    model: str
    temperature: float
    max_tokens: int
    timeout: float
    think: bool | None = None
    reasoning_effort: str | None = None


@dataclass(frozen=True)
class RoutingConfig:
    enabled: bool = False  # old configurations retain their original workflow
    backend: str = "auto"
    model: str = "jev-latest"
    base_url: str = "https://api.typesafe.ai/v1"
    api_key_env: str = "TYPESAFE_API_KEY"
    api_key: str = field(default="", repr=False)
    timeout: float = 5
    probability: float = .85
    confidence: float = .8
    creative_max_calls: int = 8


@dataclass(frozen=True)
class HintConfig:
    stalled_attempts: int = 3
    cooldown: int = 3


@dataclass(frozen=True)
class AgentConfig:
    providers: dict[str, ProviderConfig]
    roles: dict[str, RoleConfig]
    enabled: bool = True
    max_calls: int = 6
    progress_interval: int = 5
    director_evaluate: bool = False
    routing: RoutingConfig = field(default_factory=RoutingConfig)
    hints: HintConfig = field(default_factory=HintConfig)


def _boolean(value, name):
    if type(value) is not bool:
        raise ConfigError(f"{name} must be a boolean")
    return value


def _number(value, name, minimum, maximum, integer=False):
    if type(value) not in ((int,) if integer else (int, float)) \
            or not math.isfinite(value) or not minimum <= value <= maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}")
    return value


def _fields(value, allowed, name):
    if type(value) is not dict or set(value) - set(allowed):
        raise ConfigError(f"Invalid fields in {name}")


def _env_bool(env, name, default):
    value = env.get(name, "").strip().lower()
    if not value:
        return default
    if value not in ("on", "true", "1", "off", "false", "0"):
        raise ConfigError(f"{name} must be on or off")
    return value in ("on", "true", "1")


def parse_config(data: dict, env=None) -> AgentConfig:
    env = os.environ if env is None else env
    _fields(data, ("version", "providers", "roles", "supervision", "routing", "hints"), "configuration")
    if type(data.get("version", 1)) is not int or data.get("version", 1) != 1:
        raise ConfigError("Unsupported agent configuration version")
    raw_providers = data.get("providers", {})
    if type(raw_providers) is not dict:
        raise ConfigError("providers must be a table")
    providers = {}
    for name, raw in {"local": {}, **raw_providers}.items():
        _fields(raw, ("api", "base_url", "api_key_env", "json_mode"), "provider")
        api = raw.get("api", "ollama")
        if api not in ("ollama", "openai"):
            raise ConfigError("Provider api must be ollama or openai")
        url = raw.get("base_url", env.get("LLM_URL", "http://127.0.0.1:11434") if name == "local" else "")
        if not isinstance(url, str):
            raise ConfigError("Provider base_url must be a URL")
        try:
            parts = urlsplit(url)
            valid = (parts.scheme in ("http", "https") and parts.hostname and not parts.username
                     and not parts.password and not parts.query and not parts.fragment)
            _ = parts.port
        except ValueError:
            valid = False
        if not valid:
            raise ConfigError("Provider base_url must be an HTTP URL without credentials or query parameters")
        key_env = raw.get("api_key_env", "")
        if not isinstance(key_env, str) or (key_env and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key_env)):
            raise ConfigError("api_key_env must name an environment variable")
        key = env.get(key_env, "") if key_env else ""
        if key_env and (not key.strip() or "\n" in key or "\r" in key):
            raise ConfigError(f"Missing or invalid credential environment variable: {key_env}")
        providers[name] = ProviderConfig(api, url.rstrip("/"), key_env, key,
                                        _boolean(raw.get("json_mode", True), "json_mode"))
    raw_roles = data.get("roles", {})
    _fields(raw_roles, ROLES, "roles")
    roles = {}
    for name in ROLES:
        raw = raw_roles.get(name, {})
        _fields(raw, ("provider", "model", "temperature", "max_tokens", "timeout", "think",
                      "reasoning_effort"), "role")
        provider = raw.get("provider", "local")
        if not isinstance(provider, str) or provider not in providers:
            raise ConfigError(f"Unknown provider for role {name}")
        narrator = name == "narrator"
        model = raw.get("model", env.get("V4_NARR_MODEL" if narrator else "V4_DIGEST_MODEL",
                                        "qwen3.5:35b" if narrator else "qwen2.5:7b-instruct"))
        if not isinstance(model, str) or not model.strip() or len(model) > 200:
            raise ConfigError(f"Invalid model for role {name}")
        api = providers[provider].api
        if "think" in raw and api != "ollama":
            raise ConfigError(f"Role {name}: think is only supported by Ollama")
        if "reasoning_effort" in raw and api != "openai":
            raise ConfigError(f"Role {name}: reasoning_effort requires an OpenAI-compatible provider")
        think = _boolean(raw["think"], "think") if "think" in raw else (
            _env_bool(env, "V4_NARR_THINK" if narrator else "V4_DIGEST_THINK", False) if api == "ollama" else None)
        effort = raw.get("reasoning_effort")
        if effort is not None and effort not in ("none", "minimal", "low", "medium", "high"):
            raise ConfigError(f"Invalid reasoning_effort for role {name}")
        roles[name] = RoleConfig(
            provider, model.strip(),
            _number(raw.get("temperature", 0.7 if narrator else 0.8 if name == "director" else 0.1), "temperature", 0, 2),
            _number(raw.get("max_tokens", 1600 if name == "router" else 800 if name == "action" else 600 if name in ("main", "progress", "director") else 400 if narrator else 200),
                    "max_tokens", 16, 32768, True),
            _number(raw.get("timeout", 180 if narrator else 30), "timeout", 1, 300), think, effort)
    sup = data.get("supervision", {})
    _fields(sup, ("enabled", "max_calls", "progress_interval", "director_evaluate"), "supervision")
    route = data.get("routing", {})
    _fields(route, ("enabled", "backend", "model", "base_url", "api_key_env", "timeout",
                    "probability", "confidence", "creative_max_calls"), "routing")
    backend = route.get("backend", "auto")
    if backend not in ("auto", "jev", "llm"):
        raise ConfigError("routing.backend must be auto, jev, or llm")
    url = route.get("base_url", "https://api.typesafe.ai/v1")
    try:
        parts = urlsplit(url)
        valid = (parts.scheme in ("http", "https") and parts.hostname and not parts.username
                 and not parts.password and not parts.query and not parts.fragment)
        _ = parts.port
    except (ValueError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise ConfigError("Invalid routing.base_url")
    model, key_env = route.get("model", "jev-latest"), route.get("api_key_env", "TYPESAFE_API_KEY")
    if not isinstance(model, str) or not 1 <= len(model.strip()) <= 200:
        raise ConfigError("Invalid routing.model")
    if not isinstance(key_env, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key_env):
        raise ConfigError("Invalid routing.api_key_env")
    key = env.get(key_env, "")
    if "\n" in key or "\r" in key:
        raise ConfigError("Invalid routing credential")
    routing = RoutingConfig(
        _boolean(route.get("enabled", False), "routing.enabled"), backend, model.strip(),
        url.rstrip("/"), key_env, key,
        _number(route.get("timeout", 5), "routing.timeout", 1, 30),
        _number(route.get("probability", .85), "routing.probability", .5, 1),
        _number(route.get("confidence", .8), "routing.confidence", 0, 1),
        _number(route.get("creative_max_calls", 8), "routing.creative_max_calls", 6, 8, True))
    hint = data.get("hints", {})
    _fields(hint, ("stalled_attempts", "cooldown"), "hints")
    hints = HintConfig(_number(hint.get("stalled_attempts", 3), "hints.stalled_attempts", 1, 20, True),
                       _number(hint.get("cooldown", 3), "hints.cooldown", 1, 20, True))
    return AgentConfig(providers, roles,
                       _boolean(sup.get("enabled", True), "enabled"),
                       _number(sup.get("max_calls", 6), "max_calls", 4, 6, True),
                       _number(sup.get("progress_interval", 5), "progress_interval", 1, 100, True),
                       _boolean(sup["director_evaluate"] if "director_evaluate" in sup
                                else _env_bool(env, "V4_DIRECTOR_EVALUATE", False),
                                "director_evaluate"), routing, hints)


def load_agent_config(path: str | None = None, env=None) -> AgentConfig:
    env = os.environ if env is None else env
    selected = path or env.get("AGENT_CONFIG")
    target = Path(selected) if selected else Path(__file__).resolve().parents[1] / "agents.toml"
    try:
        with target.open("rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        if selected:
            raise ConfigError("Agent configuration file does not exist") from None
        data = {}
    except (OSError, tomllib.TOMLDecodeError):
        raise ConfigError("Cannot read agent configuration: invalid TOML or inaccessible file") from None
    return parse_config(data, env)


def legacy_config(url, digest_model="qwen2.5:7b-instruct", narr_model="qwen3.5:35b",
                  digest_think=None, narr_think=False, evaluate=False):
    """Compatibility for direct Python callers supplying the old constructor."""
    roles = {name: {"model": narr_model if name == "narrator" else digest_model}
             for name in ROLES}
    for name, role in roles.items():
        think = narr_think if name == "narrator" else digest_think
        if think is not None:
            role["think"] = think
    return parse_config({"providers": {"local": {"base_url": url}}, "roles": roles,
                         "supervision": {"enabled": False, "director_evaluate": evaluate}}, {})
