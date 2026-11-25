"""Stdlib-only dual-era stdio MCP JSON-RPC 2.0 client."""

from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from ..ipc import FrameError, FrameReader, MAX_FRAME_BYTES, RequestCancelled, write_frame
from ..platform import get_platform
from ..providers.agy_pipes import WorkerPipeRelay

PROTOCOL_MODERN = "2026-07-28"
PROTOCOL_LEGACY = "2025-11-25"
PROTOCOL_FALLBACKS = ("2025-11-25", "2024-11-05")
META_PROTOCOL_KEY = "io.modelcontextprotocol/protocolVersion"
MCP_CLIENT_INFO = {"name": "mavrick", "version": "2.0.0"}

MAX_MCP_FRAME_BYTES = MAX_FRAME_BYTES
MAX_MCP_STDERR_BYTES = 16 * 1024
MAX_TOOL_PAGES = 8
_CLOSE_GRACE_SEC = 0.3

_SAFE_BASE_ENV = (
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE",
    "TERM", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT", "WINDIR", "COMSPEC",
    "APPDATA", "LOCALAPPDATA", "XDG_RUNTIME_DIR", "XDG_DATA_HOME",
    "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "PYTHONNOUSERSITE", "PYTHONUTF8",
    "PYTHONDONTWRITEBYTECODE",
)

DANGEROUS_ENV_KEYS = frozenset({
    "LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT", "LD_DEBUG",
    "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP",
    "NODE_OPTIONS", "NODE_PATH", "RUBYOPT", "PERL5OPT", "PERL5LIB",
    "BASH_ENV", "ENV", "CDPATH", "GLOBIGNORE", "IFS",
    "JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS",
})
DANGEROUS_ENV_PREFIXES = ("DYLD_", "LD_")


def is_dangerous_env_var(name: str) -> bool:
    """Return True if an env var name can hijack runtime loaders or interpreters."""
    upper = name.strip().upper()
    return upper in DANGEROUS_ENV_KEYS or upper.startswith(DANGEROUS_ENV_PREFIXES)


class MCPError(RuntimeError):
    """Raised when an MCP stdio server fails, times out, or violates protocol."""

    def __init__(self, message: str, *, code: str = "mcp_error"):
        super().__init__(message)
        self.code = code


class MCPProtocolError(MCPError):
    """JSON-RPC error returned by an MCP server."""

    def __init__(self, rpc_code: int, message: str, data: Any = None):
        super().__init__(message, code="protocol_error")
        self.rpc_code = rpc_code
        self.data = data


@dataclass(frozen=True)
class MCPCallResult:
    server: str
    tool: str
    era: str
    protocol_version: str
    text: str
    is_error: bool = False
    structured: Any = None


def scoped_server_env(
    env: Mapping[str, str] | None = None,
    env_passthrough: tuple[str, ...] = (),
    *,
    allowed_env_names: tuple[str, ...] | frozenset[str] | set[str] | None = None,
) -> dict[str, str]:
    """Build a least-privilege process environment without inheriting host secret or loader env vars."""
    allowed = None if allowed_env_names is None else set(allowed_env_names)
    scoped: dict[str, str] = {}
    for key in _SAFE_BASE_ENV:
        val = os.environ.get(key)
        if val is not None:
            scoped[key] = val
    for key in env_passthrough:
        if is_dangerous_env_var(key):
            raise MCPError(f"Dangerous runtime environment variable '{key}' is blocked for MCP servers.", code="policy_error")
        if allowed is not None and key not in allowed:
            raise MCPError(f"MCP env_passthrough variable '{key}' is not approved.", code="policy_error")
        val = os.environ.get(key)
        if val is not None:
            scoped[key] = val
    if env:
        for key, val in env.items():
            key_str = str(key)
            if is_dangerous_env_var(key_str):
                raise MCPError(f"Dangerous runtime environment variable '{key_str}' is blocked for MCP servers.", code="policy_error")
            if allowed is not None and key_str not in allowed:
                raise MCPError(f"MCP env variable '{key_str}' is not approved.", code="policy_error")
            scoped[key_str] = str(val)
    return scoped


def _is_legacy_discover_error(error: MCPProtocolError) -> bool:
    if error.rpc_code in (-32601, -32600, -32602):
        return True
    detail = str(error).casefold()
    return any(tok in detail for tok in ("method", "unknown", "not found", "unsupported", "discover", "initialize"))


def _format_tool_output(result: dict[str, Any]) -> tuple[str, Any]:
    structured = result.get("structuredContent")
    chunks: list[str] = []
    content = result.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict):
                kind = item.get("type", "text")
                if kind == "text" and isinstance(item.get("text"), str):
                    chunks.append(item["text"])
                elif kind == "resource" and isinstance(item.get("resource"), dict):
                    res = item["resource"]
                    text = res.get("text")
                    uri = res.get("uri", "")
                    chunks.append(text if isinstance(text, str) else f"[resource: {uri}]")
                elif "text" in item and isinstance(item["text"], str):
                    chunks.append(item["text"])
                else:
                    chunks.append(json.dumps(item, ensure_ascii=False))
            elif isinstance(item, str):
                chunks.append(item)
    if not chunks and structured is not None:
        chunks.append(json.dumps(structured, indent=2, ensure_ascii=False))
    return "\n".join(chunks).strip(), structured


class MCPClientSession:
    """One bounded stdio MCP session with automatic 2026-07-28 -> legacy negotiation."""

    def __init__(
        self,
        server_name: str,
        command: str,
        args: tuple[str, ...] = (),
        *,
        env: Mapping[str, str] | None = None,
        env_passthrough: tuple[str, ...] = (),
        allowed_env_names: tuple[str, ...] | frozenset[str] | set[str] | None = None,
        cwd: str = "",
        timeout: float = 10.0,
        cancel_token: Any = None,
    ):
        self.server_name = server_name
        self.command = command
        self.args = tuple(args)
        self.env = scoped_server_env(env, env_passthrough, allowed_env_names=allowed_env_names)
        self.cwd = cwd or None
        self.timeout = float(timeout)
        self.cancel_token = cancel_token
        self.proc: subprocess.Popen | None = None
        self._relay: WorkerPipeRelay | None = None
        self._in_fd: Any = None
        self._out_fd: Any = None
        self._reader: FrameReader | None = None
        self._stderr_chunks: list[bytes] = []
        self._stderr_total = 0
        self._stderr_thread: threading.Thread | None = None
        self._next_id = 0
        self.era = ""
        self.protocol_version = ""

    def _cancelled(self) -> bool:
        if self.cancel_token is None:
            return False
        if callable(self.cancel_token):
            return bool(self.cancel_token())
        if hasattr(self.cancel_token, "is_set"):
            return self.cancel_token.is_set()
        return bool(self.cancel_token)

    def _spawn(self) -> None:
        argv = [self.command, *self.args]
        options = get_platform().detached_options() if os.name != "nt" else {}
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self.env,
                cwd=self.cwd,
                shell=False,
                bufsize=0,
                **options,
            )
        except OSError as error:
            raise MCPError(f"Failed to start MCP server '{self.server_name}': {error}", code="spawn_error") from error
        self.proc = proc
        self._next_id = 0
        self._stderr_chunks = []
        self._stderr_total = 0

        def drain_stderr():
            stream = proc.stderr
            if stream is None:
                return
            try:
                while True:
                    chunk = stream.read(2048)
                    if not chunk:
                        break
                    remaining = MAX_MCP_STDERR_BYTES - self._stderr_total
                    if remaining > 0:
                        piece = chunk[:remaining]
                        self._stderr_chunks.append(piece)
                        self._stderr_total += len(piece)
            except (OSError, ValueError):
                pass

        self._stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
        self._stderr_thread.start()

        if os.name == "nt":
            self._relay = WorkerPipeRelay(proc.stdin, proc.stdout)
            self._in_fd = self._relay.input
            self._out_fd = self._relay.output
        else:
            in_fd = proc.stdin.fileno()
            out_fd = proc.stdout.fileno()
            os.set_blocking(in_fd, False)
            os.set_blocking(out_fd, False)
            self._in_fd = in_fd
            self._out_fd = out_fd
        self._reader = FrameReader(self._out_fd, max_bytes=MAX_MCP_FRAME_BYTES)

    def stderr_text(self) -> str:
        return b"".join(self._stderr_chunks).decode("utf-8", errors="replace").strip()

    def _write_message(self, payload: dict[str, Any], deadline: float) -> None:
        try:
            write_frame(self._in_fd, payload, deadline, self._cancelled)
        except RequestCancelled as error:
            raise MCPError("MCP request cancelled.", code="cancelled") from error
        except TimeoutError as error:
            raise MCPError(f"MCP server '{self.server_name}' timed out while writing.", code="timeout") from error
        except (OSError, EOFError, FrameError) as error:
            detail = self.stderr_text()
            suffix = f" ({detail})" if detail else ""
            raise MCPError(f"MCP transport write failed for '{self.server_name}': {error}{suffix}", code="transport") from error

    def _request(self, method: str, params: dict[str, Any] | None = None, *, timeout: float | None = None) -> dict[str, Any]:
        if self.proc is None or self._reader is None:
            raise MCPError(f"MCP server '{self.server_name}' is not connected.")
        self._next_id += 1
        req_id = self._next_id
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            payload["params"] = params
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        self._write_message(payload, deadline)
        while True:
            try:
                message = self._reader.read(deadline, self._cancelled)
            except RequestCancelled as error:
                raise MCPError("MCP request cancelled.", code="cancelled") from error
            except TimeoutError as error:
                raise MCPError(f"MCP server '{self.server_name}' timed out waiting for '{method}'.", code="timeout") from error
            except FrameError as error:
                raise MCPError(f"Malformed MCP frame from '{self.server_name}': {error}", code="invalid_response") from error
            except (OSError, EOFError) as error:
                detail = self.stderr_text()
                suffix = f" ({detail})" if detail else ""
                raise MCPError(f"MCP server '{self.server_name}' closed connection during '{method}'{suffix}.", code="eof") from error

            if "id" not in message or message["id"] is None:
                continue  # Ignore async server notifications while awaiting response.
            if message["id"] != req_id:
                continue
            if "error" in message and message["error"] is not None:
                err = message["error"]
                if isinstance(err, dict):
                    rpc_code = err.get("code", -32000) if isinstance(err.get("code"), int) else -32000
                    msg = str(err.get("message") or "MCP server returned an error.")
                    raise MCPProtocolError(rpc_code, msg, err.get("data"))
                raise MCPProtocolError(-32000, str(err))
            result = message.get("result")
            if not isinstance(result, dict):
                raise MCPError(f"Invalid MCP result for '{method}' from '{self.server_name}'.", code="invalid_response")
            return result

    def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        deadline = time.monotonic() + min(self.timeout, 2.0)
        self._write_message(payload, deadline)

    def _legacy_handshake(self) -> dict[str, Any]:
        result = self._request("initialize", {
            "protocolVersion": PROTOCOL_LEGACY,
            "capabilities": {},
            "clientInfo": MCP_CLIENT_INFO,
        })
        version = result.get("protocolVersion")
        self.era = "legacy"
        self.protocol_version = str(version) if isinstance(version, str) and version else PROTOCOL_LEGACY
        self._notify("notifications/initialized")
        return result

    def connect(self) -> dict[str, Any]:
        """Spawn the server and negotiate 2026-07-28 or fall back to legacy MCP handshake."""
        self._spawn()
        try:
            try:
                discover_result = self._request("server/discover", {
                    "clientInfo": MCP_CLIENT_INFO,
                    "_meta": {META_PROTOCOL_KEY: PROTOCOL_MODERN},
                })
                meta = discover_result.get("_meta")
                version = discover_result.get("protocolVersion") or (
                    meta.get(META_PROTOCOL_KEY) if isinstance(meta, dict) else None
                )
                self.era = PROTOCOL_MODERN
                self.protocol_version = str(version) if isinstance(version, str) and version else PROTOCOL_MODERN
                return discover_result
            except MCPProtocolError as error:
                if not _is_legacy_discover_error(error):
                    raise
                return self._legacy_handshake()
            except MCPError as error:
                # Strict legacy servers may close stdin/stdout when sent an unknown method pre-init.
                if error.code != "eof":
                    raise
                self.close()
                self._spawn()
                return self._legacy_handshake()
        except BaseException:
            self.close()
            raise

    def list_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(MAX_TOOL_PAGES):
            params: dict[str, Any] = {}
            if self.era == PROTOCOL_MODERN:
                params["_meta"] = {META_PROTOCOL_KEY: PROTOCOL_MODERN}
            if cursor:
                params["cursor"] = cursor
            result = self._request("tools/list", params or None)
            page = result.get("tools", [])
            if not isinstance(page, list):
                raise MCPError(f"Invalid tools/list payload from '{self.server_name}'.", code="invalid_response")
            for item in page:
                if isinstance(item, dict) and isinstance(item.get("name"), str):
                    tools.append(item)
            next_cursor = result.get("nextCursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                break
            cursor = next_cursor
        return tools

    def call_tool(self, tool_name: str, arguments: dict[str, Any] | None = None) -> MCPCallResult:
        params: dict[str, Any] = {"name": tool_name, "arguments": dict(arguments or {})}
        if self.era == PROTOCOL_MODERN:
            params["_meta"] = {META_PROTOCOL_KEY: PROTOCOL_MODERN}
        result = self._request("tools/call", params)
        text, structured = _format_tool_output(result)
        is_error = bool(result.get("isError", False))
        return MCPCallResult(
            server=self.server_name,
            tool=tool_name,
            era=self.era,
            protocol_version=self.protocol_version,
            text=text,
            is_error=is_error,
            structured=structured,
        )

    def close(self) -> None:
        proc = self.proc
        self.proc = None
        self._reader = None
        if self._relay is not None:
            try:
                self._relay.close()
            except OSError:
                pass
            self._relay = None
        if proc is None:
            return
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        if proc.poll() is None:
            pgid = None
            if os.name != "nt":
                try:
                    candidate = os.getpgid(proc.pid)
                    if candidate > 1 and candidate != os.getpgrp():
                        pgid = candidate
                except OSError:
                    pgid = None
            try:
                if pgid is not None:
                    os.killpg(pgid, signal.SIGTERM)
                else:
                    proc.terminate()
                proc.wait(timeout=_CLOSE_GRACE_SEC)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    if pgid is not None:
                        os.killpg(pgid, signal.SIGKILL)
                    else:
                        proc.kill()
                    proc.wait(timeout=1.0)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=0.2)
            self._stderr_thread = None

    def __enter__(self) -> "MCPClientSession":
        self.connect()
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()
