"""Offline coverage for instant routing, HTTP providers, and release payloads."""

import contextlib
import io
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from mavrick.tests import IsolatedTestCase, TEST_ROOT, _real_popen
from mavrick import ai, cli, config, history, package_deb, router, ui
from mavrick.providers import create_provider
from mavrick.providers.base import ProviderError
from mavrick.tools import apps

_REAL_SOCKET = socket.socket
_APP = {"firefox": "firefox", "files": "cosmic-files", "com.spotify.client": "com.spotify.Client.desktop"}
_PLAN = {"type": "plan", "plan": [
    {"action": "get_system_health", "params": {}}, {"action": "get_power_profile", "params": {}},
]}


class _FixtureSocket(_REAL_SOCKET):
    def connect_ex(self, address):
        if address[0] != "127.0.0.1":
            raise AssertionError("Sprint 3 tests may only connect to the loopback fixture.")
        return super().connect_ex(address)


class _Handler(BaseHTTPRequestHandler):
    # HTTP/1.0 exercises read-to-close when http.client releases its socket.
    def log_message(self, _format, *args):
        pass

    def do_GET(self):
        self.server.answer(self, None)

    def do_POST(self):
        content = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.answer(self, content)

    def send_json(self, payload, status=200):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@contextlib.contextmanager
def _server(answer):
    with patch.object(socket, "socket", _FixtureSocket), ThreadingHTTPServer(("127.0.0.1", 0), _Handler) as server:
        server.answer = answer
        server.timeout = 1
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            thread.join(1)


def _envelope(name, content):
    return ({"message": {"content": content}} if name == "ollama" else
            {"choices": [{"message": {"content": content}}]})


class TestInstantRouter(IsolatedTestCase):
    def test_calculator_arithmetic(self):
        for expression, result in {
            "245 * 18.5": 4532.5, "-(2 + 3) ** 2": -25,
            "20 // 6 + 20 % 6": 5, "8 / 4": 2.0, "2 ** -3": 0.125,
        }.items():
            self.assertEqual(router.calculate(expression), result)

    def test_calculator_rejects_adversarial_expressions(self):
        for expression in ("__import__('os')", "1 .real", "(1, 2)", "2 << 5",
                           "True + 2", "9**9**9", "2**10000000", "1/0", "1***2",
                           "-1**.5 + (-1)**.5", "(" * 100 + "1" + ")" * 100, "1+" * 45 + "1"):
            with self.subTest(expression=expression), self.assertRaises(router.CalculationError):
                router.calculate(expression)

    def test_exact_routes_and_natural_language_fallthrough(self):
        with patch.object(router, "instant_app_index", return_value=_APP):
            self.assertEqual(router.route_local("Firefox"), router.LocalRoute("app", "firefox"))
            self.assertEqual(router.route_local("open files"), router.LocalRoute("app", "cosmic-files"))
            self.assertEqual(router.route_local("com.spotify.Client.desktop").kind, "app")
            self.assertEqual(router.route_local("245 * 18.5").value, "4532.5")
            self.assertEqual(router.route_local("history").kind, "history")
            self.assertEqual(router.route_local("undo").kind, "undo")
            for text in ("open firefox and clean downloads", "find large pdfs", "history of firefox", "firefox please"):
                self.assertIsNone(router.route_local(text))
            with patch.object(ai, "create_provider", side_effect=AssertionError("Instant route contacted provider")):
                self.assertEqual(ai.query_model("firefox").action_request.params, {"app_name": "firefox"})
                self.assertEqual(ai.query_model("undo").action_request.action_type.value, "undo")
                self.assertEqual(ai.query_model("history").response_type, "history")
                self.assertEqual(ai.query_model("245 * 18.5").source, "instant")

    def test_cached_index_reuses_discovery_and_stays_under_30_ms(self):
        with patch.object(apps, "_index", (None, 0, {})), \
             patch.object(apps, "desktop_app_ids", return_value={"firefox": "firefox.desktop"}) as discover, \
             patch.object(apps.shutil, "which", side_effect=lambda name: "/mock/" + name if name == "gtk-launch" else None):
            apps.instant_app_index()
            timings = []
            for _ in range(1000):
                start = time.perf_counter_ns()
                found = router.route_local("firefox", cached_only=True)
                timings.append((time.perf_counter_ns() - start) / 1_000_000)
                self.assertEqual(found.value, "firefox")
            self.assertLess(max(timings), 30)
            print(f"Cached router lookup: {len(timings)} samples, "
                  f"p50={sorted(timings)[len(timings)//2]:.3f} ms, max={max(timings):.3f} ms")
            discover.assert_called_once_with()
            key, _, cached = apps._index
            apps._index = (key, time.monotonic() - 1, cached)
            self.assertIsNone(router.route_local("firefox", cached_only=True))
            self.assertEqual(router.route_local("firefox").kind, "app")
            self.assertEqual(discover.call_count, 2)

    @unittest.skipUnless(ui.Gtk.init_check()[0], "GTK display unavailable")
    def test_hud_live_calculation_copies_without_an_ai_request(self):
        window = ui.MavrickWindow()
        self.addCleanup(window.destroy)
        window.show()
        with patch.object(ui, "query_model", side_effect=AssertionError("HUD called AI")) as query, \
             patch.object(ui.Gtk.Clipboard, "get") as clipboard, patch.object(window, "record_history"):
            window.entry.set_text("245 * 18.5")
            self.assertTrue(window.preview_label.is_visible())
            self.assertIn("4532.5", window.preview_label.get_text())
            window.on_submit(window.entry)
            clipboard.return_value.set_text.assert_called_once_with("4532.5", -1)
        query.assert_not_called()
        self.assertEqual(window.engine_tag.get_text(), "Instant")
        self.assertEqual(window.status_label.get_text(), "● Copied")


class TestHTTPProviders(IsolatedTestCase):
    def _settings(self, name, url, key=""):
        root = url + ("/api/v1" if name == "openai_compat" else "")
        return config.ProviderSettings(name, "mock-model", root, key)

    def test_structured_plans_and_exact_endpoints(self):
        for name, query_path, health_path in (
            ("ollama", "/api/chat", "/api/tags"),
            ("openai_compat", "/api/v1/chat/completions", "/api/v1/models"),
        ):
            seen = []

            def answer(handler, body):
                seen.append((handler.path, handler.headers.get("Authorization"), body))
                if body is None:
                    models = ({"models": [{"name": "mock-model:latest"}]} if name == "ollama" else
                              {"data": [{"id": "mock-model"}]})
                    handler.send_json(models)
                else:
                    handler.send_json(_envelope(name, json.dumps(_PLAN)))

            with self.subTest(provider=name), _server(answer) as url:
                settings = self._settings(name, url, "mock-credential")
                response = ai.query_model("find large pdfs", provider_settings=settings,
                                          timeouts=config.AITimeouts(query_timeout=1))
                health = ai.check_ai_health(settings, config.AITimeouts(query_timeout=1))
            self.assertEqual(response.response_type, "plan", response.validation_error)
            self.assertEqual(len(response.plan), 2)
            self.assertIsNone(response.action_request)
            self.assertEqual(response.source, name)
            self.assertEqual(health["status"], "connected_provider")
            self.assertEqual([row[0] for row in seen], [query_path, health_path])
            self.assertTrue(all(row[1] == "Bearer mock-credential" for row in seen))
            payload = seen[0][2]
            self.assertEqual(payload["messages"][1]["content"], "find large pdfs")
            if name == "ollama":
                self.assertEqual(payload["format"], "json")
            else:
                self.assertEqual(payload["response_format"], {"type": "json_object"})

    def test_malformed_json_rejected_and_fenced_json_recovered_by_both_providers(self):
        for name in ("ollama", "openai_compat"):
            replies = iter(["not JSON", '```json\n{"type":"message","message":"ok"}\n```'])

            def answer(handler, _body):
                handler.send_json(_envelope(name, next(replies)))

            with self.subTest(provider=name), _server(answer) as url:
                provider = create_provider(self._settings(name, url), config.AITimeouts(query_timeout=1))
                with self.assertRaises(ProviderError) as raised:
                    provider.query("fixture", "Respond in JSON")
                self.assertEqual(raised.exception.code, "invalid_response")
                self.assertEqual(json.loads(provider.query("fixture", "Respond in JSON").text)["message"], "ok")
                provider.close()

    def test_openai_retry_for_explicitly_unsupported_json_mode(self):
        seen = []

        def answer(handler, body):
            seen.append(body)
            if "response_format" in body:
                handler.send_json({"error": {"message": "Unsupported response_format"}}, 400)
            else:
                handler.send_json(_envelope("openai_compat", '{"type":"message","message":"ok"}'))

        with _server(answer) as url:
            provider = create_provider(self._settings("openai_compat", url), config.AITimeouts(query_timeout=1))
            self.assertEqual(json.loads(provider.query("fixture", "Respond in JSON").text)["message"], "ok")
        self.assertEqual(len(seen), 2)
        self.assertNotIn("response_format", seen[1])

    def test_absolute_deadline_under_trickled_response(self):
        for name in ("ollama", "openai_compat"):
            def answer(handler, _body):
                handler.send_response(200)
                handler.send_header("Content-Length", "20")
                handler.end_headers()
                for _ in range(20):
                    try:
                        handler.wfile.write(b" ")
                    except OSError:
                        break
                    time.sleep(0.03)

            with self.subTest(provider=name), _server(answer) as url:
                provider = create_provider(self._settings(name, url), config.AITimeouts(query_timeout=0.15))
                start = time.monotonic()
                with self.assertRaises(ProviderError) as raised:
                    provider.query("fixture", "")
                elapsed = time.monotonic() - start
                self.assertEqual(raised.exception.code, "timeout")
                self.assertGreaterEqual(elapsed, 0.13)
                self.assertLess(elapsed, 0.45)

    def test_cancel_midflight_closes_the_connection(self):
        for name in ("ollama", "openai_compat"):
            started, closed, stopped = (threading.Event() for _ in range(3))

            def answer(handler, _body):
                handler.send_response(200)
                handler.send_header("Content-Length", "100")
                handler.end_headers()
                handler.wfile.write(b"{")
                started.set()
                handler.connection.settimeout(1)
                try:
                    if not handler.connection.recv(1):
                        closed.set()
                except ConnectionResetError:
                    closed.set()

            with self.subTest(provider=name), _server(answer) as url:
                settings = self._settings(name, url)
                provider = create_provider(settings, config.AITimeouts(query_timeout=2))
                errors = []

                def request():
                    try:
                        provider.query("fixture", "", cancelled=stopped.is_set)
                    except ProviderError as error:
                        errors.append(error.code)

                thread = threading.Thread(target=request, daemon=True)
                thread.start()
                self.assertTrue(started.wait(1))
                start = time.monotonic()
                stopped.set()
                thread.join(0.5)
                self.assertFalse(thread.is_alive())
                self.assertLess(time.monotonic() - start, 0.5)
                self.assertTrue(closed.wait(0.5))
                self.assertEqual(errors, ["cancelled"])

    @unittest.skipUnless(ui.Gtk.init_check()[0], "GTK display unavailable")
    def test_hud_escape_stops_an_inflight_http_request(self):
        started, closed = threading.Event(), threading.Event()

        def answer(handler, _body):
            started.set()
            handler.connection.settimeout(1)
            if not handler.connection.recv(1):
                closed.set()

        window = ui.MavrickWindow()
        self.addCleanup(window.destroy)
        with _server(answer) as url, patch.dict(os.environ, {
            "MAVRICK_PROVIDER": "ollama", "MAVRICK_BASE_URL": url, "MAVRICK_MODEL": "mock-model",
        }), patch.object(window, "record_history"):
            window.entry.set_text("find large pdfs")
            window.on_submit(window.entry)
            self.assertTrue(started.wait(1))
            self.assertTrue(window.handle_escape())
            context = ui.GLib.MainContext.default()
            deadline = time.monotonic() + 1
            while window.is_processing and time.monotonic() < deadline:
                context.iteration(False)
                time.sleep(0.005)
            self.assertFalse(window.is_processing)
            self.assertEqual(window.status_label.get_text(), "● Stopped")
            self.assertTrue(closed.wait(0.5))

    def test_redacts_config_and_environment_keys_in_provider_errors_and_history(self):
        key = "fixture-raw-provider-credential"
        config_key = "another-raw-config-key"
        fake_sk = "sk-proj-private_secret-05"
        fake_router = "sk-or-v1-private_secret-05"
        text = f"failure {key} {config_key} {fake_sk} {fake_router} Bearer reflected-token"

        def answer(handler, _body):
            if handler.path.endswith("/models"):
                handler.send_json({"error": text}, 401)
            else:
                handler.send_json(_envelope("openai_compat", json.dumps({"type": "bogus " + text})))

        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as folder:
            root = Path(folder)
            local_config = root / "config.json"
            with patch.object(config, "CONFIG_FILE", str(local_config)), \
                 patch.object(history, "HISTORY_FILE", str(root / "history.json")), \
                 patch.object(history, "AUDIT_LOG_FILE", str(root / "audit.log")), \
                 patch.dict(os.environ, {"MAVRICK_API_KEY": key}):
                config.save_user_config({"ai_provider": {"api_key": config_key}})
                self.assertEqual(stat.S_IMODE(local_config.stat().st_mode), 0o600)
                local_config.chmod(0o644)
                with _server(answer) as url:
                    settings = config.load_provider_settings(provider="openai_compat", model="mock-model", base_url=url)
                    self.assertEqual(stat.S_IMODE(local_config.stat().st_mode), 0o600)
                    response = ai.query_model("find large pdfs", provider_settings=settings,
                                              timeouts=config.AITimeouts(query_timeout=1))
                    self.assertEqual(response.response_type, "error")
                    messages = [response.validation_error, response.raw_text, ai.check_ai_health(settings)["error"]]
                history.append_history(text, summary=text, job_id="fixture")
                history.append_audit_log({"detail": text, "nested": {"key": key, "api_key": config_key}})
                messages.extend([(root / name).read_text() for name in ("history.json", "audit.log")])
        for secret in (key, config_key, fake_sk, fake_router, "reflected-token"):
            for message in messages:
                self.assertNotIn(secret, message)
        self.assertIn("REDACTED_SECRET", response.validation_error)

    def test_doctor_reports_health_and_does_not_expose_credentials(self):
        key = "doctor-raw-test-credential"

        def answer(handler, _body):
            handler.send_json({"error": key}, 401)

        with _server(answer) as url, patch.dict(os.environ, {"MAVRICK_API_KEY": key}), \
             patch.object(sys, "argv", ["mavrick", "--doctor", "--provider", "openai_compat",
                                       "--base-url", url, "--model", "mock-model", "--query-timeout", "3"]), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            cli.main()
        text = output.getvalue()
        self.assertIn("Provider: OpenAI-Compatible", text)
        self.assertIn("Timeouts: init 10s | query 3s | fallback 20s", text)
        self.assertIn("Daemon socket: missing", text)
        self.assertIn("Local router cache: warm", text)
        self.assertIn("Provider returned HTTP 401.", text)
        self.assertNotIn(key, text)
        self.assertNotIn(url, text)

    def test_fallback_keys_are_scoped_to_their_origin(self):
        with patch.object(config, "load_user_config", return_value={}), patch.dict(os.environ, {
            "OPENROUTER_API_KEY": "fixture-router", "OPENAI_API_KEY": "fixture-openai",
        }, clear=True):
            default = config.load_provider_settings(provider="openai_compat")
            scoped = config.load_provider_settings(provider="openai_compat", base_url="https://openrouter.ai/api/v1")
            local = config.load_provider_settings(provider="openai_compat", base_url="http://127.0.0.1:1234")
            self.assertEqual(default.api_key, "fixture-openai")
            self.assertEqual(scoped.api_key, "fixture-router")
            self.assertEqual(local.api_key, "")
            self.assertNotIn(scoped.api_key, repr(scoped))


@unittest.skipUnless(shutil.which("dpkg-deb"), "dpkg-deb unavailable")
class TestSprint3Packaging(IsolatedTestCase):
    def test_deb_contains_router_and_every_provider_module(self):
        source = package_deb.SOURCE_DIR

        def dpkg_only(command, *args, **kwargs):
            self.assertEqual(command[0], "dpkg-deb")
            return _real_popen(command, *args, **kwargs)

        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as folder:
            root = Path(folder)
            staging, deb, extracted = root / "staging", root / "mavrick.deb", root / "unpacked"
            package_deb.create_control_file(staging, "2.0.0")
            package_deb.stage_payload_files(staging)
            with patch.object(subprocess, "Popen", side_effect=dpkg_only):
                self.assertTrue(package_deb.build_deb_package(staging, deb))
                subprocess.run(["dpkg-deb", "-x", str(deb), str(extracted)], check=True)
            packaged = extracted / "usr" / "lib" / "mavrick"
            modules = [source / "router.py", *(source / "providers").rglob("*.py")]
            self.assertTrue((packaged / "providers" / "ollama.py").is_file())
            for path in modules:
                self.assertEqual(path.read_bytes(), (packaged / path.relative_to(source)).read_bytes())
