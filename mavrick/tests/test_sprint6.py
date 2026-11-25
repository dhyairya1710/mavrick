#!/usr/bin/env python3
"""
Unit & Integration Tests for Sprint 6:
- Dual-era stdio MCP client (2026-07-28 server/discover + legacy initialize fallback)
- Zero-trust mechanical policy governance & secret isolation/redaction
- Multi-step WorkflowPlan escalation, cancellation, and cleanup
- CLI diagnostics (--mcp, --doctor), Broadway HUD rendering, and .deb/wheel packaging
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from mavrick import ai, cli, config, executor, history, mcp, policy, schema, ui, workflows
from mavrick.mcp import registry as mcp_registry
from mavrick.package_deb import (
    create_control_file,
    create_maintainer_scripts,
    runtime_manifest,
    stage_payload_files,
)
from mavrick.tests import IsolatedTestCase, _real_popen


_MOCK_MCP_SCRIPT = r'''
import json
import os
import sys
import time

mode = sys.argv[1]
spawn_log = os.environ.get("SPAWN_COUNT_FILE", "")
if spawn_log:
    with open(spawn_log, "a", encoding="utf-8") as fh:
        fh.write(mode + "\n")

def send(obj):
    sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
    sys.stdout.flush()

TOOLS = [
    {
        "name": "read_env",
        "description": "Read scoped environment and return diagnostics.",
        "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}},
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    {
        "name": "mutate_item",
        "description": "Modify a note.",
        "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}},
        "annotations": {"readOnlyHint": False},
    },
    {
        "name": "danger_tool",
        "description": "Purge entries permanently.",
        "inputSchema": {"type": "object", "properties": {"target": {"type": "string"}}},
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
    },
    {
        "name": "sneaky_read_nuke",
        "description": "Claims readOnly but also marks destructive.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True, "destructiveHint": True},
    },
]

if mode == "hang_discover":
    sys.stdin.readline()
    time.sleep(10)
    sys.exit(0)

legacy_initialized = False

while True:
    raw = sys.stdin.readline()
    if not raw:
        break
    msg = json.loads(raw)
    req_id = msg.get("id")
    method = msg.get("method", "")
    params = msg.get("params") or {}

    if method == "server/discover":
        if mode == "legacy_eof":
            sys.exit(0)
        if mode == "legacy_error":
            send({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": "Method not found"}})
            continue
        send({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2026-07-28",
                "_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"},
                "serverInfo": {"name": "mock-modern", "version": "1.0.0"},
                "capabilities": {"tools": {}},
            },
        })
        continue

    if method == "initialize":
        send({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2025-11-25",
                "serverInfo": {"name": "mock-legacy", "version": "1.0.0"},
                "capabilities": {"tools": {}},
            },
        })
        continue

    if method == "notifications/initialized":
        legacy_initialized = True
        continue

    if mode in ("legacy_error", "legacy_eof") and not legacy_initialized:
        send({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32002, "message": "Server not initialized"}})
        continue

    if method == "tools/list":
        if mode == "bad_json":
            sys.stdout.write("not valid json\n")
            sys.stdout.flush()
            continue
        if mode == "oversized":
            sys.stdout.write('{"jsonrpc":"2.0","id":' + str(req_id) + ',"result":{"pad":"' + ("x" * (2 * 1024 * 1024 + 512)) + '"}}\n')
            sys.stdout.flush()
            continue
        meta_seen = isinstance(params.get("_meta"), dict)
        if mode == "modern" and not meta_seen:
            send({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32602, "message": "Missing _meta"}})
            continue
        send({"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}})
        continue

    if method == "tools/call":
        name = params.get("name", "")
        arguments = params.get("arguments") or {}
        if mode == "sleepy":
            if name == "read_env":
                time.sleep(0.05)
                send({"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": "quick ok"}]}})
                continue
            time.sleep(5.0)
            send({"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": "late"}]}})
            continue
        if mode == "modern" and not isinstance(params.get("_meta"), dict):
            send({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32602, "message": "Missing _meta"}})
            continue
        payload = {
            "tool": name,
            "arguments": arguments,
            "env_keys": sorted(os.environ.keys()),
            "passthrough_seen": os.environ.get("MCP_PASSTHROUGH_VAR"),
            "host_secret_seen": os.environ.get("MAVRICK_HOST_SECRET"),
            "groq_key_seen": os.environ.get("GROQ_API_KEY"),
            "token_seen": os.environ.get("MCP_SERVER_TOKEN"),
        }
        text = "\x1b[31mANSI\x1b[0m " + json.dumps(payload, sort_keys=True)
        send({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "content": [{"type": "text", "text": text}],
                "structuredContent": payload,
                "isError": False,
            },
        })
        continue

    send({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Unknown method: {method}"}})
'''


class _MCPTestMixin:
    def setUp(self):
        super().setUp()
        mcp_registry._tool_cache.clear()
        mcp_registry._registry_cache = ("", None, mcp_registry.MCPRegistry({}))
        mcp_registry._grants_cache = ("", None, {})
        self._tmp_home_dir = tempfile.TemporaryDirectory(dir=os.environ["HOME"])
        self.addCleanup(self._tmp_home_dir.cleanup)
        self.mock_script = Path(self._tmp_home_dir.name) / "mock_mcp.py"
        self.mock_script.write_text(_MOCK_MCP_SCRIPT, encoding="utf-8")

    @contextlib.contextmanager
    def allow_subprocesses(self, extra_commands=()):
        spawned: list[subprocess.Popen] = []
        allowed = {sys.executable, "python3", "python", *extra_commands}

        def selective_popen(command, *args, **kwargs):
            exe = command[0] if isinstance(command, (list, tuple)) and command else ""
            if exe in allowed or os.path.basename(exe) in allowed:
                proc = _real_popen(command, *args, **kwargs)
                spawned.append(proc)
                return proc
            raise AssertionError(f"Unexpected external launch in test: {command!r}")

        with patch("subprocess.Popen", side_effect=selective_popen):
            yield spawned

    def write_mcp_servers(self, servers: dict, *, approve: bool = True):
        mcp_registry._tool_cache.clear()
        mcp_registry._registry_cache = ("", None, mcp_registry.MCPRegistry({}))
        mcp_registry._grants_cache = ("", None, {})
        grants_path = Path(config.MCP_GRANTS_FILE)
        if grants_path.exists():
            grants_path.unlink()
        path = Path(config.MCP_SERVERS_FILE)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(json.dumps({"servers": servers}), encoding="utf-8")
        os.chmod(path, 0o600)
        if approve:
            reg = mcp.load_mcp_registry(refresh=True)
            for name, cfg in reg.servers.items():
                if cfg.enabled:
                    mcp.approve_mcp_server(
                        name,
                        grant_env=tuple(sorted(set(cfg.env) | set(cfg.env_passthrough))),
                        approve_readonly_tools=("read_env",) if cfg.trusted else (),
                    )


class TestMCPDualEraAndPolicy(_MCPTestMixin, IsolatedTestCase):
    def test_modern_2026_discover_call_cache_env_isolation_and_redaction(self):
        counter = Path(self._tmp_home_dir.name) / "spawns.txt"
        secret_tok = "mcp-secret-token-xyz987"
        host_secret = "host-secret-must-never-leak-123"
        groq_secret = "gsk-host-secret-999999"

        self.write_mcp_servers({
            "modern": {
                "command": sys.executable,
                "args": [str(self.mock_script), "modern"],
                "env": {"MCP_SERVER_TOKEN": secret_tok, "SPAWN_COUNT_FILE": str(counter)},
                "env_passthrough": ["MCP_PASSTHROUGH_VAR"],
                "cwd": self._tmp_home_dir.name,
                "trusted": True,
                "allowed_tools": ["read_env", "mutate_item"],
                "blocked_tools": ["danger_tool"],
                "timeout": 5.0,
            }
        })

        with patch.dict(os.environ, {
            "MAVRICK_HOST_SECRET": host_secret,
            "GROQ_API_KEY": groq_secret,
            "MCP_PASSTHROUGH_VAR": "allowed-pass-val",
        }):
            with self.allow_subprocesses() as spawned:
                snap1 = mcp.discover_server_tools("modern")
                snap2 = mcp.discover_server_tools("modern")
                self.assertEqual(snap1.era, mcp.PROTOCOL_MODERN)
                self.assertEqual(snap1.protocol_version, "2026-07-28")
                self.assertEqual(len(snap1.tools), 4)
                self.assertEqual(snap2.era, mcp.PROTOCOL_MODERN)
                self.assertEqual(len(counter.read_text(encoding="utf-8").splitlines()), 1)

                prompt = ai.build_system_prompt()
                self.assertIn("Configured External MCP Tools", prompt)
                self.assertIn("read_env", prompt)
                self.assertNotIn("danger_tool", prompt)

                req = schema.ActionRequest(
                    schema.ActionType.MCP_TOOL,
                    {"server": "modern", "tool": "read_env", "arguments": {"query": "diag"}},
                    "Read env",
                    schema.RiskLevel.REVERSIBLE,
                )
                result = executor.ActionExecutor().execute(req, query="check mcp env")

        self.assertEqual(result.status, "success")
        self.assertEqual(result.details["era"], "2026-07-28")
        self.assertNotIn("\x1b[31m", result.stdout)
        self.assertNotIn(secret_tok, result.stdout)
        self.assertNotIn(groq_secret, result.stdout)
        self.assertIn("[REDACTED_SECRET]", result.stdout)
        self.assertIn('"passthrough_seen": "allowed-pass-val"', result.stdout)
        self.assertIn('"host_secret_seen": null', result.stdout)
        self.assertIn('"groq_key_seen": null', result.stdout)
        self.assertTrue(all(p.poll() is not None for p in spawned))

        hist_text = Path(config.HISTORY_FILE).read_text(encoding="utf-8")
        audit_text = Path(config.AUDIT_LOG_FILE).read_text(encoding="utf-8")
        for secret in (secret_tok, host_secret, groq_secret):
            self.assertNotIn(secret, hist_text)
            self.assertNotIn(secret, audit_text)

    def test_legacy_handshake_fallback_error_and_eof_modes(self):
        self.write_mcp_servers({
            "leg_err": {
                "command": sys.executable,
                "args": [str(self.mock_script), "legacy_error"],
                "trusted": True,
                "timeout": 5.0,
            },
            "leg_eof": {
                "command": sys.executable,
                "args": [str(self.mock_script), "legacy_eof"],
                "trusted": True,
                "timeout": 5.0,
            },
        })

        with self.allow_subprocesses() as spawned:
            for srv in ("leg_err", "leg_eof"):
                snap = mcp.discover_server_tools(srv)
                self.assertEqual(snap.era, "legacy")
                self.assertEqual(snap.protocol_version, "2025-11-25")
                call_res = mcp.call_mcp_tool(srv, "read_env", {"query": "ping"})
                self.assertEqual(call_res.era, "legacy")
                self.assertFalse(call_res.is_error)
                self.assertIn('"tool": "read_env"', call_res.text)
        self.assertTrue(all(p.poll() is not None for p in spawned))

    def test_zero_trust_governance_blocklist_allowlist_and_shell_rejection(self):
        counter = Path(self._tmp_home_dir.name) / "gate_spawns.txt"
        self.write_mcp_servers({
            "untrusted_srv": {
                "command": sys.executable,
                "args": [str(self.mock_script), "modern"],
                "env": {"SPAWN_COUNT_FILE": str(counter)},
                "trusted": False,
                "allowed_tools": ["read_env", "danger_tool", "sneaky_read_nuke"],
                "blocked_tools": ["mutate_item"],
            },
            "bad_shell": {
                "command": "bash",
                "args": ["-c", "echo hi"],
            },
        })

        reg = mcp.load_mcp_registry(refresh=True)
        self.assertIn("untrusted_srv", reg.servers)
        self.assertNotIn("bad_shell", reg.servers)
        self.assertTrue(any("Shell interpreter 'bash'" in err for err in reg.errors))

        ok1, err1, req1 = schema.validate_action_params("mcp_tool", {"server": "bad slug!", "tool": "x"})
        self.assertFalse(ok1)
        self.assertIsNone(req1)
        self.assertIn("slug", err1)
        ok2, err2, req2 = schema.validate_action_params("mcp_tool", {"server": "ok", "tool": "x", "arguments": "not-dict"})
        self.assertFalse(ok2)
        self.assertIsNone(req2)
        self.assertIn("arguments", err2)
        with self.assertRaises(ValueError):
            schema.validate_workflow_action("mcp_tool", {"server": "bad slug!", "tool": "x"})

        ex = executor.ActionExecutor()
        with self.allow_subprocesses() as spawned:
            # Blocked tool and non-allowlisted tool MUST fail mechanically BEFORE spawning the server.
            blocked_res = ex.execute(
                schema.ActionRequest(schema.ActionType.MCP_TOOL, {"server": "untrusted_srv", "tool": "mutate_item"}, "", schema.RiskLevel.REVERSIBLE),
                confirmed=True,
            )
            self.assertEqual(blocked_res.status, "failed")
            self.assertIn("blocked by policy", blocked_res.summary)

            unlisted_res = ex.execute(
                schema.ActionRequest(schema.ActionType.MCP_TOOL, {"server": "untrusted_srv", "tool": "other_tool"}, "", schema.RiskLevel.REVERSIBLE),
                confirmed=True,
            )
            self.assertEqual(unlisted_res.status, "failed")
            self.assertIn("allowed_tools", unlisted_res.summary)
            self.assertFalse(counter.exists())

            # Untrusted server claiming readOnlyHint=True STILL requires explicit approval, even with trusted_actions/confirmed=True.
            ro_req = schema.ActionRequest(
                schema.ActionType.MCP_TOOL,
                {"server": "untrusted_srv", "tool": "read_env", "arguments": {"query": "hi"}},
                "",
                schema.RiskLevel.REVERSIBLE,
            )
            with patch.object(executor, "load_user_config", return_value={"trusted_actions": ["mcp_tool"]}):
                gate = ex.execute(ro_req, confirmed=True)
                self.assertEqual(gate.status, "requires_confirmation")
                self.assertEqual(gate.details["action"].risk_level, schema.RiskLevel.REVERSIBLE)
                approved = ex.execute(ro_req, approval=executor.approve_action(gate.details["action"]))
                self.assertEqual(approved.status, "success")

            # sneaky_read_nuke has readOnlyHint=True AND destructiveHint=True -> escalates to DESTRUCTIVE.
            nuke_req = schema.ActionRequest(
                schema.ActionType.MCP_TOOL,
                {"server": "untrusted_srv", "tool": "sneaky_read_nuke", "arguments": {}},
                "",
                schema.RiskLevel.REVERSIBLE,
            )
            nuke_gate = ex.execute(nuke_req, confirmed=True)
            self.assertEqual(nuke_gate.status, "requires_confirmation")
            self.assertEqual(nuke_gate.details["action"].risk_level, schema.RiskLevel.DESTRUCTIVE)

        self.assertTrue(all(p.poll() is not None for p in spawned))

    def test_mixed_workflow_escalation_cancellation_and_redaction(self):
        arg_secret = "tok-secret-workflow-555"
        self.write_mcp_servers({
            "wf_srv": {
                "command": sys.executable,
                "args": [str(self.mock_script), "sleepy"],
                "env": {"MCP_WORKFLOW_TOKEN": arg_secret},
                "trusted": False,
                "timeout": 8.0,
            }
        })

        plan = schema.parse_workflow_plan([
            {"action": "get_system_health", "params": {}},
            {"action": "mcp_tool", "params": {"server": "wf_srv", "tool": "danger_tool", "arguments": {"key": arg_secret}}},
        ], name="mcp-mixed")

        ex = executor.ActionExecutor()
        with self.allow_subprocesses() as spawned:
            review = ex.preview_workflow(plan)
            self.assertTrue(review.allowed)
            self.assertEqual(review.risk, policy.WorkflowRisk.DESTRUCTIVE)
            self.assertEqual(review.steps[0].risk, policy.WorkflowRisk.READ_ONLY)
            self.assertEqual(review.steps[1].risk, policy.WorkflowRisk.DESTRUCTIVE)
            self.assertTrue(any("[REDACTED_SECRET]" in line for line in review.steps[1].targets))
            self.assertFalse(any(arg_secret in line for line in review.steps[1].targets))

            # Run a 2-step sleepy MCP workflow and fire cancel_token mid-flight.
            cancel_plan = schema.parse_workflow_plan([
                {"action": "mcp_tool", "params": {"server": "wf_srv", "tool": "read_env", "arguments": {}}},
                {"action": "mcp_tool", "params": {"server": "wf_srv", "tool": "danger_tool", "arguments": {"key": arg_secret}}},
            ], name="mcp-cancel")
            cancel_review = ex.preview_workflow(cancel_plan)
            cancel_ev = threading.Event()

            def on_progress(msg):
                if "danger_tool" in msg:
                    threading.Timer(0.15, cancel_ev.set).start()

            started = time.monotonic()
            res = ex.execute(
                cancel_plan,
                approval=workflows.approve_workflow(cancel_review),
                cancel_token=cancel_ev,
                progress_callback=on_progress,
                query="cancel mid mcp workflow",
            )
            elapsed = time.monotonic() - started

        self.assertLess(elapsed, 3.0)
        self.assertIn(res.status, ("cancelled", "partial"))
        self.assertTrue(all(p.poll() is not None for p in spawned))
        hist_text = Path(config.HISTORY_FILE).read_text(encoding="utf-8")
        self.assertNotIn(arg_secret, hist_text)

    def test_timeout_malformed_json_and_oversized_frame_guarantee_cleanup(self):
        self.write_mcp_servers({
            "hang": {"command": sys.executable, "args": [str(self.mock_script), "hang_discover"], "timeout": 0.35},
            "bad": {"command": sys.executable, "args": [str(self.mock_script), "bad_json"], "timeout": 2.0},
            "huge": {"command": sys.executable, "args": [str(self.mock_script), "oversized"], "timeout": 2.0},
        })

        with self.allow_subprocesses() as spawned:
            snap_hang = mcp.discover_server_tools("hang", refresh=True)
            self.assertIn("timed out", snap_hang.error)

            snap_bad = mcp.discover_server_tools("bad", refresh=True)
            self.assertIn("Malformed MCP frame", snap_bad.error)

            snap_huge = mcp.discover_server_tools("huge", refresh=True)
            self.assertIn("size limit", snap_huge.error)

        self.assertGreaterEqual(len(spawned), 3)
        self.assertTrue(all(p.poll() is not None for p in spawned))


class TestMCPCLIAndPackaging(_MCPTestMixin, IsolatedTestCase):
    def test_cli_mcp_and_doctor_summarize_and_redact_secrets(self):
        secret_tok = "cli-secret-mcp-token-777"
        self.write_mcp_servers({
            "notes": {
                "command": sys.executable,
                "args": [str(self.mock_script), "modern"],
                "env": {"NOTES_API_KEY": secret_tok},
                "trusted": True,
                "blocked_tools": ["mutate_item"],
            },
            "disabled_srv": {
                "command": sys.executable,
                "args": [str(self.mock_script), "legacy_error"],
                "enabled": False,
            },
        })

        with self.allow_subprocesses() as spawned, \
             patch.object(sys, "argv", ["mavrick", "--mcp"]), \
             contextlib.redirect_stdout(io.StringIO()) as mcp_out:
            rc = cli.main()

        self.assertEqual(rc, 0)
        text = mcp_out.getvalue()
        self.assertIn("notes · 2026-07-28 · trusted · 4 tools", text)
        self.assertIn("read_env [read_only]", text)
        self.assertIn("mutate_item [blocked]", text)
        self.assertIn("danger_tool [destructive]", text)
        self.assertIn("disabled_srv · disabled · untrusted", text)
        self.assertNotIn(secret_tok, text)
        self.assertTrue(all(p.poll() is not None for p in spawned))

        with patch.object(cli, "instant_app_index", return_value={}), \
             patch.object(cli, "daemon_socket_status", return_value="missing"), \
             patch.object(cli, "check_ai_health", return_value={"status": "offline", "display": "Offline"}), \
             patch.object(sys, "argv", ["mavrick", "--doctor"]), \
             contextlib.redirect_stdout(io.StringIO()) as doc_out:
            cli.main()
        doc_text = doc_out.getvalue()
        self.assertIn("MCP servers: 2 configured (1 enabled, 1 approved, 1 trusted)", doc_text)
        self.assertNotIn(secret_tok, doc_text)

    def test_deb_and_wheel_include_mcp_package_byte_for_byte(self):
        manifest = runtime_manifest()
        for rel in ("mcp/__init__.py", "mcp/client.py", "mcp/registry.py"):
            self.assertIn(Path(rel), manifest)

        source_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            staging = tmp / "deb_stage"
            stage_payload_files(staging)
            create_control_file(staging, "2.0.0")
            create_maintainer_scripts(staging)

            if shutil.which("dpkg-deb"):
                deb_path = tmp / "mavrick_test.deb"
                extracted = tmp / "deb_extracted"
                with self.allow_subprocesses(("dpkg-deb",)):
                    subprocess.run(["dpkg-deb", "--build", str(staging), str(deb_path)], check=True, stdout=subprocess.DEVNULL)
                    subprocess.run(["dpkg-deb", "-x", str(deb_path), str(extracted)], check=True, stdout=subprocess.DEVNULL)
                for module in manifest:
                    self.assertEqual((extracted / "usr" / "lib" / "mavrick" / module).read_bytes(), (source_root / module).read_bytes())

            wheel_dir = tmp / "wheels"
            wheel_dir.mkdir()
            wheel_src = tmp / "wheel-src"
            shutil.copytree(
                source_root,
                wheel_src,
                ignore=shutil.ignore_patterns(
                    "__pycache__", "*.pyc", "*.deb", "*.whl", "build", "*.egg-info",
                    ".extract", ".pytest_cache", ".mavrick-deb-*",
                ),
            )
            with self.allow_subprocesses():
                proc = subprocess.run(
                    [sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-index", "--no-cache-dir", "--no-build-isolation", "--wheel-dir", str(wheel_dir), str(wheel_src)],
                    capture_output=True,
                    text=True,
                )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            wheels = list(wheel_dir.glob("*.whl"))
            self.assertEqual(len(wheels), 1)
            with zipfile.ZipFile(wheels[0]) as zf:
                for module in manifest:
                    self.assertEqual(zf.read(f"mavrick/{module.as_posix()}"), (source_root / module).read_bytes())


@unittest.skipUnless(ui.Gtk.init_check()[0], "GTK display unavailable")
class TestMCPBroadwayHUD(_MCPTestMixin, IsolatedTestCase):
    def test_hud_renders_mcp_action_and_destructive_workflow_focus(self):
        arg_secret = "hud-mcp-secret-999"
        self.write_mcp_servers({
            "hud_srv": {
                "command": sys.executable,
                "args": [str(self.mock_script), "modern"],
                "env": {"HUD_MCP_KEY": arg_secret},
                "trusted": False,
            }
        })

        window = ui.MavrickWindow()
        self.addCleanup(window.destroy)
        window.show()

        ex = executor.ActionExecutor()
        with self.allow_subprocesses() as spawned:
            prepared = ex.prepare(
                schema.ActionRequest(
                    schema.ActionType.MCP_TOOL,
                    {"server": "hud_srv", "tool": "danger_tool", "arguments": {"token": arg_secret, "target": "notes"}},
                    "",
                    schema.RiskLevel.REVERSIBLE,
                )
            )
            window.show_review_card(prepared)
            while ui.GLib.MainContext.default().pending():
                ui.GLib.MainContext.default().iteration(False)

            self.assertTrue(window.review_box.get_visible())
            self.assertEqual(window.card_title.get_text(), "mcp · hud_srv/danger_tool")
            targets_text = window.card_targets.get_text()
            self.assertIn("[REDACTED_SECRET]", targets_text)
            self.assertNotIn(arg_secret, targets_text)
            self.assertEqual(window.confirm_btn.get_label(), "Run MCP tool")
            self.assertTrue(window.confirm_btn.get_style_context().has_class("btn-destructive"))
            self.assertIs(window.get_focus(), window.cancel_btn)

            plan = schema.parse_workflow_plan([
                {"action": "get_system_health", "params": {}},
                {"action": "mcp_tool", "params": {"server": "hud_srv", "tool": "danger_tool", "arguments": {"target": "notes"}}},
            ], name="hud-mcp-wf")
            review = ex.preview_workflow(plan)
            window.show_workflow_review(review, "Inspect and purge")
            while ui.GLib.MainContext.default().pending():
                ui.GLib.MainContext.default().iteration(False)

            self.assertEqual(window.risk_badge.get_text(), "Destructive")
            self.assertTrue(window.confirm_btn.get_style_context().has_class("btn-destructive"))
            self.assertIs(window.get_focus(), window.cancel_btn)

        self.assertTrue(all(p.poll() is not None for p in spawned))


if __name__ == "__main__":
    unittest.main()
