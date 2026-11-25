#!/usr/bin/env python3
"""
Offline Unit & Integration Tests for Sprint 7:
- Toolkit-agnostic HUDController / UIScheduler / HUDView decoupling
- Zero-dependency Tkinter Spotlight HUD (real Tk if available + deterministic fake-Tk verification)
- Backend selection (auto/gtk/tk) and isolated import-hygiene check with gi blocked
- Onboarding (mavrick --setup) & cross-platform daemon templates (systemd, launchd, Windows XML)
- Live provider preset updates (groq/cerebras) & packaging parity (.deb + temp wheel)
"""

import contextlib
import importlib.util
import io
import json
import os
import plistlib
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from unittest.mock import patch

from mavrick import ai, cli, config, executor, package_deb, policy, platform, router, schema, ui, workflows
from mavrick.platform import services
from mavrick.tests import IsolatedTestCase, TEST_ROOT, _real_popen
from mavrick.ui import controller, state, styles, tk_hud


class _HeadlessHUD(controller.HUDController):
    """Minimal headless HUDView for testing HUDController independent of GTK/Tk."""

    def __init__(self, *, theme: str = "light", ex: executor.ActionExecutor | None = None):
        self.state = state.HUDState(theme=theme)
        self.ui_scheduler = controller.InlineScheduler()
        self.executor = ex or executor.ActionExecutor()
        self.send_desktop_notification = lambda *_args: None
        self.entry_text = ""
        self.preview_text = ""
        self.preview_visible = False
        self.review_visible = False
        self.browse_visible = False
        self.output_visible = False
        self.progress_visible = False
        self.visible = True
        self.status_text = "● Ready"
        self.status_style = "ready"
        self.result_source_label = ""
        self.progress_text = ""
        self.clipboard_text = ""
        self.output_card: tuple[str, str, bool] | None = None
        self.technical_text = ""
        self.undo_visible = False
        self.retry_visible = False
        self.card_style: dict | None = None
        self.workflow_view: dict | None = None
        self.initial_focus = ""
        self.selected_files: list[int] = [0]
        self.entry_enabled = True
        self.history_enabled = True
        self.stop_enabled = True
        self.history_button_label = "⏱ History"
        self.processes_shown: list | None = None

    def get_entry_text(self) -> str:
        return self.entry_text

    def set_entry_text(self, text: str) -> None:
        self.entry_text = text

    def set_entry_enabled(self, enabled: bool) -> None:
        self.entry_enabled = enabled

    def focus_entry(self) -> None:
        self.initial_focus = "entry"

    def is_review_visible(self) -> bool:
        return self.review_visible

    def is_browse_visible(self) -> bool:
        return self.browse_visible

    def show_instant_preview(self, text: str) -> None:
        self.preview_text = text
        self.preview_visible = True

    def hide_instant_preview(self) -> None:
        self.preview_visible = False

    def set_status(self, text: str, state: str = "ready") -> None:
        self.status_text = text
        self.status_style = state

    def _show_health_badge(self, health: dict) -> None:
        self.health_running = False
        self.state.provider_label = config.PROVIDER_LABELS.get(health.get("provider"), "AI")

    def set_result_source(self, source: str) -> None:
        self.state.result_source = source
        self.result_source_label = source

    def set_history_button_label(self, text: str) -> None:
        self.history_button_label = text

    def set_history_enabled(self, enabled: bool) -> None:
        self.history_enabled = enabled

    def set_stop_enabled(self, enabled: bool) -> None:
        self.stop_enabled = enabled

    def clear_output_text(self) -> None:
        self.output_card = None

    def show_output_view(self) -> None:
        self.output_visible = True

    def set_progress_text(self, text: str) -> None:
        self.progress_text = text

    def _show_progress(self, label: str) -> None:
        self.is_processing = True
        self.progress_visible = True
        self.review_visible = False
        self.progress_text = label

    def _settle_progress(self) -> None:
        self.is_processing = False
        self.progress_visible = False

    def render_welcome_state(self) -> None:
        self.review_visible = False
        self.output_visible = False
        self.browse_visible = False
        self.preview_visible = False
        self.set_status("● Ready", "ready")

    def render_output_card(self, title: str, body: str = "", duration_ms: float = 0.0,
                           verification_status: str = "verified", is_error: bool = False) -> None:
        self.output_card = (title, body, is_error)

    def show_processes(self, processes: list) -> None:
        self.processes_shown = processes

    def set_technical_details(self, text: str) -> None:
        self.technical_text = text

    def show_result_actions(self, *, show_undo: bool, show_retry: bool) -> None:
        self.undo_visible = show_undo
        self.retry_visible = show_retry

    def set_clipboard_text(self, text: str) -> None:
        self.clipboard_text = text

    def show_review_card(self, action: schema.ActionRequest) -> None:
        self.review_visible = True
        self.card_style = tk_hud.review_action_presentation(action, styles.theme_colors(self.state.theme))
        self.initial_focus = self.card_style["initial_focus"]

    def show_workflow_review(self, review, summary: str) -> None:
        self.review_visible = True
        self.workflow_view = tk_hud.review_workflow_presentation(review, summary, styles.theme_colors(self.state.theme))
        self.initial_focus = self.workflow_view["initial_focus"]

    def show_file_review(self, review, summary: str) -> None:
        self.file_review = review
        self.file_rows = list(review.rows)
        self.review_visible = True
        self.initial_focus = "cancel"

    def get_selected_file_indices(self) -> list[int]:
        return list(self.selected_files)

    def set_review_error(self, message: str) -> None:
        self.status_text = message

    def update_renamed_file_row(self, index: int, label, updated) -> None:
        pass

    def render_history_view(self) -> None:
        pass

    def toggle_history(self) -> None:
        self.history_mode = not self.history_mode

    def adjust_window_size(self, content_lines=None) -> None:
        pass

    def is_visible(self) -> bool:
        return self.visible

    def show_window(self) -> bool:
        self.visible = True
        return False

    def apply_chip(self, snippet: str) -> None:
        self.entry_text = snippet


def _make_fake_tk_module():
    """Minimal deterministic stdlib-tkinter stand-in for headless widget verification."""
    class _FakeTclError(Exception):
        pass

    class _FakeVar:
        def __init__(self, value=""):
            self._val = value

        def get(self):
            return self._val

        def set(self, val):
            self._val = val

    class _FakeWidget:
        def __init__(self, master=None, **kwargs):
            self.master = master
            self.props = dict(kwargs)
            self.bindings = {}
            self.children = []
            self.packed = False
            self.text_content = ""
            if isinstance(master, _FakeWidget):
                master.children.append(self)

        def configure(self, **kwargs):
            self.props.update(kwargs)

        config = configure

        def cget(self, key):
            return self.props[key]

        def __getitem__(self, key):
            return self.props[key]

        def pack(self, **_kwargs):
            self.packed = True

        def pack_forget(self):
            self.packed = False

        def bind(self, sequence, func):
            self.bindings[sequence] = func

        def focus_set(self):
            root = self
            while getattr(root, "master", None) is not None:
                root = root.master
            root._focused = self

        def focus_get(self):
            root = self
            while getattr(root, "master", None) is not None:
                root = root.master
            return getattr(root, "_focused", None)

        def winfo_children(self):
            return list(self.children)

        def destroy(self):
            if isinstance(self.master, _FakeWidget) and self in self.master.children:
                self.master.children.remove(self)
            self.children.clear()

        def insert(self, _index, text):
            self.text_content += text

        def delete(self, *_args):
            self.text_content = ""

        def curselection(self):
            return ()

        def selection_set(self, *_args):
            pass

        def icursor(self, *_args):
            pass

    class _FakeRoot(_FakeWidget):
        def __init__(self):
            super().__init__(None)
            self._focused = None
            self._after_seq = 0
            self._after_callbacks = {}
            self._clipboard = ""
            self.geometry_str = ""
            self.withdrawn = False
            self.destroyed = False

        def title(self, *_args):
            pass

        def overrideredirect(self, *_args):
            pass

        def attributes(self, *_args):
            pass

        def geometry(self, val):
            self.geometry_str = val

        def winfo_screenwidth(self):
            return 1440

        def winfo_screenheight(self):
            return 900

        def protocol(self, *_args):
            pass

        def after(self, _ms, fn):
            self._after_seq += 1
            tok = f"after-{self._after_seq}"
            self._after_callbacks[tok] = fn
            return tok

        def after_cancel(self, tok):
            self._after_callbacks.pop(tok, None)

        def clipboard_clear(self):
            self._clipboard = ""

        def clipboard_append(self, text):
            self._clipboard += text

        def update_idletasks(self):
            pass

        def withdraw(self):
            self.withdrawn = True

        def deiconify(self):
            self.withdrawn = False

        def lift(self):
            pass

        def mainloop(self):
            pass

        def destroy(self):
            self.destroyed = True

    class _FakeTkModule:
        TclError = _FakeTclError
        Tk = _FakeRoot
        Frame = _FakeWidget
        Label = _FakeWidget
        Button = _FakeWidget
        Entry = _FakeWidget
        Listbox = _FakeWidget
        Text = _FakeWidget
        Checkbutton = _FakeWidget
        @staticmethod
        def StringVar(value=""):
            return _FakeVar(value)

        @staticmethod
        def BooleanVar(value=False):
            return _FakeVar(bool(value))

    return _FakeTkModule()


class TestBackendSelectionAndImportHygiene(IsolatedTestCase):
    def test_backend_selection_matrix_and_cli_fallback_guidance(self):
        with patch.object(ui, "gtk_available", return_value=True), \
             patch.object(ui, "tk_available", return_value=True):
            self.assertEqual(ui.select_ui_backend("auto", platform_name="linux"), "gtk")
            self.assertEqual(ui.select_ui_backend("auto", platform_name="windows"), "tk")
            self.assertEqual(ui.select_ui_backend("auto", platform_name="macos"), "tk")

        with patch.object(ui, "gtk_available", return_value=False), \
             patch.object(ui, "tk_available", return_value=True):
            self.assertEqual(ui.select_ui_backend("auto", platform_name="linux"), "tk")

        with patch.object(ui, "gtk_available", return_value=False), \
             patch.object(ui, "tk_available", return_value=False):
            self.assertEqual(ui.select_ui_backend("auto", platform_name="linux"), "none")

        with patch.dict(os.environ, {"MAVRICK_UI_BACKEND": "tk"}):
            self.assertEqual(ui.select_ui_backend(), "tk")
        with patch.dict(os.environ, {"MAVRICK_UI_BACKEND": "gtk"}):
            self.assertEqual(ui.select_ui_backend(), "gtk")
        with self.assertRaises(ValueError):
            ui.select_ui_backend("qt")

        with patch.object(ui, "select_ui_backend", return_value="none"), \
             patch.object(sys, "argv", ["mavrick"]), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            rc = cli.main()
        self.assertEqual(rc, 1)
        self.assertIn("No GUI toolkit", out.getvalue())

    def test_headless_ui_and_controller_imports_with_gi_blocked(self):
        source = str(package_deb.SOURCE_DIR.parent)
        script = '''
import sys
sys.path.insert(0, sys.argv[1])
for name in list(sys.modules):
    if name == "gi" or name.startswith("gi."):
        sys.modules.pop(name, None)
class BlockGI:
    def find_spec(self, fullname, *_):
        if fullname.split(".")[0] == "gi":
            raise ModuleNotFoundError(fullname, name=fullname)
sys.meta_path.insert(0, BlockGI())
import mavrick.ui
import mavrick.ui.controller
import mavrick.ui.styles
import mavrick.ui.tk_hud
assert not any(k == "gi" or k.startswith("gi.") for k in sys.modules)
assert mavrick.ui.styles.theme_colors("dark")["linen"] == "#141517"
assert not mavrick.ui.gtk_available()
print("gi-blocked ui imports ok")
'''
        command = [sys.executable, "-I", "-c", script, source]
        with patch("subprocess.Popen", side_effect=lambda argv, *a, **kw: _real_popen(argv, *a, **kw)):
            proc = subprocess.run(command, cwd=TEST_ROOT, capture_output=True, text=True, timeout=5, env=os.environ)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("gi-blocked ui imports ok", proc.stdout)


class TestToolkitAgnosticControllerAndTkHUD(IsolatedTestCase):
    def test_headless_controller_instant_preview_review_escape_and_undo(self):
        hud = _HeadlessHUD(theme="dark")

        # 1. Instant calculator + clipboard sync
        hud.entry_text = "245 * 18.5"
        hud.update_local_preview(refresh=False)
        self.assertTrue(hud.preview_visible)
        self.assertIn("= 4532.5", hud.preview_text)
        hud.on_submit()
        self.assertEqual(hud.clipboard_text, "4532.5")
        self.assertEqual(hud.status_style, "done")

        # 2. Destructive MCP review card -> red confirm styling + initial focus on Cancel
        mcp_act = schema.ActionRequest(
            schema.ActionType.MCP_TOOL,
            {"server": "notes", "tool": "wipe_all", "arguments": {}},
            "mcp · notes/wipe_all",
            schema.RiskLevel.DESTRUCTIVE,
            impact="Destructive MCP tool requires explicit confirmation.",
            affected_targets=["mcp · notes/wipe_all"],
        )
        hud.on_execution_complete(
            mcp_act,
            executor.ActionResult("requires_confirmation", details={"action": mcp_act}, summary=mcp_act.summary),
        )
        self.assertTrue(hud.review_visible)
        self.assertEqual(hud.card_style["confirm_bg"], styles.DARK_COLORS["danger_btn"])
        self.assertEqual(hud.card_style["border_color"], styles.DARK_COLORS["danger"])
        self.assertEqual(hud.card_style["confirm_label"], "Run MCP tool")
        self.assertEqual(hud.initial_focus, "cancel")

        # 3. Privileged workflow review -> amber border + initial focus on Cancel
        priv_act = schema.ActionRequest(
            schema.ActionType.INSTALL_PACKAGE,
            {"package_name": "htop", "manager": "apt"},
            "Install htop",
            schema.RiskLevel.PRIVILEGED,
        )
        priv_review = workflows.WorkflowReview(
            schema.WorkflowPlan((priv_act,), name="setup-tools"),
            (workflows.WorkflowStepReview(1, policy.WorkflowRisk.PRIVILEGED, "Install htop", targets=("htop",)),),
            "Workflow · setup-tools",
            policy.WorkflowRisk.PRIVILEGED,
        )
        hud.show_workflow_review(priv_review, "1 privileged step")
        self.assertEqual(hud.workflow_view["border_color"], styles.DARK_COLORS["warning"])
        self.assertEqual(hud.initial_focus, "cancel")

        # 4. Batch Undo trigger dispatches ActionType.UNDO
        hud.on_cancel_action(record=False)
        hud.undo_id = "batch-123"
        with patch.object(hud, "execute_pending_action") as run_exec:
            hud.on_trigger_undo()
        self.assertEqual(hud.pending_action.action_type, schema.ActionType.UNDO)
        self.assertEqual(hud.pending_action.params["undo_id"], "batch-123")
        run_exec.assert_called_once()

    def test_tk_hud_window_dark_light_destructive_focus_esc_and_undo(self):
        real_tk_ok = False
        if tk_hud.tk_available():
            try:
                import tkinter as _tk
                probe = _tk.Tk()
                probe.destroy()
                real_tk_ok = True
            except Exception:
                real_tk_ok = False

        if real_tk_ok:
            root_cm = contextlib.nullcontext()
        else:
            fake_tk = _make_fake_tk_module()
            root_cm = patch.object(tk_hud, "_import_tk", return_value=fake_tk)

        with root_cm:
            win = tk_hud.TkMavrickWindow(theme="dark", executor=executor.ActionExecutor())
            self.addCleanup(win.destroy)
            win.show_window()
            self.assertEqual(win.main_box["bg"], styles.DARK_COLORS["linen"])

            # Calculator via Return key binding
            win.set_entry_text("12 + 30")
            win.entry.bindings["<Return>"](None)
            self.assertEqual(win.root._clipboard if not real_tk_ok else win.root.clipboard_get(), "42")

            # Destructive review card -> red confirm btn, red border, initial focus on cancel_btn
            nuke = schema.ActionRequest(
                schema.ActionType.MCP_TOOL,
                {"server": "notes", "tool": "purge", "arguments": {}},
                "mcp · notes/purge",
                schema.RiskLevel.DESTRUCTIVE,
                impact="Destructive MCP tool requires explicit confirmation.",
                affected_targets=["mcp · notes/purge"],
            )
            win.show_review_card(nuke)
            self.assertTrue(win.is_review_visible())
            self.assertEqual(win.confirm_btn["bg"], styles.DARK_COLORS["danger_btn"])
            self.assertEqual(win.confirm_btn["text"], "Run MCP tool")
            self.assertEqual(win.review_box["highlightbackground"], styles.DARK_COLORS["danger"])
            self.assertIs(win.root.focus_get(), win.cancel_btn)

            # Esc cancels active review
            win.root.bindings["<Escape>"](None)
            self.assertFalse(win.is_review_visible())
            self.assertEqual(win.status_label["text"], "● Cancelled")

            # Privileged review -> amber border, initial focus on cancel_btn
            priv = schema.ActionRequest(
                schema.ActionType.INSTALL_PACKAGE,
                {"package_name": "htop", "manager": "apt"},
                "Install htop",
                schema.RiskLevel.PRIVILEGED,
                impact="Requires administrator confirmation.",
            )
            win.show_review_card(priv)
            self.assertEqual(win.review_box["highlightbackground"], styles.DARK_COLORS["warning"])
            self.assertIs(win.root.focus_get(), win.cancel_btn)
            win.on_cancel_action(record=False)

            # Ctrl+Z binding triggers batch Undo
            win.undo_id = "batch-xyz"
            with patch.object(win, "execute_pending_action") as trigger_exec:
                win.root.bindings["<Control-z>"](None)
            self.assertEqual(win.pending_action.action_type, schema.ActionType.UNDO)
            trigger_exec.assert_called_once()


class TestOnboardingServiceTemplatesAndPresets(IsolatedTestCase):
    def test_provider_presets_updated_and_non_interactive_setup_writes_0600(self):
        self.assertEqual(config.PROVIDER_PRESETS["groq"][0], "openai/gpt-oss-20b")
        self.assertEqual(config.PROVIDER_PRESETS["cerebras"][0], "gpt-oss-120b")

        groq = config.load_provider_settings(provider="groq")
        cerebras = config.load_provider_settings(provider="cerebras")
        self.assertEqual(groq.model, "openai/gpt-oss-20b")
        self.assertEqual(cerebras.model, "gpt-oss-120b")

        cfg_path = Path(config.CONFIG_FILE)
        had_cfg = cfg_path.exists()
        prev_cfg = cfg_path.read_bytes() if had_cfg else None

        def _restore_cfg():
            if had_cfg and prev_cfg is not None:
                cfg_path.write_bytes(prev_cfg)
            elif cfg_path.exists():
                cfg_path.unlink()

        self.addCleanup(_restore_cfg)

        secret = "gsk-setup-secret-never-echo-999"
        with patch.dict(os.environ, {"GROQ_API_KEY": secret}, clear=True), \
             patch.object(cli, "instant_app_index", return_value={}), \
             patch.object(cli, "daemon_socket_status", return_value="missing"), \
             patch.object(cli, "check_ai_health", return_value={"status": "connected_provider", "display": "Groq reachable"}), \
             patch.object(sys, "argv", ["mavrick", "--setup", "--non-interactive", "--provider", "groq", "--theme", "dark"]), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            rc = cli.main()

        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertNotIn(secret, text)
        self.assertIn("provider=groq", text)
        self.assertIn("model=openai/gpt-oss-20b", text)
        self.assertIn("theme=dark", text)
        self.assertIn("Provider: Groq", text)
        self.assertIn("Model: openai/gpt-oss-20b", text)

        cfg_path = Path(config.CONFIG_FILE)
        self.assertEqual(stat.S_IMODE(cfg_path.stat().st_mode), 0o600)
        saved = json.loads(cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["theme"], "dark")
        self.assertEqual(saved["ai_provider"]["name"], "groq")
        self.assertEqual(saved["ai_provider"]["model"], "openai/gpt-oss-20b")
        self.assertEqual(config.load_theme_setting(), "dark")

    def test_cross_platform_service_templates_validate_and_reject_unsafe_inputs(self):
        # macOS launchd plist via CLI & direct helper
        with patch.object(sys, "argv", ["mavrick", "--service-template", "macos"]), \
             contextlib.redirect_stdout(io.StringIO()) as mac_out:
            self.assertEqual(cli.main(), 0)
        parsed_plist = plistlib.loads(mac_out.getvalue().encode("utf-8"))
        self.assertEqual(parsed_plist["Label"], "com.mavrick.daemon")
        self.assertEqual(parsed_plist["ProgramArguments"][-2:], ["-m", "mavrick.daemon"])
        self.assertTrue(parsed_plist["RunAtLoad"])

        # Windows Scheduled Task XML via CLI & direct helper
        with patch.object(sys, "argv", ["mavrick", "--service-template", "windows"]), \
             contextlib.redirect_stdout(io.StringIO()) as win_out:
            self.assertEqual(cli.main(), 0)
        xml_root = ET.fromstring(win_out.getvalue())
        self.assertTrue(xml_root.tag.endswith("Task"))
        ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        cmd = xml_root.findtext(".//t:Exec/t:Command", namespaces=ns)
        args = xml_root.findtext(".//t:Exec/t:Arguments", namespaces=ns)
        self.assertTrue(cmd)
        self.assertEqual(args, "-m mavrick.daemon")
        self.assertEqual(xml_root.findtext(".//t:LogonTrigger/t:Enabled", namespaces=ns), "true")

        # Linux systemd reference
        with patch.object(sys, "argv", ["mavrick", "--service-template", "linux"]), \
             contextlib.redirect_stdout(io.StringIO()) as lin_out:
            self.assertEqual(cli.main(), 0)
        self.assertIn("mavrickd.service", lin_out.getvalue())
        self.assertIn("mavrick.service", lin_out.getvalue())
        self.assertIn("systemctl --user", lin_out.getvalue())

        # Rejection & XML escaping checks
        with self.assertRaises(ValueError):
            services.macos_launchd_plist(python_executable="python\nrm -rf /")
        with self.assertRaises(ValueError):
            services.windows_task_xml(task_name="Bad;Task")
        ps = services.windows_powershell_register_command(r"C:\Users\O'Connor\mavrick.xml")
        self.assertIn("O''Connor", ps)


@unittest.skipUnless(shutil.which("dpkg-deb") and importlib.util.find_spec("wheel"), "Build tools unavailable")
class TestSprint7Packaging(IsolatedTestCase):
    def test_deb_and_wheel_include_all_runtime_modules_byte_for_byte(self):
        source = package_deb.SOURCE_DIR
        manifest = package_deb.runtime_manifest()
        for expected_rel in ("ui/tk_hud.py", "platform/services.py"):
            self.assertIn(Path(expected_rel), manifest)

        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as tmpdir:
            root = Path(tmpdir)
            stage = root / "stage"
            deb = root / "mavrick_test.deb"
            extracted = root / "unpacked"
            wheel_src = root / "wheel-src"
            shutil.copytree(source, wheel_src, ignore=shutil.ignore_patterns(
                "__pycache__", "*.pyc", "*.deb", "*.zip", "build", ".extract", ".pytest_cache", ".mavrick-deb-*"))

            wheel_cmd = [
                sys.executable, "-m", "pip", "wheel", "--disable-pip-version-check",
                "--no-deps", "--no-index", "--no-cache-dir", "--no-build-isolation",
                "--wheel-dir", str(root), str(wheel_src),
            ]

            package_deb.create_control_file(stage, "2.0.0")
            package_deb.stage_payload_files(stage)
            with patch.object(subprocess, "Popen", side_effect=lambda argv, *a, **kw: _real_popen(argv, *a, **kw)):
                self.assertTrue(package_deb.build_deb_package(stage, deb))
                subprocess.run(["dpkg-deb", "-x", str(deb), str(extracted)], check=True)
                built = subprocess.run(wheel_cmd, cwd=root, capture_output=True, text=True, timeout=20)
                self.assertEqual(built.returncode, 0, built.stdout + built.stderr)

            wheel, = root.glob("*.whl")
            with zipfile.ZipFile(wheel) as contents:
                for module in manifest:
                    expected = (source / module).read_bytes()
                    self.assertEqual((extracted / "usr" / "lib" / "mavrick" / module).read_bytes(), expected)
                    self.assertEqual(contents.read(f"mavrick/{module.as_posix()}"), expected)


if __name__ == "__main__":
    unittest.main()
