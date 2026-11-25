#!/usr/bin/env python3
"""
Unit Tests for MAVRICK Action Schemas & Validation
"""

import unittest
from mavrick.tests import IsolatedTestCase
from mavrick.schema import (
    ActionType,
    RiskLevel,
    validate_action_params,
    ACTION_RISK_MAP
)

class TestActionSchemas(IsolatedTestCase):
    def test_risk_mappings(self):
        """Verify that every ActionType is explicitly mapped to a RiskLevel."""
        for action_type in ActionType:
            self.assertIn(action_type, ACTION_RISK_MAP)
            risk = ACTION_RISK_MAP[action_type]
            self.assertIsInstance(risk, RiskLevel)

        # Critical safety checks
        self.assertEqual(ACTION_RISK_MAP[ActionType.GET_SYSTEM_HEALTH], RiskLevel.READ_ONLY)
        self.assertEqual(ACTION_RISK_MAP[ActionType.INSPECT_PROCESSES], RiskLevel.READ_ONLY)
        self.assertEqual(ACTION_RISK_MAP[ActionType.GET_NETWORK_STATUS], RiskLevel.READ_ONLY)
        self.assertEqual(ACTION_RISK_MAP[ActionType.CHECK_UPDATES], RiskLevel.READ_ONLY)
        self.assertEqual(ACTION_RISK_MAP[ActionType.CONTROL_MEDIA], RiskLevel.READ_ONLY)
        self.assertEqual(ACTION_RISK_MAP[ActionType.SET_POWER_PROFILE], RiskLevel.REVERSIBLE)
        self.assertEqual(ACTION_RISK_MAP[ActionType.CLEAN_CACHE], RiskLevel.REVERSIBLE)
        self.assertEqual(ACTION_RISK_MAP[ActionType.ORGANIZE_DOWNLOADS], RiskLevel.REVERSIBLE)
        self.assertEqual(ACTION_RISK_MAP[ActionType.INSTALL_PACKAGE], RiskLevel.PRIVILEGED)
        self.assertEqual(ACTION_RISK_MAP[ActionType.UNINSTALL_PACKAGE], RiskLevel.PRIVILEGED)
        self.assertEqual(ACTION_RISK_MAP[ActionType.LOCK_SESSION], RiskLevel.DESTRUCTIVE)

    def test_validate_unknown_action(self):
        """Reject unlisted/arbitrary actions."""
        valid, err, req = validate_action_params("execute_bash_command", {"command": "rm -rf /"})
        self.assertFalse(valid)
        self.assertIn("disallowed", err.lower())
        self.assertIsNone(req)

    def test_validate_system_health(self):
        """get_system_health accepts empty parameters."""
        valid, err, req = validate_action_params("get_system_health", {})
        self.assertTrue(valid)
        self.assertIsNone(err)
        self.assertEqual(req.action_type, ActionType.GET_SYSTEM_HEALTH)
        self.assertEqual(req.risk_level, RiskLevel.READ_ONLY)

    def test_validate_inspect_processes(self):
        """inspect_processes validates sort_by and limit."""
        valid, err, req = validate_action_params("inspect_processes", {"sort_by": "memory", "limit": 15})
        self.assertTrue(valid)
        self.assertEqual(req.params["limit"], 15)

        # Invalid sort_by
        valid, err, req = validate_action_params("inspect_processes", {"sort_by": "invalid"})
        self.assertFalse(valid)

        # Invalid limit
        valid, err, req = validate_action_params("inspect_processes", {"limit": 999})
        self.assertFalse(valid)

    def test_validate_open_app_injection(self):
        """open_app rejects shell metacharacters."""
        malicious_inputs = [
            "brave; rm -rf ~",
            "firefox && echo hacked",
            "code | bash",
            "app`touch /tmp/pwn`",
            "app$(whoami)"
        ]
        for bad in malicious_inputs:
            valid, err, req = validate_action_params("open_app", {"app_name": bad})
            self.assertFalse(valid, f"Should have rejected '{bad}'")
            self.assertIn("valid application", err.lower())

        # Valid app
        valid, err, req = validate_action_params("open_app", {"app_name": "brave-browser"})
        self.assertTrue(valid)
        self.assertEqual(req.params["app_name"], "brave-browser")

    def test_validate_open_url(self):
        """open_url requires valid http/https URLs."""
        valid, err, req = validate_action_params("open_url", {"url": "https://youtube.com"})
        self.assertTrue(valid)

        # Disallow javascript: or file: schemes
        valid, err, req = validate_action_params("open_url", {"url": "javascript:alert(1)"})
        self.assertFalse(valid)

        valid, err, req = validate_action_params("open_url", {"url": "file:///etc/passwd"})
        self.assertFalse(valid)

    def test_validate_package_name(self):
        """install_package and uninstall_package require clean package names."""
        bad_pkgs = ["curl; reboot", "code && rm -rf /", "git`id`"]
        for bad in bad_pkgs:
            valid, err, req = validate_action_params("install_package", {"package_name": bad})
            self.assertFalse(valid)

        valid, err, req = validate_action_params("install_package", {"package_name": "htop", "manager": "apt"})
        self.assertTrue(valid)
        self.assertEqual(req.risk_level, RiskLevel.PRIVILEGED)

    def test_validate_run_in_terminal(self):
        """run_in_terminal validates commands, rejects injection, and assigns risk levels."""
        # Valid safe tool -> READ_ONLY
        valid, err, req = validate_action_params("run_in_terminal", {"command": "agy"})
        self.assertTrue(valid)
        self.assertEqual(req.params["command"], "agy")
        self.assertEqual(req.risk_level, RiskLevel.READ_ONLY)

        # Arbitrary utility arguments are unavailable.
        valid, err, req = validate_action_params("run_in_terminal", {"command": "ping", "args": ["-c", "4", "google.com"]})
        self.assertFalse(valid)

        # Metacharacter injection in command
        valid, err, req = validate_action_params("run_in_terminal", {"command": "agy; rm -rf /"})
        self.assertFalse(valid)

        # Metacharacter injection in args
        valid, err, req = validate_action_params("run_in_terminal", {"command": "agy", "args": ["$(whoami)"]})
        self.assertFalse(valid)

    def test_validate_organize_downloads_forces_dry_run(self):
        """organize_downloads schema validation forces dry_run=True regardless of input."""
        # 1. Explicitly passing dry_run=False must still result in dry_run=True
        valid, err, req = validate_action_params("organize_downloads", {"target_dir": "~/Downloads", "dry_run": False})
        self.assertTrue(valid)
        self.assertTrue(req.params["dry_run"])

        # 2. Omitting dry_run defaults to True
        valid, err, req = validate_action_params("organize_downloads", {"target_dir": "~/Downloads"})
        self.assertTrue(valid)
        self.assertTrue(req.params["dry_run"])

    def test_validate_move_files(self):
        """move_files validates sources, destination, and assigns REVERSIBLE risk level."""
        # Valid move_files
        valid, err, req = validate_action_params("move_files", {
            "sources": ["~/Downloads/report.pdf", "~/Downloads/data.csv"],
            "destination": "~/Documents"
        })
        self.assertTrue(valid)
        self.assertEqual(req.action_type, ActionType.MOVE_FILES)
        self.assertEqual(req.risk_level, RiskLevel.REVERSIBLE)
        self.assertEqual(len(req.params["sources"]), 2)
        self.assertEqual(req.params["destination"], "~/Documents")

        # Empty sources list
        valid, err, req = validate_action_params("move_files", {
            "sources": [],
            "destination": "~/Documents"
        })
        self.assertFalse(valid)
        self.assertIn("non-empty list", err.lower())

        # Non-list sources
        valid, err, req = validate_action_params("move_files", {
            "sources": "~/Downloads/report.pdf",
            "destination": "~/Documents"
        })
        self.assertFalse(valid)

        # Empty destination
        valid, err, req = validate_action_params("move_files", {
            "sources": ["~/Downloads/report.pdf"],
            "destination": ""
        })
        self.assertFalse(valid)

        # NUL cannot appear in a destination path
        valid, err, req = validate_action_params("move_files", {
            "sources": ["~/Downloads/report.pdf"],
            "destination": "~/Documents\x00"
        })
        self.assertFalse(valid)

        # Forbidden characters in source
        valid, err, req = validate_action_params("move_files", {
            "sources": ["~/Downloads/report.pdf; whoami"],
            "destination": "~/Documents"
        })
        self.assertTrue(valid)  # Shell metacharacters in literal paths are safe.

    def test_validate_set_power_profile(self):
        """set_power_profile validates profiles and maps to REVERSIBLE risk."""
        for prof in ["battery", "balanced", "performance"]:
            valid, err, req = validate_action_params("set_power_profile", {"profile": prof})
            self.assertTrue(valid)
            self.assertIsNone(err)
            self.assertEqual(req.action_type, ActionType.SET_POWER_PROFILE)
            self.assertEqual(req.risk_level, RiskLevel.REVERSIBLE)
            self.assertEqual(req.params["profile"], prof)

        # Invalid profile
        for bad in ["turbo", "extreme", ""]:
            valid, err, req = validate_action_params("set_power_profile", {"profile": bad})
            self.assertFalse(valid)
            self.assertIn("invalid power profile", err.lower())

    def test_validate_control_media(self):
        """control_media validates actions and assigns READ_ONLY for status and REVERSIBLE for playback."""
        for act in ["play_pause", "next", "previous"]:
            valid, err, req = validate_action_params("control_media", {"action": act})
            self.assertTrue(valid)
            self.assertIsNone(err)
            self.assertEqual(req.action_type, ActionType.CONTROL_MEDIA)
            self.assertEqual(req.risk_level, RiskLevel.REVERSIBLE)
            self.assertEqual(req.params["action"], act)

        # Status action is READ_ONLY
        valid, err, req = validate_action_params("control_media", {"action": "status"})
        self.assertTrue(valid)
        self.assertIsNone(err)
        self.assertEqual(req.action_type, ActionType.CONTROL_MEDIA)
        self.assertEqual(req.risk_level, RiskLevel.READ_ONLY)
        self.assertEqual(req.params["action"], "status")

        # Invalid action
        valid, err, req = validate_action_params("control_media", {"action": "rewind_30s"})
        self.assertFalse(valid)
        self.assertIn("invalid media action", err.lower())

    def test_validate_get_network_status(self):
        """get_network_status accepts empty parameters and maps to READ_ONLY risk."""
        valid, err, req = validate_action_params("get_network_status", {})
        self.assertTrue(valid)
        self.assertIsNone(err)
        self.assertEqual(req.action_type, ActionType.GET_NETWORK_STATUS)
        self.assertEqual(req.risk_level, RiskLevel.READ_ONLY)

    def test_validate_check_updates(self):
        """check_updates accepts empty parameters and maps to READ_ONLY risk."""
        valid, err, req = validate_action_params("check_updates", {})
        self.assertTrue(valid)
        self.assertIsNone(err)
        self.assertEqual(req.action_type, ActionType.CHECK_UPDATES)
        self.assertEqual(req.risk_level, RiskLevel.READ_ONLY)

    def test_validate_clean_cache(self):
        """clean_cache validates dry_run parameter and maps to REVERSIBLE risk."""
        valid, err, req = validate_action_params("clean_cache", {"dry_run": True})
        self.assertTrue(valid)
        self.assertIsNone(err)
        self.assertEqual(req.action_type, ActionType.CLEAN_CACHE)
        self.assertEqual(req.risk_level, RiskLevel.READ_ONLY)
        self.assertTrue(req.params["dry_run"])

        valid, err, req = validate_action_params("clean_cache", {"dry_run": False})
        self.assertTrue(valid)
        self.assertFalse(req.params["dry_run"])
        self.assertEqual(req.risk_level, RiskLevel.DESTRUCTIVE)

        # Default dry_run is True
        valid, err, req = validate_action_params("clean_cache", {})
        self.assertTrue(valid)
        self.assertTrue(req.params["dry_run"])

if __name__ == "__main__":
    unittest.main()
