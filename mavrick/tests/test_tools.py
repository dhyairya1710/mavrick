#!/usr/bin/env python3
"""
Unit Tests for MAVRICK Typed Tools
"""

import os
import glob
import shutil
import tempfile
import unittest
from mavrick.tests import IsolatedTestCase
from unittest.mock import patch, MagicMock
from mavrick.tools.system import (
    get_system_health,
    inspect_processes,
    set_power_profile,
    get_power_profile,
    control_media,
    get_network_status,
    clean_cache,
    inspect_storage_usage
)
from mavrick.tools.packages import check_updates
from mavrick.tools.files import list_files, organize_downloads, move_files, undo_last_action, apply_file_review
from mavrick.tools.apps import detect_preferred_terminal

class TestTools(IsolatedTestCase):
    def test_system_health(self):
        """get_system_health reads valid kernel /proc metrics."""
        health = get_system_health()
        self.assertIn("metrics", health)
        self.assertIn("report", health)
        metrics = health["metrics"]
        self.assertIn("memory", metrics)
        self.assertIn("disk", metrics)
        self.assertIn("cpu", metrics)
        self.assertGreater(metrics["memory"].get("total_mb", 0), 0)
        self.assertGreater(metrics["disk"].get("total_gb", 0), 0)

    def test_inspect_processes(self):
        """inspect_processes parses ps output into structured rows."""
        procs, table = inspect_processes("memory", limit=5)
        self.assertIsInstance(procs, list)
        self.assertGreater(len(procs), 0)
        self.assertIn("PROCESS", table)
        for p in procs:
            self.assertIn("command", p)
            self.assertIn("ram_mb", p)
            self.assertIn("pid", p)

    @patch("subprocess.run", return_value=MagicMock(returncode=0, stdout=""))
    def test_inspect_storage_usage(self, _run):
        """inspect_storage_usage returns structured storage metrics and formatted table."""
        ok, report, details = inspect_storage_usage(category="apps", limit=5)
        self.assertTrue(ok)
        self.assertIn("Application Storage Usage", report)
        self.assertIn("items", details)
        self.assertIn("disk_total_gb", details)
        self.assertIn("disk_free_gb", details)
        self.assertIsInstance(details["items"], list)

    def test_list_files(self):
        """list_files reads contents within bounded depth."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create sample files
            f1 = os.path.join(tmpdir, "sample.txt")
            with open(f1, "w") as fp:
                fp.write("Hello MAVRICK")

            sub = os.path.join(tmpdir, "subdir")
            os.makedirs(sub, exist_ok=True)
            f2 = os.path.join(sub, "nested.md")
            with open(f2, "w") as fp:
                fp.write("Nested document")

            ok, report, details = list_files(tmpdir, max_depth=2)
            self.assertTrue(ok)
            self.assertEqual(details["total_files"], 2)
            self.assertIn("sample.txt", report)
            self.assertIn("nested.md", report)

    def test_organize_downloads_and_undo(self):
        """organize_downloads preview, execution, and full undo restoration."""
        with tempfile.TemporaryDirectory() as tmpdir:
            img = os.path.join(tmpdir, "photo.jpg")
            doc = os.path.join(tmpdir, "notes.pdf")
            arch = os.path.join(tmpdir, "backup.zip")
            for p in [img, doc, arch]:
                with open(p, "w") as fp:
                    fp.write("content")

            # 1. Dry Run (Preview)
            ok, report, details = organize_downloads(tmpdir, dry_run=True)
            self.assertTrue(ok)
            self.assertEqual(details["planned_count"], 3)
            self.assertIn("preview", details)
            self.assertEqual(len(details["preview"]), 3)
            for item in details["preview"]:
                self.assertIn("source", item)
                self.assertIn("destination", item)
                self.assertIn(item["source"], report)
                self.assertIn(item["destination"], report)
            # Files should still be loose in tmpdir
            self.assertTrue(os.path.exists(img))
            self.assertTrue(os.path.exists(doc))
            self.assertTrue(os.path.exists(arch))

            # 2. Live Run (Execute)
            ok, report, details = apply_file_review(details["review"], [0, 1, 2])
            self.assertTrue(ok)
            self.assertEqual(details["executed_count"], 3)
            # Files should now be in categorized folders
            moved_img = os.path.join(tmpdir, "Images", "photo.jpg")
            moved_doc = os.path.join(tmpdir, "Documents", "notes.pdf")
            moved_arch = os.path.join(tmpdir, "Archives", "backup.zip")
            self.assertTrue(os.path.exists(moved_img))
            self.assertTrue(os.path.exists(moved_doc))
            self.assertTrue(os.path.exists(moved_arch))

            # 3. Undo
            ok, msg, u_details = undo_last_action()
            self.assertTrue(ok)
            self.assertEqual(u_details["restored_count"], 3)
            # Files must be restored to original loose paths
            self.assertTrue(os.path.exists(img))
            self.assertTrue(os.path.exists(doc))
            self.assertTrue(os.path.exists(arch))

    def test_move_files_and_undo(self):
        """move_files moves files safely, avoids collisions, and undo restores them."""
        with tempfile.TemporaryDirectory() as tmpdir:
            src_dir = os.path.join(tmpdir, "source_folder")
            dest_dir = os.path.join(tmpdir, "dest_folder")
            os.makedirs(src_dir)
            os.makedirs(dest_dir)

            f1 = os.path.join(src_dir, "file1.txt")
            f2 = os.path.join(src_dir, "file2.txt")
            with open(f1, "w") as fp:
                fp.write("file1 content")
            with open(f2, "w") as fp:
                fp.write("file2 content")

            # Move files
            ok, msg, details = move_files([f1, f2], dest_dir)
            self.assertTrue(ok)
            self.assertEqual(details["executed_count"], 2)
            self.assertTrue(details["undo_available"])

            # Verify files moved
            moved_f1 = os.path.join(dest_dir, "file1.txt")
            moved_f2 = os.path.join(dest_dir, "file2.txt")
            self.assertTrue(os.path.exists(moved_f1))
            self.assertTrue(os.path.exists(moved_f2))
            self.assertFalse(os.path.exists(f1))
            self.assertFalse(os.path.exists(f2))

            # Undo action
            u_ok, u_msg, u_details = undo_last_action()
            self.assertTrue(u_ok)
            self.assertEqual(u_details["restored_count"], 2)

            # Verify restored
            self.assertTrue(os.path.exists(f1))
            self.assertTrue(os.path.exists(f2))
            self.assertFalse(os.path.exists(moved_f1))
            self.assertFalse(os.path.exists(moved_f2))

    def test_file_operations_cancellation(self):
        """Cancellation token aborts file operations cleanly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            f1 = os.path.join(tmpdir, "test.txt")
            with open(f1, "w") as fp:
                fp.write("data")

            # Cancellation token always True
            cancel_token = lambda: True

            # list_files cancellation
            ok, report, details = list_files(tmpdir, cancel_token=cancel_token)
            self.assertFalse(ok)
            self.assertTrue(details.get("cancelled"))

            # organize_downloads cancellation
            ok, report, details = organize_downloads(tmpdir, dry_run=True, cancel_token=cancel_token)
            self.assertFalse(ok)
            self.assertTrue(details.get("cancelled"))

            # move_files cancellation
            ok, msg, details = move_files([f1], os.path.join(tmpdir, "dest"), cancel_token=cancel_token)
            self.assertFalse(ok)
            self.assertTrue(details.get("cancelled"))

    def test_organize_downloads_exact_preview_structure(self):
        """organize_downloads in dry-run returns exact structured list [{'source': ..., 'destination': ...}]."""
        with tempfile.TemporaryDirectory() as tmpdir:
            pdf_path = os.path.join(tmpdir, "doc.pdf")
            png_path = os.path.join(tmpdir, "image.png")
            with open(pdf_path, "w") as fp:
                fp.write("pdf data")
            with open(png_path, "w") as fp:
                fp.write("png data")

            ok, report, details = organize_downloads(tmpdir, dry_run=True)
            self.assertTrue(ok)
            self.assertTrue(details["dry_run"])
            self.assertIn("preview", details)
            preview = details["preview"]
            self.assertEqual(len(preview), 2)

            # Validate exact dictionary structure
            expected_dest_pdf = os.path.join(tmpdir, "Documents", "doc.pdf")
            expected_dest_png = os.path.join(tmpdir, "Images", "image.png")

            sources = [p["source"] for p in preview]
            destinations = [p["destination"] for p in preview]

            self.assertIn(pdf_path, sources)
            self.assertIn(png_path, sources)
            self.assertIn(expected_dest_pdf, destinations)
            self.assertIn(expected_dest_png, destinations)

            # Files must remain untouched during preview
            self.assertTrue(os.path.exists(pdf_path))
            self.assertTrue(os.path.exists(png_path))

    def test_terminal_detection(self):
        """detect_preferred_terminal returns a valid terminal emulator on PATH."""
        term = detect_preferred_terminal()
        self.assertIsNotNone(term)
        self.assertTrue(shutil.which(term) is not None)

    @patch("shutil.which")
    @patch("subprocess.run")
    def test_system_health_nvidia_gpu(self, mock_run, mock_which):
        """get_system_health captures NVIDIA GPU utilization, VRAM, and temperature."""
        def which_side_effect(cmd):
            return "/usr/bin/nvidia-smi" if cmd == "nvidia-smi" else shutil.which(cmd)
        mock_which.side_effect = which_side_effect

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "42.0, 8192.0, 2048.0, 65.0\n"
        mock_run.return_value = mock_proc

        health = get_system_health()
        gpu = health["metrics"].get("gpu", {})
        self.assertEqual(gpu.get("vendor"), "NVIDIA")
        self.assertEqual(gpu.get("utilization_pct"), 42.0)
        self.assertEqual(gpu.get("memory_total_mb"), 8192.0)
        self.assertEqual(gpu.get("memory_used_mb"), 2048.0)
        self.assertEqual(gpu.get("temperature_c"), 65.0)
        self.assertIn("GPU:  NVIDIA (42% util | 2048 / 8192 MB VRAM | 65.0°C)", health["report"])

    @patch("shutil.which")
    def test_system_health_amd_gpu(self, mock_which):
        """get_system_health captures AMD GPU metrics from sysfs and hwmon."""
        mock_which.return_value = None  # No nvidia-smi

        with tempfile.TemporaryDirectory() as tmpdir:
            dev_dir = os.path.join(tmpdir, "card0", "device")
            hwmon_dir = os.path.join(dev_dir, "hwmon", "hwmon0")
            os.makedirs(hwmon_dir, exist_ok=True)

            busy_file = os.path.join(dev_dir, "gpu_busy_percent")
            vtotal_file = os.path.join(dev_dir, "mem_info_vram_total")
            vused_file = os.path.join(dev_dir, "mem_info_vram_used")
            temp_file = os.path.join(hwmon_dir, "temp1_input")

            with open(busy_file, "w") as fp:
                fp.write("28\n")
            with open(vtotal_file, "w") as fp:
                fp.write(str(4096 * 1024 * 1024) + "\n")
            with open(vused_file, "w") as fp:
                fp.write(str(1024 * 1024 * 1024) + "\n")
            with open(temp_file, "w") as fp:
                fp.write("58000\n")

            with patch("glob.glob") as mock_glob:
                def glob_side_effect(pattern):
                    if "gpu_busy_percent" in pattern:
                        return [busy_file]
                    elif "temp*_input" in pattern:
                        return [temp_file]
                    return []
                mock_glob.side_effect = glob_side_effect

                health = get_system_health()
                gpu = health["metrics"].get("gpu", {})
                self.assertEqual(gpu.get("vendor"), "AMD")
                self.assertEqual(gpu.get("utilization_pct"), 28.0)
                self.assertEqual(gpu.get("memory_total_mb"), 4096.0)
                self.assertEqual(gpu.get("memory_used_mb"), 1024.0)
                self.assertEqual(gpu.get("temperature_c"), 58.0)
                self.assertIn("GPU:  AMD", health["report"])

    @patch("shutil.which", return_value=None)
    @patch("glob.glob", return_value=[])
    def test_system_health_no_gpu(self, mock_glob, mock_which):
        """get_system_health gracefully reports when no dedicated GPU is detected."""
        health = get_system_health()
        gpu = health["metrics"].get("gpu", {})
        self.assertEqual(gpu.get("status"), "No dedicated GPU detected")
        self.assertIn("GPU:  No dedicated GPU detected", health["report"])

    def test_set_power_profile_invalid(self):
        """set_power_profile rejects unsupported power profiles."""
        ok, msg, ver = set_power_profile("overclocked")
        self.assertFalse(ok)
        self.assertIn("Invalid power profile", msg)

    @patch("shutil.which")
    @patch("subprocess.run")
    def test_set_power_profile_system76(self, mock_run, mock_which):
        """set_power_profile sets profile via system76-power and verifies current profile."""
        mock_which.side_effect = lambda cmd: "/usr/bin/system76-power" if cmd == "system76-power" else None

        set_proc = MagicMock(returncode=0)
        query_proc = MagicMock(returncode=0, stdout="Power Profile: performance\n")
        mock_run.side_effect = [set_proc, query_proc]

        ok, msg, ver = set_power_profile("performance")
        self.assertTrue(ok)
        self.assertIn("system76-power", msg)
        self.assertIn("performance", ver)

    @patch("shutil.which")
    @patch("subprocess.run")
    def test_set_power_profile_powerprofilesctl_fallback(self, mock_run, mock_which):
        """set_power_profile falls back to powerprofilesctl when system76-power is absent."""
        mock_which.side_effect = lambda cmd: "/usr/bin/powerprofilesctl" if cmd == "powerprofilesctl" else None

        set_proc = MagicMock(returncode=0)
        query_proc = MagicMock(returncode=0, stdout="power-saver\n")
        mock_run.side_effect = [set_proc, query_proc]

        ok, msg, ver = set_power_profile("battery")
        self.assertTrue(ok)
        self.assertIn("powerprofilesctl", msg)
        self.assertIn("battery", ver)

    @patch("shutil.which", return_value=None)
    def test_set_power_profile_unavailable(self, mock_which):
        """set_power_profile returns clear error when no power management daemon is available."""
        ok, msg, ver = set_power_profile("balanced")
        self.assertFalse(ok)
        self.assertIn("not available", msg.lower())

    @patch("shutil.which", return_value="/mock/playerctl")
    def test_control_media_invalid_action(self, _which):
        """control_media rejects unlisted media commands."""
        ok, msg, details = control_media("rewind_30s")
        self.assertFalse(ok)
        self.assertIn("Invalid media action", msg)

    @patch("shutil.which", return_value=None)
    def test_control_media_no_playerctl(self, mock_which):
        """control_media returns informative message if playerctl is uninstalled."""
        ok, msg, details = control_media("play_pause")
        self.assertFalse(ok)
        self.assertIn("playerctl is not installed", msg)

    @patch("shutil.which", return_value="/usr/bin/playerctl")
    @patch("subprocess.run")
    def test_control_media_playback_actions(self, mock_run, mock_which):
        """control_media triggers play/pause and reports metadata."""
        action_proc = MagicMock(returncode=0)
        meta_proc = MagicMock(returncode=0, stdout="Queen\tBohemian Rhapsody\tPlaying\n")
        mock_run.side_effect = [action_proc, meta_proc]

        ok, msg, details = control_media("play_pause")
        self.assertTrue(ok)
        self.assertEqual(details["action"], "play_pause")
        self.assertEqual(details["artist"], "Queen")
        self.assertEqual(details["title"], "Bohemian Rhapsody")
        self.assertEqual(details["status"], "Playing")
        self.assertIn("Bohemian Rhapsody", msg)

    @patch("shutil.which", return_value="/usr/bin/playerctl")
    @patch("subprocess.run")
    def test_control_media_status_query(self, mock_run, mock_which):
        """control_media queries status without triggering playback control."""
        meta_proc = MagicMock(returncode=0, stdout="Hans Zimmer\tTime\tPaused\n")
        mock_run.return_value = meta_proc

        ok, msg, details = control_media("status")
        self.assertTrue(ok)
        self.assertEqual(details["action"], "status")
        self.assertEqual(details["artist"], "Hans Zimmer")
        self.assertEqual(details["title"], "Time")
        self.assertEqual(details["status"], "Paused")
        self.assertIn("Paused. Time by Hans Zimmer", msg)

    @patch("shutil.which", return_value="/usr/bin/playerctl")
    @patch("subprocess.run")
    def test_control_media_no_players(self, mock_run, mock_which):
        """control_media reports cleanly when no media player is active."""
        action_proc = MagicMock(returncode=1, stderr="No players found")
        mock_run.return_value = action_proc

        ok, msg, details = control_media("next")
        self.assertFalse(ok)
        self.assertIn("No players found", msg)

    @patch("shutil.which")
    @patch("subprocess.run")
    def test_get_network_status_json_success(self, mock_run, mock_which):
        """get_network_status parses ip -j route output and ping latency."""
        mock_which.return_value = "/usr/bin/ip"

        ip_proc = MagicMock(
            returncode=0,
            stdout='[{"dev": "wlan0", "gateway": "192.168.1.1", "prefsrc": "192.168.1.55"}]'
        )
        ping_proc = MagicMock(
            returncode=0,
            stdout="64 bytes from 1.1.1.1: icmp_seq=1 ttl=57 time=12.4 ms\n"
        )
        mock_run.side_effect = [ip_proc, ping_proc]

        net = get_network_status()
        self.assertEqual(net["interface"], "wlan0")
        self.assertEqual(net["ip"], "192.168.1.55")
        self.assertEqual(net["gateway"], "192.168.1.1")
        self.assertEqual(net["dns_latency_ms"], 12.4)
        self.assertTrue(net["connected"])
        self.assertIn("12.4 ms to 1.1.1.1", net["report"])

    @patch("shutil.which")
    @patch("subprocess.run")
    def test_get_network_status_plaintext_fallback(self, mock_run, mock_which):
        """get_network_status parses plain text ip route fallback."""
        mock_which.return_value = "/usr/bin/ip"

        ip_json_proc = MagicMock(returncode=1, stdout="")
        ip_txt_proc = MagicMock(
            returncode=0,
            stdout="default via 10.0.0.1 dev enp4s0 proto dhcp src 10.0.0.99 metric 100\n"
        )
        ping_proc = MagicMock(
            returncode=0,
            stdout="rtt min/avg/max/mdev = 8.1/9.5/11.0/1.2 ms\n"
        )
        mock_run.side_effect = [ip_json_proc, ip_txt_proc, ping_proc]

        net = get_network_status()
        self.assertEqual(net["interface"], "enp4s0")
        self.assertEqual(net["gateway"], "10.0.0.1")
        self.assertEqual(net["ip"], "10.0.0.99")
        self.assertEqual(net["dns_latency_ms"], 9.5)
        self.assertTrue(net["connected"])

    @patch("shutil.which")
    @patch("subprocess.run")
    def test_get_network_status_disconnected(self, mock_run, mock_which):
        """get_network_status marks disconnected when ping fails."""
        mock_which.return_value = "/usr/bin/ip"

        ip_proc = MagicMock(returncode=1, stdout="")
        ip_txt_proc = MagicMock(returncode=1, stdout="")
        ping_proc = MagicMock(returncode=1, stdout="ping: connect: Network is unreachable\n")
        mock_run.side_effect = [ip_proc, ip_txt_proc, ping_proc]

        net = get_network_status()
        self.assertFalse(net["connected"])
        self.assertIn("Disconnected / Unreachable", net["report"])

    @patch("shutil.which", return_value="/usr/bin/apt")
    @patch("subprocess.run")
    def test_check_updates_with_packages(self, mock_run, mock_which):
        """check_updates reports pending APT and Flatpak updates."""
        apt_output = (
            "Listing... Done\n"
            "curl/jammy-updates 7.81.0-1ubuntu1.15 amd64 [upgradable from: 7.81.0-1ubuntu1.14]\n"
            "git/jammy-updates 1:2.34.1-1ubuntu1.10 amd64 [upgradable from: 1:2.34.1-1ubuntu1.9]\n"
        )
        flatpak_output = "org.videolan.VLC\tstable\n"

        apt_proc = MagicMock(returncode=0, stdout=apt_output)
        flatpak_proc = MagicMock(returncode=0, stdout=flatpak_output)
        mock_run.side_effect = [apt_proc, flatpak_proc]

        upd = check_updates()
        self.assertEqual(upd["total_updates"], 3)
        self.assertEqual(len(upd["apt_updates"]), 2)
        self.assertEqual(upd["apt_updates"][0]["package"], "curl")
        self.assertEqual(upd["apt_updates"][1]["package"], "git")
        self.assertEqual(len(upd["flatpak_updates"]), 1)
        self.assertIn("APT (2)", upd["report"])
        self.assertIn("curl, git", upd["report"])

    @patch("shutil.which", return_value="/usr/bin/apt")
    @patch("subprocess.run")
    def test_check_updates_up_to_date(self, mock_run, mock_which):
        """check_updates reports up-to-date state when no updates pending."""
        apt_proc = MagicMock(returncode=0, stdout="Listing... Done\n")
        flatpak_proc = MagicMock(returncode=0, stdout="")
        mock_run.side_effect = [apt_proc, flatpak_proc]

        upd = check_updates()
        self.assertEqual(upd["total_updates"], 0)
        self.assertIn("APT: Up to date", upd["report"])
        self.assertIn("Flatpak: Up to date", upd["report"])

    def test_clean_cache_dry_run_and_execution(self):
        """clean_cache preview preserves files; live run purges thumbnails."""
        with tempfile.TemporaryDirectory() as tmpdir:
            thumb_dir = os.path.join(tmpdir, "thumbnails")
            apt_dir = os.path.join(tmpdir, "apt")
            os.makedirs(thumb_dir, exist_ok=True)
            os.makedirs(apt_dir, exist_ok=True)

            t1 = os.path.join(thumb_dir, "thumb1.png")
            t2 = os.path.join(thumb_dir, "thumb2.png")
            a1 = os.path.join(apt_dir, "pkg1.deb")
            for p in [t1, t2, a1]:
                with open(p, "w") as fp:
                    fp.write("dummy-data-content" * 100)

            # 1. Dry Run (Preview)
            ok, report, details = clean_cache(dry_run=True, thumbnail_dir=thumb_dir, apt_dir=apt_dir)
            self.assertTrue(ok)
            self.assertTrue(details["dry_run"])
            self.assertEqual(details["thumbnails_count"], 2)
            self.assertEqual(details["apt_count"], 1)
            self.assertGreater(details["total_reclaimable_bytes"], 0)
            self.assertIn("Cache Clean Preview (Dry Run)", report)
            self.assertTrue(os.path.exists(t1))
            self.assertTrue(os.path.exists(t2))
            self.assertTrue(os.path.exists(a1))

            # 2. Live Run (Purge)
            ok, report, details = clean_cache(dry_run=False, thumbnail_dir=thumb_dir, apt_dir=apt_dir)
            self.assertTrue(ok)
            self.assertFalse(details["dry_run"])
            self.assertEqual(details["thumbnails_cleaned_count"], 2)
            self.assertIn("Cache Clean Completed", report)
            # Thumbnails must be deleted
            self.assertFalse(os.path.exists(t1))
            self.assertFalse(os.path.exists(t2))

if __name__ == "__main__":
    unittest.main()
