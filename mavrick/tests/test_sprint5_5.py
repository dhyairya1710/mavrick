"""Offline tests for Sprint 5.5: multi-app instant launch, provider presets, and HUD dark/light themes."""

import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from mavrick.tests import IsolatedTestCase, TEST_ROOT
from mavrick import ai, cli, config, core, history, router, ui
from mavrick.executor import ActionExecutor
from mavrick.providers import create_provider
from mavrick.providers.ollama import OllamaProvider
from mavrick.providers.openai_compat import OpenAICompatibleProvider
from mavrick.schema import ActionType, RiskLevel
from mavrick.tools import apps
from mavrick.ui import styles
from mavrick.workflows import workflow_catalog


_DESKTOP_FIXTURES = {
    "cosmic-files": "cosmic-files.desktop",
    "org.videolan.vlc": "org.videolan.VLC.desktop",
    "code": "code.desktop",
    "firefox": "org.mozilla.firefox.desktop",
    "cosmic-term": "cosmic-term.desktop",
}
_BIN_FIXTURES = {
    "gtk-launch": "/mock/gtk-launch",
    "cosmic-files": "/mock/cosmic-files",
    "code": "/mock/code",
    "cosmic-term": "/mock/cosmic-term",
}


class TestMultiAppInstantRouter(IsolatedTestCase):
    def setUp(self):
        self.executor = ActionExecutor()
        apps._index = (None, 0.0, {})
        apps._norm_cache = (None, {}, ())

    def _warm_mock_index(self):
        with patch.object(apps, "desktop_app_ids", return_value=_DESKTOP_FIXTURES), \
             patch.object(apps.shutil, "which", side_effect=_BIN_FIXTURES.get):
            return apps.instant_app_index()

    def test_multi_app_instant_launch_latency_and_zero_ai_calls(self):
        self._warm_mock_index()
        timings = []
        for _ in range(1000):
            start = time.perf_counter_ns()
            route = router.route_local("open files, vlc and vscode", cached_only=True)
            timings.append((time.perf_counter_ns() - start) / 1_000_000)
            self.assertEqual(route.kind, "multi_app")
            self.assertEqual(route.apps, ("cosmic-files", "vlc", "code"))
        self.assertLess(max(timings), 30.0)
        print(f"Multi-app router lookup: {len(timings)} samples, "
              f"p50={sorted(timings)[500]:.3f} ms, max={max(timings):.3f} ms")

        with patch.object(apps, "desktop_app_ids", return_value=_DESKTOP_FIXTURES), \
             patch.object(apps.shutil, "which", side_effect=_BIN_FIXTURES.get), \
             patch.object(ai, "create_provider", side_effect=AssertionError("Multi-app route called AI")), \
             patch.object(apps, "launch_command", return_value=(True, "Launch requested", {})) as launch:
            response = ai.query_model("open files, vlc and vscode")
            self.assertEqual(response.source, "instant")
            self.assertEqual(response.response_type, "action")
            self.assertEqual(response.action_request.action_type, ActionType.OPEN_APPS)
            self.assertEqual(response.action_request.risk_level, RiskLevel.READ_ONLY)

            result = self.executor.execute(response.action_request)
            self.assertEqual(result.status, "success")
            self.assertEqual(result.summary, "Launched: Files, VLC, VS Code")
            self.assertEqual(launch.call_count, 3)

            via_core = core.execute_jarvis("launch files + vlc & vs code")
            self.assertEqual(via_core["status"], "success")
            self.assertEqual(via_core["message"], "Launched: Files, VLC, VS Code")

    def test_alias_fallbacks_and_fuzzy_app_matching(self):
        alt_desktops = {
            "org.gnome.nautilus": "org.gnome.Nautilus.desktop",
            "com.visualstudio.code": "com.visualstudio.code.desktop",
            "org.mozilla.firefox": "org.mozilla.firefox.desktop",
            "alacritty": "Alacritty.desktop",
        }
        alt_bins = {"gtk-launch": "/mock/gtk-launch"}
        with patch.object(apps, "desktop_app_ids", return_value=alt_desktops), \
             patch.object(apps.shutil, "which", side_effect=alt_bins.get):
            index = apps.instant_app_index()
            self.assertEqual(index["files"], "nautilus")
            self.assertEqual(index["file manager"], "nautilus")
            self.assertEqual(index["vscode"], "code")
            self.assertEqual(index["vs code"], "code")
            self.assertEqual(index["terminal"], "alacritty")
            self.assertEqual(index["browser"], "firefox")

            route = router.route_local("start file manager, browser and terminal", cached_only=True)
            self.assertEqual(route.kind, "multi_app")
            self.assertEqual(route.apps, ("nautilus", "firefox", "alacritty"))
            self.assertEqual(apps.resolve_cached_app("firefx", index), "firefox")
            self.assertEqual(apps.resolve_cached_app("vscod", index), "code")

    def test_partial_match_reports_both_launched_and_missing_targets(self):
        self._warm_mock_index()
        with patch.object(apps, "desktop_app_ids", return_value=_DESKTOP_FIXTURES), \
             patch.object(apps.shutil, "which", side_effect=_BIN_FIXTURES.get), \
             patch.object(ai, "create_provider", side_effect=AssertionError("Partial multi-app route called AI")), \
             patch.object(apps, "launch_command", return_value=(True, "Launch requested", {})) as launch:
            response = ai.query_model("open files, vlc and foobar")
            self.assertEqual(response.source, "instant")
            result = self.executor.execute(response.action_request)
        self.assertEqual(result.status, "success")
        self.assertIn("Launched: Files, VLC", result.summary)
        self.assertIn("Not found: foobar", result.summary)
        self.assertIn("Not found: foobar", result.stdout)
        self.assertEqual(launch.call_count, 2)

    def test_disambiguation_and_saved_workflow_priority(self):
        self._warm_mock_index()
        for query in (
            "open firefox and clean downloads",
            "open files in downloads and sort pdfs",
            "open files and claude app in split screen",
        ):
            self.assertIsNone(router.route_local(query, cached_only=True), f"Should fall through: {query}")

        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as folder:
            catalog_path = Path(folder) / "workflows.json"
            catalog_path.write_text(json.dumps({
                "workflows": [{
                    "name": "files",
                    "description": "Mutating saved workflow sharing an app name",
                    "steps": [{"action": "set_volume", "params": {"mute": True}}],
                }]
            }))
            with patch.object(config, "WORKFLOWS_FILE", str(catalog_path)):
                workflow_catalog(refresh=True)
                self.addCleanup(lambda: workflow_catalog(refresh=True))
                route = router.route_local("run files", cached_only=True)
                self.assertEqual(route.kind, "workflow")
                self.assertEqual(route.value, "files")
                with patch.object(ai, "create_provider", side_effect=AssertionError("Saved workflow called AI")):
                    response = ai.query_model("run files")
                self.assertEqual(response.source, "instant_workflow")
                gate = self.executor.execute(response.plan)
                self.assertEqual(gate.status, "workflow_review")


class TestFastPresetsAndDoctor(IsolatedTestCase):
    def test_presets_host_scoped_keys_and_secret_redaction(self):
        expected = {
            "groq": ("openai/gpt-oss-20b", "https://api.groq.com/openai/v1", "GROQ_API_KEY", OpenAICompatibleProvider),
            "cerebras": ("gpt-oss-120b", "https://api.cerebras.ai/v1", "CEREBRAS_API_KEY", OpenAICompatibleProvider),
            "openrouter": ("openai/gpt-4o-mini", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", OpenAICompatibleProvider),
            "ollama": ("llama3.2", "http://127.0.0.1:11434", None, OllamaProvider),
        }
        env_keys = {
            "GROQ_API_KEY": "gsk-fixture-secret-value-1",
            "CEREBRAS_API_KEY": "csk-fixture-secret-value-2",
            "OPENROUTER_API_KEY": "sk-or-fixture-secret-value-3",
            "OPENAI_API_KEY": "sk-openai-fixture-secret-4",
        }
        with patch.object(config, "load_user_config", return_value={"ai_provider": {"name": "agy", "model": "gemini-3.8-flash-low"}}), \
             patch.dict(os.environ, env_keys, clear=True):
            for name, (model, url, env_var, provider_cls) in expected.items():
                settings = config.load_provider_settings(provider=name)
                self.assertEqual(settings.name, name)
                self.assertEqual(settings.model, model)
                self.assertEqual(settings.base_url, url)
                self.assertEqual(settings.api_key, env_keys[env_var] if env_var else "")
                self.assertIsInstance(create_provider(settings, config.AITimeouts(query_timeout=1)), provider_cls)

            # Host scoping must block sending vendor keys to third-party or loopback URLs.
            loopback = config.load_provider_settings(provider="groq", base_url="http://127.0.0.1:1234/v1")
            other_host = config.load_provider_settings(provider="groq", base_url="https://api.cerebras.ai/v1")
            self.assertEqual(loopback.api_key, "")
            self.assertEqual(other_host.api_key, env_keys["CEREBRAS_API_KEY"])

            override = config.load_provider_settings(provider="groq", base_url="http://127.0.0.1:1234/v1")
            self.assertEqual(override.api_key, "")
            with patch.dict(os.environ, {**env_keys, "MAVRICK_API_KEY": "mavrick-override-secret"}):
                overridden = config.load_provider_settings(provider="groq", base_url="http://127.0.0.1:1234/v1")
                self.assertEqual(overridden.api_key, "mavrick-override-secret")

            redacted = history.redact_secrets({"msg": " ".join(env_keys.values())})["msg"]
            for secret in env_keys.values():
                self.assertNotIn(secret, redacted)

    def test_doctor_reports_model_and_agy_latency_hint(self):
        with patch.object(cli, "instant_app_index", return_value={}), \
             patch.object(cli, "daemon_socket_status", return_value="missing"), \
             patch.object(cli, "check_ai_health", return_value={"status": "offline", "display": "Offline"}), \
             patch.object(sys, "argv", ["mavrick", "--doctor"]), \
             contextlib.redirect_stdout(io.StringIO()) as agy_out:
            cli.main()
        agy_text = agy_out.getvalue()
        self.assertIn("Provider: agy", agy_text)
        self.assertIn(f"Model: {config.DEFAULT_AGY_MODEL}", agy_text)
        self.assertIn("20-40s", agy_text)
        for preset in ("groq", "cerebras", "openrouter", "ollama"):
            self.assertIn(preset, agy_text)

        with patch.object(cli, "instant_app_index", return_value={}), \
             patch.object(cli, "daemon_socket_status", return_value="missing"), \
             patch.object(cli, "check_ai_health", return_value={"status": "connected_provider", "display": "Groq reachable"}), \
             patch.object(sys, "argv", ["mavrick", "--doctor", "--provider", "groq"]), \
             contextlib.redirect_stdout(io.StringIO()) as groq_out:
            cli.main()
        groq_text = groq_out.getvalue()
        self.assertIn("Provider: Groq", groq_text)
        self.assertIn("Model: openai/gpt-oss-20b", groq_text)
        self.assertNotIn("20-40s", groq_text)


@unittest.skipUnless(ui.Gtk.init_check()[0], "GTK display unavailable")
class TestDarkThemeAndHUDMultiApp(IsolatedTestCase):
    def test_light_and_dark_css_load_without_errors_and_multi_app_launches_in_hud(self):
        css_errors = []
        provider = ui.Gtk.CssProvider()
        provider.connect("parsing-error", lambda _p, _sec, err: css_errors.append(str(err)))
        for theme_name in ("light", "dark"):
            provider.load_from_data(styles.build_css(theme_name))
        self.assertEqual(css_errors, [])
        self.assertIn(b"#141517", styles.DARK_CSS)
        self.assertIn(b"#f1f2f4", styles.DARK_CSS)
        self.assertNotIn(b"#63fe13", styles.DARK_CSS)

        with patch.dict(os.environ, {"MAVRICK_THEME": "dark"}):
            dark_window = ui.MavrickWindow()
            self.addCleanup(dark_window.destroy)
            self.assertEqual(dark_window.state.theme, "dark")
            self.assertEqual(dark_window.get_screen()._mavrick_theme, "dark")

        with patch.dict(os.environ, {"MAVRICK_THEME": "light"}):
            window = ui.MavrickWindow()
            window.show()
            self.addCleanup(window.destroy)
            self.assertEqual(window.state.theme, "light")
            self.assertEqual(window.get_screen()._mavrick_theme, "light")

            def pump_until(condition):
                ctx = ui.GLib.MainContext.default()
                end = time.monotonic() + 2
                while not condition() and time.monotonic() < end:
                    ctx.iteration(False)
                    time.sleep(0.005)
                self.assertTrue(condition())

            with patch.object(apps, "desktop_app_ids", return_value=_DESKTOP_FIXTURES), \
                 patch.object(apps.shutil, "which", side_effect=_BIN_FIXTURES.get), \
                 patch.object(ai, "create_provider", side_effect=AssertionError("HUD multi-app called AI")), \
                 patch.object(apps, "launch_command", return_value=(True, "Launch requested", {})) as launch, \
                 patch.object(window, "record_history"):
                apps._index = (None, 0.0, {})
                apps.instant_app_index()
                window.entry.set_text("open files, vlc and vscode")
                self.assertTrue(window.preview_label.is_visible())
                self.assertIn("Instant · Open Files, VLC, VS Code", window.preview_label.get_text())
                window.on_submit(window.entry)
                pump_until(lambda: not window.is_processing and window.output_scroll.get_visible())
            self.assertEqual(launch.call_count, 3)
            self.assertEqual(window.engine_tag.get_text(), "Instant")
            self.assertEqual(window.status_label.get_text(), "● Launch requested")
            self.assertFalse(window.review_box.get_visible())
            start, end = window.buffer.get_bounds()
            self.assertIn("Launched: Files, VLC, VS Code", window.buffer.get_text(start, end, False))
