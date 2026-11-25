#!/usr/bin/env python3
"""
Unit Tests for MAVRICK Pop!_OS Packaging & Installation Scripts
"""

import os
import stat
import tempfile
import unittest
from mavrick.tests import IsolatedTestCase
from pathlib import Path
from unittest.mock import patch, MagicMock

from mavrick.package_deb import (
    get_mavrick_version,
    create_control_file,
    create_maintainer_scripts,
    runtime_manifest,
    stage_payload_files
)
from mavrick.install import (
    ensure_albert_sans_font,
    check_and_prompt_optional_utilities
)

class TestPackagingAndInstall(IsolatedTestCase):
    def test_package_deb_version(self):
        """get_mavrick_version extracts valid version string."""
        ver = get_mavrick_version()
        self.assertIsInstance(ver, str)
        self.assertTrue(len(ver.split(".")) >= 2)

    def test_package_deb_control_file(self):
        """create_control_file generates compliant Debian control metadata."""
        with tempfile.TemporaryDirectory() as tmpdir:
            staging = Path(tmpdir)
            create_control_file(staging, "2.0.0")

            control_path = staging / "DEBIAN" / "control"
            self.assertTrue(control_path.exists())
            content = control_path.read_text(encoding="utf-8")

            self.assertIn("Package: mavrick", content)
            self.assertIn("Version: 2.0.0", content)
            self.assertIn("Depends: python3 (>= 3.10), python3-gi, gir1.2-gtk-3.0, libnotify-bin, playerctl", content)
            self.assertIn("Recommends: system76-power", content)
            self.assertIn("Architecture: all", content)

            mode = stat.S_IMODE(os.stat(control_path).st_mode)
            self.assertEqual(mode, 0o644)

    def test_package_deb_maintainer_scripts(self):
        """create_maintainer_scripts creates executable postinst and prerm hooks."""
        with tempfile.TemporaryDirectory() as tmpdir:
            staging = Path(tmpdir)
            create_maintainer_scripts(staging)

            postinst = staging / "DEBIAN" / "postinst"
            prerm = staging / "DEBIAN" / "prerm"

            self.assertTrue(postinst.exists())
            self.assertTrue(prerm.exists())

            postinst_text = postinst.read_text(encoding="utf-8")
            self.assertIn("update-desktop-database", postinst_text)
            self.assertIn("daemon-reload", postinst_text)

            prerm_text = prerm.read_text(encoding="utf-8")
            self.assertIn("stop mavrick.service mavrickd.service", prerm_text)

            postinst_mode = stat.S_IMODE(os.stat(postinst).st_mode)
            prerm_mode = stat.S_IMODE(os.stat(prerm).st_mode)
            self.assertEqual(postinst_mode, 0o755)
            self.assertEqual(prerm_mode, 0o755)

    def test_package_deb_stage_payload_files(self):
        """stage_payload_files populates standard Debian hierarchy."""
        with tempfile.TemporaryDirectory() as tmpdir:
            staging = Path(tmpdir)
            stage_payload_files(staging)

            # 1. /usr/lib/mavrick
            usr_lib = staging / "usr" / "lib" / "mavrick"
            source_root = Path(__file__).resolve().parents[1]
            self.assertTrue(usr_lib.exists())
            manifest = runtime_manifest()
            for rel in ("mcp/__init__.py", "mcp/client.py", "mcp/registry.py", "ui/tk_hud.py", "platform/services.py"):
                self.assertIn(Path(rel), manifest)
            for module in manifest:
                self.assertEqual((usr_lib / module).read_bytes(), (source_root / module).read_bytes())

            # 2. /usr/bin/mavrick
            usr_bin = staging / "usr" / "bin" / "mavrick"
            self.assertTrue(usr_bin.exists())
            bin_mode = stat.S_IMODE(os.stat(usr_bin).st_mode)
            self.assertEqual(bin_mode, 0o755)
            bin_content = usr_bin.read_text(encoding="utf-8")
            self.assertIn("python3 -m mavrick.cli", bin_content)

            # 3. /usr/share/applications/mavrick.desktop
            desktop_file = staging / "usr" / "share" / "applications" / "mavrick.desktop"
            self.assertTrue(desktop_file.exists())

            # 4. /usr/share/doc/mavrick/README.md
            doc_file = staging / "usr" / "share" / "doc" / "mavrick" / "README.md"
            self.assertTrue(doc_file.exists())

            # 5. /usr/lib/systemd/user services
            systemd_user = staging / "usr" / "lib" / "systemd" / "user"
            self.assertTrue((systemd_user / "mavrick.service").exists())
            self.assertTrue((systemd_user / "mavrickd.service").exists())

    @patch("subprocess.run")
    def test_ensure_albert_sans_font_already_present(self, mock_run):
        """ensure_albert_sans_font detects existing font via fc-match without downloading."""
        mock_proc = MagicMock(returncode=0, stdout="AlbertSans-VariableFont_wght.ttf: 'Albert Sans' 'Regular'\n")
        mock_run.return_value = mock_proc

        result = ensure_albert_sans_font()
        self.assertTrue(result)
        mock_run.assert_called_once_with(["fc-match", "Albert Sans"], capture_output=True, text=True)

    @patch("shutil.which")
    def test_check_optional_utilities(self, mock_which):
        """check_and_prompt_optional_utilities inspects system for Pop!_OS tools."""
        # Simulate all utilities installed
        mock_which.side_effect = lambda cmd: f"/usr/bin/{cmd}"
        # Should complete without throwing exception
        check_and_prompt_optional_utilities(auto_yes=False)

if __name__ == "__main__":
    unittest.main()
