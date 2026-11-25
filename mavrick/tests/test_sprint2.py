"""Shared AI budgets, modular HUD behavior, and Debian payload regressions."""

import os
import runpy
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mavrick.tests import IsolatedTestCase, TEST_ROOT, _real_popen
from mavrick import ai, cli, config, daemon, ipc, package_deb, ui
from mavrick.schema import ActionRequest, ActionType, ModelResponse, RiskLevel
from mavrick.ui import controller, window

_REAL_SOCKET = socket.socket


class TestAITimeoutSynchronization(IsolatedTestCase):
    def test_config_environment_and_cli_precedence(self):
        data = {"ai_timeouts": {"init_timeout": 5, "query_timeout": 17, "fallback_timeout": 9}}
        with patch.object(config, "load_user_config", return_value=data), patch.dict(
            os.environ, {"MAVRICK_QUERY_TIMEOUT": "23", "MAVRICK_FALLBACK_TIMEOUT": "11"}, clear=True
        ):
            timing = config.load_ai_timeouts(fallback_timeout=13)
            self.assertEqual(timing, config.AITimeouts(5, 23, 13))
            self.assertEqual(timing.client_timeout, timing.query_timeout + config.AI_TRANSPORT_HEADROOM)
            with self.assertRaisesRegex(ValueError, "query_timeout"):
                config.load_ai_timeouts(query_timeout=float("inf"))

    def test_cli_query_flags_set_client_budget_and_request_cap(self):
        args = ["mavrick", "arbitrary question", "--init-timeout", "3",
                "--query-timeout", "21", "--fallback-timeout", "8"]
        with patch.object(sys, "argv", args), patch.object(cli, "execute_cli_query", return_value=0) as execute:
            self.assertEqual(cli.main(), 0)
        execute.assert_called_once_with("arbitrary question", auto_confirm=False,
                                        timeouts=config.AITimeouts(3, 21, 8), query_limit=21)

    def test_daemon_advertises_effective_cli_budget_over_ipc(self):
        args = ["mavrickd", "--init-timeout", "8", "--query-timeout", "61"]
        with patch.object(sys, "argv", args), patch.object(daemon.signal, "signal"), \
             patch.object(daemon.MavrickDaemon, "start", autospec=True) as start, \
             patch.object(daemon, "get_active_socket_path", return_value=str(TEST_ROOT / "daemon.sock")):
            daemon.main()
        instance, = start.call_args.args
        self.assertEqual(instance.worker.init_timeout, 8)
        self.assertEqual(instance.worker.query_timeout, 61)

        def reply(*_args, **_kwargs):
            yield {"event": "done", "response": "fixture"}

        for requested_cap, expected in ((None, 61), (12, 12), (90, 61)):
            with self.subTest(requested_cap=requested_cap), patch.object(socket, "socket", _REAL_SOCKET), \
                 patch.object(instance.worker, "query_stream", side_effect=reply) as query:
                sender, receiver = socket.socketpair()
                thread = threading.Thread(target=instance._handle_client, args=(receiver,), daemon=True)
                with sender:
                    thread.start()
                    started = time.monotonic()
                    deadline = started + 2
                    request = {"prompt": "fixture", "negotiate_timeouts": True, "query_timeout": requested_cap}
                    ipc.write_frame(sender, request, deadline)
                    reader = ipc.FrameReader(sender)
                    self.assertEqual(reader.read(deadline), {"event": "timeouts", "query_timeout": expected})
                    self.assertEqual(reader.read(deadline)["event"], "done")
                    self.assertAlmostEqual(query.call_args.kwargs["deadline"] - started, expected, delta=0.25)
                thread.join(2)
                self.assertFalse(thread.is_alive())

    def test_client_uses_advertised_deadline_from_original_start(self):
        for override, expected in ((None, 161 + config.AI_TRANSPORT_HEADROOM), (106, 106)):
            with self.subTest(legacy_override=override), patch.object(ai, "FrameReader") as reader:
                terminal = {"event": "done", "response": "fixture"}
                read = reader.return_value.read
                read.side_effect = [{"event": "timeouts", "query_timeout": 61}, terminal]
                initial = override or 100 + config.AITimeouts().client_timeout
                events = ai._read_events(object(), 100, initial, None, negotiate=override is None)
                self.assertEqual(next(events), terminal)
                self.assertEqual([entry.args[0] for entry in read.call_args_list], [initial, expected])

    def test_fallback_uses_configured_init_and_total_budget(self):
        def reply(*_args, **_kwargs):
            yield {"event": "done", "response": '{"type":"message","message":"fixture"}'}

        with patch.object(daemon, "WarmAIWorker") as worker, patch.object(ai.time, "monotonic", return_value=100):
            worker.return_value.query_stream.side_effect = reply
            response = ai._query_oneshot("fixture", "", None, None, config.AITimeouts(7, 35, 19))
        self.assertEqual(response.message, "fixture")
        worker.assert_called_once_with(binary=ai.AGY_BIN, background_start=False, init_timeout=7, query_timeout=19)
        worker.return_value.query_stream.assert_called_once_with("fixture", "", cancelled=None, deadline=119)
        worker.return_value.terminate.assert_called_once()

    def test_provider_failure_is_not_returned_as_a_successful_turn(self):
        worker = daemon.WarmAIWorker(background_start=False)
        with patch.object(worker, "proc") as proc, patch.object(worker, "_reader") as reader, \
             patch.object(daemon, "write_frame"):
            proc.stdin.fileno.return_value = 1
            reader.read.return_value = {"event": "result", "result": {"status": "ERROR", "error": "authentication required"}}
            with self.assertRaisesRegex(RuntimeError, "authentication required"):
                next(worker._query_locked("fixture", None, time.monotonic() + 1, None))


class TestHUDCompatibility(IsolatedTestCase):
    def test_legacy_imports_and_module_entry_point(self):
        from mavrick.ui import MAVRICK_CSS, MavrickWindow, main

        self.assertIs(ui.MavrickWindow, MavrickWindow)
        self.assertIs(MavrickWindow, window.MavrickWindow)
        self.assertIs(main, window.main)
        self.assertIs(ui.client_send_toggle, controller.client_send_toggle)
        self.assertIn(b"@define-color mv_linen #efe7d2", MAVRICK_CSS)
        with patch.object(ui, "main") as launch:
            runpy.run_module("mavrick.ui", run_name="__main__")
            launch.assert_called_once_with()

    @unittest.skipUnless(ui.Gtk.init_check()[0], "GTK display unavailable")
    def test_ipc_startup_runs_off_the_gtk_thread(self):
        main_thread = threading.get_ident()
        startup_threads = []

        def connected():
            startup_threads.append(threading.get_ident())
            return True

        # PyGObject creates a local wakeup socketpair on entry to Gtk.main().
        with patch.object(socket, "socket", _REAL_SOCKET), \
             patch.object(ui, "client_send_toggle", side_effect=connected), \
             patch.object(ui, "MavrickWindow") as create:
            ui.main()
        create.assert_not_called()
        self.assertEqual(len(startup_threads), 1)
        self.assertNotEqual(startup_threads[0], main_thread)

    @unittest.skipUnless(ui.Gtk.init_check()[0], "GTK display unavailable")
    def test_cold_hud_releases_its_temporary_socket_on_close(self):
        hud = window.MavrickWindow()
        endpoint = TEST_ROOT / "runtime" / "sprint2-hud.sock"

        def close_after_show():
            self.assertTrue(endpoint.is_socket())
            ui.GLib.idle_add(hud.destroy)

        with patch.object(socket, "socket", _REAL_SOCKET), patch.object(ui, "MAVRICK_SOCK", str(endpoint)), \
             patch.object(ui, "MavrickWindow", return_value=hud) as create, \
             patch.object(hud, "show_window", side_effect=close_after_show):
            ui.main()
        create.assert_called_once_with()
        self.assertTrue(hud.state.closed)
        self.assertFalse(endpoint.exists())


@unittest.skipUnless(ui.Gtk.init_check()[0], "GTK display unavailable")
class TestHUDKeyboard(IsolatedTestCase):
    def setUp(self):
        self.window = ui.MavrickWindow()
        self.addCleanup(self.window.destroy)

    def test_escape_signals_cancellation_before_a_blocked_backend_releases(self):
        main_thread = threading.get_ident()
        model_started, cancel_entered, release_cancel = (threading.Event() for _ in range(3))
        worker_threads = []

        def query(_text, *, cancel_token):
            worker_threads.append(threading.get_ident())
            model_started.set()
            cancel_token.wait(2)
            return ModelResponse("message", message="Misleading late result")

        def cancel(_job_id):
            worker_threads.append(threading.get_ident())
            cancel_entered.set()
            release_cancel.wait(2)

        self.addCleanup(release_cancel.set)
        self.addCleanup(lambda: self.window.cancel_token and self.window.cancel_token.set())
        with patch.object(ui, "query_model", side_effect=query), \
             patch.object(self.window.executor, "cancel_current_job", side_effect=cancel) as stop, \
             patch.object(self.window, "record_history"):
            self.window.entry.set_text("Run a fixture query")
            self.window.on_submit(self.window.entry)
            self.assertTrue(model_started.wait(1))
            job_id = self.window.request_id
            self.assertTrue(self.window.handle_escape())
            self.assertTrue(self.window.cancel_token.is_set())
            self.assertTrue(self.window.is_processing)
            self.assertTrue(cancel_entered.wait(1))
            stop.assert_called_once_with(job_id)
            context = ui.GLib.MainContext.default()
            deadline = time.monotonic() + 1
            while self.window.is_processing and time.monotonic() < deadline:
                context.iteration(False)
                time.sleep(0.005)
            self.assertEqual(self.window.status_label.get_text(), "● Stopped")
            self.assertFalse(self.window.output_scroll.get_visible())
            self.assertFalse(release_cancel.is_set())
        self.assertEqual(len(worker_threads), 2)
        self.assertNotIn(main_thread, worker_threads)

    def test_command_browser_arrows_and_enter_fill_without_submitting(self):
        self.window.toggle_commands()
        search = self.window.command_search
        search.set_text("Spotify")
        press = SimpleNamespace(keyval=ui.Gdk.KEY_Down)
        self.assertTrue(self.window.on_command_search_key(search, press))
        search.emit("activate")
        self.assertEqual(self.window.entry.get_text(), "resume spotify")
        self.assertFalse(self.window.browse_box.get_visible())
        self.assertFalse(self.window.is_processing)

    def test_destructive_review_emphasizes_action_without_initial_focus(self):
        request = ActionRequest(ActionType.UNINSTALL_PACKAGE, {}, "Uninstall fixture", RiskLevel.PRIVILEGED)
        self.window.show_all()
        self.window.show_review_card(request)
        self.assertEqual(self.window.confirm_btn.get_label(), "Uninstall")
        self.assertTrue(self.window.confirm_btn.get_style_context().has_class("btn-destructive"))
        self.assertEqual(self.window.get_focus(), self.window.cancel_btn)


@unittest.skipUnless(shutil.which("dpkg-deb"), "dpkg-deb unavailable")
class TestDebPayload(IsolatedTestCase):
    def test_source_matches_every_runtime_module_in_the_built_deb(self):
        def deb_tools_only(command, *args, **kwargs):
            self.assertEqual(command[0], "dpkg-deb")
            return _real_popen(command, *args, **kwargs)

        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as directory:
            root = Path(directory)
            staging, deb, extracted = root / "staging", root / "mavrick.deb", root / "extracted"
            package_deb.create_control_file(staging, "2.0.0")
            package_deb.stage_payload_files(staging)
            with patch.object(subprocess, "Popen", side_effect=deb_tools_only):
                self.assertTrue(package_deb.build_deb_package(staging, deb))
                self.assertEqual(package_deb.verify_runtime_modules(deb), len(package_deb.runtime_manifest()))
                subprocess.run(["dpkg-deb", "-x", str(deb), str(extracted)], check=True)
            source = package_deb.SOURCE_DIR / "ui"
            packaged = extracted / "usr" / "lib" / "mavrick" / "ui"
            expected = {path.relative_to(source): path.read_bytes() for path in source.rglob("*.py")}
            actual = {path.relative_to(packaged): path.read_bytes() for path in packaged.rglob("*.py")}
            self.assertIn(Path("__main__.py"), expected)
            self.assertEqual(actual, expected)
            self.assertFalse((packaged.parent / "ui.py").exists())
            self.assertFalse(list(packaged.rglob("*.pyc")))
