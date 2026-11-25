"""MCP server catalog, command/tool schema validation, and TTL discovery cache."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from .. import config
from ..history import redact_secrets
from ..platform import get_platform
from ..policy import is_safe_user_path
from ..schema import FORBIDDEN_CHARS_PATTERN, MCP_SERVER_SLUG, MCP_TOOL_NAME
from .client import MCPCallResult, MCPClientSession, MCPError, is_dangerous_env_var

_ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")

_MAX_SERVERS_BYTES = 128 * 1024
_MAX_SERVERS = 32
_MAX_ARGS = 32
_MAX_ENV_VARS = 64
_MAX_TOOLS_LIST = 128
_TOOL_CACHE_TTL = 30.0

_BLOCKED_SHELLS = frozenset({
    "sh", "bash", "dash", "zsh", "fish", "csh", "tcsh", "ksh", "cmd", "powershell", "pwsh",
})
_ALLOWED_RUNNERS = frozenset({
    "python", "python3", "node", "npx", "uv", "uvx", "deno", "bun", "docker", "podman",
})


@dataclass(frozen=True)
class MCPServerConfig:
    name: str
    command: str
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = MappingProxyType({})
    env_passthrough: tuple[str, ...] = ()
    cwd: str = ""
    timeout: float = 10.0
    trusted: bool = False
    destructive: bool = False
    allowed_tools: frozenset[str] | None = None
    blocked_tools: frozenset[str] = frozenset()
    destructive_tools: frozenset[str] = frozenset()
    enabled: bool = True


@dataclass(frozen=True)
class MCPToolSpec:
    server: str
    name: str
    description: str = ""
    input_schema: Mapping[str, Any] = MappingProxyType({})
    read_only_hint: bool = False
    destructive_hint: bool = False
    idempotent_hint: bool = False
    open_world_hint: bool = True


@dataclass(frozen=True)
class MCPDiscoverySnapshot:
    server: str
    era: str
    protocol_version: str = ""
    tools: tuple[MCPToolSpec, ...] = ()
    error: str = ""
    discovered_at: float = 0.0


@dataclass(frozen=True)
class MCPRegistry:
    servers: Mapping[str, MCPServerConfig]
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class MCPServerGrant:
    server: str
    launch_hash: str
    executable: str
    args: tuple[str, ...] = ()
    cwd: str = ""
    granted_env: tuple[str, ...] = ()
    approved_readonly_tools: frozenset[str] = frozenset()
    approved_at: float = 0.0


_registry_lock = threading.Lock()
_registry_cache: tuple[str, object, MCPRegistry] = ("", None, MCPRegistry(MappingProxyType({})))
_grants_lock = threading.Lock()
_grants_cache: tuple[str, object, Mapping[str, MCPServerGrant]] = ("", None, MappingProxyType({}))
_tool_cache_lock = threading.Lock()
_tool_cache: dict[tuple[str, object, object, str], tuple[float, MCPDiscoverySnapshot]] = {}


def _extra_allowed_commands() -> set[str]:
    try:
        raw = config.load_user_config().get("mcp_allowed_commands", [])
    except Exception:
        return set()
    if not isinstance(raw, list):
        return set()
    return {item.strip().casefold().removesuffix(".exe") for item in raw if isinstance(item, str) and item.strip()}


def _validate_command(command: object, extra_allowed: set[str]) -> str:
    if not isinstance(command, str) or not command.strip() or "\x00" in command:
        raise ValueError("MCP server command must be a non-empty executable name or path.")
    cleaned = command.strip()
    if any(ch.isspace() for ch in cleaned) or FORBIDDEN_CHARS_PATTERN.search(cleaned):
        raise ValueError("MCP server command contains forbidden characters.")
    base = os.path.basename(cleaned).casefold().removesuffix(".exe")
    if not base or base in _BLOCKED_SHELLS:
        raise ValueError(f"Shell interpreter '{base or cleaned}' is not allowed as an MCP server command.")
    py_base = os.path.basename(sys.executable).casefold().removesuffix(".exe")
    if (
        base in _ALLOWED_RUNNERS
        or base == py_base
        or re.fullmatch(r"python3(?:\.\d+)?", base)
        or base.startswith("mcp-")
        or base in extra_allowed
    ):
        return cleaned
    raise ValueError(f"MCP server command '{base}' is not in the approved runner allowlist.")


def _validate_tool_set(raw: object, label: str) -> frozenset[str]:
    if not isinstance(raw, list) or len(raw) > _MAX_TOOLS_LIST:
        raise ValueError(f"'{label}' must be a list of up to {_MAX_TOOLS_LIST} tool names.")
    items: list[str] = []
    for entry in raw:
        if not isinstance(entry, str) or not MCP_TOOL_NAME.fullmatch(entry):
            raise ValueError(f"Invalid tool name in '{label}': {entry!r}")
        items.append(entry)
    return frozenset(items)


def _parse_server_entry(slug: object, item: object, extra_allowed: set[str]) -> MCPServerConfig:
    if not isinstance(slug, str) or not MCP_SERVER_SLUG.fullmatch(slug):
        raise ValueError(f"Invalid MCP server slug '{slug}'.")
    if not isinstance(item, dict):
        raise ValueError(f"MCP server '{slug}' must be an object.")
    allowed_keys = {
        "name", "command", "args", "env", "env_passthrough", "cwd",
        "timeout", "trusted", "destructive", "allowed_tools",
        "blocked_tools", "destructive_tools", "enabled",
    }
    if extras := set(item) - allowed_keys:
        raise ValueError(f"Unexpected field(s) in MCP server '{slug}': {', '.join(sorted(extras))}.")
    if "name" in item and item["name"] != slug:
        raise ValueError(f"MCP server entry name '{item['name']}' does not match '{slug}'.")

    command = _validate_command(item.get("command"), extra_allowed)

    raw_args = item.get("args", [])
    if not isinstance(raw_args, list) or len(raw_args) > _MAX_ARGS:
        raise ValueError(f"MCP server '{slug}' args must be a list of up to {_MAX_ARGS} strings.")
    args = []
    for arg in raw_args:
        if not isinstance(arg, str) or "\x00" in arg:
            raise ValueError(f"MCP server '{slug}' args must be strings without NUL bytes.")
        args.append(arg)

    raw_env = item.get("env", {})
    if not isinstance(raw_env, dict) or len(raw_env) > _MAX_ENV_VARS:
        raise ValueError(f"MCP server '{slug}' env must be an object of up to {_MAX_ENV_VARS} variables.")
    env: dict[str, str] = {}
    for k, v in raw_env.items():
        if not isinstance(k, str) or not _ENV_KEY.fullmatch(k) or not isinstance(v, str) or "\x00" in v:
            raise ValueError(f"Invalid environment variable in MCP server '{slug}'.")
        if is_dangerous_env_var(k):
            raise ValueError(f"Environment variable '{k}' is blocked in MCP server '{slug}' due to runtime injection risk.")
        env[k] = v

    raw_pass = item.get("env_passthrough", [])
    if not isinstance(raw_pass, list) or len(raw_pass) > _MAX_ARGS:
        raise ValueError(f"MCP server '{slug}' env_passthrough must be a list of variable names.")
    env_passthrough: list[str] = []
    for k in raw_pass:
        if not isinstance(k, str) or not _ENV_KEY.fullmatch(k):
            raise ValueError(f"Invalid env_passthrough variable name '{k}' in MCP server '{slug}'.")
        if is_dangerous_env_var(k):
            raise ValueError(f"Environment variable '{k}' is blocked in MCP server '{slug}' due to runtime injection risk.")
        env_passthrough.append(k)

    cwd = item.get("cwd", "")
    if cwd is None:
        cwd = ""
    if not isinstance(cwd, str) or "\x00" in cwd:
        raise ValueError(f"MCP server '{slug}' cwd must be text.")
    if cwd:
        safe, reason, canonical = is_safe_user_path(cwd, must_exist=True)
        if not safe:
            raise ValueError(f"MCP server '{slug}' cwd blocked: {reason}")
        cwd = canonical

    timeout = item.get("timeout", 10.0)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0.2 <= float(timeout) <= 60.0:
        raise ValueError(f"MCP server '{slug}' timeout must be between 0.2 and 60 seconds.")

    for flag in ("trusted", "destructive", "enabled"):
        if flag in item and not isinstance(item[flag], bool):
            raise ValueError(f"MCP server '{slug}' field '{flag}' must be a boolean.")

    allowed_tools = (
        _validate_tool_set(item["allowed_tools"], "allowed_tools")
        if item.get("allowed_tools") is not None else None
    )
    blocked_tools = (
        _validate_tool_set(item["blocked_tools"], "blocked_tools")
        if item.get("blocked_tools") is not None else frozenset()
    )
    destructive_tools = (
        _validate_tool_set(item["destructive_tools"], "destructive_tools")
        if item.get("destructive_tools") is not None else frozenset()
    )

    return MCPServerConfig(
        name=slug,
        command=command,
        args=tuple(args),
        env=MappingProxyType(env),
        env_passthrough=tuple(env_passthrough),
        cwd=cwd,
        timeout=float(timeout),
        trusted=item.get("trusted", False),
        destructive=item.get("destructive", False),
        allowed_tools=allowed_tools,
        blocked_tools=blocked_tools,
        destructive_tools=destructive_tools,
        enabled=item.get("enabled", True),
    )


def _read_registry(path: str) -> MCPRegistry:
    known = config.configured_api_keys()
    try:
        with open(path, "rb") as handle:
            raw = handle.read(_MAX_SERVERS_BYTES + 1)
        if len(raw) > _MAX_SERVERS_BYTES:
            raise ValueError("mcp_servers.json exceeds the 128 KiB limit.")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("mcp_servers.json root must be a JSON object.")
        raw_servers = data.get("servers", data.get("mcpServers", {}))
    except FileNotFoundError:
        return MCPRegistry(MappingProxyType({}))
    except (OSError, UnicodeError, ValueError, RecursionError) as error:
        return MCPRegistry(MappingProxyType({}), (redact_secrets(f"Cannot read mcp_servers.json. {error}", known),))

    pairs: list[tuple[object, object]] = []
    if isinstance(raw_servers, dict):
        if len(raw_servers) > _MAX_SERVERS:
            return MCPRegistry(MappingProxyType({}), (f"mcp_servers.json supports at most {_MAX_SERVERS} servers.",))
        pairs = list(raw_servers.items())
    elif isinstance(raw_servers, list):
        if len(raw_servers) > _MAX_SERVERS:
            return MCPRegistry(MappingProxyType({}), (f"mcp_servers.json supports at most {_MAX_SERVERS} servers.",))
        for item in raw_servers:
            slug = item.get("name") if isinstance(item, dict) else None
            pairs.append((slug, item))
    else:
        return MCPRegistry(MappingProxyType({}), ("mcp_servers.json 'servers' must be an object or array.",))

    extra_allowed = _extra_allowed_commands()
    servers: dict[str, MCPServerConfig] = {}
    errors: list[str] = []
    duplicates: set[str] = set()
    for slug, item in pairs:
        try:
            cfg = _parse_server_entry(slug, item, extra_allowed)
            if cfg.name in servers or cfg.name in duplicates:
                servers.pop(cfg.name, None)
                duplicates.add(cfg.name)
                raise ValueError(f"Duplicate MCP server name '{cfg.name}'.")
            servers[cfg.name] = cfg
        except ValueError as error:
            errors.append(redact_secrets(str(error), known))
    return MCPRegistry(MappingProxyType(servers), tuple(errors))


def _file_stamp(path: str) -> object:
    try:
        info = Path(path).stat()
        return (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size)
    except FileNotFoundError:
        return None
    except OSError:
        return object()


def load_mcp_registry(*, refresh: bool = False) -> MCPRegistry:
    global _registry_cache
    path = os.fspath(config.MCP_SERVERS_FILE)
    stamp = _file_stamp(path)
    with _registry_lock:
        cached_path, cached_stamp, cached = _registry_cache
        if not refresh and cached_path == path and cached_stamp == stamp:
            return cached
        updated = _read_registry(path)
        _registry_cache = (path, stamp, updated)
        return updated


def _parse_tool_spec(server_name: str, raw: dict[str, Any], known: tuple[str, ...]) -> MCPToolSpec | None:
    name = raw.get("name")
    if not isinstance(name, str) or not MCP_TOOL_NAME.fullmatch(name):
        return None
    description = raw.get("description", "")
    description = redact_secrets(description, known) if isinstance(description, str) else ""
    schema = raw.get("inputSchema")
    if not isinstance(schema, dict):
        schema = {}
    annotations = raw.get("annotations")
    if not isinstance(annotations, dict):
        annotations = {}
    return MCPToolSpec(
        server=server_name,
        name=name,
        description=description[:240],
        input_schema=MappingProxyType(schema),
        read_only_hint=annotations.get("readOnlyHint") is True,
        destructive_hint=annotations.get("destructiveHint") is True,
        idempotent_hint=annotations.get("idempotentHint") is True,
        open_world_hint=annotations.get("openWorldHint") is not False,
    )


def _resolve_executable(command: str) -> str:
    if os.path.sep in command or (os.altsep and os.altsep in command):
        return os.path.realpath(os.path.abspath(command))
    resolved = shutil.which(command)
    return os.path.realpath(resolved) if resolved else command


def compute_server_launch_hash(server: MCPServerConfig, granted_env: tuple[str, ...]) -> tuple[str, str]:
    resolved_exe = _resolve_executable(server.command)
    env_digest = hashlib.sha256(
        json.dumps(sorted(server.env.items()), separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    payload = json.dumps({
        "executable": resolved_exe,
        "args": list(server.args),
        "cwd": server.cwd,
        "granted_env": sorted(granted_env),
        "env_digest": env_digest,
        "env_passthrough": sorted(server.env_passthrough),
    }, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return resolved_exe, hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _parse_grant_entry(slug: object, item: object) -> MCPServerGrant | None:
    if not isinstance(slug, str) or not MCP_SERVER_SLUG.fullmatch(slug) or not isinstance(item, dict):
        return None
    launch_hash = item.get("launch_hash")
    executable = item.get("executable")
    args = item.get("args", [])
    cwd = item.get("cwd", "")
    granted_env = item.get("granted_env", [])
    ro_tools = item.get("approved_readonly_tools", [])
    approved_at = item.get("approved_at", 0.0)
    if (
        not isinstance(launch_hash, str)
        or not re.fullmatch(r"[0-9a-f]{64}", launch_hash)
        or not isinstance(executable, str)
        or not executable
        or not isinstance(args, list)
        or not all(isinstance(a, str) for a in args)
        or not isinstance(cwd, str)
        or not isinstance(granted_env, list)
        or not all(isinstance(k, str) and _ENV_KEY.fullmatch(k) and not is_dangerous_env_var(k) for k in granted_env)
        or not isinstance(ro_tools, list)
        or not all(isinstance(t, str) and MCP_TOOL_NAME.fullmatch(t) for t in ro_tools)
        or isinstance(approved_at, bool)
        or not isinstance(approved_at, (int, float))
    ):
        return None
    return MCPServerGrant(
        server=slug,
        launch_hash=launch_hash,
        executable=executable,
        args=tuple(args),
        cwd=cwd,
        granted_env=tuple(sorted(set(granted_env))),
        approved_readonly_tools=frozenset(ro_tools),
        approved_at=float(approved_at),
    )


def _read_grants(path: str) -> Mapping[str, MCPServerGrant]:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return MappingProxyType({})
    except OSError:
        return MappingProxyType({})
    if not get_platform().is_private(info):
        return MappingProxyType({})
    try:
        with open(path, "rb") as handle:
            raw = handle.read(_MAX_SERVERS_BYTES + 1)
        if len(raw) > _MAX_SERVERS_BYTES:
            return MappingProxyType({})
        data = json.loads(raw)
    except (OSError, UnicodeError, ValueError, RecursionError):
        return MappingProxyType({})
    raw_grants = data.get("grants") if isinstance(data, dict) else None
    if not isinstance(raw_grants, dict):
        return MappingProxyType({})
    grants: dict[str, MCPServerGrant] = {}
    for slug, item in raw_grants.items():
        parsed = _parse_grant_entry(slug, item)
        if parsed is not None:
            grants[parsed.server] = parsed
    return MappingProxyType(grants)


def load_mcp_grants(*, refresh: bool = False) -> Mapping[str, MCPServerGrant]:
    global _grants_cache
    path = os.fspath(config.MCP_GRANTS_FILE)
    stamp = _file_stamp(path)
    with _grants_lock:
        cached_path, cached_stamp, cached = _grants_cache
        if not refresh and cached_path == path and cached_stamp == stamp:
            return cached
        updated = _read_grants(path)
        _grants_cache = (path, stamp, updated)
        return updated


def is_grant_valid(server: MCPServerConfig | None, grant: MCPServerGrant | None) -> tuple[bool, str]:
    if server is None or not server.enabled:
        return False, "server disabled or not configured"
    if grant is None:
        return False, "approval required"
    configured_env = tuple(sorted(set(server.env) | set(server.env_passthrough)))
    if tuple(sorted(grant.granted_env)) != configured_env:
        return False, "granted env variables changed; re-approval required"
    if any(is_dangerous_env_var(k) for k in grant.granted_env):
        return False, "dangerous env variable rejected"
    resolved_exe, launch_hash = compute_server_launch_hash(server, grant.granted_env)
    if (
        grant.executable != resolved_exe
        or grant.args != server.args
        or grant.cwd != server.cwd
        or grant.launch_hash != launch_hash
    ):
        return False, "server launch configuration changed; re-approval required"
    return True, ""


def _write_grants_atomic(grants: Mapping[str, MCPServerGrant]) -> None:
    config.ensure_secure_storage()
    path = Path(config.MCP_GRANTS_FILE)
    payload = {
        "version": 1,
        "grants": {
            slug: {
                "launch_hash": g.launch_hash,
                "executable": g.executable,
                "args": list(g.args),
                "cwd": g.cwd,
                "granted_env": list(g.granted_env),
                "approved_readonly_tools": sorted(g.approved_readonly_tools),
                "approved_at": g.approved_at,
            }
            for slug, g in sorted(grants.items())
        },
    }
    fd, temp = tempfile.mkstemp(prefix=".mcp-grants-", dir=os.fspath(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        get_platform().restrict_permissions(temp)
        os.replace(temp, path)
        get_platform().sync_directory(path.parent)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def approve_mcp_server(
    server_name: str,
    *,
    grant_env: tuple[str, ...] | list[str] | None = None,
    approve_readonly_tools: tuple[str, ...] | list[str] | None = None,
) -> MCPServerGrant:
    """Validate and atomically save explicit launch, env, and read-only grants for one MCP server."""
    global _grants_cache
    registry = load_mcp_registry(refresh=True)
    server = registry.servers.get(server_name)
    if server is None:
        detail = "; ".join(registry.errors)
        raise ValueError(
            f"MCP server '{server_name}' is not configured or invalid."
            + (f" ({detail})" if detail else "")
        )

    configured_env = tuple(sorted(set(server.env) | set(server.env_passthrough)))
    if grant_env is None:
        chosen_env = configured_env
    else:
        cleaned_env: list[str] = []
        for raw_key in grant_env:
            key = str(raw_key).strip()
            if not key:
                continue
            if not _ENV_KEY.fullmatch(key):
                raise ValueError(f"Invalid environment variable name '{key}'.")
            if is_dangerous_env_var(key):
                raise ValueError(f"Environment variable '{key}' is blocked due to runtime injection risk.")
            cleaned_env.append(key)
        chosen_env = tuple(sorted(set(cleaned_env)))

    resolved_exe, launch_hash = compute_server_launch_hash(server, chosen_env)
    existing_grants = dict(load_mcp_grants(refresh=True))
    prev = existing_grants.get(server_name)
    if approve_readonly_tools is not None:
        ro_set: list[str] = []
        for raw_tool in approve_readonly_tools:
            tname = str(raw_tool).strip()
            if not tname:
                continue
            if not MCP_TOOL_NAME.fullmatch(tname):
                raise ValueError(f"Invalid tool name '{tname}' for --grant-readonly.")
            ro_set.append(tname)
        ro_frozen = frozenset(ro_set)
    elif prev is not None and prev.launch_hash == launch_hash:
        ro_frozen = prev.approved_readonly_tools
    else:
        ro_frozen = frozenset()

    grant = MCPServerGrant(
        server=server_name,
        launch_hash=launch_hash,
        executable=resolved_exe,
        args=server.args,
        cwd=server.cwd,
        granted_env=chosen_env,
        approved_readonly_tools=ro_frozen,
        approved_at=time.time(),
    )
    existing_grants[server_name] = grant
    _write_grants_atomic(existing_grants)
    with _grants_lock:
        _grants_cache = ("", None, MappingProxyType({}))
    with _tool_cache_lock:
        _tool_cache.clear()
    load_mcp_grants(refresh=True)
    return grant


def discover_server_tools(
    server_name: str,
    *,
    refresh: bool = False,
    cancel_token: Any = None,
) -> MCPDiscoverySnapshot:
    path = os.fspath(config.MCP_SERVERS_FILE)
    stamp = _file_stamp(path)
    grants_path = os.fspath(config.MCP_GRANTS_FILE)
    grants_stamp = _file_stamp(grants_path)
    registry = load_mcp_registry(refresh=refresh)
    server = registry.servers.get(server_name)
    if server is None:
        return MCPDiscoverySnapshot(server_name, "offline", error=f"MCP server '{server_name}' is not configured.")
    if not server.enabled:
        return MCPDiscoverySnapshot(server_name, "disabled", error=f"MCP server '{server_name}' is disabled.")

    grant = load_mcp_grants(refresh=refresh).get(server_name)
    granted, grant_reason = is_grant_valid(server, grant)
    if not granted:
        return MCPDiscoverySnapshot(
            server.name,
            "unapproved",
            error=f"MCP server '{server.name}' requires user approval ({grant_reason}).",
        )

    cache_key = (path, stamp, grants_stamp, server_name)
    now = time.monotonic()
    if not refresh:
        with _tool_cache_lock:
            cached = _tool_cache.get(cache_key)
            if cached and cached[0] > now:
                return cached[1]

    known = config.configured_api_keys()
    try:
        with MCPClientSession(
            server.name,
            server.command,
            server.args,
            env=server.env,
            env_passthrough=server.env_passthrough,
            allowed_env_names=grant.granted_env,
            cwd=server.cwd,
            timeout=server.timeout,
            cancel_token=cancel_token,
        ) as session:
            raw_tools = session.list_tools()
            era = session.era
            version = session.protocol_version
        specs = tuple(
            spec for item in raw_tools[:_MAX_TOOLS_LIST]
            if (spec := _parse_tool_spec(server.name, item, known)) is not None
        )
        snapshot = MCPDiscoverySnapshot(
            server=server.name,
            era=era,
            protocol_version=version,
            tools=specs,
            discovered_at=time.monotonic(),
        )
    except (MCPError, OSError, ValueError) as error:
        snapshot = MCPDiscoverySnapshot(
            server=server.name,
            era="offline",
            error=redact_secrets(str(error), known),
            discovered_at=time.monotonic(),
        )

    with _tool_cache_lock:
        _tool_cache[cache_key] = (time.monotonic() + _TOOL_CACHE_TTL, snapshot)
    return snapshot


def call_mcp_tool(
    server_name: str,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    *,
    cancel_token: Any = None,
) -> MCPCallResult:
    registry = load_mcp_registry()
    server = registry.servers.get(server_name)
    if server is None or not server.enabled:
        raise MCPError(f"MCP server '{server_name}' is not configured or is disabled.")
    grant = load_mcp_grants().get(server_name)
    granted, grant_reason = is_grant_valid(server, grant)
    if not granted:
        raise MCPError(f"MCP server '{server_name}' is not approved ({grant_reason}).", code="policy_error")
    known = config.configured_api_keys()
    try:
        with MCPClientSession(
            server.name,
            server.command,
            server.args,
            env=server.env,
            env_passthrough=server.env_passthrough,
            allowed_env_names=grant.granted_env,
            cwd=server.cwd,
            timeout=server.timeout,
            cancel_token=cancel_token,
        ) as session:
            result = session.call_tool(tool_name, arguments or {})
    except MCPError as error:
        raise MCPError(redact_secrets(str(error), known), code=error.code) from None
    return MCPCallResult(
        server=result.server,
        tool=result.tool,
        era=result.era,
        protocol_version=result.protocol_version,
        text=redact_secrets(result.text, known),
        is_error=result.is_error,
        structured=redact_secrets(result.structured, known) if result.structured is not None else None,
    )


_SAFE_SCHEMA_TYPES = frozenset({"string", "number", "integer", "boolean", "array", "object", "null"})
_SAFE_PARAM_NAME = re.compile(r"[A-Za-z0-9_.-]{1,48}")


def _constrained_param_signature(schema: Mapping[str, Any]) -> str:
    props = schema.get("properties") if isinstance(schema, Mapping) else None
    if not isinstance(props, Mapping) or not props:
        return "none"
    req = set(schema.get("required", ())) if isinstance(schema.get("required"), list) else set()
    parts: list[str] = []
    for pname, pspec in list(props.items())[:16]:
        if not isinstance(pname, str) or not _SAFE_PARAM_NAME.fullmatch(pname):
            continue
        raw_t = pspec.get("type") if isinstance(pspec, Mapping) else None
        if isinstance(raw_t, str) and raw_t in _SAFE_SCHEMA_TYPES:
            tlabel = raw_t
        elif isinstance(raw_t, list):
            clean = [t for t in raw_t if isinstance(t, str) and t in _SAFE_SCHEMA_TYPES]
            tlabel = "|".join(clean) if clean else "any"
        else:
            tlabel = "any"
        mark = "*" if pname in req else ""
        parts.append(f"{pname}{mark}: {tlabel}")
    return ", ".join(parts) if parts else "none"


def mcp_prompt_summary() -> str:
    """Return constrained tool/parameter descriptors only (no untrusted prose) for approved MCP servers."""
    registry = load_mcp_registry()
    grants = load_mcp_grants()
    enabled = [srv for srv in registry.servers.values() if srv.enabled]
    if not enabled:
        return ""
    lines: list[str] = ["Configured External MCP Tools (call via action 'mcp_tool' with {server, tool, arguments}):"]
    for server in sorted(enabled, key=lambda s: s.name):
        if not is_grant_valid(server, grants.get(server.name))[0]:
            continue
        snapshot = discover_server_tools(server.name)
        if snapshot.error or not snapshot.tools:
            continue
        visible = [
            t for t in snapshot.tools
            if t.name not in server.blocked_tools
            and (server.allowed_tools is None or t.name in server.allowed_tools)
        ]
        if not visible:
            continue
        trust_tag = "trusted" if server.trusted else "untrusted"
        lines.append(f"  Server '{server.name}' ({trust_tag}, {snapshot.era}):")
        for tool in visible[:16]:
            sig = _constrained_param_signature(tool.input_schema)
            lines.append(f"    - {tool.name} (params: {sig})")
    return "\n".join(lines) if len(lines) > 1 else ""
