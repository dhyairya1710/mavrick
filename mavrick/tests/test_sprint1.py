"""Sprint 1 regressions using temporary paths, real local I/O, and a fake agy."""

import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from unittest.mock import patch

from mavrick.tests import IsolatedTestCase, _real_popen
from mavrick import ai, cli, daemon, history, ipc, package_deb
from mavrick.executor import ActionExecutor, approve_action
from mavrick.schema import RiskLevel, validate_action_params
from mavrick.tools import files
from mavrick.tools.system import clean_cache


_REAL_SOCKET = socket.socket
_REAL_WHICH = shutil.which
_SOURCE = Path(__file__).resolve().parents[1]
_FAKE_AGY = """
import json, os, sys, time

def emit(data):
    print(json.dumps(data, ensure_ascii=False), flush=True)

if os.environ.get("MAVRICK_FAKE_AGY_REJECT_INIT") == "1":
    sys.stderr.write("fake startup stalled\\n")
    sys.stderr.flush()
    time.sleep(60)
sys.stderr.buffer.write(b"A" * (256 * 1024) + b"initial stderr marker\\n")
sys.stderr.flush()
emit({"event": "init", "conversation_id": str(os.getpid())})
for line in sys.stdin:
    msg = json.loads(line)
    text = msg["message"]["content"][0]["text"]
    if text == "crash":
        sys.stderr.buffer.write(b"B" * (256 * 1024) + b"fake crash marker\\n")
        sys.stderr.flush()
        sys.exit(12)
    if text == "hang":
        emit({"event": "step_update", "step_update": {"text_delta": "working"}})
        time.sleep(60)
    response = json.dumps({"type": "message", "message": text}, ensure_ascii=False)
    emit({"event": "result", "result": {"response": response}})
"""


class _FragmentedSocket:
    """Deliver one byte at a time, independent of how the kernel groups writes."""
    def __init__(self, connection):
        self.connection = connection

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.connection.close()

    def recv(self, amount, flags=0):
        return self.connection.recv(min(amount, 1), flags)


class TestSprint1(IsolatedTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="mavrick-sprint1-fixture-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def _agy(self):
        target = self.root / "agy"
        target.write_text(f"#!{sys.executable}\n" + textwrap.dedent(_FAKE_AGY))
        target.chmod(0o700)
        return target

    def _only_fake_agy(self, script, invocations=None):
        def spawn(argv, *args, **kwargs):
            self.assertEqual(argv[0], str(script))
            if invocations is not None:
                invocations.append(argv)
            return _real_popen(argv, *args, **kwargs)

        return patch.object(subprocess, "Popen", side_effect=spawn)

    def _start_daemon(self, socket_path, **kwargs):
        server = daemon.MavrickDaemon(socket_path, **kwargs)
        thread = threading.Thread(target=server.start, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.stop)
        deadline = time.monotonic() + 2
        while not server.running and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(server.running)
        return server

    def test_warm_child_recovers_from_crash_and_restarts_without_deadlock(self):
        script = self._agy()
        worker = daemon.WarmAIWorker(binary=str(script), background_start=False,
                                     init_timeout=2, query_timeout=2)
        self.addCleanup(worker.terminate)
        with self._only_fake_agy(script):
            first = list(worker.query_stream("hello"))[-1]
            first_pid = worker.proc.pid
            self.assertEqual(first["event"], "done")
            failed = list(worker.query_stream("crash"))[-1]
            self.assertEqual(failed["event"], "error")
            self.assertIn("fake crash marker", failed["error"])
            self.assertLessEqual(len(worker._stderr), daemon.STDERR_LIMIT)
            self.assertIsNone(worker.proc)
            self.assertEqual(list(worker.query_stream("after crash"))[-1]["event"], "done")
            recovered_pid = worker.proc.pid
            self.assertNotEqual(first_pid, recovered_pid)
            worker.restart()
            self.assertNotEqual(recovered_pid, worker.proc.pid)
            self.assertEqual(list(worker.query_stream("after restart"))[-1]["event"], "done")

    def test_initialization_and_query_deadlines_reap_the_worker(self):
        script = self._agy()
        worker = daemon.WarmAIWorker(binary=str(script), background_start=False,
                                     init_timeout=0.35, query_timeout=0.35)
        self.addCleanup(worker.terminate)
        with self._only_fake_agy(script):
            with patch.dict(os.environ, {"MAVRICK_FAKE_AGY_REJECT_INIT": "1"}):
                failed = list(worker.query_stream("startup"))[-1]
            self.assertIn("deadline exceeded", failed["error"])
            self.assertIn("fake startup stalled", failed["error"])
            self.assertIsNone(worker.proc)
            self.assertEqual(list(worker.query_stream("ready"))[-1]["event"], "done")
            stalled = list(worker.query_stream("hang"))
            self.assertEqual([event["event"] for event in stalled], ["delta", "error"])
            self.assertIn("deadline exceeded", stalled[-1]["error"])
            self.assertIsNone(worker.proc)

    def test_concurrent_restart_interrupts_the_active_turn(self):
        script = self._agy()
        worker = daemon.WarmAIWorker(binary=str(script), background_start=False,
                                     init_timeout=2, query_timeout=20)
        self.addCleanup(worker.terminate)
        active = threading.Event()
        interrupted = threading.Event()

        def request():
            try:
                for event in worker.query_stream("hang"):
                    if event["event"] == "delta":
                        active.set()
            except ipc.RequestCancelled:
                interrupted.set()

        with self._only_fake_agy(script):
            thread = threading.Thread(target=request, daemon=True)
            thread.start()
            self.assertTrue(active.wait(2))
            old_pid = worker.proc.pid
            worker.restart()
            thread.join(2)
            self.assertTrue(interrupted.is_set())
            self.assertFalse(thread.is_alive())
            self.assertNotEqual(worker.proc.pid, old_pid)
            self.assertEqual(list(worker.query_stream("new turn"))[-1]["event"], "done")

    def test_disconnect_and_client_cancel_drop_the_turn(self):
        script = self._agy()
        path = self.root / "mavrickd.sock"
        real_which = lambda name: str(script) if name == "agy" else _REAL_WHICH(name)
        with patch.object(socket, "socket", new=_REAL_SOCKET), \
             patch.object(daemon.shutil, "which", side_effect=real_which), self._only_fake_agy(script), \
             patch.object(ai, "DAEMON_SOCKET", str(path)), patch.object(ai, "AGY_BIN", str(self.root / "absent")), \
             patch.object(ai, "build_system_prompt", return_value=""):
            server = self._start_daemon(path, init_timeout=2, query_timeout=20)
            token = threading.Event()
            requested = ai.query_model("hang", lambda _: token.set(), cancel_token=token, daemon_timeout=2)
            self.assertEqual(requested.response_type, "error")
            self.assertIn("cancelled", requested.validation_error)
            self.assertEqual(ai.query_model("next turn", daemon_timeout=2).message, "next turn")

            # A client can also close the connection without using the Python API.
            old_pid = server.worker.proc.pid
            with _REAL_SOCKET(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.connect(str(path))
                deadline = time.monotonic() + 2
                ipc.write_frame(connection, {"action": "query", "prompt": "hang"}, deadline)
                self.assertEqual(ipc.FrameReader(connection).read(deadline)["event"], "delta")
            self.assertEqual(ai.query_model("after disconnect", daemon_timeout=2).message, "after disconnect")
            self.assertNotEqual(server.worker.proc.pid, old_pid)
            server.stop()

    def test_server_decodes_split_utf8_and_rejects_oversized_request(self):
        path = self.root / "unused.sock"
        server = daemon.MavrickDaemon(path)
        received = []

        def reply(prompt, *_args, **_kwargs):
            received.append(prompt)
            yield {"event": "done", "response": prompt}

        with patch.object(socket, "socket", new=_REAL_SOCKET), \
             patch.object(server.worker, "query_stream", side_effect=reply):
            sender, receiver = socket.socketpair()
            with sender:
                thread = threading.Thread(target=server._handle_client, args=(_FragmentedSocket(receiver),))
                thread.start()
                deadline = time.monotonic() + 2
                ipc.write_frame(sender, {"prompt": "Grüße 🌙"}, deadline)
                sender.shutdown(socket.SHUT_WR)
                self.assertEqual(ipc.FrameReader(sender).read(deadline)["response"], "Grüße 🌙")
                thread.join(2)
                self.assertFalse(thread.is_alive())

            sender, receiver = socket.socketpair()
            with sender:
                thread = threading.Thread(target=server._handle_client, args=(receiver,))
                thread.start()
                sender.settimeout(2)
                sender.sendall(b"x" * (ipc.MAX_FRAME_BYTES + 1))
                result = ipc.FrameReader(sender).read(time.monotonic() + 2)
                self.assertEqual(result["event"], "error")
                self.assertIn("size limit", result["error"])
                thread.join(2)
                self.assertFalse(thread.is_alive())
        self.assertEqual(received, ["Grüße 🌙"])

    def test_ai_client_decodes_split_utf8_and_rejects_oversized_response(self):
        path = self.root / "response.sock"
        result = {"event": "done", "response": json.dumps(
            {"type": "message", "message": "γειά 🌙"}, ensure_ascii=False)}

        def serve(listener):
            for oversized in (False, True):
                connection, _ = listener.accept()
                with connection:
                    deadline = time.monotonic() + 2
                    ipc.FrameReader(connection).read(deadline)
                    if oversized:
                        connection.sendall(b"x" * (ipc.MAX_FRAME_BYTES + 1))
                    else:
                        ipc.write_frame(connection, result, deadline)

        with _REAL_SOCKET(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(path))
            listener.listen(2)
            listener.settimeout(2)
            thread = threading.Thread(target=serve, args=(listener,), daemon=True)
            thread.start()

            def socket_for_client(*args, **kwargs):
                return _FragmentedSocket(_REAL_SOCKET(*args, **kwargs))

            with patch.object(ai.socket, "socket", side_effect=socket_for_client), \
                 patch.object(ai, "DAEMON_SOCKET", str(path)), patch.object(ai, "AGY_BIN", str(self.root / "absent")):
                self.assertEqual(ai.query_model("unicode fixture query", daemon_timeout=2).message, "γειά 🌙")
            with patch.object(ai.socket, "socket", new=_REAL_SOCKET), \
                 patch.object(ai, "DAEMON_SOCKET", str(path)), patch.object(ai, "AGY_BIN", str(self.root / "absent")):
                oversized = ai.query_model("large fixture query", daemon_timeout=2)
                self.assertEqual(oversized.response_type, "error")
                self.assertIn("size limit", oversized.validation_error)
            thread.join(2)
            self.assertFalse(thread.is_alive())

    def test_ai_client_read_deadline_reports_the_stalled_connection(self):
        path = self.root / "silent.sock"
        with _REAL_SOCKET(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(path))
            listener.listen(1)
            with patch.object(ai.socket, "socket", new=_REAL_SOCKET), \
                 patch.object(ai, "DAEMON_SOCKET", str(path)), patch.object(ai, "AGY_BIN", str(self.root / "absent")):
                result = ai.query_model("stalled daemon fixture", daemon_timeout=0.2)
        self.assertEqual(result.response_type, "error")
        self.assertIn("deadline exceeded", result.validation_error)

    def test_frame_at_exact_limit_and_rejects_truncated_data(self):
        with patch.object(socket, "socket", new=_REAL_SOCKET):
            a, b = socket.socketpair()
            with a, b:
                payload = b'{"x":"y"}'
                a.sendall(payload + b"\n")
                self.assertEqual(ipc.FrameReader(b, max_bytes=len(payload)).read(time.monotonic() + 1),
                                 {"x": "y"})
                a.sendall(payload)
                a.shutdown(socket.SHUT_WR)
                with self.assertRaisesRegex(ipc.FrameError, "closed during"):
                    ipc.FrameReader(b).read(time.monotonic() + 1)

    def test_single_instance_preserves_live_listener_and_replaces_stale_socket(self):
        script = self._agy()
        path = self.root / "instance.sock"
        with patch.object(socket, "socket", new=_REAL_SOCKET), \
             patch.object(daemon.shutil, "which", side_effect=lambda name: str(script) if name == "agy" else _REAL_WHICH(name)), \
             self._only_fake_agy(script) as launch:
            with _REAL_SOCKET(socket.AF_UNIX, socket.SOCK_STREAM) as stale:
                stale.bind(str(path))
            first = self._start_daemon(path, init_timeout=2, query_timeout=1)
            live = path.stat()
            duplicate = daemon.MavrickDaemon(path)
            duplicate.start()
            self.assertFalse(duplicate.running)
            duplicate.stop()
            self.assertEqual((live.st_dev, live.st_ino), (path.stat().st_dev, path.stat().st_ino))
            with _REAL_SOCKET(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.connect(str(path))
                deadline = time.monotonic() + 2
                ipc.write_frame(connection, {"action": "query", "prompt": "healthy"}, deadline)
                self.assertEqual(ipc.FrameReader(connection).read(deadline)["event"], "done")
            launch.assert_called_once()
            first.stop()
            self.assertFalse(path.exists())

    def test_daemon_refuses_to_unlink_a_non_socket(self):
        path = self.root / "occupied"
        path.write_text("keep this file")
        server = daemon.MavrickDaemon(path)
        with self.assertRaisesRegex(OSError, "non-socket"):
            server.start()
        self.assertEqual(path.read_text(), "keep this file")

    def test_fallback_transmits_prompt_and_context_by_stdin_only(self):
        script = self._agy()
        calls = []
        absent = self.root / "no.sock"
        with self._only_fake_agy(script, calls), patch.object(ai, "AGY_BIN", str(script)), \
             patch.object(ai, "DAEMON_SOCKET", str(absent)), patch.object(ai, "FALLBACK_DAEMON_SOCKET", str(absent)), \
             patch.object(ai, "build_system_prompt", return_value="private context fixture"):
            result = ai.query_model("private request fixture")
        self.assertEqual(result.response_type, "message")
        self.assertIn("private context fixture", result.message)
        self.assertIn("private request fixture", result.message)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("private", " ".join(calls[0]))
        self.assertEqual(calls[0][-4:], ["--input-format", "stream-json", "--output-format", "stream-json"])

    def test_cache_refuses_symlinked_root_and_skips_symlinked_entries(self):
        actual = self.root / "unrelated"
        actual.mkdir()
        protected = actual / "keep"
        protected.write_text("outside cache")
        link = self.root / "linked-cache"
        link.symlink_to(actual, target_is_directory=True)
        absent = str(self.root / "absent")
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run):
                ok, report, details = clean_cache(dry_run, str(link), absent)
                self.assertFalse(ok)
                self.assertEqual(details["failed"], 1)
                self.assertEqual(details["deleted"], 0)
                self.assertIn("without following symlinks", report)
                self.assertTrue(protected.exists())

        thumbnail = self.root / "thumbnails"
        thumbnail.mkdir()
        thumbnail.joinpath("nested-link").symlink_to(actual, target_is_directory=True)
        thumbnail.joinpath("file-link").symlink_to(protected)
        ok, _, details = clean_cache(False, str(thumbnail), absent)
        self.assertTrue(ok)
        self.assertEqual(details["skipped"], 2)
        self.assertTrue(thumbnail.joinpath("file-link").is_symlink())
        self.assertTrue(protected.exists())

    def test_cache_partial_failure_and_apt_scope_report_exact_counts(self):
        thumbnail = self.root / "thumb"
        apt = self.root / "archives"
        thumbnail.mkdir()
        apt.mkdir()
        for name in ("deleted", "denied"):
            thumbnail.joinpath(name).write_text(name)
        for name in ("download.deb", "lock", "index"):
            apt.joinpath(name).write_text(name)
        apt.joinpath("partial").mkdir()
        apt.joinpath("partial/active.deb").write_text("active")
        unlink = os.unlink

        def remove(name, *, dir_fd=None):
            if name == "denied":
                raise PermissionError("fixture permission denial")
            return unlink(name, dir_fd=dir_fd)

        with patch("mavrick.tools.system.os.unlink", side_effect=remove):
            ok, report, details = clean_cache(False, str(thumbnail), str(apt))
        self.assertFalse(ok)
        self.assertEqual((details["deleted"], details["skipped"], details["failed"]), (2, 3, 1))
        self.assertIn("fixture permission denial", report)
        self.assertIn("Incomplete", report)
        self.assertTrue(apt.joinpath("lock").is_file())
        self.assertTrue(apt.joinpath("partial/active.deb").is_file())
        self.assertTrue(thumbnail.joinpath("denied").is_file())

    def test_cache_rejects_broad_roots_and_executor_reports_partial_results(self):
        cache = self.root / "home" / ".cache"
        cache.mkdir(parents=True)
        keep = cache / "unrelated"
        keep.write_text("keep")
        with patch.dict(os.environ, {"HOME": str(cache.parent)}):
            ok, report, details = clean_cache(False, str(cache), str(self.root / "absent"))
        self.assertFalse(ok)
        self.assertEqual((details["deleted"], details["failed"]), (0, 1))
        self.assertIn("outside a cache subdirectory", report)
        self.assertTrue(keep.is_file())

        executor = ActionExecutor()
        _, _, request = validate_action_params("clean_cache", {"dry_run": False})
        review = executor.execute(request)
        self.assertEqual(review.status, "requires_confirmation")
        with patch("mavrick.executor.clean_cache",
                   return_value=(False, "Cache Clean Incomplete", {"deleted": 1, "failed": 1})):
            result = executor.execute(request, approval=approve_action(review.details["action"]))
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.exit_code, 1)

    def test_cache_confirmation_calls_deletion_irreversible(self):
        ok, _, action = validate_action_params("clean_cache", {"dry_run": False})
        self.assertTrue(ok)
        self.assertEqual(action.risk_level, RiskLevel.DESTRUCTIVE)
        self.assertIn("irreversible", action.impact.lower())
        self.assertIn("Permanently delete", action.summary)

    def test_recursive_secrets_in_history_audit_context_and_model_prompt(self):
        opaque = "opaque-fixture-key"
        secret = "sk-" + "s" * 24
        nested = {"rows": [{"api_key": opaque}, ("credentials: safe", secret)]}
        paths = {name: str(self.root / f"{name}.json") for name in ("HISTORY_FILE", "AUDIT_LOG_FILE", "CONTEXT_FILE")}
        with patch.multiple(history, **paths):
            history_path = Path(paths["HISTORY_FILE"])
            history_path.write_text(json.dumps([{"query": "legacy", "metadata": nested}]))
            history.append_history("new query")
            history.append_audit_log({"details": nested})
            history.save_context({"last_verified_change": {"details": nested}})
            for filename in paths.values():
                stored = Path(filename).read_text()
                self.assertNotIn(opaque, stored)
                self.assertNotIn(secret, stored)
                self.assertIn("[REDACTED_SECRET]", stored)

            # Legacy context can already contain unredacted nested data on disk.
            Path(paths["CONTEXT_FILE"]).write_text(json.dumps({"last_verified_change": nested}))
            prompt = ai.build_system_prompt()
            self.assertNotIn(opaque, prompt)
            self.assertNotIn(secret, prompt)
            self.assertIn("[REDACTED_SECRET]", prompt)
        sanitized = history.redact_secrets(nested)
        self.assertIsInstance(sanitized["rows"][1], tuple)
        self.assertEqual(nested["rows"][0]["api_key"], opaque)

    def _rename_race(self, occupy_source=False):
        source = self.root / "source.txt"
        source.write_text("reviewed")
        destination = self.root / "destination.txt"
        review = files.review_files("move_files", {"sources": [str(source)], "destination": str(destination)})
        rename = files._rename_no_replace
        parked = self.root / "reviewed-entry.txt"
        swapped = False

        def swap(source_fd, source_name, dest_fd, dest_name):
            nonlocal swapped
            if not swapped:
                swapped = True
                source.rename(parked)
                source.write_text("replacement")
                rename(source_fd, source_name, dest_fd, dest_name)
                if occupy_source:
                    source.write_text("new source occupant")
            else:
                rename(source_fd, source_name, dest_fd, dest_name)

        journal = self.root / "undo.json"
        with patch.object(files, "UNDO_STATE_FILE", str(journal)), \
             patch.object(files, "_rename_no_replace", side_effect=swap):
            ok, report, details = files.apply_file_review(review, [0])
        self.assertFalse(ok)
        self.assertIn("Source identity changed", report)
        self.assertEqual(details["executed_count"], 0)
        self.assertFalse(details["undo_available"])
        self.assertEqual(json.loads(journal.read_text()), {"batches": [], "pending": None})
        self.assertEqual(parked.read_text(), "reviewed")
        return source, destination, report

    def test_rename_race_restores_replacement_without_recording_undo(self):
        source, destination, report = self._rename_race()
        self.assertEqual(source.read_text(), "replacement")
        self.assertFalse(destination.exists())
        self.assertIn("returned to the source path", report)

    def test_rename_race_rolls_back_only_when_original_name_is_free(self):
        source, destination, report = self._rename_race(occupy_source=True)
        self.assertEqual(source.read_text(), "new source occupant")
        self.assertEqual(destination.read_text(), "replacement")
        self.assertIn("Could not return the replacement", report)

    def test_undo_journal_fsyncs_the_directory_after_replace(self):
        real_fsync, real_replace = os.fsync, os.replace
        events = []

        def sync(fd):
            kind = "directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file"
            events.append(kind)
            real_fsync(fd)

        def replace(source, destination):
            events.append("replace")
            real_replace(source, destination)

        journal = self.root / "undo.json"
        with patch.object(files, "UNDO_STATE_FILE", str(journal)), \
             patch.object(files.os, "fsync", side_effect=sync), \
             patch.object(files.os, "replace", side_effect=replace):
            files._write_undo([], pending={"id": "pending"})
        self.assertEqual(events, ["file", "replace", "directory"])
        self.assertEqual(json.loads(journal.read_text())["pending"]["id"], "pending")

    def test_shortcut_installer_passes_a_literal_path_as_an_argument(self):
        text = (_SOURCE / "install.sh").read_text()
        start = text.index('python3 -c "\nimport os\nimport sys\n')
        end = text.index('" "$SHORTCUTS_FILE"', start) + len('" "$SHORTCUTS_FILE"')
        snippet = text[start:end]
        shortcut = self.root / "path with 'quote' and $var" / "custom"
        with _real_popen(["/bin/bash", "-c", snippet], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         env={**os.environ, "SHORTCUTS_FILE": str(shortcut)}, text=True) as proc:
            _, error = proc.communicate(timeout=2)
        self.assertEqual(proc.returncode, 0, error)
        self.assertIn('Spawn("mavrick")', shortcut.read_text())

    def test_packaged_uninstall_prints_apt_removal_without_prompting(self):
        output = io.StringIO()
        with patch.object(cli, "find_spec", return_value=None), patch("builtins.input") as confirm, \
             contextlib.redirect_stdout(output):
            cli.run_uninstall()
        self.assertIn("sudo apt remove mavrick", output.getvalue())
        confirm.assert_not_called()

    def test_source_uninstall_keeps_existing_confirmation(self):
        with patch.object(cli, "find_spec", return_value=object()), patch("builtins.input", return_value="no"), \
             patch("mavrick.install.uninstall") as uninstall, contextlib.redirect_stdout(io.StringIO()):
            cli.run_uninstall()
        uninstall.assert_not_called()

    def test_debian_staging_includes_framing_module_and_version_matches(self):
        package_deb.stage_payload_files(self.root)
        self.assertEqual((self.root / "usr/lib/mavrick/ipc.py").read_bytes(), (_SOURCE / "ipc.py").read_bytes())
        manifest = (_SOURCE / "pyproject.toml").read_text()
        self.assertRegex(manifest, rf'(?m)^version = "{package_deb.get_mavrick_version()}"$')
