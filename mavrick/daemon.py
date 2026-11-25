#!/usr/bin/env python3
"""Keep one Antigravity stream-JSON worker warm behind a local Unix socket."""

import argparse
import logging
import os
import secrets
import select
import shutil
import signal
import socket
import stat
import sys
import threading
import time

if not __package__:
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from mavrick.config import (AI_TRANSPORT_HEADROOM, DAEMON_SOCKET, FALLBACK_DAEMON_SOCKET,
                            MAVRICK_LOG_DIR, MAVRICK_STATE_DIR, XDG_RUNTIME_DIR, ensure_secure_storage,
                            configured_api_keys, load_ipc_settings, load_provider_settings, validate_timeout)
from mavrick.ipc import (FrameError, FrameReader, RequestCancelled, authorized_request, connect_local,
                         peer_disconnected, prepare_descriptor_directory, publish_tcp_descriptor,
                         read_tcp_descriptor, write_frame)
from mavrick.platform import get_platform
from mavrick.policy import scrub_secrets
from mavrick.providers.agy import DEFAULT_MODEL, STDERR_LIMIT, WarmAIWorker


PID_FILE = os.path.join(MAVRICK_STATE_DIR, "mavrickd.pid")
LOG_FILE = os.path.join(MAVRICK_LOG_DIR, "mavrickd.log")
MAX_CONCURRENT_CONNECTIONS = 16
INITIAL_FRAME_TIMEOUT = 2.0


def get_active_socket_path():
    """Choose the runtime socket when the runtime directory is writable."""
    ensure_secure_storage()
    if get_platform().name == "macos":
        os.makedirs(XDG_RUNTIME_DIR, mode=0o700, exist_ok=True)
        if not get_platform().is_private(os.lstat(XDG_RUNTIME_DIR), directory=True):
            raise OSError("The macOS runtime directory must be private.")
    if os.path.isdir(XDG_RUNTIME_DIR) and os.access(XDG_RUNTIME_DIR, os.W_OK):
        return DAEMON_SOCKET
    return FALLBACK_DAEMON_SOCKET


def _startup_lock(endpoint_path):
    return get_platform().lock_file(os.fspath(endpoint_path) + ".lock")


class MavrickDaemon:
    def __init__(self, socket_path=None, *, init_timeout=None, query_timeout=None, model=None,
                 transport=None, descriptor=None, max_connections: int = MAX_CONCURRENT_CONNECTIONS):
        if type(max_connections) is not int or max_connections < 1:
            raise ValueError("max_connections must be a positive integer.")
        ipc = load_ipc_settings(transport=transport, descriptor=descriptor)
        self.transport = ipc.transport
        self.descriptor = ipc.descriptor
        self._token = ""
        self.socket_path = socket_path or (get_active_socket_path() if self.transport == "unix-socket" else None)
        self._endpoint_path = self.descriptor if self.transport == "tcp-token" else self.socket_path
        settings = load_provider_settings()
        model = model or (settings.model if settings.name == "agy" else DEFAULT_MODEL)
        self.worker = WarmAIWorker(model=model, init_timeout=init_timeout, query_timeout=query_timeout,
                                   background_start=False)
        self.running = False
        self._stopping = threading.Event()
        self.server_sock = None
        self._socket_identity = None
        self._owns_pid_file = False
        self.max_connections = max_connections
        self._conn_slots = threading.BoundedSemaphore(max_connections)

    def _remove_stale_socket(self):
        try:
            info = os.lstat(self.socket_path)
        except FileNotFoundError:
            return True
        if not stat.S_ISSOCK(info.st_mode):
            raise OSError(f"Refusing to replace a non-socket at {self.socket_path}.")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.5)
            try:
                probe.connect(os.fspath(self.socket_path))
            except ConnectionRefusedError:
                os.unlink(self.socket_path)
                return True
            except FileNotFoundError:
                return True
        return False

    def _tcp_endpoint_is_active(self):
        try:
            endpoint = read_tcp_descriptor(self.descriptor)
            deadline = time.monotonic() + 0.5
            with connect_local(endpoint, deadline) as connection:
                write_frame(connection, endpoint.request({"action": "ping"}), deadline)
                return FrameReader(connection).read(deadline).get("service") == "mavrickd"
        except (OSError, EOFError, FrameError):
            return False

    def _bind_listener(self):
        if self.transport == "tcp-token":
            if self._tcp_endpoint_is_active():
                return None
            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server_sock = server
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                server.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            server.bind(("127.0.0.1", 0))
            server.listen(10)
            self._token = secrets.token_hex(32)
            publish_tcp_descriptor(self.descriptor, server.getsockname()[1], self._token)
            return server
        if not self._remove_stale_socket():
            return None
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server_sock = server
        server.bind(os.fspath(self.socket_path))
        info = os.lstat(self.socket_path)
        self._socket_identity = info.st_dev, info.st_ino
        os.chmod(self.socket_path, 0o700)
        server.listen(10)
        # A custom listener must not overwrite the standard daemon's PID file.
        if self.socket_path in (DAEMON_SOCKET, FALLBACK_DAEMON_SOCKET):
            fd = os.open(PID_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as pid_file:
                pid_file.write(str(os.getpid()))
            self._owns_pid_file = True
        return server

    def start(self):
        ensure_secure_storage()
        if self.transport == "tcp-token":
            prepare_descriptor_directory(self.descriptor)
        try:
            with _startup_lock(self._endpoint_path):
                server = self._bind_listener()
                if server is None:
                    logging.info("Mavrick daemon already listening on %s", self._endpoint_path)
                    return
                self.running = True
                self.worker.start_background()
            logging.info("Mavrick daemon listening via %s", self.transport)
            while not self._stopping.is_set():
                readable, _, _ = select.select([server], [], [], 0.2)
                if readable:
                    conn, _ = server.accept()
                    if not self._conn_slots.acquire(blocking=False):
                        try:
                            conn.close()
                        except OSError:
                            pass
                        continue
                    try:
                        threading.Thread(target=self._handle_client, args=(conn,), daemon=True).start()
                    except Exception:
                        self._conn_slots.release()
                        try:
                            conn.close()
                        except OSError:
                            pass
                        raise
        except (OSError, ValueError):
            if not self._stopping.is_set():
                raise
        finally:
            if self.server_sock is not None:
                self.stop()

    def _handle_client(self, conn):
        try:
            with conn:
                conn.setblocking(False)
                try:
                    tcp = self.transport == "tcp-token"
                    req = FrameReader(conn).read(
                        time.monotonic() + INITIAL_FRAME_TIMEOUT,
                        self._stopping.is_set,
                    )
                    if tcp and not authorized_request(req, self._token):
                        write_frame(conn, {"event": "error", "code": "auth", "error": "IPC authentication failed."},
                                    time.monotonic() + 0.5, self._stopping.is_set)
                        return
                    action = req.get("action", "query")
                    lost_peer = peer_disconnected(conn, tcp=tcp)

                    def disconnected():
                        return self._stopping.is_set() or lost_peer()

                    def respond(message):
                        write_frame(conn, message, time.monotonic() + AI_TRANSPORT_HEADROOM, disconnected)

                    if action == "ping":
                        rss_mb = 0.0
                        try:
                            import psutil
                            rss_mb = round(psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024), 1)
                        except (ImportError, OSError):
                            pass
                        status = "ready" if self.worker.is_ready else "starting" if self.worker.starting else "idle"
                        response = {"status": status, "rss_mb": rss_mb, "service": "mavrickd"}
                        if req.get("include_model") is True:
                            response["model"] = self.worker.model
                        respond(response)
                    elif action == "query":
                        prompt, system_prompt = req.get("prompt", ""), req.get("system_prompt")
                        if not isinstance(prompt, str) or (system_prompt is not None and not isinstance(system_prompt, str)):
                            raise FrameError("AI request prompt and instructions must be text.")
                        if req.get("model") is not None and req["model"] != self.worker.model:
                            respond({"event": "error", "code": "model_mismatch",
                                     "error": "Requested agy model differs from the warm worker."})
                            return
                        limit = self.worker.query_timeout
                        if req.get("query_timeout") is not None:
                            limit = min(limit, validate_timeout(req["query_timeout"], "query_timeout"))
                        deadline = time.monotonic() + limit
                        if req.get("negotiate_timeouts") is True:
                            response = {"event": "timeouts", "query_timeout": limit}
                            if req.get("model") is not None:
                                response["model"] = self.worker.model
                            respond(response)
                        stream = self.worker.query_stream(prompt, system_prompt, cancelled=disconnected,
                                                          deadline=deadline)
                        try:
                            for item in stream:
                                respond(item)
                        finally:
                            stream.close()
                    elif action == "restart":
                        self.worker.restart(cancelled=disconnected)
                        respond({"status": "restarted"})
                except (EOFError, RequestCancelled):
                    pass  # An empty listener probe or a client that stopped listening.
                except (OSError, ValueError, RuntimeError) as error:
                    message = scrub_secrets(str(error), configured_api_keys())
                    logging.debug("Mavrick client error: %s", message)
                    try:
                        write_frame(conn, {"event": "error", "error": message}, time.monotonic() + 0.5)
                    except (OSError, EOFError, FrameError):
                        pass
        finally:
            self._conn_slots.release()

    def stop(self):
        self._stopping.set()
        self.running = False
        if self.server_sock is None:
            return
        with _startup_lock(self._endpoint_path):
            server, self.server_sock = self.server_sock, None
            if server is not None:
                server.close()
            try:
                if self.transport == "tcp-token":
                    if self._token and read_tcp_descriptor(self.descriptor).token == self._token:
                        os.unlink(self.descriptor)
                else:
                    info = os.lstat(self.socket_path)
                    if self._socket_identity == (info.st_dev, info.st_ino):
                        os.unlink(self.socket_path)
                        if self._owns_pid_file:
                            os.unlink(PID_FILE)
            except (FileNotFoundError, FrameError):
                pass
        self.worker.terminate()


def main():
    parser = argparse.ArgumentParser(description="MAVRICK AI Intelligence Daemon (mavrickd)")
    parser.add_argument("--socket", help="Custom socket path")
    parser.add_argument("--transport", choices=("unix-socket", "tcp-token"), help="Local IPC transport")
    parser.add_argument("--descriptor", help="Absolute path to the private TCP runtime descriptor")
    parser.add_argument("--init-timeout", type=float, help="Worker startup deadline in seconds")
    parser.add_argument("--query-timeout", type=float, help="Worker query deadline in seconds")
    parser.add_argument("--model", help="Antigravity warm worker model")
    parser.add_argument("--log-level", default="INFO", help="Logging level")
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format="[%(asctime)s] [%(levelname)s] %(message)s")
    try:
        daemon = MavrickDaemon(args.socket, init_timeout=args.init_timeout,
                               query_timeout=args.query_timeout, model=args.model,
                               transport=args.transport, descriptor=args.descriptor)
    except ValueError as error:
        parser.error(str(error))

    def sig_handler(_sig, _frame):
        daemon._stopping.set()

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)
    daemon.start()


if __name__ == "__main__":
    main()
