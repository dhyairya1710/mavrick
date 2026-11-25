import unittest
from mavrick.tests import IsolatedTestCase
import subprocess
from unittest.mock import patch, MagicMock
import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
from gi.repository import Gtk, Gdk
from mavrick.ui import MavrickWindow
from mavrick.schema import ActionType, ActionRequest, RiskLevel
from mavrick.executor import ActionResult

@unittest.skipUnless(Gtk.init_check()[0], "GTK display unavailable")
class TestMavrickUI(IsolatedTestCase):
    def setUp(self):
        self.win = MavrickWindow()
        self.addCleanup(self.win.destroy)
        self.addCleanup(patch.stopall)
        patch.object(self.win, "send_desktop_notification").start()

    def test_window_initialization(self):
        """GTK window initializes with correct widget structure and ready status."""
        self.assertIsInstance(self.win, Gtk.Window)
        self.assertTrue(self.win.get_resizable())
        self.assertIsNotNone(self.win.entry)
        self.assertIsNotNone(self.win.status_label)
        self.assertEqual(self.win.status_label.get_text(), "● Ready")
        self.assertFalse(self.win.review_box.get_visible())
        self.assertFalse(self.win.progress_box.get_visible())
        self.assertFalse(self.win.undo_box.get_visible())

    def test_responsive_window_sizing(self):
        # 1. Responsive Window Sizing: window is resizable and queries monitor geometry
        self.assertTrue(self.win.get_resizable())
        with patch.object(self.win, "get_monitor_geometry", return_value=(440, 360)):
            self.assertLess(self.win.get_responsive_width(), 440)
        with patch.object(self.win, "resize") as resize:
            self.win.manual_resize = True
            self.win.adjust_window_size()
            resize.assert_not_called()

        # Scrolled window has dynamic natural height propagation
        self.assertTrue(self.win.output_scroll.get_propagate_natural_height())
        self.assertGreaterEqual(self.win.output_scroll.get_min_content_height(), 50)
        self.assertLess(self.win.get_responsive_height(), 200)

    def test_quick_chips_behavior(self):
        # 3. Quick Chips Behavior: clicking chip does NOT auto-submit; populates entry and grabs focus
        snippet = "split screen youtube and reddit in brave"
        self.win.apply_chip(snippet)
        self.assertEqual(self.win.entry.get_text(), snippet)
        self.assertFalse(self.win.is_processing)

    def test_review_card_improvements(self):
        # 4. Review Card Improvements: Confirm, Edit, and Cancel buttons
        act = ActionRequest(
            action_type=ActionType.OPEN_APP,
            params={"app_name": "firefox"},
            summary="Launch application 'firefox'",
            risk_level=RiskLevel.READ_ONLY
        )
        self.win.current_query = "open firefox browser"
        self.win.show_review_card(act)
        self.assertTrue(self.win.review_box.get_visible())
        self.assertIsNotNone(self.win.confirm_btn)
        self.assertIsNotNone(self.win.edit_btn)
        self.assertIsNotNone(self.win.cancel_btn)

        # 4a. Click edit: restores command and hides card
        self.win.on_edit_action()
        self.assertFalse(self.win.review_box.get_visible())
        self.assertIsNone(self.win.pending_action)
        self.assertEqual(self.win.entry.get_text(), "open firefox browser")

        # 4b. Click confirm: hides review card and dispatches execution
        self.win.pending_action = act
        self.win.show_review_card(act)
        self.assertTrue(self.win.review_box.get_visible())
        with patch.object(self.win, "execute_pending_action") as mock_exec:
            self.win.on_confirm_action()
            mock_exec.assert_called_once()

        # 4c. Click cancel: hides card and sets cancelled status
        self.win.pending_action = act
        self.win.show_review_card(act)
        self.assertTrue(self.win.review_box.get_visible())
        self.win.on_cancel_action()
        self.assertFalse(self.win.review_box.get_visible())
        self.assertIsNone(self.win.pending_action)
        self.assertEqual(self.win.status_label.get_text(), "● Cancelled")

    def test_accurate_status_reporting(self):
        # 2. Accurate Status Reporting:
        # launch_requested for apps/urls/windows -> "● Launch requested"
        act_app = ActionRequest(
            action_type=ActionType.OPEN_APP,
            params={"app_name": "vlc"},
            summary="Launch vlc",
            risk_level=RiskLevel.READ_ONLY
        )
        res_launch = ActionResult(status="success", summary="Launched vlc", verification_status="launch_requested")
        self.win.on_execution_complete(act_app, res_launch)
        self.assertEqual(self.win.status_label.get_text(), "● Launch requested")

        # terminal_launched / run_in_terminal -> "● Done"
        act_term = ActionRequest(
            action_type=ActionType.RUN_IN_TERMINAL,
            params={"command": "htop"},
            summary="Run htop in terminal",
            risk_level=RiskLevel.READ_ONLY
        )
        res_term = ActionResult(status="success", summary="Running htop", verification_status="terminal_launched")
        self.win.on_execution_complete(act_term, res_term)
        self.assertEqual(self.win.status_label.get_text(), "● Done")

        # packages -> "● Launch requested"
        act_pkg = ActionRequest(
            action_type=ActionType.INSTALL_PACKAGE,
            params={"package_name": "curl"},
            summary="Install curl",
            risk_level=RiskLevel.PRIVILEGED
        )
        res_pkg = ActionResult(status="success", summary="Opening installer", verification_status="launch_requested")
        self.win.on_execution_complete(act_pkg, res_pkg)
        self.assertEqual(self.win.status_label.get_text(), "● Launch requested")

        # Only verified -> "● Done"
        act_health = ActionRequest(
            action_type=ActionType.GET_SYSTEM_HEALTH,
            params={},
            summary="Health check",
            risk_level=RiskLevel.READ_ONLY
        )
        res_ver = ActionResult(status="success", summary="System health OK", verification_status="verified")
        self.win.on_execution_complete(act_health, res_ver)
        self.assertEqual(self.win.status_label.get_text(), "● Done")

    @patch("mavrick.ui.Gtk.MessageDialog")
    @patch("mavrick.ui.clear_history_and_context")
    def test_privacy_and_audit_view(self, mock_clear, mock_dialog_cls):
        # 5. Privacy & Audit View: [🗑 Clear History] button visible in history mode
        self.win.toggle_history()
        self.assertTrue(self.win.history_mode)
        self.assertTrue(self.win.history_bar.get_visible())
        self.assertIsNotNone(self.win.clear_hist_btn)
        self.assertEqual(self.win.clear_hist_btn.get_label(), "🗑 Clear History")

        # Test clear history with confirmation dialog OK
        mock_dialog = MagicMock()
        mock_dialog.run.return_value = Gtk.ResponseType.OK
        mock_dialog_cls.return_value = mock_dialog

        self.win.history = ["test_entry"]
        self.win.on_clear_history()
        mock_clear.assert_called_once()
        self.assertEqual(len(self.win.history), 0)
        self.assertEqual(self.win.status_label.get_text(), "● History Cleared")

        # Toggle closes history view
        self.win.toggle_history()
        self.assertFalse(self.win.history_mode)
        self.assertFalse(self.win.history_bar.get_visible())

    @patch("mavrick.ui.Gdk.Display.get_default")
    def test_get_active_monitor_pointer_detection(self, mock_get_default_display):
        """get_active_monitor detects pointer coordinates via Gdk seat and maps to active monitor."""
        mock_display = MagicMock()
        mock_seat = MagicMock()
        mock_pointer = MagicMock()
        # Pointer at coordinates x=2560, y=720 (secondary monitor)
        mock_pointer.get_position.return_value = (None, 2560, 720)
        mock_seat.get_pointer.return_value = mock_pointer
        mock_display.get_default_seat.return_value = mock_seat

        mock_target_monitor = MagicMock()
        mock_display.get_monitor_at_point.return_value = mock_target_monitor
        mock_get_default_display.return_value = mock_display

        detected_mon = self.win.get_active_monitor()
        self.assertEqual(detected_mon, mock_target_monitor)
        mock_display.get_monitor_at_point.assert_called_once_with(2560, 720)

    def test_position_on_active_monitor_centering(self):
        """position_on_active_monitor centers HUD on active monitor geometry."""
        mock_mon = MagicMock()
        mock_geom = MagicMock()
        mock_geom.x = 1920
        mock_geom.y = 0
        mock_geom.width = 1920
        mock_geom.height = 1080
        mock_mon.get_geometry.return_value = mock_geom

        with patch.object(self.win, "get_active_monitor", return_value=mock_mon), \
             patch.object(self.win, "get_size", return_value=(720, 140)), \
             patch.object(self.win, "move") as mock_move:
            self.win.position_on_active_monitor()
            # Expected center x: 1920 + (1920 - 720)//2 = 1920 + 600 = 2520
            # Expected center y: 0 + (1080 - 140)//2 = 470
            mock_move.assert_called_once_with(2520, 470)

    def test_show_window_positions_on_active_monitor(self):
        """show_window centers window on active monitor where mouse cursor resides."""
        with patch.object(self.win, "position_on_active_monitor") as mock_pos, \
             patch.object(self.win, "present"), \
             patch.object(self.win, "show_all"):
            self.win.show_window()
            self.assertGreaterEqual(mock_pos.call_count, 1)

    def test_history_up_down_cycling(self):
        """Up/Down arrows cycle through history and restore in-progress draft text."""
        sample_history = [
            {"query": "system health"},
            {"query": "open terminal"},
            {"query": "clean cache"}
        ]
        with patch("mavrick.ui.load_history", return_value=sample_history):
            self.win.entry.set_text("in-progress query")
            self.win.history_draft = ""
            self.win.history_idx = len(self.win.get_history_queries())

            # 1. Up arrow: cycles to last query ("clean cache") and saves draft
            self.win.navigate_history(-1)
            self.assertEqual(self.win.history_idx, 2)
            self.assertEqual(self.win.entry.get_text(), "clean cache")
            self.assertEqual(self.win.history_draft, "in-progress query")

            # 2. Up arrow: cycles backwards to "open terminal"
            self.win.navigate_history(-1)
            self.assertEqual(self.win.history_idx, 1)
            self.assertEqual(self.win.entry.get_text(), "open terminal")

            # 3. Up arrow: cycles backwards to "system health"
            self.win.navigate_history(-1)
            self.assertEqual(self.win.history_idx, 0)
            self.assertEqual(self.win.entry.get_text(), "system health")

            # 4. Up arrow at top clamps to 0
            self.win.navigate_history(-1)
            self.assertEqual(self.win.history_idx, 0)
            self.assertEqual(self.win.entry.get_text(), "system health")

            # 5. Down arrow: cycles forward to "open terminal"
            self.win.navigate_history(1)
            self.assertEqual(self.win.history_idx, 1)
            self.assertEqual(self.win.entry.get_text(), "open terminal")

            # 6. Down arrow: cycles forward to "clean cache"
            self.win.navigate_history(1)
            self.assertEqual(self.win.history_idx, 2)
            self.assertEqual(self.win.entry.get_text(), "clean cache")

            # 7. Down arrow back to end: restores original draft text
            self.win.navigate_history(1)
            self.assertEqual(self.win.history_idx, 3)
            self.assertEqual(self.win.entry.get_text(), "in-progress query")

    def test_on_entry_key_press_events(self):
        """on_entry_key_press hooks Up, Down, and Escape keys correctly."""
        event_up = MagicMock()
        event_up.keyval = Gdk.KEY_Up
        with patch.object(self.win, "navigate_history") as mock_nav:
            res = self.win.on_entry_key_press(self.win.entry, event_up)
            self.assertTrue(res)
            mock_nav.assert_called_once_with(-1)

        event_down = MagicMock()
        event_down.keyval = Gdk.KEY_Down
        with patch.object(self.win, "navigate_history") as mock_nav:
            res = self.win.on_entry_key_press(self.win.entry, event_down)
            self.assertTrue(res)
            mock_nav.assert_called_once_with(1)

        event_esc = MagicMock()
        event_esc.keyval = Gdk.KEY_Escape
        with patch.object(self.win, "handle_escape") as mock_esc:
            res = self.win.on_entry_key_press(self.win.entry, event_esc)
            mock_esc.assert_called_once()

    def test_handle_escape_logic(self):
        """handle_escape clears entry when text present; hides window when empty."""
        # When text is present, Escape clears entry
        self.win.entry.set_text("typed query")
        handled = self.win.handle_escape()
        self.assertTrue(handled)
        self.assertEqual(self.win.entry.get_text(), "")

        # When entry is already empty, Escape hides the window
        with patch.object(self.win, "hide_window") as mock_hide:
            self.win.entry.set_text("")
            handled = self.win.handle_escape()
            self.assertTrue(handled)
            mock_hide.assert_called_once()

    @patch("subprocess.Popen")
    def test_send_desktop_notification(self, mock_popen):
        """send_desktop_notification calls notify-send subprocess."""
        MavrickWindow.send_desktop_notification(self.win, "MAVRICK", "Power profile changed to battery")
        mock_popen.assert_called_once_with(
            ["notify-send", "MAVRICK", "Power profile changed to battery"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )

    def test_notification_dispatch_conditions(self):
        """Notifications are dispatched when window was hidden or duration > 1.5s."""
        act = ActionRequest(
            action_type=ActionType.CLEAN_CACHE,
            params={"dry_run": False},
            summary="Cleaned 80 MB cache",
            risk_level=RiskLevel.REVERSIBLE
        )

        # Condition 1: Window was hidden -> notification sent regardless of duration
        res_fast = ActionResult(status="success", summary="Cleaned 80 MB cache", verification_status="verified", duration_ms=250.0)
        with patch.object(self.win, "is_visible", return_value=False), \
             patch.object(self.win, "send_desktop_notification") as mock_notify:
            self.win.on_execution_complete(act, res_fast)
            mock_notify.assert_called_once_with("MAVRICK", "Cleaned 80 MB cache")

        # Condition 2: Window visible, but duration > 1500ms -> notification sent
        res_slow = ActionResult(status="success", summary="Updates installed", verification_status="verified", duration_ms=1850.0)
        with patch.object(self.win, "is_visible", return_value=True), \
             patch.object(self.win, "send_desktop_notification") as mock_notify:
            self.win.on_execution_complete(act, res_slow)
            mock_notify.assert_called_once_with("MAVRICK", "Updates installed")

        # Condition 3: Window visible AND duration <= 1500ms -> NO notification sent
        with patch.object(self.win, "is_visible", return_value=True), \
             patch.object(self.win, "send_desktop_notification") as mock_notify:
            self.win.on_execution_complete(act, res_fast)
            mock_notify.assert_not_called()

    def test_popos_quick_chips_present(self):
        """Verify quick chips container includes all Pop!_OS tool pills."""
        labels = [b.get_label() for b in self.win.chips_box.get_children() if isinstance(b, Gtk.Button)]
        self.assertEqual(len(labels), 3)
        self.assertEqual(self.win.browse_btn.get_label(), "Browse commands")
        self.win.toggle_commands()
        self.win.command_search.set_text("spotify")
        self.win.filter_commands(self.win.command_search)
        self.assertEqual([row.phrase for row in self.win.command_list.get_children()],
                         ["pause spotify", "resume spotify"])

    def test_file_review_uses_row_selection_and_cancel_focus(self):
        from mavrick.tools.files import FileReview, FileRow

        action = ActionRequest(ActionType.ORGANIZE_DOWNLOADS, {}, "File review", RiskLevel.REVERSIBLE)
        self.win.pending_action = action
        row = FileRow("/tmp/example/a.txt", "/tmp/example/Documents/a.txt", (), ())
        occupied = FileRow("/tmp/example/b.txt", "/tmp/example/Documents/b.txt", (), (),
                           problem="Destination already exists", changed=True)
        self.win.show()
        self.win.show_file_review(FileReview("organize_downloads", (row, occupied)), "Two rows")
        self.assertEqual(self.win.get_focus(), self.win.cancel_btn)
        self.assertEqual(self.win.confirm_btn.get_label(), "Move 1 file")
        self.assertFalse(self.win.file_checks[1].get_sensitive())
        self.win.file_checks[0].set_active(False)
        self.assertFalse(self.win.confirm_btn.get_sensitive())

    def test_ai_pipeline_forwards_the_request_cancellation_token(self):
        from threading import Event

        cancel = Event()
        with patch("mavrick.ui.query_model") as query, patch("mavrick.ui.GLib.idle_add"):
            self.win.run_ai_pipeline("fixture request", "request-id", cancel)
        query.assert_called_once_with("fixture request", cancel_token=cancel)

    def test_duplicate_submissions_and_late_results_leave_current_request_intact(self):
        self.win.entry.set_text("system health")
        with patch("mavrick.ui.threading.Thread") as thread:
            self.win.on_submit(self.win.entry)
            first = self.win.request_id
            self.win.on_submit(self.win.entry)
            self.assertEqual(self.win.request_id, first)
            thread.assert_called_once()
        self.win._settle_progress()
        self.win._begin_request("next request")
        action = ActionRequest(ActionType.GET_SYSTEM_HEALTH, {}, "Health", RiskLevel.READ_ONLY)
        self.win.on_execution_complete(action, ActionResult("success", summary="Old result", job_id=first))
        self.assertTrue(self.win.is_processing)
        self.assertEqual(self.win.current_query, "next request")
        self.assertFalse(self.win.output_scroll.get_visible())

    def test_health_probe_is_dispatched_to_background(self):
        with patch("mavrick.ui.threading.Thread") as thread, patch("mavrick.ui.check_ai_health") as probe:
            self.win.refresh_health_badge()
            thread.return_value.start.assert_called_once()
            probe.assert_not_called()

    def test_render_output_card_formatting(self):
        """render_output_card formats cards with monospace divider, status badges, and duration."""
        self.win.render_output_card(
            title="System Telemetry Verified",
            body="RAM: 4096 MB / 16384 MB\nGPU: NVIDIA 35% util",
            duration_ms=48.0,
            verification_status="verified"
        )
        text = self.win.buffer.get_text(*self.win.buffer.get_bounds(), True)
        self.assertIn("System Telemetry Verified", text)
        self.assertIn("RAM: 4096 MB", text)
        self.assertIn("GPU: NVIDIA 35% util", text)
        self.assertIn("48ms", text)
        self.assertIn("Verification: verified", text)

if __name__ == "__main__":
    unittest.main()
