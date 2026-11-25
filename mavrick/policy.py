#!/usr/bin/env python3
"""
MAVRICK Security Policy & Least Privilege Enforcement
Enforces confirmation rules, path constraints, secret scrubbing, and allowlists.
"""

import hashlib
import json
import math
import os
import re
from enum import Enum
from typing import Any, Mapping, Tuple, List, Optional

from .platform import get_platform
from .schema import RiskLevel, ActionRequest, ActionType

# Allowlist of trusted application names / binaries
APPLICATION_ALLOWLIST = {
    "brave", "brave-browser", "firefox", "google-chrome", "chromium",
    "cosmic-term", "gnome-terminal", "alacritty", "kitty", "x-terminal-emulator",
    "code", "cursor", "subl",
    "cosmic-files", "nautilus", "dolphin", "thunar",
    "spotify", "discord", "obs", "vlc", "steam", "gimp",
    "cosmic-settings", "gnome-control-center", "cosmic-store",
    "agy", "antigravity", "htop", "btop", "nvtop", "top", "neofetch", "fastfetch",
    "baobab", "ncdu", "dust", "gnome-disks",
}

# CLI utilities that should launch inside an interactive terminal window
TERMINAL_CLI_APPS = {
    "agy", "htop", "btop", "nvtop", "top", "neofetch", "fastfetch",
    "ncdu", "dust",
    "vim", "nano", "ranger", "claude-code"
}

# Regex to scrub sensitive tokens, credentials, and keys from logs and history
SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|token|secret|password|passwd|auth)[\s:=]+['\"]?([a-zA-Z0-9_\-\.]{8,})['\"]?"),
    re.compile(r"(?i)\bsk-[a-z0-9_\-]{8,}"),          # OpenAI and OpenRouter keys
    re.compile(r"AIza[0-9A-Za-z\-_]{35}"),                  # Google API keys
    re.compile(r"(?i)\bbearer[ \t]+[a-zA-Z0-9_~+./=\-]+"),
    re.compile(r"gh[pous]_[a-zA-Z0-9]{36,}")               # GitHub tokens
]

def scrub_secrets(text: str, known: tuple[str, ...] = ()) -> str:
    """Masks secrets, tokens, and API keys from text before storage or logging."""
    if not text:
        return text
    sanitized = text
    for pat in SECRET_PATTERNS:
        def mask_match(m):
            full = m.group(0)
            if len(m.groups()) == 2:
                # Key-value match: keep key name, mask secret
                return f"{m.group(1)}: [REDACTED_SECRET]"
            return "[REDACTED_SECRET]"
        sanitized = pat.sub(mask_match, sanitized)
    for secret in known:
        sanitized = sanitized.replace(secret, "[REDACTED_SECRET]")
    return sanitized

def is_safe_user_path(raw_path: str, must_exist: bool = False) -> Tuple[bool, str, str]:
    """
    Validates that a path is strictly inside the user's home directory or /tmp,
    canonicalizing symlinks and preventing directory traversal (../).
    Returns (is_safe, error_message, canonical_path).
    """
    try:
        # Expand user ~
        expanded = os.path.expanduser(raw_path)
        canonical = os.path.realpath(expanded)
        adapter = get_platform()
        comparable = os.path.normcase(canonical)
        if any(comparable == os.path.normcase(root) or comparable.startswith(os.path.normcase(root) + os.sep)
               for root in adapter.blocked_user_roots()):
            return False, f"Access to system directory '{canonical}' is strictly prohibited.", canonical
        roots = adapter.user_path_roots()
        if not any((comparable == os.path.normcase(root) and (root == roots[0] or adapter.name != "linux"))
                   or comparable.startswith(os.path.normcase(root) + os.sep) for root in roots):
            return False, f"Path '{canonical}' is outside permitted user directories.", canonical

        if must_exist and not os.path.exists(canonical):
            return False, f"Path '{canonical}' does not exist.", canonical

        return True, "", canonical
    except Exception as e:
        return False, f"Path resolution error: {e}", raw_path

def requires_confirmation(action: ActionRequest, user_trusted_actions: Optional[List[str]] = None) -> bool:
    """
    Determines whether an action requires explicit user confirmation before running.
    - READ_ONLY actions: never require confirmation.
    - REVERSIBLE actions: require confirmation unless explicitly listed in user_trusted_actions.
    - PRIVILEGED / DESTRUCTIVE actions: ALWAYS require explicit confirmation (cannot be bypassed).
    """
    if action.risk_level == RiskLevel.READ_ONLY:
        return False

    if action.risk_level in [RiskLevel.PRIVILEGED, RiskLevel.DESTRUCTIVE]:
        # High-risk actions cannot be bypassed by config
        return True

    if action.risk_level == RiskLevel.REVERSIBLE:
        if action.action_type == ActionType.MCP_TOOL:
            return True
        trusted = user_trusted_actions or []
        if action.action_type.value in trusted:
            return False
        return True

    return True


class WorkflowRisk(str, Enum):
    READ_ONLY = "read_only"
    MUTATING = "mutating"
    PRIVILEGED = "privileged"
    DESTRUCTIVE = "destructive"


_WORKFLOW_RANK = {risk: index for index, risk in enumerate(WorkflowRisk)}
_WORKFLOW_LAUNCHES = {ActionType.OPEN_APP, ActionType.OPEN_APPS, ActionType.OPEN_URL, ActionType.RUN_IN_TERMINAL,
                      ActionType.ARRANGE_WINDOWS}
_LINUX_TOOLS = {ActionType.INSPECT_PROCESSES, ActionType.INSPECT_STORAGE, ActionType.SET_VOLUME,
                ActionType.GET_POWER_PROFILE, ActionType.SET_POWER_PROFILE, ActionType.CONTROL_MEDIA,
                ActionType.GET_NETWORK_STATUS, ActionType.LOCK_SESSION}


def workflow_risk(action: ActionRequest) -> WorkflowRisk:
    """Score a schema-validated step independently of trust settings."""
    if action.action_type == ActionType.UNINSTALL_PACKAGE or action.risk_level == RiskLevel.DESTRUCTIVE:
        return WorkflowRisk.DESTRUCTIVE
    if action.risk_level == RiskLevel.PRIVILEGED:
        return WorkflowRisk.PRIVILEGED
    if action.risk_level == RiskLevel.REVERSIBLE or action.action_type in _WORKFLOW_LAUNCHES:
        return WorkflowRisk.MUTATING
    return WorkflowRisk.READ_ONLY


def aggregate_workflow_risk(risks) -> WorkflowRisk:
    return max(risks, key=_WORKFLOW_RANK.__getitem__, default=WorkflowRisk.READ_ONLY)


def enforce_workflow_step(action: ActionRequest, adapter=None) -> None:
    adapter = adapter or get_platform()
    kind = action.action_type
    if kind == ActionType.UNDO:
        raise ValueError("Review Undo as a separate request from its stored batch.")
    if kind in (ActionType.MOVE_FILES, ActionType.ORGANIZE_DOWNLOADS) and not adapter.capabilities.atomic_moves:
        raise adapter.unsupported("Verified file moves")
    if kind in _LINUX_TOOLS and adapter.name != "linux":
        raise adapter.unsupported(kind.value.replace("_", " ").capitalize())
    if kind == ActionType.CLEAN_CACHE and not action.params["dry_run"] and not adapter.capabilities.cache_cleanup:
        raise adapter.unsupported("Verified cache cleanup")
    if kind == ActionType.RUN_IN_TERMINAL or (kind == ActionType.OPEN_APP and
            (action.params.get("in_terminal") or action.params["app_name"].casefold() in TERMINAL_CLI_APPS)):
        if not adapter.terminal_path():
            raise adapter.unsupported("An approved interactive terminal")


def enforce_mcp_access(server_cfg, tool_name: str, *, grant=None) -> None:
    """Reject disabled, missing, unapproved, blocked, or unlisted MCP tools before spawning a server."""
    if server_cfg is None or not server_cfg.enabled:
        raise ValueError("MCP server is not configured or is disabled.")
    if grant is not None:
        from .mcp.registry import is_grant_valid
        ok, reason = is_grant_valid(server_cfg, grant)
        if not ok:
            raise ValueError(f"MCP server '{server_cfg.name}' is not approved ({reason}).")
    if tool_name in server_cfg.blocked_tools:
        raise ValueError(f"MCP tool '{server_cfg.name}/{tool_name}' is blocked by policy.")
    if server_cfg.allowed_tools is not None and tool_name not in server_cfg.allowed_tools:
        raise ValueError(f"MCP tool '{server_cfg.name}/{tool_name}' is not in the server's allowed_tools list.")


def evaluate_mcp_tool_risk(server_cfg, tool_spec, *, grant=None) -> tuple[RiskLevel, WorkflowRisk]:
    """Derive mechanical confirmation risk from server trust config, per-tool grants, and untrusted hints."""
    enforce_mcp_access(server_cfg, tool_spec.name, grant=grant)
    if server_cfg.destructive or tool_spec.name in server_cfg.destructive_tools or tool_spec.destructive_hint:
        return RiskLevel.DESTRUCTIVE, WorkflowRisk.DESTRUCTIVE
    approved_ro = getattr(grant, "approved_readonly_tools", frozenset()) if grant is not None else frozenset()
    if (
        server_cfg.trusted
        and tool_spec.read_only_hint
        and not tool_spec.destructive_hint
        and tool_spec.name in approved_ro
    ):
        return RiskLevel.READ_ONLY, WorkflowRisk.READ_ONLY
    return RiskLevel.REVERSIBLE, WorkflowRisk.MUTATING


def _schema_to_plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _schema_to_plain(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple)):
        return [_schema_to_plain(item) for item in value]
    return value


def mcp_policy_fingerprint(server_cfg, grant, tool_spec, risk_level: RiskLevel) -> str:
    """Compute a canonical SHA-256 fingerprint binding launch config, grant, tool schema, and risk."""
    payload = {
        "server": server_cfg.name,
        "tool": tool_spec.name,
        "risk": risk_level.value,
        "launch_hash": grant.launch_hash if grant is not None else "",
        "granted_env": sorted(grant.granted_env) if grant is not None else [],
        "approved_readonly_tools": sorted(grant.approved_readonly_tools) if grant is not None else [],
        "server_policy": {
            "trusted": bool(server_cfg.trusted),
            "destructive": bool(server_cfg.destructive),
            "allowed_tools": sorted(server_cfg.allowed_tools) if server_cfg.allowed_tools is not None else None,
            "blocked_tools": sorted(server_cfg.blocked_tools),
            "destructive_tools": sorted(server_cfg.destructive_tools),
        },
        "tool_policy": {
            "read_only_hint": bool(tool_spec.read_only_hint),
            "destructive_hint": bool(tool_spec.destructive_hint),
            "input_schema": _schema_to_plain(tool_spec.input_schema),
        },
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


_SUPPORTED_OBJECT_KEYWORDS = frozenset({
    "type", "properties", "required", "additionalProperties", "title", "description", "$schema", "default",
})
_SUPPORTED_VALUE_KEYWORDS = frozenset({
    "type", "enum", "minimum", "maximum", "minLength", "maxLength", "items",
    "properties", "required", "additionalProperties", "title", "description", "default",
})
_JSON_TYPES = frozenset({"string", "number", "integer", "boolean", "array", "object", "null"})


def _matches_json_type(value: Any, expected_type: Any) -> bool:
    if isinstance(expected_type, list):
        return any(_matches_json_type(value, item) for item in expected_type)
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "boolean":
        return type(value) is bool
    if expected_type == "integer":
        return type(value) is int
    if expected_type == "number":
        return type(value) in (int, float) and math.isfinite(value)
    if expected_type == "array":
        return isinstance(value, list)
    if expected_type == "object":
        return isinstance(value, dict)
    if expected_type == "null":
        return value is None
    return False


def _validate_schema_value(value: Any, schema: Mapping[str, Any], path: str, *, strict: bool, depth: int = 0) -> None:
    if depth > 12:
        raise ValueError(f"MCP argument nesting exceeds limit at '{path}'.")
    if not isinstance(schema, Mapping):
        if strict:
            raise ValueError(f"Invalid schema definition at '{path}'.")
        return
    unsupported = set(schema.keys()) - _SUPPORTED_VALUE_KEYWORDS
    if strict and unsupported:
        raise ValueError(
            f"MCP tool schema at '{path}' uses unsupported keyword(s) ({', '.join(sorted(map(str, unsupported)))}); refusing strict execution."
        )
    if "type" in schema:
        expected = schema["type"]
        valid_types = [expected] if isinstance(expected, str) else expected if isinstance(expected, list) else []
        if not valid_types or not all(t in _JSON_TYPES for t in valid_types):
            if strict:
                raise ValueError(f"Unsupported schema type at '{path}': {expected!r}.")
        elif not _matches_json_type(value, valid_types):
            raise ValueError(f"MCP argument '{path}' expected type {expected!r}.")
    if "enum" in schema:
        allowed_vals = schema["enum"]
        if not isinstance(allowed_vals, list):
            if strict:
                raise ValueError(f"Invalid enum constraint at '{path}'.")
        elif value not in allowed_vals:
            raise ValueError(f"MCP argument '{path}' must be one of {allowed_vals!r}.")
    if isinstance(value, str):
        if "minLength" in schema and isinstance(schema["minLength"], int) and len(value) < schema["minLength"]:
            raise ValueError(f"MCP argument '{path}' is shorter than minLength {schema['minLength']}.")
        if "maxLength" in schema and isinstance(schema["maxLength"], int) and len(value) > schema["maxLength"]:
            raise ValueError(f"MCP argument '{path}' exceeds maxLength {schema['maxLength']}.")
    if type(value) in (int, float):
        if "minimum" in schema and isinstance(schema["minimum"], (int, float)) and not isinstance(schema["minimum"], bool):
            if value < schema["minimum"]:
                raise ValueError(f"MCP argument '{path}' is below minimum {schema['minimum']}.")
        if "maximum" in schema and isinstance(schema["maximum"], (int, float)) and not isinstance(schema["maximum"], bool):
            if value > schema["maximum"]:
                raise ValueError(f"MCP argument '{path}' exceeds maximum {schema['maximum']}.")
    if isinstance(value, list) and "items" in schema:
        item_schema = schema["items"]
        if isinstance(item_schema, Mapping):
            for idx, item in enumerate(value):
                _validate_schema_value(item, item_schema, f"{path}[{idx}]", strict=strict, depth=depth + 1)
        elif strict:
            raise ValueError(f"Unsupported array items schema at '{path}'.")
    if isinstance(value, dict):
        props = schema.get("properties")
        req = schema.get("required")
        if isinstance(req, list):
            for key in req:
                if isinstance(key, str) and key not in value:
                    raise ValueError(f"Missing required MCP argument '{path}.{key}'.")
        if schema.get("additionalProperties") is False:
            allowed_keys = set(props.keys()) if isinstance(props, Mapping) else set()
            if extras := set(value.keys()) - allowed_keys:
                raise ValueError(f"Unexpected MCP argument(s) at '{path}': {', '.join(sorted(extras))}.")
        elif strict and "additionalProperties" in schema and not isinstance(schema["additionalProperties"], bool):
            raise ValueError(f"Unsupported additionalProperties schema at '{path}'.")
        if isinstance(props, Mapping):
            for key, val in value.items():
                if key in props:
                    _validate_schema_value(val, props[key], f"{path}.{key}", strict=strict, depth=depth + 1)


def validate_mcp_arguments(tool_spec, arguments: dict[str, Any], *, strict: bool = True) -> None:
    """Validate MCP tool arguments against the supported JSON Schema object subset."""
    if not isinstance(arguments, dict):
        raise ValueError("MCP tool 'arguments' must be a JSON object.")
    schema = getattr(tool_spec, "input_schema", None)
    if not schema:
        return
    if not isinstance(schema, Mapping):
        if strict:
            raise ValueError("Invalid MCP tool inputSchema.")
        return
    unsupported = set(schema.keys()) - _SUPPORTED_OBJECT_KEYWORDS
    if strict and unsupported:
        raise ValueError(
            f"MCP tool '{tool_spec.server}/{tool_spec.name}' uses unsupported inputSchema keyword(s) "
            f"({', '.join(sorted(map(str, unsupported)))}); refusing strict execution."
        )
    if "type" in schema and schema["type"] != "object":
        raise ValueError(f"MCP tool '{tool_spec.server}/{tool_spec.name}' inputSchema must expect an object.")
    req = schema.get("required")
    if isinstance(req, list):
        for key in req:
            if isinstance(key, str) and key not in arguments:
                raise ValueError(f"Missing required MCP argument '{key}' for '{tool_spec.server}/{tool_spec.name}'.")
    props = schema.get("properties")
    if schema.get("additionalProperties") is False:
        allowed_keys = set(props.keys()) if isinstance(props, Mapping) else set()
        if extras := set(arguments.keys()) - allowed_keys:
            raise ValueError(
                f"Unexpected MCP argument(s) for '{tool_spec.server}/{tool_spec.name}': {', '.join(sorted(extras))}."
            )
    elif strict and "additionalProperties" in schema and not isinstance(schema["additionalProperties"], bool):
        raise ValueError(f"Unsupported additionalProperties rule in '{tool_spec.server}/{tool_spec.name}'.")
    if isinstance(props, Mapping):
        for key, val in arguments.items():
            if key in props:
                _validate_schema_value(val, props[key], key, strict=strict, depth=0)

APP_ALIASES = {
    "vscode": "code", "vs code": "code", "visual studio code": "code", "visual-studio-code": "code",
    "chrome": "google-chrome", "cosmic terminal": "cosmic-term",
    "files": "cosmic-files", "cosmic files": "cosmic-files", "file manager": "cosmic-files",
    "claude": "com.anthropic.claude", "claude app": "com.anthropic.claude",
    "claude desktop": "com.anthropic.claude", "calude": "com.anthropic.claude",
    "calude app": "com.anthropic.claude", "calude desktop": "com.anthropic.claude",
    "claude-desktop": "com.anthropic.claude",
    "claude code": "claude-code", "claude terminal": "claude-code",
    "calude code": "claude-code", "calude terminal": "claude-code",
}


def validate_app_target(app_name: str, terminal: bool = False) -> Tuple[bool, str]:
    """Resolve a built-in app ID or an exact installed desktop ID."""
    if not isinstance(app_name, str):
        return False, "Invalid application ID."
    clean = app_name.strip().lower()
    if terminal and clean in ("claude", "calude"):
        clean = "claude-code"
    clean = APP_ALIASES.get(clean, clean)
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", clean):
        return False, "Invalid application ID."
    if clean in TERMINAL_CLI_APPS:
        return True, clean
    if terminal:
        return False, f"'{app_name}' is unavailable as an approved terminal utility."
    if get_platform().name != "linux":
        installed = get_platform().discover_apps()
        key = clean if clean in installed else clean.removesuffix(".exe")
        if key in installed:
            return True, key
        return False, f"Application '{app_name}' is unavailable as an installed application ID."
    if clean in APPLICATION_ALLOWLIST:
        return True, clean

    installed = desktop_app_ids()
    desktop_id = clean.removesuffix(".desktop")
    exact = installed.get(desktop_id)
    if exact:
        return True, exact
    return False, f"Application '{app_name}' is unavailable as an approved application ID."


def desktop_app_ids():
    """Compatibility entry point for discovery on Linux and native app IDs elsewhere."""
    return get_platform().discover_apps()
