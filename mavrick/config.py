#!/usr/bin/env python3
"""
MAVRICK System Configuration & Paths
Manages directories, secure file permissions (0700/0600), and user preferences.
"""

import os
import json
import logging
import math
import tempfile
from dataclasses import dataclass, field, fields
from typing import Dict, Any, List, Optional
from urllib.parse import urlsplit

from .platform import get_platform

# Core Directory & File Paths
HOME_DIR = os.path.expanduser("~")
MAVRICK_STATE_DIR = str(get_platform().data_dir())
MAVRICK_LOG_DIR = str(get_platform().log_dir())
CONFIG_FILE = str(get_platform().config_dir() / "config.json")
WORKFLOWS_FILE = str(get_platform().config_dir() / "workflows.json")
MCP_SERVERS_FILE = str(get_platform().config_dir() / "mcp_servers.json")
MCP_GRANTS_FILE = str(get_platform().config_dir() / "mcp_grants.json")
HISTORY_FILE = os.path.join(MAVRICK_STATE_DIR, "history.json")
AUDIT_LOG_FILE = os.path.join(MAVRICK_LOG_DIR, "audit.log")
CONTEXT_FILE = os.path.join(MAVRICK_STATE_DIR, "context.json")
UNDO_STATE_FILE = os.path.join(MAVRICK_STATE_DIR, "undo_state.json")

# Runtime IPC Sockets
XDG_RUNTIME_DIR = str(get_platform().runtime_dir())
MAVRICK_SOCK = os.path.join(XDG_RUNTIME_DIR, "mavrick.sock")
DAEMON_SOCKET = os.path.join(XDG_RUNTIME_DIR, "mavrickd.sock")
FALLBACK_DAEMON_SOCKET = os.path.join(MAVRICK_STATE_DIR, "mavrickd.sock")

# Antigravity CLI Binary
AGY_BIN = str(get_platform().agy_binary())

AI_TRANSPORT_HEADROOM = 2.0
DEFAULT_AGY_MODEL = "gemini-3.8-flash-low"
PROVIDER_LABELS = {
    "agy": "agy",
    "ollama": "Ollama",
    "openai_compat": "OpenAI-Compatible",
    "groq": "Groq",
    "cerebras": "Cerebras",
    "openrouter": "OpenRouter",
}
PROVIDER_PRESETS = {
    "agy": (DEFAULT_AGY_MODEL, ""),
    "ollama": ("llama3.2", "http://127.0.0.1:11434"),
    "openai_compat": ("gpt-4o-mini", "https://api.openai.com/v1"),
    "groq": ("openai/gpt-oss-20b", "https://api.groq.com/openai/v1"),
    "cerebras": ("gpt-oss-120b", "https://api.cerebras.ai/v1"),
    "openrouter": ("openai/gpt-4o-mini", "https://openrouter.ai/api/v1"),
}
HOST_API_KEY_ENV = {
    "api.groq.com": "GROQ_API_KEY",
    "api.cerebras.ai": "CEREBRAS_API_KEY",
    "openrouter.ai": "OPENROUTER_API_KEY",
    "api.openai.com": "OPENAI_API_KEY",
}
API_KEY_ENV = ("MAVRICK_API_KEY", "GROQ_API_KEY", "CEREBRAS_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY")
THEME_NAMES = ("light", "dark")


@dataclass(frozen=True)
class ProviderSettings:
    name: str = "agy"
    model: str = DEFAULT_AGY_MODEL
    base_url: str = ""
    api_key: str = field(default="", repr=False)
    require_model: bool = field(default=False, repr=False, compare=False)


def _normalize_provider_name(raw: object) -> str:
    if not isinstance(raw, str):
        raise ValueError("Invalid AI provider. Use agy, groq, cerebras, openrouter, ollama, or openai_compat.")
    name = raw.strip().lower().replace("-", "_")
    if name == "openai_compatible":
        name = "openai_compat"
    if name not in PROVIDER_LABELS:
        raise ValueError("Invalid AI provider. Use agy, groq, cerebras, openrouter, ollama, or openai_compat.")
    return name


def load_provider_settings(
    *, provider: str | None = None, model: str | None = None, base_url: str | None = None
) -> ProviderSettings:
    settings = load_user_config().get("ai_provider", {})
    if not isinstance(settings, dict):
        raise ValueError("ai_provider must be a config object.")
    name = _normalize_provider_name(provider or os.environ.get("MAVRICK_PROVIDER") or settings.get("name", "agy"))
    cfg_raw = settings.get("name")
    try:
        cfg_name = _normalize_provider_name(cfg_raw if cfg_raw is not None else "agy")
    except ValueError:
        cfg_name = ""
    same_provider = (name == cfg_name)
    default_model, default_url = PROVIDER_PRESETS[name]
    chosen_model = (
        model or os.environ.get("MAVRICK_MODEL")
        or (settings.get("model") if same_provider else None)
        or default_model
    )
    chosen_url = (
        base_url or os.environ.get("MAVRICK_BASE_URL")
        or (settings.get("base_url") if same_provider else None)
        or default_url
    )
    cfg_key = settings.get("api_key", "") if (same_provider or cfg_raw is None) else ""
    key = (os.environ.get(API_KEY_ENV[0]) or cfg_key) if name != "agy" else ""
    if not isinstance(chosen_model, str) or not chosen_model.strip():
        raise ValueError("AI provider model must be nonempty text.")
    if not isinstance(chosen_url, str):
        raise ValueError("AI provider base URL must be text.")
    if not key and name in ("openai_compat", "groq", "cerebras", "openrouter"):
        try:
            destination = urlsplit(chosen_url.strip())
            hostname = destination.hostname if destination.scheme == "https" and destination.port in (None, 443) else None
        except ValueError:
            raise ValueError("AI provider base URL is invalid.") from None
        # Service-specific credentials only go to the service they name.
        if hostname in HOST_API_KEY_ENV:
            key = os.environ.get(HOST_API_KEY_ENV[hostname], "")
    if not isinstance(key, str) or any(not 33 <= ord(char) <= 126 for char in key):
        raise ValueError("AI provider API key contains invalid characters.")
    explicit_model = bool(model or os.environ.get("MAVRICK_MODEL") or (same_provider and settings.get("model")))
    return ProviderSettings(name, chosen_model.strip(), chosen_url.strip(), key, explicit_model)


def load_theme_setting(theme: str | None = None) -> str:
    """Resolve HUD theme in explicit override, environment, user config, then light default order."""
    raw = theme if theme is not None else (os.environ.get("MAVRICK_THEME") or load_user_config().get("theme", "light"))
    if not isinstance(raw, str):
        raise ValueError("Invalid theme. Use 'light' or 'dark'.")
    chosen = raw.strip().lower()
    if chosen not in THEME_NAMES:
        raise ValueError("Invalid theme. Use 'light' or 'dark'.")
    return chosen


def _mcp_env_secrets() -> list[str]:
    try:
        with open(MCP_SERVERS_FILE, "rb") as handle:
            raw = handle.read(128 * 1024 + 1)
        if len(raw) > 128 * 1024:
            return []
        data = json.loads(raw)
    except (OSError, ValueError, UnicodeError, RecursionError):
        return []
    raw_servers = data.get("servers", data.get("mcpServers")) if isinstance(data, dict) else None
    items = raw_servers.values() if isinstance(raw_servers, dict) else raw_servers if isinstance(raw_servers, list) else ()
    collected: list[str] = []
    secret_markers = ("token", "secret", "key", "pass", "auth", "cred", "pwd")
    for entry in items:
        env = entry.get("env") if isinstance(entry, dict) else None
        if not isinstance(env, dict):
            continue
        for k, v in env.items():
            if not isinstance(v, str) or not v:
                continue
            key_low = k.lower() if isinstance(k, str) else ""
            if any(m in key_low for m in secret_markers) and len(v) >= 4:
                collected.append(v)
            elif len(v) >= 8 and not v.startswith(("/", "~")) and " " not in v and "UTF-8" not in v:
                collected.append(v)
    return collected


def configured_api_keys() -> tuple[str, ...]:
    settings = load_user_config().get("ai_provider", {})
    keys = [os.environ.get(name) for name in API_KEY_ENV]
    if isinstance(settings, dict):
        keys.append(settings.get("api_key"))
    keys.extend(_mcp_env_secrets())
    return tuple(sorted({key for key in keys if isinstance(key, str) and key}, key=len, reverse=True))


def validate_timeout(value: object, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be finite positive seconds.")
    try:
        seconds = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be finite positive seconds.") from error
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError(f"{name} must be finite positive seconds.")
    return seconds


@dataclass(frozen=True)
class AITimeouts:
    init_timeout: float = 10.0
    query_timeout: float = 35.0
    fallback_timeout: float = 20.0

    def __post_init__(self):
        for field in fields(self):
            object.__setattr__(self, field.name, validate_timeout(getattr(self, field.name), field.name))

    @property
    def client_timeout(self) -> float:
        return self.query_timeout + AI_TRANSPORT_HEADROOM


def load_ai_timeouts(**overrides: float | None) -> AITimeouts:
    """Resolve timeout values in CLI, environment, user config, then default order."""
    configured = load_user_config().get("ai_timeouts", {})
    if not isinstance(configured, dict):
        raise ValueError("ai_timeouts must be a config object.")
    values = {}
    for field in fields(AITimeouts):
        name = field.name
        override = overrides.get(name)
        values[name] = (override if override is not None else
                        os.environ.get(f"MAVRICK_{name.upper()}", configured.get(name, field.default)))
    return AITimeouts(**values)


# Default User Preferences
DEFAULT_CONFIG = {
    "version": "2.0.0",
    "theme": "light",               # HUD color theme: 'light' or 'dark'
    "trusted_actions": [],          # Reversible actions user has explicitly trusted
    "approved_apps": {},            # Explicit Windows/user launcher path allowlist ({name: abs_path})
    "default_browser": "auto",       # 'auto' or specific executable
    "show_audit_in_gui": True,       # Display execution audit trace
    "max_history_items": 200,        # Max history entries retained
    "auto_dismiss_delay_sec": 3.0,   # Seconds before auto-closing on verified completion
    "custom_commands": {            # User-defined phrases mapped to structured actions
        "gaming mode": {"action": "set_power_profile", "params": {"profile": "performance"}, "summary": "Switch to performance power profile for gaming"},
        "battery saver": {"action": "set_power_profile", "params": {"profile": "battery"}, "summary": "Switch to battery saver profile"},
        "mute all": {"action": "set_volume", "params": {"mute": True}, "summary": "Mute system audio"},
        "unmute": {"action": "set_volume", "params": {"mute": False}, "summary": "Unmute system audio"},
        "clean junk": {"action": "clean_cache", "params": {"dry_run": True}, "summary": "Calculate reclaimable cache and junk"},
        "free storage": {"action": "inspect_storage", "params": {"category": "apps", "limit": 10}, "summary": "Inspect application storage usage"},
        "pause": {"action": "control_media", "params": {"action": "pause"}, "summary": "Pause media playback"}
    }
}

def ensure_secure_storage() -> None:
    """Create native state roots and restrict permissions where the OS supports it."""
    adapter = get_platform()
    for directory in {MAVRICK_STATE_DIR, os.path.dirname(CONFIG_FILE), os.path.dirname(AUDIT_LOG_FILE)}:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        adapter.restrict_permissions(directory, directory=True)
    for path in (CONFIG_FILE, WORKFLOWS_FILE, MCP_SERVERS_FILE, MCP_GRANTS_FILE, HISTORY_FILE, AUDIT_LOG_FILE, CONTEXT_FILE, UNDO_STATE_FILE):
        try:
            adapter.restrict_permissions(path)
        except FileNotFoundError:
            pass


@dataclass(frozen=True)
class IPCSettings:
    transport: str
    descriptor: str


def load_ipc_settings(*, transport=None, descriptor=None) -> IPCSettings:
    raw = load_user_config().get("ipc", {})
    if not isinstance(raw, dict):
        raise ValueError("ipc must be a config object.")
    transport = transport or os.environ.get("MAVRICK_IPC_TRANSPORT") or raw.get("transport", get_platform().capabilities.ipc_transport)
    if transport not in ("unix-socket", "tcp-token"):
        raise ValueError("Select unix-socket or tcp-token IPC.")
    if get_platform().name == "windows" and transport == "unix-socket":
        raise get_platform().unsupported("Unix-socket Mavrick IPC")
    runtime = XDG_RUNTIME_DIR if get_platform().name == "linux" else str(get_platform().runtime_dir())
    if get_platform().name == "linux" and not (os.path.isdir(runtime) and os.access(runtime, os.W_OK)):
        runtime = os.path.join(MAVRICK_STATE_DIR, "runtime")
    descriptor = (descriptor or os.environ.get("MAVRICK_IPC_DESCRIPTOR") or raw.get("descriptor") or
                  os.path.join(runtime, "mavrick", "daemon.json"))
    if not isinstance(descriptor, (str, os.PathLike)) or not os.path.isabs(descriptor):
        raise ValueError("The IPC descriptor must be an absolute path.")
    return IPCSettings(transport, os.fspath(descriptor))


def load_user_config() -> Dict[str, Any]:
    """Loads user configuration or returns defaults."""
    ensure_secure_storage()
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as fp:
                loaded = json.load(fp)
                config = dict(DEFAULT_CONFIG)
                config.update(loaded)
                return config
        except (OSError, ValueError, TypeError):
            logging.warning("Could not read Mavrick config. Using defaults.")
    return dict(DEFAULT_CONFIG)

def save_user_config(config: Dict[str, Any]) -> None:
    """Saves user configuration with secure 0600 permissions."""
    ensure_secure_storage()
    fd, temp_file = tempfile.mkstemp(prefix=".config-", dir=os.path.dirname(CONFIG_FILE))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            json.dump(config, fp, indent=2)
        os.replace(temp_file, CONFIG_FILE)
        get_platform().restrict_permissions(CONFIG_FILE)
    finally:
        if os.path.exists(temp_file):
            os.unlink(temp_file)


def configure_user_preferences(
    *,
    provider: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    theme: str | None = None,
    api_key: str | None = None,
) -> Dict[str, Any]:
    """Validate and atomically save AI provider and HUD theme preferences."""
    current = load_user_config()
    old_ai = current.get("ai_provider", {}) if isinstance(current.get("ai_provider"), dict) else {}
    chosen_theme = load_theme_setting(theme if theme is not None else current.get("theme", "light"))
    chosen_provider = _normalize_provider_name(provider if provider is not None else old_ai.get("name", "agy"))
    default_model, default_url = PROVIDER_PRESETS[chosen_provider]
    same_provider = (chosen_provider == old_ai.get("name"))
    chosen_model = (
        model.strip()
        if isinstance(model, str) and model.strip()
        else (old_ai.get("model") if same_provider and old_ai.get("model") else default_model)
    )
    chosen_url = (
        base_url.strip()
        if isinstance(base_url, str) and base_url.strip()
        else (old_ai.get("base_url") if same_provider and old_ai.get("base_url") else default_url)
    )
    if not isinstance(chosen_model, str) or not chosen_model.strip():
        raise ValueError("AI provider model must be nonempty text.")
    if chosen_url and chosen_provider != "agy":
        dest = urlsplit(chosen_url)
        if dest.scheme not in ("http", "https") or not dest.netloc:
            raise ValueError("AI provider base URL is invalid.")

    ai_cfg: Dict[str, Any] = {"name": chosen_provider, "model": chosen_model.strip()}
    if chosen_url:
        ai_cfg["base_url"] = chosen_url
    if api_key is not None:
        cleaned_key = api_key.strip()
        if any(not 33 <= ord(ch) <= 126 for ch in cleaned_key):
            raise ValueError("AI provider API key contains invalid characters.")
        if cleaned_key:
            ai_cfg["api_key"] = cleaned_key
    elif same_provider and isinstance(old_ai.get("api_key"), str) and old_ai["api_key"]:
        ai_cfg["api_key"] = old_ai["api_key"]

    updated = dict(current)
    updated["theme"] = chosen_theme
    updated["ai_provider"] = ai_cfg
    save_user_config(updated)
    return updated

def get_custom_command(query: str) -> Optional[Dict[str, Any]]:
    """
    Checks if a user query matches a configured custom command (case-insensitive, normalized).
    Returns the action definition or None.
    """
    cfg = load_user_config()
    customs = cfg.get("custom_commands", {})
    clean_q = query.strip().lower()

    # 1. Exact match
    for trigger, act_def in customs.items():
        if clean_q == trigger.strip().lower():
            return act_def

    # 2. Normalized prefix/suffix match (e.g. "turn on gaming mode" or "activate battery saver")
    for trigger, act_def in customs.items():
        trig = trigger.strip().lower()
        if trig in clean_q and len(trig) >= 3:
            fillers = ["please", "can you", "mavrick", "turn on", "enable", "switch to", "activate", "start", "set", "run"]
            remainder = clean_q
            for f in fillers:
                remainder = remainder.replace(f, "").strip()
            if remainder == trig:
                return act_def

    return None

def set_custom_command(trigger: str, action: str, params: Dict[str, Any], summary: str = "") -> bool:
    """Saves or updates a custom command trigger in user configuration."""
    cfg = load_user_config()
    if "custom_commands" not in cfg:
        cfg["custom_commands"] = {}
    cfg["custom_commands"][trigger.strip().lower()] = {
        "action": action,
        "params": params,
        "summary": summary or f"Execute {action}"
    }
    save_user_config(cfg)
    return True

def delete_custom_command(trigger: str) -> bool:
    """Removes a custom command from user configuration."""
    cfg = load_user_config()
    customs = cfg.get("custom_commands", {})
    key = trigger.strip().lower()
    if key in customs:
        del customs[key]
        cfg["custom_commands"] = customs
        save_user_config(cfg)
        return True
    return False

def list_custom_commands() -> Dict[str, Any]:
    """Returns a dictionary of all active custom commands."""
    cfg = load_user_config()
    return cfg.get("custom_commands", {})

def is_wayland_session() -> bool:
    return get_platform().is_wayland_session()


def is_cosmic_desktop() -> bool:
    return get_platform().is_cosmic_desktop()
