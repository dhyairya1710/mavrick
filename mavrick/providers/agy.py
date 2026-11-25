"""Antigravity warm worker, Unix IPC client, and one-shot fallback."""

from contextlib import contextmanager
import logging
import os
import select
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from typing import Any

from ..config import (AGY_BIN, AI_TRANSPORT_HEADROOM, DEFAULT_AGY_MODEL, DAEMON_SOCKET,
                      FALLBACK_DAEMON_SOCKET, AITimeouts, configured_api_keys, load_ai_timeouts,
                      validate_timeout)
from ..ipc import (MAX_FRAME_BYTES, FrameError, FrameReader, RequestCancelled, check_wait, connect_local,
                   local_endpoint, write_frame)
from ..platform import get_platform
from ..policy import scrub_secrets
from .agy_pipes import WorkerPipeRelay
from .base import Cancelled, ProviderError, ProviderHealth, ProviderResult, StreamCallback

DEFAULT_MODEL = DEFAULT_AGY_MODEL
STDERR_LIMIT = 8 * 1024


def _worker_result(frame):
    result = frame.get("result")
    if not isinstance(result, dict):
        raise FrameError("Invalid AI worker result.")
    status = result.get("status", "SUCCESS")
    if status != "SUCCESS":
        detail = result.get("error") or result.get("response") or status
        raise RuntimeError(scrub_secrets(f"Antigravity returned {detail}", configured_api_keys()))
    return result


class WarmAIWorker:
    def __init__(self, model=DEFAULT_MODEL, *, init_timeout=None, query_timeout=None,
                 binary=None, background_start=True):
        timing = load_ai_timeouts(init_timeout=init_timeout, query_timeout=query_timeout)
        self.model = model
        self.init_timeout = timing.init_timeout
        self.query_timeout = timing.query_timeout
        self.binary = binary
        self.proc = None
        self.is_ready = False
        self.starting = False
        self.conversation_id = None
        self.last_used = time.time()
        self._operation = threading.Lock()
        self._lifecycle = threading.Lock()
        self._interrupt = threading.Event()
        self._stderr_lock = threading.Lock()
        self._stderr = bytearray()
        self._stderr_stop = None
        self._stderr_thread = None
        self._reader = None
        self._input = None
        self._relay = None
        if background_start:
            self.start_background()

    def start_background(self):
        threading.Thread(target=self._start_worker, daemon=True).start()

    def _cancelled(self, cancelled):
        return self._interrupt.is_set() or (cancelled is not None and cancelled())

    @contextmanager
    def _serialized(self, deadline, cancelled):
        while not self._operation.acquire(timeout=check_wait(deadline, cancelled)):
            pass
        try:
            check_wait(deadline, cancelled)
            yield
        finally:
            self._operation.release()

    def _start_worker(self):
        # The background start and foreground recovery take the same lock.
        deadline = time.monotonic() + self.init_timeout
        try:
            with self._serialized(deadline, self._interrupt.is_set):
                self._start_worker_locked(deadline, self._interrupt.is_set)
        except RequestCancelled:
            pass
        except (OSError, ValueError, EOFError, RuntimeError) as error:
            logging.warning("Mavrick worker startup: %s", error)

    def _drain_stderr(self, source, tail, stopped):
        while not stopped.is_set():
            try:
                if isinstance(source, int):
                    ready, _, _ = select.select([source], [], [], 0.1)
                    if not ready:
                        continue
                    data = os.read(source, 4096)
                else:
                    data = source.read(4096)
            except (OSError, ValueError):
                return
            if not data:
                return
            with self._stderr_lock:
                tail.extend(data)
                del tail[:-STDERR_LIMIT]

    def _failure(self, message):
        with self._stderr_lock:
            tail = bytes(self._stderr)
        stderr = scrub_secrets(tail.decode("utf-8", errors="replace"), configured_api_keys()).strip()
        return message + (f"\nRecent agy stderr:\n{stderr}" if stderr else "")

    def _start_worker_locked(self, deadline, cancelled):
        if self.is_ready and self.proc is not None and self.proc.poll() is None:
            return
        self._terminate_locked()
        self.starting = True
        self._stderr = bytearray()
        try:
            relay_pipes = get_platform().capabilities.agy_socket_relay
            executable = "agy.exe" if relay_pipes else "agy"
            binary = self.binary or shutil.which(executable) or AGY_BIN
            argv = [binary, "--model", self.model, "--effort", "low", "--disable-slash-commands",
                    "--input-format", "stream-json", "--output-format", "stream-json"]
            process_options = ({"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
                               if relay_pipes else {"start_new_session": True})
            self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, bufsize=0, **process_options)
            if relay_pipes:
                self._relay = WorkerPipeRelay(self.proc.stdin, self.proc.stdout)
                self._input = self._relay.input
                self._reader = FrameReader(self._relay.output)
                stderr = self.proc.stderr
            else:
                self._input = self.proc.stdin.fileno()
                os.set_blocking(self._input, False)
                self._reader = FrameReader(self.proc.stdout.fileno())
                stderr = self.proc.stderr.fileno()
            self._stderr_stop = threading.Event()
            self._stderr_thread = threading.Thread(
                target=self._drain_stderr, args=(stderr, self._stderr, self._stderr_stop), daemon=True)
            self._stderr_thread.start()
            while True:
                message = self._reader.read(deadline, cancelled)
                if message.get("event") == "init":
                    self.conversation_id = message.get("conversation_id")
                    self.is_ready = True
                    self.last_used = time.time()
                    return
                if message.get("event") == "error":
                    raise RuntimeError(scrub_secrets(str(message.get("error", "Initialization failed.")),
                                                     configured_api_keys()))
                if message.get("event") == "result":
                    _worker_result(message)
                    raise RuntimeError("Antigravity ended before initializing.")
        except RequestCancelled:
            self._terminate_locked()
            raise
        except (OSError, ValueError, EOFError, RuntimeError) as error:
            self._terminate_locked()
            raise RuntimeError(self._failure(f"AI worker initialization failed: {error}")) from error
        finally:
            self.starting = False

    def _terminate_locked(self):
        self.is_ready = False
        self.conversation_id = None
        proc, self.proc = self.proc, None
        self._reader = None
        self._input = None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
            proc.wait(timeout=0.25)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=0.5)
        finally:
            if self._relay is not None:
                self._relay.close()
                self._relay = None
            if self._stderr_thread is not None:
                # If the child already exited, give the drainer a chance to read the tail.
                self._stderr_thread.join(timeout=0.15)
                self._stderr_stop.set()
                self._stderr_thread.join(timeout=0.15)
            self._stderr_thread = None
            self._stderr_stop = None
            for channel in (proc.stdin, proc.stdout, proc.stderr):
                channel.close()

    def restart(self, cancelled=None):
        with self._lifecycle:
            self._interrupt.set()
            with self._operation:
                self._terminate_locked()
                self._interrupt.clear()
                self._start_worker_locked(time.monotonic() + self.init_timeout,
                                          lambda: self._cancelled(cancelled))

    def terminate(self):
        with self._lifecycle:
            self._interrupt.set()
            with self._operation:
                self._terminate_locked()

    def query_stream(self, prompt, system_prompt=None, *, cancelled=None, deadline=None, query_timeout=None):
        """Yield events for one turn, discarding the worker after an interrupted turn."""
        budget = self.query_timeout if query_timeout is None else min(self.query_timeout, query_timeout)
        lifetime = time.monotonic() + budget
        if deadline is not None:
            lifetime = min(lifetime, deadline)

        def interrupted():
            return self._cancelled(cancelled)

        try:
            with self._serialized(lifetime, interrupted):
                init_limit = min(lifetime, time.monotonic() + self.init_timeout)
                self._start_worker_locked(init_limit, interrupted)
                try:
                    yield from self._query_locked(prompt, system_prompt, lifetime, interrupted)
                except (RequestCancelled, GeneratorExit):
                    # A later client must never read the rest of this turn.
                    self._terminate_locked()
                    raise
                except (OSError, ValueError, EOFError, RuntimeError) as error:
                    self._terminate_locked()
                    yield {"event": "error", "error": self._failure(f"AI worker query failed: {error}")}
        except RequestCancelled:
            raise
        except (OSError, ValueError, EOFError, RuntimeError) as error:
            yield {"event": "error", "error": str(error)}

    def _query_locked(self, prompt, system_prompt, deadline, cancelled):
        text = f"{system_prompt}\n\nUser: {prompt}" if system_prompt else prompt
        message = {"event": "user", "message": {"content": [{"type": "text", "text": text}]}}
        endpoint = self._input if self._input is not None else self.proc.stdin.fileno()
        write_frame(endpoint, message, deadline, cancelled)
        self.last_used = time.time()
        chunks = []
        total = 0
        while True:
            data = self._reader.read(deadline, cancelled)
            event = data.get("event")
            if event == "step_update":
                step = data.get("step_update", {})
                delta = step.get("text_delta") if isinstance(step, dict) else None
                if isinstance(delta, str) and delta:
                    total += len(delta.encode("utf-8"))
                    if total > MAX_FRAME_BYTES:
                        raise FrameError("AI response exceeds the size limit.")
                    chunks.append(delta)
                    yield {"event": "delta", "text": delta}
            elif event == "result":
                result = _worker_result(data)
                yield {"event": "done", "response": result.get("response") or "".join(chunks),
                       "status": result.get("status", "SUCCESS"), "duration": result.get("duration_seconds", 0)}
                return
            elif event == "error":
                error = scrub_secrets(str(data.get("error", "Unknown error")), configured_api_keys())
                yield {"event": "error", "error": self._failure(error)}
                return


def daemon_socket(paths: tuple[str, str]) -> str | None:
    return next((path for path in paths if os.path.exists(path)), None)


def probe_daemon(paths: tuple[str, str], *, reader_type=FrameReader, model=None) -> tuple[str, str, float]:
    address = ""
    try:
        endpoint = local_endpoint(paths)
        if endpoint is None:
            return "missing", "", 0
        address = endpoint.address if isinstance(endpoint.address, str) else "loopback"
        deadline = time.monotonic() + 1.5
        with connect_local(endpoint, deadline) as connection:
            request = {"action": "ping", "include_model": True} if model else {"action": "ping"}
            write_frame(connection, endpoint.request(request), deadline)
            response = reader_type(connection).read(deadline)
        if model and response.get("model") != model:
            return "model_mismatch", address, 0
        status = response.get("status")
        memory = response.get("rss_mb", 0)
        return (status if status in ("ready", "starting", "idle") else "invalid",
                address, memory if isinstance(memory, (int, float)) else 0)
    except (OSError, EOFError, ValueError):
        return "unreachable", address, 0


def read_events(connection, started, deadline, cancelled, *, negotiate, reader_type=FrameReader, model=None):
    reader = reader_type(connection)
    event = reader.read(deadline, cancelled)
    if model and (event.get("event") != "timeouts" or event.get("model") != model):
        raise OSError("Warm agy daemon cannot verify the requested model.")
    if event.get("event") == "timeouts":
        try:
            budget = validate_timeout(event.get("query_timeout"), "query_timeout")
        except ValueError as error:
            raise FrameError("Invalid daemon timeout advertisement.") from error
        if negotiate:
            deadline = started + budget + AI_TRANSPORT_HEADROOM
    else:
        yield event
    while True:
        yield reader.read(deadline, cancelled)


def collect_events(events: Iterator[dict[str, Any]], stream_callback: StreamCallback | None) -> ProviderResult:
    chunks = []
    total = 0
    for event in events:
        kind = event.get("event")
        if kind == "delta":
            text = event.get("text", "")
            if not isinstance(text, str):
                raise FrameError("Invalid AI response delta.")
            total += len(text.encode("utf-8"))
            if total > MAX_FRAME_BYTES:
                raise FrameError("AI response exceeds the size limit.")
            chunks.append(text)
            if stream_callback:
                stream_callback(text)
        elif kind == "done":
            response = event.get("response")
            if response is not None and not isinstance(response, str):
                raise FrameError("Invalid AI response text.")
            return ProviderResult(response if response is not None else "".join(chunks))
        elif kind == "error":
            detail = scrub_secrets(str(event.get("error", "AI Daemon Error")), configured_api_keys())
            raise ProviderError("transport", detail)
    raise EOFError("AI connection closed before completing a response.")


def query_oneshot(prompt, system_prompt, stream_callback, cancelled, timing, *, binary=AGY_BIN, model=DEFAULT_MODEL):
    # The daemon module still exposes the worker used by legacy callers.
    from ..daemon import WarmAIWorker

    model_option = {"model": model} if model != DEFAULT_MODEL else {}
    worker = WarmAIWorker(binary=binary, background_start=False, init_timeout=timing.init_timeout,
                          query_timeout=timing.fallback_timeout, **model_option)
    deadline = time.monotonic() + timing.fallback_timeout
    stream = worker.query_stream(prompt, system_prompt, cancelled=cancelled, deadline=deadline)
    try:
        return collect_events(stream, stream_callback)
    finally:
        stream.close()
        worker.terminate()


class AgyProvider:
    def __init__(self, model: str, timeouts: AITimeouts, *, binary: str = AGY_BIN,
                 paths: tuple[str, str] = (DAEMON_SOCKET, FALLBACK_DAEMON_SOCKET),
                 reader_type=FrameReader, daemon_timeout: float | None = None,
                 query_limit: float | None = None, force_model: bool = False):
        self.model = model
        self.timeouts = timeouts
        self.binary = binary
        self.paths = paths
        self.reader_type = reader_type
        self.daemon_timeout = daemon_timeout
        self.query_limit = query_limit
        self.force_model = force_model

    def _fallback_binary(self):
        if os.path.exists(self.binary) and os.access(self.binary, os.X_OK):
            return self.binary
        host = get_platform().name
        return shutil.which("agy.exe" if host == "windows" else "agy") if host != "linux" else None

    def health_check(self) -> ProviderHealth:
        model = self.model if self.force_model or self.model != DEFAULT_MODEL else None
        status, path, memory = probe_daemon(self.paths, reader_type=self.reader_type, model=model)
        if status == "ready":
            return ProviderHealth("connected_daemon", "Connected (mavrickd ready)", rss_mb=memory, socket=path)
        if status == "starting":
            return ProviderHealth("starting", "AI worker initializing", socket=path)
        if binary := self._fallback_binary():
            try:
                result = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=2.0)
                if result.returncode == 0:
                    return ProviderHealth("fallback_cli", "agy CLI available (--version succeeded)", binary=binary)
            except (OSError, subprocess.SubprocessError):
                pass
        detail = ("The warm daemon could not verify the requested model and agy CLI is unreachable."
                  if status == "model_mismatch" else "mavrickd.service is inactive and agy CLI is unreachable.")
        return ProviderHealth("offline", "Disconnected (AI Offline)", detail)

    def query(self, prompt: str, system_prompt: str, *, cancelled: Cancelled | None = None,
              stream_callback: StreamCallback | None = None) -> ProviderResult:
        try:
            return self._query(prompt, system_prompt, cancelled, stream_callback)
        except RequestCancelled as error:
            raise ProviderError("cancelled", str(error)) from error

    def _query(self, prompt: str, system_prompt: str, cancelled: Cancelled | None,
               stream_callback: StreamCallback | None) -> ProviderResult:
        daemon_error = None
        try:
            endpoint = local_endpoint(self.paths)
        except (OSError, ValueError) as error:
            raise ProviderError("configuration", str(error)) from error
        if endpoint:
            budget = self.timeouts.client_timeout if self.daemon_timeout is None else self.daemon_timeout
            started = time.monotonic()
            deadline = started + budget
            try:
                with connect_local(endpoint, deadline, cancelled) as connection:
                    request = {"action": "query", "prompt": prompt, "system_prompt": system_prompt,
                               "negotiate_timeouts": True}
                    if self.query_limit is not None:
                        request["query_timeout"] = self.query_limit
                    model = self.model if self.force_model or self.model != DEFAULT_MODEL else None
                    if model:
                        request["model"] = model
                    write_frame(connection, endpoint.request(request), deadline, cancelled)
                    events = read_events(connection, started, deadline, cancelled,
                                         negotiate=self.daemon_timeout is None, reader_type=self.reader_type, model=model)
                    return collect_events(events, stream_callback)
            except FrameError as error:
                raise ProviderError("invalid_response", str(error)) from error
            except (OSError, EOFError) as error:
                detail = scrub_secrets(str(error), configured_api_keys())
                daemon_error = f"Mavrick daemon connection failed: {detail}"
                logging.warning("%s. Attempting CLI fallback.", daemon_error)

        if cancelled and cancelled():
            raise RequestCancelled("AI request cancelled.")
        if binary := self._fallback_binary():
            try:
                return query_oneshot(prompt, system_prompt, stream_callback, cancelled, self.timeouts,
                                     binary=binary, model=self.model)
            except (OSError, EOFError, FrameError) as error:
                detail = scrub_secrets(str(error), configured_api_keys())
                raise ProviderError("transport", f"CLI invocation error: {detail}") from error
        raise ProviderError("transport", daemon_error or
                            "MAVRICK AI engine is unreachable. Please verify mavrickd is running or agy CLI is installed.")

    def close(self) -> None:
        """A one-shot worker and daemon connections close within each query."""
