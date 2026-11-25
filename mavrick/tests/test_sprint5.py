"""Offline workflow policy, grouped Undo, GTK review, and package regressions."""

import contextlib
import importlib.util
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
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

from mavrick.tests import IsolatedTestCase, TEST_ROOT, _real_popen
from mavrick import ai, cli, config, package_deb, router, ui
from mavrick.executor import ActionExecutor, approve_files
from mavrick.policy import WorkflowRisk
from mavrick.platform import use_platform
from mavrick.platform.windows import WindowsAdapter
from mavrick.schema import ActionType, ModelResponse, parse_workflow_plan, validate_action_params
from mavrick.tools import files
from mavrick.workflows import approve_workflow, workflow_catalog


def _step(action, **params):
    return {"action": action, "params": params}


def _status(result):
    return [step["status"] for step in result.details["steps"]]


class TestWorkflows(IsolatedTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(dir=TEST_ROOT)
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.journal = self.root / "undo.json"
        self.catalog = self.root / "workflows.json"
        self.addCleanup(patch.object(files, "UNDO_STATE_FILE", str(self.journal)).stop)
        undo = patch.object(files, "UNDO_STATE_FILE", str(self.journal))
        recipes = patch.object(config, "WORKFLOWS_FILE", str(self.catalog))
        undo.start()
        recipes.start()
        self.addCleanup(undo.stop)
        self.addCleanup(recipes.stop)
        self.executor = ActionExecutor()

    def _move(self, filename, destination=None):
        source = self.root / filename
        source.write_text(filename)
        destination = destination or self.root / "sorted" / filename
        return source, destination, _step("move_files", sources=[str(source)], destination=str(destination))

    def _run_approved(self, plan, **options):
        pending = self.executor.execute(plan, preview=True)
        self.assertEqual(pending.status, "workflow_review", pending.stdout)
        return self.executor.execute(plan, approval=approve_workflow(pending.details["workflow_review"]), **options)

    def _undo_review(self, batch_id=None):
        params = {"undo_id": batch_id} if batch_id else {}
        action = validate_action_params("undo", params)[2]
        pending = self.executor.execute(action)
        self.assertEqual(pending.status, "review", pending.summary)
        return action, pending.details["review"]

    def _save(self, name, *steps):
        self.catalog.write_text(json.dumps({"workflows": [
            {"name": name, "description": "Workflow fixture", "steps": list(steps)}
        ]}))
        workflow_catalog(refresh=True)

    def test_provider_plan_runs_read_only_and_file_move_after_exact_approval(self):
        source, dest, move = self._move("fixture.txt")
        proposal = ai.parse_model_response(json.dumps({"type": "plan", "plan": [_step("get_system_health"), move]}))
        inode = source.stat().st_ino
        self.assertEqual(proposal.response_type, "plan")
        with patch("mavrick.executor.get_system_health", return_value={"report": "Healthy fixture"}) as health:
            result = self._run_approved(proposal.plan, query="fixture workflow")
        self.assertEqual(result.status, "success", result.summary)
        self.assertEqual(_status(result), ["completed", "completed"])
        self.assertEqual(result.details["steps"][0]["output"], "Healthy fixture")
        health.assert_called_once_with()
        self.assertFalse(source.exists())
        self.assertEqual(dest.stat().st_ino, inode)
        self.assertTrue(result.undo_available)
        batch = json.loads(self.journal.read_text())["batches"][-1]
        self.assertEqual(batch["id"], result.details["batch_id"])
        self.assertEqual(batch["moves"][0]["step"], 2)

    def test_destructive_step_escalates_and_confirmed_flag_cannot_bypass_review(self):
        plan = parse_workflow_plan([_step("get_system_health"), _step("clean_cache", dry_run=False)])
        with patch("mavrick.executor.get_system_health") as health, patch("mavrick.executor.clean_cache") as clean:
            result = self.executor.execute(plan, confirmed=True)
            self.assertEqual(result.status, "workflow_review")
            self.assertFalse(self.journal.exists())
        review = result.details["workflow_review"]
        self.assertEqual(review.risk, WorkflowRisk.DESTRUCTIVE)
        self.assertEqual([step.risk for step in review.steps], [WorkflowRisk.READ_ONLY, WorkflowRisk.DESTRUCTIVE])
        health.assert_not_called()
        clean.assert_not_called()

    def test_disallowed_step_blocks_preflight_before_first_action(self):
        plan = parse_workflow_plan([_step("get_system_health"), _step("list_files", path="/etc")])
        with patch("mavrick.executor.get_system_health") as health:
            result = self.executor.execute(plan)
        self.assertEqual(result.status, "failed")
        self.assertEqual(_status(result), ["skipped", "failed"])
        self.assertIn("/etc", result.details["steps"][1]["summary"])
        self.assertIn("Policy: blocked", result.stdout)
        self.assertFalse(self.journal.exists())
        health.assert_not_called()

    def test_platform_unsupported_step_and_unexpected_model_parameters(self):
        plan = parse_workflow_plan([_step("get_system_health"), _step("lock_session")])
        with use_platform(WindowsAdapter(self.root)):
            review = self.executor.preview_workflow(plan)
        self.assertFalse(review.allowed)
        self.assertEqual(review.risk, WorkflowRisk.DESTRUCTIVE)
        self.assertIn("windows", review.steps[1].error)
        malformed = ai.parse_model_response(json.dumps({"type": "plan", "plan": [
            _step("get_system_health"), _step("get_system_health", command="forbidden")
        ]}))
        self.assertEqual(malformed.response_type, "error")
        self.assertIn("Step 2", malformed.validation_error)

    def test_changed_file_invalidates_whole_approval_before_any_step_runs(self):
        source, dest, move = self._move("fixture.txt")
        plan = parse_workflow_plan([_step("get_system_health"), move])
        preview = self.executor.preview_workflow(plan)
        source.write_text("changed after authorization")
        with patch("mavrick.executor.get_system_health") as health:
            result = self.executor.execute(plan, approval=approve_workflow(preview))
        self.assertEqual(result.status, "workflow_review")
        self.assertIn("changed", result.summary)
        health.assert_not_called()
        self.assertFalse(dest.exists())
        self.assertFalse(self.journal.exists())

    def test_mid_plan_failure_stops_later_steps_and_retains_grouped_undo(self):
        source, dest, move = self._move("fixture.txt")
        plan = parse_workflow_plan([
            _step("get_system_health"), move, _step("get_power_profile"), _step("get_system_health")
        ])
        with patch("mavrick.executor.get_system_health", return_value={"report": "Healthy fixture"}) as health, \
             patch("mavrick.executor.get_power_profile", return_value=(False, "Backend unavailable")):
            result = self._run_approved(plan)
        self.assertEqual(result.status, "partial")
        self.assertEqual(_status(result), ["completed", "completed", "failed", "skipped"])
        health.assert_called_once_with()
        self.assertTrue(dest.exists())
        action, review = self._undo_review(result.details["batch_id"])
        self.assertEqual(review.rows[0].workflow_step, 2)
        restored = self.executor.execute(action, approval=approve_files(review, [0]))
        self.assertEqual(restored.status, "success", restored.stdout)
        self.assertEqual(source.read_text(), "fixture.txt")

    def test_batch_undo_reverses_steps_and_continues_after_conflict(self):
        entries = [self._move(f"{name}.txt") for name in ("first", "second", "third")]
        plan = parse_workflow_plan([step for _, _, step in entries])
        result = self._run_approved(plan)
        self.assertEqual(result.status, "success", result.stdout)
        batch_id = result.details["batch_id"]
        action, review = self._undo_review()
        self.assertEqual(review.undo_id, batch_id)
        self.assertEqual([row.workflow_step for row in review.rows], [3, 2, 1])
        occupied, retained, _ = entries[1]
        original_inode = retained.stat().st_ino
        occupied.write_text("conflicting file")
        progress = []
        restored = self.executor.execute(action, approval=approve_files(review, [0, 1, 2]),
                                         progress_callback=progress.append)
        self.assertEqual(restored.status, "partial", restored.stdout)
        self.assertEqual(restored.details["restored_count"], 2)
        self.assertIn("Step 2", restored.stdout)
        self.assertEqual([line.split(" · ")[0] for line in progress if "Restoring " in line],
                         ["Step 3", "Step 2", "Step 1"])
        self.assertEqual(occupied.read_text(), "conflicting file")
        self.assertEqual(retained.stat().st_ino, original_inode)
        self.assertEqual(retained.read_text(), "second.txt")
        self.assertTrue(entries[0][0].exists() and entries[2][0].exists())
        _, remaining = self._undo_review(batch_id)
        self.assertEqual([row.workflow_step for row in remaining.rows], [2])

    def test_read_only_runs_get_distinct_durable_batch_ids(self):
        plan = parse_workflow_plan([_step("get_system_health"), _step("get_system_health")])
        with patch("mavrick.executor.get_system_health", return_value={"report": "Healthy fixture"}):
            one, two = (self.executor.execute(plan) for _ in range(2))
        ids = [one.details["batch_id"], two.details["batch_id"]]
        self.assertNotEqual(*ids)
        self.assertFalse(one.undo_available or two.undo_available)
        self.assertEqual([batch["id"] for batch in json.loads(self.journal.read_text())["batches"]], ids)

    def test_saved_router_is_local_fast_and_never_bypasses_policy(self):
        source, dest, move = self._move("fixture.txt")
        self._save("tidy-fixture", move)
        with patch.object(ai, "create_provider", side_effect=AssertionError("Saved workflow called a model")):
            for phrase in ("/tidy-fixture", "run tidy-fixture"):
                response = ai.query_model(phrase)
                self.assertEqual(response.source, "instant_workflow")
                gate = self.executor.execute(response.plan)
                self.assertEqual(gate.status, "workflow_review")
            self.assertEqual(ai.query_model("/unknown-workflow").response_type, "error")
        timings = []
        for _ in range(1000):
            start = time.perf_counter_ns()
            match = router.route_local("/tidy-fixture", cached_only=True)
            timings.append((time.perf_counter_ns() - start) / 1_000_000)
            self.assertEqual(match.kind, "workflow")
        self.assertLess(max(timings), 30)
        print(f"Saved workflow routing: 1000 samples, p50={sorted(timings)[500]:.3f} ms, max={max(timings):.3f} ms")
        self.assertTrue(source.exists())
        self.assertFalse(dest.exists())

    def test_cli_lists_dry_runs_and_requires_explicit_confirmation_with_yes(self):
        source, dest, move = self._move("fixture.txt")
        self._save("tidy-fixture", _step("get_system_health"), move)
        with patch.object(ai, "create_provider", side_effect=AssertionError("Recipe called a provider")), \
             patch("mavrick.executor.get_system_health") as health, \
             patch.object(cli.ActionExecutor, "get_instance", return_value=self.executor):
            for argv, text in (
                (["--workflows"], "tidy-fixture · 2 steps · mutating"),
                (["--run-workflow", "tidy-fixture", "--dry-run"], f"{source} → {dest}"),
                (["--yes", "/tidy-fixture"], "Workflow cancelled."),
            ):
                with self.subTest(argv=argv), patch.object(sys, "argv", ["mavrick", *argv]), \
                     patch("builtins.input", return_value="no") as confirm, \
                     contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(cli.main(), 0)
                    self.assertIn(text, output.getvalue())
                    if "--dry-run" in argv:
                        self.assertIn("Policy: approval required", output.getvalue())
                    if "--yes" in argv:
                        confirm.assert_called_once()
                    else:
                        confirm.assert_not_called()
        health.assert_not_called()
        self.assertFalse(self.journal.exists())

    def _pump_until(self, condition):
        context = ui.GLib.MainContext.default()
        end = time.monotonic() + 2
        while not condition() and time.monotonic() < end:
            context.iteration(False)
            time.sleep(0.005)
        self.assertTrue(condition())

    @unittest.skipUnless(ui.Gtk.init_check()[0], "GTK display unavailable")
    def test_broadway_destructive_review_focus_and_escape_halt(self):
        plan = parse_workflow_plan([_step("get_system_health"), _step("clean_cache", dry_run=False)])
        window = ui.MavrickWindow()
        window.show()
        self.addCleanup(window.destroy)
        begun, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def health_tool():
            begun.set()
            release.wait(2)
            return {"report": "Healthy fixture"}

        original = window.on_execution_complete
        results = []

        def on_complete(action, result):
            results.append(result)
            original(action, result)

        with patch.object(window, "executor", self.executor), \
             patch.object(window, "on_execution_complete", side_effect=on_complete), \
             patch.object(ui, "query_model", return_value=ModelResponse("plan", plan=plan)), \
             patch("mavrick.executor.get_system_health", side_effect=health_tool), \
             patch("mavrick.executor.clean_cache") as clean:
            window.entry.set_text("workflow broadband fixture request")
            window.on_submit(window.entry)
            self._pump_until(lambda: window.state.workflow_review is not None)
            self.assertEqual(window.confirm_btn.get_label(), "Run 2 steps")
            self.assertEqual(len(window.file_list.get_children()), 2)
            self.assertEqual(window.get_focus(), window.cancel_btn)
            self.assertTrue(window.confirm_btn.get_style_context().has_class("btn-destructive"))
            window.on_confirm_action()
            self._pump_until(lambda: begun.is_set() and "Step 1 of 2" in window.progress_label.get_text())
            self.assertTrue(window.handle_escape())
            self.assertTrue(window.cancel_token.is_set())
            release.set()
            self._pump_until(lambda: len(results) == 2 and not window.is_processing)
        clean.assert_not_called()
        self.assertEqual(results[-1].status, "cancelled")
        self.assertEqual(_status(results[-1]), ["completed", "skipped"])

    @unittest.skipUnless(ui.Gtk.init_check()[0], "GTK display unavailable")
    def test_broadway_privileged_border_and_instant_workflow_batch_undo(self):
        priv_plan = parse_workflow_plan([_step("get_system_health"), _step("install_package", package_name="htop", manager="apt")])
        first_src, first_dst, move_one = self._move("alpha.txt")
        second_src, second_dst, move_two = self._move("beta.txt")
        self._save("tidy-pair", move_one, move_two)

        window = ui.MavrickWindow()
        window.show()
        self.addCleanup(window.destroy)

        with patch.object(window, "executor", self.executor), \
             patch.object(ui, "query_model", return_value=ModelResponse("plan", plan=priv_plan)), \
             patch("mavrick.executor.resolve_package", return_value=("htop", "apt", None)):
            window.entry.set_text("install htop after health")
            window.on_submit(window.entry)
            self._pump_until(lambda: window.state.workflow_review is not None)
            self.assertTrue(window.review_box.get_style_context().has_class("review-privileged"))
            self.assertFalse(window.confirm_btn.get_style_context().has_class("btn-destructive"))
            self.assertEqual(window.get_focus(), window.cancel_btn)
            window.toggle_history()
            self.assertFalse(window.history_mode)
            window.on_cancel_action()
            self.assertIsNone(window.state.pending_workflow)

        with patch.object(window, "executor", self.executor), \
             patch.object(ai, "create_provider", side_effect=AssertionError("Instant workflow called AI")):
            workflow_catalog(refresh=True)
            window.entry.set_text("/tidy-pair")
            self.assertTrue(window.preview_label.is_visible())
            self.assertIn("Instant · Workflow", window.preview_label.get_text())
            window.on_submit(window.entry)
            self._pump_until(lambda: window.state.workflow_review is not None)
            self.assertEqual(window.engine_tag.get_text(), "Instant · Workflow")
            self.assertEqual(window.confirm_btn.get_label(), "Run 2 steps")
            window.on_confirm_action()
            self._pump_until(lambda: not window.is_processing and window.undo_btn.get_visible())
            self.assertTrue(first_dst.exists() and second_dst.exists())
            self.assertFalse(first_src.exists() or second_src.exists())

            window.on_trigger_undo()
            self._pump_until(lambda: window.file_review is not None)
            self.assertEqual(window.card_title.get_text(), "Review batch Undo")
            self.assertEqual([row.workflow_step for row in window.file_rows], [2, 1])
            self.assertEqual(window.confirm_btn.get_label(), "Restore 2 files")
            window.on_confirm_action()
            self._pump_until(lambda: not window.is_processing and first_src.exists() and second_src.exists())
        self.assertEqual(first_src.read_text(), "alpha.txt")
        self.assertEqual(second_src.read_text(), "beta.txt")


@unittest.skipUnless(shutil.which("dpkg-deb") and importlib.util.find_spec("wheel"), "Build tools unavailable")
class TestWorkflowPackaging(IsolatedTestCase):
    def test_source_matches_workflows_module_in_deb_and_wheel(self):
        source = package_deb.SOURCE_DIR
        manifest = package_deb.runtime_manifest()
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as directory:
            root = Path(directory)
            stage = root / "stage"
            deb = root / "fixture.deb"
            extracted = root / "unpacked"
            wheel_src = root / "wheel-src"
            shutil.copytree(source, wheel_src, ignore=shutil.ignore_patterns(
                "__pycache__", "*.pyc", "*.deb", "*.zip", ".extract", ".pytest_cache", ".mavrick-deb-*"))
            command = [sys.executable, "-m", "pip", "wheel", "--disable-pip-version-check",
                       "--no-deps", "--no-index", "--no-cache-dir", "--no-build-isolation",
                       "--wheel-dir", str(root), str(wheel_src)]

            def packaging_only(argv, *args, **kwargs):
                self.assertTrue(argv[0] == "dpkg-deb" or argv == command)
                return _real_popen(argv, *args, **kwargs)

            package_deb.create_control_file(stage, "2.0.0")
            package_deb.stage_payload_files(stage)
            with patch.object(subprocess, "Popen", side_effect=packaging_only):
                self.assertTrue(package_deb.build_deb_package(stage, deb))
                subprocess.run(["dpkg-deb", "-x", str(deb), str(extracted)], check=True)
                built = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=20)
                self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
            self.assertIn(Path("workflows.py"), manifest)
            wheel, = root.glob("*.whl")
            with ZipFile(wheel) as contents:
                for module in manifest:
                    expected = (source / module).read_bytes()
                    self.assertEqual((extracted / "usr/lib/mavrick" / module).read_bytes(), expected)
                    self.assertEqual(contents.read(f"mavrick/{module.as_posix()}"), expected)
