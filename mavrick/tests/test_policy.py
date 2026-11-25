#!/usr/bin/env python3
"""
Unit Tests for MAVRICK Security Policy & Privilege Enforcement
"""

import os
import unittest
from mavrick.tests import IsolatedTestCase
from mavrick.schema import ActionType, RiskLevel, ActionRequest
from mavrick.policy import (
    requires_confirmation,
    is_safe_user_path,
    scrub_secrets,
    validate_app_target
)

class TestSecurityPolicy(IsolatedTestCase):
    def test_confirmation_rules(self):
        """Verify strict confirmation requirements by risk tier."""
        # 1. READ_ONLY never requires confirmation
        read_only_req = ActionRequest(
            action_type=ActionType.GET_SYSTEM_HEALTH,
            params={},
            summary="Health check",
            risk_level=RiskLevel.READ_ONLY
        )
        self.assertFalse(requires_confirmation(read_only_req))

        # 2. REVERSIBLE requires confirmation by default
        rev_req = ActionRequest(
            action_type=ActionType.ORGANIZE_DOWNLOADS,
            params={},
            summary="Organize downloads",
            risk_level=RiskLevel.REVERSIBLE
        )
        self.assertTrue(requires_confirmation(rev_req))
        # But can be trusted if explicitly in user preferences
        self.assertFalse(requires_confirmation(rev_req, user_trusted_actions=["organize_downloads"]))

        # 3. PRIVILEGED ALWAYS requires confirmation
        priv_req = ActionRequest(
            action_type=ActionType.INSTALL_PACKAGE,
            params={"package_name": "htop"},
            summary="Install htop",
            risk_level=RiskLevel.PRIVILEGED
        )
        self.assertTrue(requires_confirmation(priv_req))
        # Cannot be bypassed even if added to user trusted list!
        self.assertTrue(requires_confirmation(priv_req, user_trusted_actions=["install_package"]))

        # 4. DESTRUCTIVE ALWAYS requires confirmation
        dest_req = ActionRequest(
            action_type=ActionType.LOCK_SESSION,
            params={},
            summary="Lock session",
            risk_level=RiskLevel.DESTRUCTIVE
        )
        self.assertTrue(requires_confirmation(dest_req))
        self.assertTrue(requires_confirmation(dest_req, user_trusted_actions=["lock_session"]))

        # 5. New Pop!_OS Tools confirmation policies
        # Read-only actions never require confirmation
        net_req = ActionRequest(
            action_type=ActionType.GET_NETWORK_STATUS,
            params={},
            summary="Network status",
            risk_level=RiskLevel.READ_ONLY
        )
        self.assertFalse(requires_confirmation(net_req))

        upd_req = ActionRequest(
            action_type=ActionType.CHECK_UPDATES,
            params={},
            summary="Check updates",
            risk_level=RiskLevel.READ_ONLY
        )
        self.assertFalse(requires_confirmation(upd_req))

        media_status_req = ActionRequest(
            action_type=ActionType.CONTROL_MEDIA,
            params={"action": "status"},
            summary="Media status",
            risk_level=RiskLevel.READ_ONLY
        )
        self.assertFalse(requires_confirmation(media_status_req))

        # Reversible actions require confirmation by default, bypassable via trusted_actions
        media_play_req = ActionRequest(
            action_type=ActionType.CONTROL_MEDIA,
            params={"action": "play_pause"},
            summary="Media play/pause",
            risk_level=RiskLevel.REVERSIBLE
        )
        self.assertTrue(requires_confirmation(media_play_req))
        self.assertFalse(requires_confirmation(media_play_req, user_trusted_actions=["control_media"]))

        power_req = ActionRequest(
            action_type=ActionType.SET_POWER_PROFILE,
            params={"profile": "performance"},
            summary="Set performance profile",
            risk_level=RiskLevel.REVERSIBLE
        )
        self.assertTrue(requires_confirmation(power_req))
        self.assertFalse(requires_confirmation(power_req, user_trusted_actions=["set_power_profile"]))

        cache_req = ActionRequest(
            action_type=ActionType.CLEAN_CACHE,
            params={"dry_run": False},
            summary="Clean cache",
            risk_level=RiskLevel.REVERSIBLE
        )
        self.assertTrue(requires_confirmation(cache_req))
        self.assertFalse(requires_confirmation(cache_req, user_trusted_actions=["clean_cache"]))

    def test_path_safety_and_traversal(self):
        """Prevent directory traversal and root path modification."""
        user_home = os.path.expanduser("~")

        # Safe user paths
        safe, err, canon = is_safe_user_path("~/Downloads")
        self.assertTrue(safe)
        self.assertTrue(canon.startswith(user_home))

        safe, err, canon = is_safe_user_path("/tmp/test_dir")
        self.assertTrue(safe)

        # Dangerous paths
        bad_paths = [
            "/etc/shadow",
            "/etc/sudoers",
            "/bin/bash",
            "/usr/bin/python3",
            "~/../../../../../../../etc/passwd",
            "/root/.ssh/id_rsa"
        ]
        for bad in bad_paths:
            safe, err, canon = is_safe_user_path(bad)
            self.assertFalse(safe, f"Path '{bad}' should be blocked")

    def test_secret_scrubbing(self):
        """Scrub tokens, API keys, and credentials."""
        inputs = [
            "User said api_key: sk-1234567890abcdef1234567890abcdef please run",
            "Authorization: Bearer mySecretToken1234567890abcd",
            "Google key AIzaSyD1234567890abcdefghijklmnopqrstuv"
        ]
        for raw in inputs:
            cleaned = scrub_secrets(raw)
            self.assertNotIn("sk-1234567890abcdef", cleaned)
            self.assertNotIn("mySecretToken1234567890abcd", cleaned)
            self.assertNotIn("AIzaSyD1234567890", cleaned)
            self.assertIn("REDACTED", cleaned)

    def test_app_allowlist(self):
        """Validate desktop applications, safe tools allowlist, and rejection of arbitrary PATH binaries."""
        # 1. Allowlisted browsers and GUI apps
        ok, app = validate_app_target("brave")
        self.assertTrue(ok)
        self.assertEqual(app, "brave")

        # 2. Common safe tools in APPLICATION_ALLOWLIST
        for safe_tool in ["agy", "htop", "btop", "nvtop", "fastfetch", "baobab", "ncdu"]:
            ok, app = validate_app_target(safe_tool)
            self.assertTrue(ok, f"Safe tool '{safe_tool}' should be allowed")

        # 3. Arbitrary PATH executables without .desktop entries MUST be rejected
        for arbitrary_bin in ["rm", "chmod", "curl", "wget", "cat", "dd", "nc"]:
            # Ensure it actually exists in PATH for test realism
            import shutil
            if shutil.which(arbitrary_bin):
                ok, err = validate_app_target(arbitrary_bin)
                self.assertFalse(ok, f"Arbitrary binary '{arbitrary_bin}' in PATH must NOT be accepted as app target")

        # 4. Nonexistent binary
        ok, app = validate_app_target("nonexistent_malicious_binary_xyz_123")
        self.assertFalse(ok)

        # 5. Path traversal attempts
        ok, err = validate_app_target("../../bin/sh")
        self.assertFalse(ok)

        # 6. Valid .desktop entries are accepted
        local_app_dir = os.path.expanduser("~/.local/share/applications")
        os.makedirs(local_app_dir, exist_ok=True)
        test_desktop_file = os.path.join(local_app_dir, "testcustomeditor.desktop")
        try:
            with open(test_desktop_file, "w") as fp:
                fp.write("[Desktop Entry]\nType=Application\nName=Test Custom Editor\nExec=custom-editor %U\n")
            ok, app = validate_app_target("testcustomeditor")
            self.assertTrue(ok, "Custom valid .desktop file should be accepted")
            self.assertEqual(app, "testcustomeditor.desktop")
        finally:
            if os.path.exists(test_desktop_file):
                os.unlink(test_desktop_file)

if __name__ == "__main__":
    unittest.main()
