"""Regressions across file review, execution policy, and command outcomes."""

import io
import os
import subprocess
import tempfile
import threading
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from mavrick.tests import IsolatedTestCase
from mavrick import cli
from mavrick.ai import query_model
from mavrick.executor import ActionExecutor, approve_action, approve_files
from mavrick.history import load_context, load_history
from mavrick.schema import ActionRequest, ActionType, ModelResponse, RiskLevel
from mavrick.tools import apps, files, packages, system


def request(kind, **params):
    # Intentionally untrusted presentation and risk to exercise executor validation.
    return ActionRequest(ActionType(kind), params, "Model-supplied title", RiskLevel.READ_ONLY)


class TestFileWorkflow(IsolatedTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mavrick-file-fixtures-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.executor = ActionExecutor()

    def write(self, name, data):
        file = self.root / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(data)
        return file

    def test_move_conflict_can_be_skipped_or_renamed(self):
        source = self.write("source.txt", "source")
        target = self.write("existing.txt", "occupied")
        action = request("move_files", sources=[str(source)], destination=str(target))
        review = self.executor.execute(action, confirmed=True).details["review"]
        self.assertIn("already exists", review.rows[0].problem)
        self.assertEqual(target.read_text(), "occupied")
        revised = files.rename_review_row(review.rows[0], "new name.txt")
        result = self.executor.execute(action, approval=approve_files(files.FileReview(review.action, (revised,)), [0]))
        self.assertEqual(result.status, "success")
        self.assertEqual((self.root / "new name.txt").read_text(), "source")
        self.assertEqual(target.read_text(), "occupied")
        self.assertFalse(source.exists())

    def test_undo_conflict_retains_records_across_later_move(self):
        source = self.write("one.txt", "moved")
        move = request("move_files", sources=[str(source)], destination=str(self.root / "sorted"))
        review = self.executor.execute(move).details["review"]
        result = self.executor.execute(move, approval=approve_files(review, [0]))
        undo = request("undo", undo_id=result.details["undo_id"])
        self.write("one.txt", "new occupant")
        review = self.executor.execute(undo).details["review"]
        self.assertIn("already exists", review.rows[0].problem)
        self.assertEqual(self.executor.execute(undo, approval=approve_files(review, [0])).status, "review")
        other = self.write("other.txt", "other")
        self.assertTrue(files.move_files([str(other)], str(self.root / "other-dir"))[0])
        review = self.executor.execute(undo).details["review"]
        fresh = files.rename_review_row(review.rows[0], "restored.txt")
        result = self.executor.execute(undo, approval=approve_files(files.FileReview(review.action, (fresh,), review.undo_id), [0]))
        self.assertEqual(result.status, "success")
        self.assertEqual(source.read_text(), "new occupant")
        self.assertEqual((self.root / "restored.txt").read_text(), "moved")

    def test_rechecks_changed_source_and_destination_before_any_move(self):
        first = self.write("first.txt", "first")
        second = self.write("second.txt", "second")
        excluded = self.write("excluded.png", "excluded")
        action = request("organize_downloads", target_dir=str(self.root), dry_run=False)
        review = self.executor.execute(action, confirmed=True).details["review"]
        self.assertFalse((self.root / "Documents").exists())
        selected = [i for i, row in enumerate(review.rows) if row.source != str(excluded)]
        first.write_text("changed source")
        self.write("Documents/second.txt", "new target")
        added = self.write("third.txt", "arrived after preview")
        response = self.executor.execute(action, approval=approve_files(review, selected))
        self.assertEqual(response.status, "review")
        refreshed = response.details["review"]
        self.assertEqual([row.changed for row in refreshed.rows if row.included], [True, True])
        self.assertTrue(second.exists())
        self.assertTrue(first.exists())
        selected = [i for i, row in enumerate(refreshed.rows) if row.included and not row.problem]
        result = self.executor.execute(action, approval=approve_files(refreshed, selected))
        self.assertEqual(result.summary, "1 moved, 2 skipped, 0 failed.")
        self.assertTrue(excluded.exists())
        self.assertTrue(added.exists())
        self.assertEqual((self.root / "Documents/second.txt").read_text(), "new target")

    def test_same_new_destination_directory_and_stop_after_partial_move(self):
        for name in ("a.txt", "b.txt", "c.txt"):
            self.write(name, name)
        action = request("organize_downloads", target_dir=str(self.root))
        review = self.executor.execute(action).details["review"]
        rename = files._rename_no_replace
        called = 0

        def one_then_stop(*args):
            nonlocal called
            rename(*args)
            called += 1
            if called == 2:
                self.assertTrue(self.executor.cancel_current_job("selected-job"))

        with patch.object(files, "_rename_no_replace", side_effect=one_then_stop):
            result = self.executor.execute(action, approval=approve_files(review, [0, 1, 2]),
                                           job_id="selected-job", query="organize test downloads")
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(result.summary, "Stopped. 2 moved, 1 skipped, 0 failed.")
        self.assertTrue(result.undo_available)
        self.assertEqual(load_history()[-1]["job_id"], "selected-job")
        undo = request("undo", undo_id=result.details["undo_id"])
        undo_review = self.executor.execute(undo).details["review"]
        self.assertEqual(len(undo_review.rows), 2)
        restored = self.executor.execute(undo, approval=approve_files(undo_review, [0, 1]))
        self.assertEqual(restored.summary, "2 restored, 0 skipped, 0 failed.")
        self.assertTrue(all((self.root / name).exists() for name in ("a.txt", "b.txt", "c.txt")))

    def test_cli_downloads_exclusion_is_used_even_with_yes(self):
        self.write("a.txt", "include")
        second = self.write("b.txt", "exclude")
        action = request("organize_downloads", target_dir=str(self.root))
        with patch.object(cli, "query_model", return_value=ModelResponse("action", action_request=action)), \
             patch("builtins.input", side_effect=["skip 2", "y"]) as choose, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.execute_cli_query("Organize Downloads", auto_confirm=True), 0)
        self.assertIn("Move 2 files", choose.call_args_list[0].args[0])
        self.assertIn("Move 1 file", choose.call_args_list[1].args[0])
        self.assertIn("1 moved, 1 skipped, 0 failed.", output.getvalue())
        self.assertTrue(second.exists())
        self.assertTrue((self.root / "Documents/a.txt").exists())

    def test_destination_appearing_at_rename_is_never_replaced(self):
        source = self.write("source.txt", "original")
        target = self.root / "new.txt"
        action = request("move_files", sources=[str(source)], destination=str(target))
        review = self.executor.execute(action).details["review"]
        rename = files._rename_no_replace

        def conflict(*args):
            target.write_text("concurrent writer")
            rename(*args)

        with patch.object(files, "_rename_no_replace", side_effect=conflict):
            result = self.executor.execute(action, approval=approve_files(review, [0]))
        self.assertEqual(result.status, "failed")
        self.assertIn("1 failed", result.summary)
        self.assertTrue(result.details["review"].rows[0].changed)
        self.assertEqual(target.read_text(), "concurrent writer")
        self.assertEqual(source.read_text(), "original")


class TestExecutionWorkflow(IsolatedTestCase):
    def test_stop_keeps_job_active_and_scoped_until_settled(self):
        executor = ActionExecutor()
        working = threading.Event()
        release = threading.Event()
        results = []

        def health():
            working.set()
            self.assertTrue(release.wait(2))
            return {"report": "Work settled"}

        job = request("get_system_health")
        with patch("mavrick.executor.get_system_health", side_effect=health):
            thread = threading.Thread(target=lambda: results.append(executor.execute(job, job_id="first")))
            thread.start()
            try:
                self.assertTrue(working.wait(2))
                self.assertFalse(executor.cancel_current_job("different"))
                self.assertTrue(executor.cancel_current_job("first"))
                self.assertEqual(executor.execute(job, job_id="new").status, "busy")
                self.assertEqual(ActionExecutor().execute(job, job_id="other-instance").status, "busy")
                self.assertEqual(executor.get_active_job().id, "first")
            finally:
                release.set()
                thread.join(2)
        self.assertEqual(results[0].status, "cancelled")
        self.assertIsNone(executor.get_active_job())

    def test_cli_yes_requires_exact_privileged_and_destructive_approval(self):
        for action, title, patch_target in (
            (request("install_package", package_name="vlc", manager="flatpak", scope="user"),
             "Install VLC from Flathub", "mavrick.executor._package_operation"),
            (request("lock_session"), "Lock desktop session", "mavrick.executor.lock_session"),
        ):
            with self.subTest(action=action.action_type), patch(patch_target) as backend, \
                 patch("mavrick.cli.query_model", return_value=ModelResponse("action", action_request=action)), \
                 patch("builtins.input", return_value="no") as confirm, redirect_stdout(io.StringIO()) as output:
                self.assertEqual(cli.execute_cli_query(title, auto_confirm=True), 0)
                self.assertIn(title, confirm.call_args.args[0])
                self.assertNotIn("Model-supplied title", output.getvalue())
                backend.assert_not_called()

    def test_approval_is_bound_to_exact_package_and_verified_changes(self):
        executor = ActionExecutor()
        vlc = request("install_package", package_name="vlc", manager="flatpak", scope="user")
        pending = executor.execute(vlc, confirmed=True)
        approval = approve_action(pending.details["action"])
        other = request("install_package", package_name="com.spotify.Client", manager="flatpak", scope="user")
        self.assertEqual(executor.execute(other, approval=approval).status, "requires_confirmation")
        before = load_context().get("last_verified_package")
        with patch("mavrick.executor._package_operation", return_value=(True, "Opened installer", {"verification": "launch_requested"})):
            self.assertEqual(executor.execute(vlc, approval=approval).verification_status, "launch_requested")
        self.assertEqual(load_context().get("last_verified_package"), before)
        verified = {"package": "org.videolan.VLC", "manager": "flatpak", "scope": "user",
                    "changed": True, "verification": "verified"}
        with patch("mavrick.executor._package_operation", return_value=(True, "Verified package", verified)):
            self.assertEqual(executor.execute(vlc, approval=approval).status, "success")
        self.assertEqual(query_model("uninstall that").action_request.params["package_name"], "org.videolan.VLC")
        with patch.object(packages, "_installed", return_value=True):
            result = executor.execute(request("uninstall_package", package_name="vlc"))
        self.assertIn("Which installation", result.summary)
        self.assertEqual(result.status, "failed")

    def test_package_compatibility_wrapper_asks_for_exact_approval(self):
        with patch("mavrick.executor._package_operation") as backend:
            ok, title, details = packages.install_package("vlc", "flatpak", scope="user")
        self.assertFalse(ok)
        self.assertEqual(title, "Install VLC from Flathub")
        self.assertEqual(details["status"], "requires_confirmation")
        backend.assert_not_called()

    def test_launch_tools_reject_unapproved_commands_and_paths(self):
        executor = ActionExecutor()
        bad = [
            request("open_app", app_name="python3", args=["-c", "print(0)"]),
            request("run_in_terminal", command="bash"),
            request("open_url", browser="/tmp/browser", url="https://example.org"),
            request("arrange_windows", left_target="firefox", right_target="/tmp/editor")
        ]
        with patch.object(apps, "launch_command") as launch:
            for action in bad:
                self.assertEqual(executor.execute(action).status, "failed")
            launch.assert_not_called()

    def test_plain_claude_opens_desktop_and_explicit_terminal_opens_claude_code(self):
        desktop = Path(os.environ["XDG_DATA_HOME"], "applications", "com.anthropic.Claude.desktop")
        desktop.parent.mkdir(exist_ok=True)
        desktop.write_text("[Desktop Entry]\nName=Claude\nType=Application\nExec=claude-desktop\n")
        self.addCleanup(desktop.unlink)
        installed = {
            "gtk-launch": "/mock/gtk-launch",
            "cosmic-term": "/mock/cosmic-term",
            "claude": "/mock/claude",
        }
        with patch("mavrick.ai.get_custom_command") as custom, \
             patch.object(apps.shutil, "which", side_effect=installed.get), \
             patch.object(apps, "launch_command", return_value=(True, "Launch requested", {})) as launch:
            executor = ActionExecutor()
            for prompt in ("open claude", "open claude desktop", "open claude terminal",
                           "open claude in terminal", "open Claude Code"):
                with self.subTest(prompt=prompt):
                    result = executor.execute(query_model(prompt).action_request)
                    self.assertEqual(result.status, "success")
                    self.assertEqual(result.verification_status, "launch_requested")
            custom.assert_not_called()
        desktop_launch = ["/mock/gtk-launch", "com.anthropic.Claude.desktop"]
        terminal_launch = ["/mock/cosmic-term", "-e", "/mock/claude"]
        self.assertEqual([call.args[0] for call in launch.call_args_list],
                         [desktop_launch, desktop_launch, terminal_launch, terminal_launch, terminal_launch])

    def test_claude_targets_report_missing_installation_and_reject_arguments(self):
        executor = ActionExecutor()
        with patch("mavrick.policy.desktop_app_ids", return_value={}), \
             patch.object(apps.shutil, "which", return_value=None), \
             patch.object(apps, "launch_command") as launch:
            desktop = executor.execute(query_model("open claude").action_request)
            terminal = executor.execute(query_model("open claude in terminal").action_request)
            injected = executor.execute(request("run_in_terminal", command="claude", args=["--print", "example"]))
        self.assertEqual(desktop.status, "failed")
        self.assertIn("claude desktop", desktop.summary.lower())
        self.assertEqual(terminal.status, "failed")
        self.assertIn("Claude Code executable", terminal.summary)
        self.assertEqual(injected.status, "failed")
        launch.assert_not_called()

    def test_split_screen_prompt_launches_files_and_claude_desktop(self):
        desktop = Path(os.environ["XDG_DATA_HOME"], "applications", "com.anthropic.Claude.desktop")
        desktop.parent.mkdir(exist_ok=True)
        desktop.write_text("[Desktop Entry]\nName=Claude\nType=Application\nExec=claude-desktop\n")
        self.addCleanup(desktop.unlink)
        commands = {"cosmic-files": "/mock/cosmic-files", "gtk-launch": "/mock/gtk-launch"}

        with patch("mavrick.ai.get_custom_command") as custom, \
             patch.object(apps.shutil, "which", side_effect=commands.get), \
             patch.object(apps, "is_wayland_session", return_value=True), \
             patch.object(apps, "is_cosmic_desktop", return_value=True), \
             patch.object(apps, "launch_command", side_effect=[
                 (True, "Files launch requested", {}),
                 (True, "Claude launch requested", {}),
                 (True, "Files launch requested", {}),
                 (False, "Claude launcher exited with status 1", {}),
             ]) as launch:
            proposal = query_model("open files and calude app in split screen").action_request
            custom.assert_not_called()
            self.assertEqual(proposal.action_type, ActionType.ARRANGE_WINDOWS)
            executor = ActionExecutor()
            result = executor.execute(proposal)
            failed = executor.execute(proposal)

        self.assertEqual(result.status, "success")
        self.assertEqual(result.verification_status, "launch_requested")
        self.assertEqual([call.args[0] for call in launch.call_args_list[:2]],
                         [["/mock/cosmic-files"], ["/mock/gtk-launch", "com.anthropic.Claude.desktop"]])
        self.assertIn("Files launch requested", result.stdout)
        self.assertIn("Claude launch requested", result.stdout)
        self.assertIn("Tile current workspace", result.stdout)
        self.assertEqual(failed.status, "partial")
        self.assertIn("Files launch requested", failed.stdout)
        self.assertIn("Claude launcher exited with status 1", failed.stdout)

    def test_split_screen_missing_app_names_the_unresolved_target(self):
        with patch("mavrick.policy.desktop_app_ids", return_value={}), \
             patch.object(apps, "launch_command") as launch:
            proposal = query_model("open files and claude app in split screen").action_request
            result = ActionExecutor().execute(proposal)
        self.assertEqual(result.status, "failed")
        self.assertIn("claude app", result.summary)
        launch.assert_not_called()

    def test_split_screen_browser_shorthand_preserves_both_sites(self):
        proposal = query_model("split screen youtube and reddit in brave").action_request
        self.assertEqual(proposal.action_type, ActionType.ARRANGE_WINDOWS)
        self.assertEqual(proposal.params["left_target"], "https://www.youtube.com")
        self.assertEqual(proposal.params["right_target"], "https://www.reddit.com")
        self.assertEqual(proposal.params["browser"], "brave")

    def test_update_failure_preserves_individual_backend_results(self):
        def command(argv, **kwargs):
            if argv[0] == "apt":
                return subprocess.CompletedProcess(argv, 20, "", "Package index unavailable")
            return subprocess.CompletedProcess(argv, 0, "org.mozilla.firefox\n", "")
        with patch.object(packages.shutil, "which", return_value="/mock/bin"), \
             patch.object(packages.subprocess, "run", side_effect=command):
            result = ActionExecutor().execute(request("check_updates"))
        self.assertEqual(result.status, "partial")
        self.assertIn("APT: Package index unavailable", result.stdout)
        self.assertIn("org.mozilla.firefox", result.stdout)
        self.assertNotIn("Up to date", result.stdout)

    def test_package_command_completion_and_backend_errors_are_distinct(self):
        def launch(command, label):
            self.assertEqual(command[-6:], ["flatpak", "install", "--user", "--", "flathub", "org.videolan.VLC"])
            Path(command[-7]).write_text("1")
            return True, f"Launch requested for {label}", {"verification": "launch_requested"}

        with patch.object(packages, "_installed", side_effect=[False, False]), \
             patch.object(packages, "detect_preferred_terminal", return_value="/mock/terminal"), \
             patch.object(packages.shutil, "which", return_value="/mock/flatpak"), \
             patch.object(packages, "launch_command", side_effect=launch):
            executor = ActionExecutor()
            proposed = request("install_package", package_name="vlc", manager="flatpak", scope="user")
            pending = executor.execute(proposed)
            result = executor.execute(proposed, approval=approve_action(pending.details["action"]))
        self.assertEqual(result.status, "failed")
        self.assertIn("exited with status 1", result.summary)
        self.assertFalse(result.details["changed"])

    def test_workspace_review_and_individual_launch_results(self):
        with tempfile.TemporaryDirectory() as project:
            action = request("open_workspace", project=project)
            def which(name):
                return f"/mock/{name}" if name in ("code", "cosmic-term") else None
            with patch.object(apps.shutil, "which", side_effect=which), \
                 patch.object(apps, "launch_command", side_effect=[
                     (True, "Editor launch requested", {}), (False, "Terminal unavailable", {})
                 ]) as launch:
                executor = ActionExecutor()
                pending = executor.execute(action)
                exact = pending.details["action"]
                self.assertEqual(pending.status, "requires_confirmation")
                self.assertIn(project, exact.affected_targets[0])
                launch.assert_not_called()
                result = executor.execute(action, approval=approve_action(exact))
                self.assertEqual(result.status, "partial")
                self.assertIn("Editor launch requested", result.stdout)
                self.assertIn("Terminal unavailable", result.stdout)
                self.assertEqual(launch.call_args_list[0].args[0], ["/mock/code", project])

    def test_named_pause_is_idempotent_and_mute_preserves_volume(self):
        playback = {"spotify": "Playing", "vlc": "Playing"}
        seen = []

        def playerctl(argv, **kwargs):
            seen.append(argv)
            player, verb = argv[2:4]
            if verb in ("pause", "play"):
                playback[player] = "Paused" if verb == "pause" else "Playing"
            output = f"A\tTrack\t{playback[player]}" if verb == "metadata" else ""
            return subprocess.CompletedProcess(argv, 0, output, "")

        executor = ActionExecutor()
        with patch.object(system.shutil, "which", return_value="/mock/playerctl"), \
             patch.object(system.subprocess, "run", side_effect=playerctl):
            for command in ("Pause Spotify", "Pause Spotify", "Resume Spotify"):
                action = query_model(command).action_request
                pending = executor.execute(action)
                result = executor.execute(action, approval=approve_action(pending.details["action"]))
                self.assertEqual(result.status, "success")
        self.assertEqual([row[-1] for row in seen[::2]], ["pause", "pause", "play"])
        self.assertEqual(playback["vlc"], "Playing")

        with patch.object(system.shutil, "which", return_value="/mock/wpctl"), \
             patch.object(system.subprocess, "run", side_effect=[
                 subprocess.CompletedProcess([], 0, "", ""),
                 subprocess.CompletedProcess([], 0, "Volume: 0.62 [MUTED]", "")
             ]) as run:
            self.assertTrue(system.set_volume(mute=True)[0])
            self.assertEqual(run.call_args_list[0].args[0][1], "set-mute")
        self.assertEqual(query_model("Turn it down a little").action_request.params, {"delta": -5})
        self.assertEqual(query_model("What power mode am I on?").action_request.action_type, ActionType.GET_POWER_PROFILE)
