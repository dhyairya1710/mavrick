#!/usr/bin/env python3
"""
Unit Tests for MAVRICK History, Context, and Storage Security
"""

import os
import stat
import unittest
from mavrick.tests import IsolatedTestCase
from mavrick.config import (
    MAVRICK_STATE_DIR,
    HISTORY_FILE,
    AUDIT_LOG_FILE,
    ensure_secure_storage
)
from mavrick.history import (
    append_history,
    load_history,
    clear_history_and_context,
    search_history,
    append_audit_log
)

class TestHistoryAndStorage(IsolatedTestCase):
    def setUp(self):
        ensure_secure_storage()

    def test_directory_and_file_permissions(self):
        """MAVRICK state directory must be 0700, and files 0600."""
        # Check directory mode
        dir_mode = stat.S_IMODE(os.stat(MAVRICK_STATE_DIR).st_mode)
        self.assertEqual(dir_mode, 0o700)

        # Write test history and audit log
        append_history("test query for permissions check", status="completed", summary="testing")
        append_audit_log({"action": "test", "status": "completed"})

        # Check history file mode
        self.assertTrue(os.path.exists(HISTORY_FILE))
        hist_mode = stat.S_IMODE(os.stat(HISTORY_FILE).st_mode)
        self.assertEqual(hist_mode, 0o600)

        # Check audit log mode
        self.assertTrue(os.path.exists(AUDIT_LOG_FILE))
        audit_mode = stat.S_IMODE(os.stat(AUDIT_LOG_FILE).st_mode)
        self.assertEqual(audit_mode, 0o600)

    def test_secret_scrubbing_in_history(self):
        """Passwords and tokens must be scrubbed before persisting to disk."""
        secret_query = "install app with token sk-abcdef123456789012345678"
        append_history(secret_query, status="completed", summary="testing")

        hist = load_history()
        self.assertTrue(len(hist) > 0)
        last_entry = hist[-1]
        self.assertNotIn("sk-abcdef123456789012345678", last_entry["query"])
        self.assertIn("REDACTED", last_entry["query"])

    def test_search_and_clear_history(self):
        """Search history and clear history functionality."""
        append_history("special_unique_search_query_123", status="completed", summary="test")
        results = search_history("special_unique_search_query_123")
        self.assertTrue(len(results) >= 1)

        ok = clear_history_and_context()
        self.assertTrue(ok)
        self.assertEqual(len(load_history()), 0)

if __name__ == "__main__":
    unittest.main()
