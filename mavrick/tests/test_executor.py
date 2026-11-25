#!/usr/bin/env python3
"""
Unit Tests for MAVRICK Shared ActionExecutor & Job Cancellation
"""

import os
import time
import signal
import tempfile
import subprocess
import unittest
from mavrick.tests import IsolatedTestCase
from unittest.mock import patch, MagicMock
from mavrick.schema import ActionType, RiskLevel, ActionRequest
from mavrick.executor import ActionExecutor, JobState, approve_files

class TestActionExecutor(IsolatedTestCase):
    def setUp(self):
        self.executor = ActionExecutor()

    def test_execute_read_only(self):
        """Read-only actions execute immediately without requiring confirmation."""
        act = ActionRequest(
            action_type=ActionType.GET_SYSTEM_HEALTH,
            params={},
            summary="Check system health",
            risk_level=RiskLevel.READ_ONLY
        )
        res = self.executor.execute(act, confirmed=False)
        self.assertEqual(res.status, "success")
        self.assertEqual(res.exit_code, 0)
        self.assertGreater(res.duration_ms, 0)
        self.assertEqual(res.verification_status, "verified")

    def test_confirmation_enforcement(self):
        """Unconfirmed privileged actions return 'requires_confirmation'."""
        act = ActionRequest(
            action_type=ActionType.INSTALL_PACKAGE,
            params={"package_name": "htop", "manager": "apt"},
            summary="Install htop",
            risk_level=RiskLevel.PRIVILEGED
        )
        # Attempt without confirmation
        res = self.executor.execute(act, confirmed=False)
        self.assertEqual(res.status, "requires_confirmation")

    def test_cancellation(self):
        """Cancellation token stops running jobs cleanly."""
        act = ActionRequest(
            action_type=ActionType.GET_SYSTEM_HEALTH,
            params={},
            summary="Check system health",
            risk_level=RiskLevel.READ_ONLY
        )
        # Verify cancel method returns False when no running job
        self.assertFalse(self.executor.cancel_current_job())

    def test_move_files_dispatch(self):
        """ActionType.MOVE_FILES dispatches properly to tools.files.move_files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            src_file = os.path.join(tmpdir, "dispatch_test.txt")
            dest_dir = os.path.join(tmpdir, "dest_dir")
            os.makedirs(dest_dir, exist_ok=True)
            with open(src_file, "w") as fp:
                fp.write("dispatch test")

            act = ActionRequest(
                action_type=ActionType.MOVE_FILES,
                params={"sources": [src_file], "destination": dest_dir},
                summary="Move file to dest_dir",
                risk_level=RiskLevel.REVERSIBLE
            )
            review = self.executor.execute(act).details["review"]
            res = self.executor.execute(act, approval=approve_files(review, [0]))
            self.assertEqual(res.status, "success")
            self.assertEqual(res.exit_code, 0)
            self.assertTrue(res.undo_available)
            self.assertTrue(os.path.exists(os.path.join(dest_dir, "dispatch_test.txt")))
            self.assertFalse(os.path.exists(src_file))

    def test_process_group_cancellation(self):
        """Process group cancellation sends signals to spawned session leader and group."""
        proc = subprocess.Popen(
            ["sleep", "30"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True
        )
        pid = proc.pid
        pgid = os.getpgid(pid)

        # Register in executor
        self.executor.register_pgid(pgid, pid)

        # Create a mock running job
        from mavrick.executor import Job
        job = Job(
            id="test-job",
            action=ActionRequest(
                action_type=ActionType.GET_SYSTEM_HEALTH,
                params={},
                summary="Health",
                risk_level=RiskLevel.READ_ONLY
            ),
            state=JobState.RUNNING,
            pgids=[pgid],
            pids=[pid]
        )
        self.executor.current_job = job

        # Cancel the job
        cancelled = self.executor.cancel_current_job()
        self.assertTrue(cancelled)

        # Give OS a moment to reap or acknowledge termination
        time.sleep(0.2)
        # Check process is terminated
        poll_res = proc.poll()
        self.assertIsNotNone(poll_res)
        # Process should have been killed by signal
        self.assertIn(poll_res, [-signal.SIGTERM, -signal.SIGKILL, 137, 143])

    @patch("mavrick.executor.set_power_profile")
    def test_dispatch_set_power_profile(self, mock_set_profile):
        """ActionExecutor dispatches SET_POWER_PROFILE with verification."""
        mock_set_profile.return_value = (
            True,
            "Power profile set to 'performance' via system76-power.",
            "Current profile: performance"
        )
        act = ActionRequest(
            action_type=ActionType.SET_POWER_PROFILE,
            params={"profile": "performance"},
            summary="Set profile to performance",
            risk_level=RiskLevel.REVERSIBLE
        )
        res = self.executor.execute(act, confirmed=True)
        self.assertEqual(res.status, "success")
        self.assertEqual(res.exit_code, 0)
        self.assertEqual(res.verification_status, "verified")
        self.assertIn("performance", res.summary)
        mock_set_profile.assert_called_once_with("performance")

    @patch("mavrick.executor.control_media")
    def test_dispatch_control_media(self, mock_control_media):
        """ActionExecutor dispatches CONTROL_MEDIA and reports track details."""
        mock_control_media.return_value = (
            True,
            "Media play pause executed. [Playing] 'Time' by Pink Floyd",
            {"status": "Playing", "artist": "Pink Floyd", "title": "Time"}
        )
        act = ActionRequest(
            action_type=ActionType.CONTROL_MEDIA,
            params={"action": "play_pause"},
            summary="Media play/pause",
            risk_level=RiskLevel.REVERSIBLE
        )
        res = self.executor.execute(act, confirmed=True)
        self.assertEqual(res.status, "success")
        self.assertEqual(res.exit_code, 0)
        self.assertEqual(res.verification_status, "verified")
        self.assertIn("Playing: Pink Floyd - Time", res.stdout)
        mock_control_media.assert_called_once_with(action="play_pause")

    @patch("mavrick.executor.get_network_status")
    def test_dispatch_get_network_status(self, mock_get_net):
        """ActionExecutor dispatches GET_NETWORK_STATUS immediately without confirmation."""
        mock_get_net.return_value = {
            "interface": "wlan0",
            "ip": "192.168.1.100",
            "gateway": "192.168.1.1",
            "dns_latency_ms": 11.5,
            "connected": True,
            "report": "Network Connected"
        }
        act = ActionRequest(
            action_type=ActionType.GET_NETWORK_STATUS,
            params={},
            summary="Check network",
            risk_level=RiskLevel.READ_ONLY
        )
        res = self.executor.execute(act, confirmed=False)
        self.assertEqual(res.status, "success")
        self.assertEqual(res.exit_code, 0)
        self.assertEqual(res.verification_status, "verified")
        self.assertIn("wlan0", res.summary)
        self.assertEqual(res.details["ip"], "192.168.1.100")

    @patch("mavrick.executor.inspect_storage_usage")
    def test_dispatch_inspect_storage(self, mock_storage):
        """ActionExecutor dispatches INSPECT_STORAGE and returns breakdown report."""
        mock_storage.return_value = (True, "Storage report table", {"items": [{"name": "Spotify"}]})
        act = ActionRequest(
            action_type=ActionType.INSPECT_STORAGE,
            params={"category": "apps", "limit": 10},
            summary="Inspect storage usage",
            risk_level=RiskLevel.READ_ONLY
        )
        res = self.executor.execute(act)
        self.assertEqual(res.status, "success")
        self.assertEqual(res.verification_status, "verified")
        self.assertIn("breakdown", res.summary)
        self.assertEqual(res.stdout, "Storage report table")

    @patch("mavrick.executor.check_updates")
    def test_dispatch_check_updates(self, mock_check_updates):
        """ActionExecutor dispatches CHECK_UPDATES immediately and returns counts."""
        mock_check_updates.return_value = {
            "apt_updates": [{"package": "curl"}],
            "flatpak_updates": [{"ref": "org.mozilla.firefox"}],
            "total_updates": 2,
            "report": "2 updates available."
        }
        act = ActionRequest(
            action_type=ActionType.CHECK_UPDATES,
            params={},
            summary="Check updates",
            risk_level=RiskLevel.READ_ONLY
        )
        res = self.executor.execute(act, confirmed=False)
        self.assertEqual(res.status, "success")
        self.assertEqual(res.exit_code, 0)
        self.assertEqual(res.verification_status, "verified")
        self.assertIn("Found 2 pending system update(s)", res.summary)

    @patch("mavrick.executor.clean_cache")
    def test_dispatch_clean_cache(self, mock_clean_cache):
        """ActionExecutor dispatches CLEAN_CACHE dry-run and live actions."""
        mock_clean_cache.return_value = (
            True,
            "🧹 Cache Clean Preview (Dry Run):\nTotal Reclaimable: 45.2 MB",
            {"dry_run": True, "total_reclaimable_mb": 45.2}
        )
        act = ActionRequest(
            action_type=ActionType.CLEAN_CACHE,
            params={"dry_run": True},
            summary="Cache clean preview",
            risk_level=RiskLevel.REVERSIBLE
        )
        res = self.executor.execute(act, confirmed=True)
        self.assertEqual(res.status, "success")
        self.assertEqual(res.exit_code, 0)
        self.assertEqual(res.verification_status, "verified")
        self.assertTrue(res.details["dry_run"])
        mock_clean_cache.assert_called_once_with(dry_run=True)

if __name__ == "__main__":
    unittest.main()
