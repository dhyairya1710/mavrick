"""Offline native-adapter fixtures, authenticated IPC, and import/release checks."""

import json
import os
import plistlib
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from mavrick.tests import IsolatedTestCase, TEST_ROOT, _real_popen
from mavrick import ai, config, daemon, ipc, package_deb
from mavrick.platform import get_platform, use_platform
from mavrick.platform.base import UnsupportedPlatformAction
from mavrick.platform.linux import LinuxAdapter
from mavrick.platform.macos import MacOSAdapter
from mavrick.platform.windows import WindowsAdapter
from mavrick.providers.agy_pipes import WorkerPipeRelay
from mavrick.tools import apps, files, packages, system

_REAL_SOCKET = socket.socket


def _write_test_lnk(path: Path, target: str) -> None:
    """Write a minimal valid MS-SHLLINK (.lnk) binary pointing to target."""
    import struct

    hdr_size = 0x4C
    clsid = b"\x01\x14\x02\x00\x00\x00\x00\x00\xc0\x00\x00\x00\x00\x00\x00\x46"
    link_flags = 0x02  # HasLinkInfo
    header = struct.pack("<I", hdr_size) + clsid + struct.pack("<I", link_flags) + (b"\x00" * (hdr_size - 24))
    local_bytes = target.encode("utf-8") + b"\x00"
    suffix_bytes = b"\x00"
    info_hdr_size = 28
    local_off = info_hdr_size
    suffix_off = local_off + len(local_bytes)
    link_info_size = suffix_off + len(suffix_bytes)
    link_info = struct.pack("<7I", link_info_size, info_hdr_size, 0x01, 0, local_off, 0, suffix_off) + local_bytes + suffix_bytes
    path.write_bytes(header + link_info)


class TestPlatformAdapters(IsolatedTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(dir=TEST_ROOT)
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def test_windows_profile_start_menu_and_exact_launch(self):
        suffix = Path("Microsoft/Windows/Start Menu/Programs")
        roaming = self.root / "roaming"
        local = self.root / "local"
        shared = self.root / "common"
        search = self.root / "bin"
        tools = self.root / "tools"
        adapter = WindowsAdapter(self.root, {"APPDATA": str(roaming), "LOCALAPPDATA": str(local),
                                             "ProgramData": str(shared), "PATH": str(search)})
        shortcut = roaming / suffix / "Fixture Tool.lnk"
        common = shared / suffix / "Common Utility.lnk"
        unsafe_lnk = roaming / suffix / "Evil Script.lnk"
        executable = search / "fixture.exe"
        approved_exe = tools / "custom-editor.exe"
        for path in (shortcut, common, unsafe_lnk, executable, approved_exe):
            path.parent.mkdir(parents=True, exist_ok=True)
        executable.touch()
        approved_exe.touch()
        (search / "unsafe.cmd").touch()
        _write_test_lnk(shortcut, r"C:\Program Files\Fixture\fixture.exe")
        _write_test_lnk(common, r"C:\Program Files\Common\common.exe")
        _write_test_lnk(unsafe_lnk, r"C:\Windows\System32\cmd.exe")
        cfg_file = adapter.config_dir() / "config.json"
        cfg_file.parent.mkdir(parents=True, exist_ok=True)
        cfg_file.write_text(json.dumps({"approved_apps": {"custom-editor": str(approved_exe)}}), encoding="utf-8")

        self.assertEqual(adapter.config_dir(), roaming / "Mavrick")
        self.assertEqual(adapter.runtime_dir(), local / "Mavrick" / "Run")
        self.assertEqual(adapter.capabilities.ipc_transport, "tcp-token")
        self.assertEqual(adapter.get_cache_targets()[1].path, local / "Temp" / "Mavrick")
        index = adapter.discover_apps()
        self.assertEqual(index["fixture-tool"], str(shortcut))
        self.assertEqual(index["common-utility"], str(common))
        self.assertEqual(index["custom-editor"], str(approved_exe))
        self.assertNotIn("fixture", index)  # Arbitrary PATH .exe scanning is disabled.
        self.assertNotIn("evil-script", index)  # Shortcuts to scripts/interpreters are rejected.
        self.assertNotIn("unsafe", index)
        with use_platform(adapter), patch.object(os, "startfile", create=True) as launch:
            self.assertIs(get_platform(), adapter)
            self.assertTrue(apps.open_app("fixture-tool")[0])
            launch.assert_called_once_with(str(shortcut))
        with use_platform(adapter):
            self.assertFalse(files.apply_file_review(None, ())[0])
            with self.assertRaises(UnsupportedPlatformAction):
                system.set_volume(level=10)

    def test_windows_winget_vectors_and_scoop_detection(self):
        adapter = WindowsAdapter(self.root)
        with patch("shutil.which", side_effect=lambda name: "/fixture/winget.exe" if name == "winget" else None):
            self.assertEqual(adapter.package_manager_name(), "winget")
            self.assertEqual(adapter.package_command("search", "Vendor.App"),
                             ["winget", "search", "--id", "Vendor.App", "--exact", "--disable-interactivity"])
            self.assertEqual(adapter.package_command("remove", "Vendor.App")[1:5],
                             ["uninstall", "--id", "Vendor.App", "--exact"])
            for bad in ("--all", "Vendor.App & whoami", "Vendor.App\nother", "C:/temp/tool", "foo bar"):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    adapter.package_command("install", bad)
            with use_platform(adapter):
                self.assertEqual(packages.resolve_package("install_package", {
                    "package_name": "Vendor.App", "manager": "auto"
                }), ("Vendor.App", "winget", None))
        with patch("shutil.which", side_effect=lambda name: "/fixture/scoop.exe" if name == "scoop.exe" else None):
            self.assertEqual(adapter.package_manager_name(), "scoop")
            self.assertEqual(adapter.package_command("update", "fixture"), ["/fixture/scoop.exe", "update", "fixture"])

    def test_macos_bundle_launch_and_brew_vectors(self):
        adapter = MacOSAdapter(self.root, {})
        bundle = self.root / "Applications" / "Fixture Browser.app"
        info = bundle / "Contents" / "Info.plist"
        info.parent.mkdir(parents=True)
        info.write_bytes(plistlib.dumps({"CFBundleIdentifier": "dev.mavrick.Fixture", "CFBundleName": "Fixture Browser"}))
        malformed = self.root / "Applications" / "Broken.app" / "Contents" / "Info.plist"
        malformed.parent.mkdir(parents=True)
        malformed.write_text("invalid")
        discovered = adapter.discover_apps()
        self.assertEqual(discovered["dev.mavrick.fixture"], str(bundle))
        self.assertEqual(discovered["fixture-browser"], str(bundle))
        self.assertNotIn("broken", discovered)
        self.assertEqual(adapter.cache_dir(), self.root / "Library" / "Caches" / "Mavrick")
        self.assertEqual(adapter.log_dir(), self.root / "Library" / "Logs" / "Mavrick")
        with use_platform(adapter), patch("shutil.which", return_value="/fixture/bin"), \
             patch.object(apps, "launch_command", return_value=(True, "requested", {})) as launch:
            self.assertTrue(apps.open_app("dev.mavrick.fixture")[0])
            launch.assert_called_once_with(["open", "-a", str(bundle)], "dev.mavrick.fixture", None)
            self.assertEqual(adapter.package_manager_name(), "brew")
        self.assertEqual(adapter.package_command("list", "wget"), ["brew", "list", "--versions", "wget"])
        self.assertEqual(adapter.package_command("install", "wget"), ["brew", "install", "wget"])
        self.assertEqual(adapter.package_command("updates"), ["brew", "outdated", "--json=v2"])
        with self.assertRaises(ValueError):
            adapter.package_command("remove", "wget;evil")

    def test_linux_xdg_apt_and_safe_cache_preview(self):
        data = self.root / "data"
        app = data / "applications" / "dev.mavrick.Fixture.desktop"
        app.parent.mkdir(parents=True)
        app.touch()
        adapter = LinuxAdapter(self.root, {"XDG_DATA_HOME": str(data), "XDG_DATA_DIRS": str(self.root / "system")})
        self.assertEqual(adapter.discover_apps()["dev.mavrick.fixture"], app.name)
        self.assertEqual(adapter.package_command("install", "htop", version="1:3.2-1"), [
            "sudo", "apt-get", "install", "--", "htop=1:3.2-1"
        ])
        with self.assertRaises(ValueError):
            adapter.package_command("remove", "--all")
        cache = self.root / "cache"
        cache.mkdir()
        (cache / "thumbnail").write_bytes(b"cached")
        (cache / "outside-link").symlink_to(app)
        archives = self.root / "archives"
        archives.mkdir()
        with use_platform(adapter):
            ok, _, preview = system.clean_cache(True, str(cache), str(archives))
            refused, _, _ = system.clean_cache(True, str(cache), "/tmp")
        self.assertTrue(ok)
        self.assertFalse(refused)
        self.assertEqual(preview["thumbnails_count"], 1)
        self.assertTrue(app.exists())
        self.assertTrue((cache / "thumbnail").exists())

    def test_linux_file_move_rolls_back_an_inode_replacement(self):
        source, destination = self.root / "source.txt", self.root / "destination.txt"
        source.write_text("reviewed")
        review = files.review_files("move_files", {"sources": [str(source)], "destination": str(destination)})
        real_rename = files._rename_no_replace
        raced = False

        def replace_then_move(src_fd, src, dst_fd, dst):
            nonlocal raced
            if not raced:
                raced = True
                moved_away = self.root / "reviewed-original.txt"
                source.rename(moved_away)
                source.write_text("replacement")
            return real_rename(src_fd, src, dst_fd, dst)

        journal = str(self.root / "undo.json")
        with patch.object(files, "UNDO_STATE_FILE", journal), \
             patch.object(files, "_rename_no_replace", side_effect=replace_then_move):
            ok, _, details = files.apply_file_review(review, {0})
        self.assertFalse(ok)
        self.assertEqual(details["executed_count"], 0)
        self.assertEqual(source.read_text(), "replacement")
        self.assertFalse(destination.exists())
        self.assertEqual(json.loads(Path(journal).read_text())["batches"], [])


class TestTCPTokenIPC(IsolatedTestCase):
    def test_token_negotiation_frame_limit_and_cancellation_end_to_end(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as directory:
            descriptor = str(Path(directory) / "daemon.json")
            env = {"MAVRICK_IPC_TRANSPORT": "tcp-token", "MAVRICK_IPC_DESCRIPTOR": descriptor}
            with patch.dict(os.environ, env), patch.object(socket, "socket", _REAL_SOCKET):
                service = daemon.MavrickDaemon(transport="tcp-token", descriptor=descriptor, query_timeout=1.5)
                entered, disconnected = threading.Event(), threading.Event()
                prompts = []

                def reply(prompt, _instructions, *, cancelled, deadline):
                    prompts.append(prompt[:30])
                    if prompt == "stall fixture":
                        entered.set()
                        while time.monotonic() < deadline:
                            if cancelled():
                                disconnected.set()
                                raise ipc.RequestCancelled
                            time.sleep(0.01)
                    yield {"event": "done", "response": '{"type":"message","message":"verified fixture"}'}

                with patch.object(service.worker, "start_background"), \
                     patch.object(service.worker, "query_stream", side_effect=reply):
                    serving = threading.Thread(target=service.start, daemon=True)
                    serving.start()
                    try:
                        until = time.monotonic() + 2
                        while not service.running and serving.is_alive() and time.monotonic() < until:
                            time.sleep(0.01)
                        self.assertTrue(service.running)
                        endpoint = ipc.read_tcp_descriptor(descriptor)
                        self.assertEqual(stat.S_IMODE(Path(descriptor).stat().st_mode), 0o600)
                        self.assertEqual(endpoint.address[0], "127.0.0.1")
                        self.assertEqual(endpoint.pid, os.getpid())
                        self.assertNotIn(endpoint.token, repr(endpoint))

                        for bad in ({}, {"token": "z" * 64}, {"token": "\ud800"}):
                            end = time.monotonic() + 2
                            with ipc.connect_local(endpoint, end) as connection:
                                # The JSON surrogate fixture is escaped before encoding.
                                connection.setblocking(True)
                                connection.sendall((json.dumps({"action": "query", **bad}) + "\n").encode())
                                connection.setblocking(False)
                                self.assertEqual(ipc.FrameReader(connection).read(end)["code"], "auth")
                        self.assertFalse(prompts)
                        end = time.monotonic() + 2
                        with ipc.connect_local(endpoint, end) as connection:
                            payload = endpoint.request({"action": "query", "prompt": "", "negotiate_timeouts": True})
                            overhead = len(json.dumps(payload, separators=(",", ":")).encode())
                            payload["prompt"] = "é" * ((ipc.MAX_FRAME_BYTES - overhead) // 2)
                            if (ipc.MAX_FRAME_BYTES - overhead) % 2:
                                payload["prompt"] += "A"
                            ipc.write_frame(connection, payload, end)
                            reader = ipc.FrameReader(connection)
                            self.assertEqual(reader.read(end), {"event": "timeouts", "query_timeout": 1.5})
                            self.assertEqual(reader.read(end)["event"], "done")

                        end = time.monotonic() + 2
                        with ipc.connect_local(endpoint, end) as connection:
                            connection.setblocking(True)
                            connection.sendall(b"x" * (ipc.MAX_FRAME_BYTES + 1))
                            connection.setblocking(False)
                            self.assertIn("size limit", ipc.FrameReader(connection).read(end)["error"])

                        result = ai.query_model("a valid provider fixture")
                        self.assertEqual(result.message, "verified fixture")
                        stop, results = threading.Event(), []
                        client = threading.Thread(target=lambda: results.append(
                            ai.query_model("stall fixture", cancel_token=stop)), daemon=True)
                        client.start()
                        self.assertTrue(entered.wait(1))
                        stop.set()
                        client.join(1)
                        self.assertFalse(client.is_alive())
                        self.assertIn("cancelled", results[0].validation_error.lower())
                        self.assertTrue(disconnected.wait(1))
                    finally:
                        service.stop()
                        serving.join(2)
                self.assertFalse(serving.is_alive())
                self.assertFalse(Path(descriptor).exists())

    def test_descriptor_rejects_public_permissions_and_non_loopback(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as directory:
            path = Path(directory) / "daemon.json"
            path.write_text(json.dumps({"host": "192.0.2.1", "port": 12345, "token": "a" * 64, "pid": 1}))
            path.chmod(0o644)
            with self.assertRaisesRegex(ipc.FrameError, "private"):
                ipc.read_tcp_descriptor(str(path))
            path.chmod(0o600)
            with self.assertRaisesRegex(ipc.FrameError, "Invalid"):
                ipc.read_tcp_descriptor(str(path))


class TestSprint4Runtime(IsolatedTestCase):
    def test_headless_imports_with_posix_and_gtk_modules_blocked(self):
        source = str(package_deb.SOURCE_DIR.parent)
        script = '''
import sys
sys.path.insert(0, sys.argv[1])
blocked = {"gi", "fcntl", "pwd", "grp", "termios", "resource"}
for name in blocked:
    sys.modules.pop(name, None)
class Block:
    def find_spec(self, fullname, *_):
        if fullname.split(".")[0] in blocked:
            raise ModuleNotFoundError(fullname, name=fullname)
sys.meta_path.insert(0, Block())
from mavrick import cli, core, daemon, history, router, providers
from mavrick.platform.windows import WindowsAdapter
assert not blocked.intersection(sys.modules)
assert router.calculate("245 * 18.5") == 4532.5
print("headless imports and calculator verified")
'''
        command = [sys.executable, "-I", "-c", script, source]

        def fixture_only(argv, *args, **kwargs):
            self.assertEqual(argv, command)
            return _real_popen(argv, *args, **kwargs)

        with patch.object(subprocess, "Popen", side_effect=fixture_only):
            result = subprocess.run(command, cwd=TEST_ROOT, text=True, capture_output=True, timeout=5, env=os.environ)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("calculator verified", result.stdout)

    def test_socket_relay_transports_worker_ndjson(self):
        script = "import sys; line = sys.stdin.buffer.readline(); sys.stdout.buffer.write(line); sys.stdout.flush()"
        command = [sys.executable, "-I", "-u", "-c", script]
        with patch.object(socket, "socket", _REAL_SOCKET), _real_popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0
        ) as worker:
            relay = WorkerPipeRelay(worker.stdin, worker.stdout)
            try:
                end = time.monotonic() + 2
                ipc.write_frame(relay.input, {"prompt": "Grüße 🌙"}, end)
                self.assertEqual(ipc.FrameReader(relay.output).read(end)["prompt"], "Grüße 🌙")
                self.assertEqual(worker.wait(timeout=2), 0)
            finally:
                if worker.poll() is None:
                    worker.kill()
                relay.close()
            self.assertTrue(all(not thread.is_alive() for thread in relay._threads))

    def test_platform_runtime_modules_are_packaged_byte_for_byte(self):
        source = package_deb.SOURCE_DIR
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as folder:
            stage = Path(folder)
            package_deb.stage_payload_files(stage)
            actual = stage / "usr" / "lib" / "mavrick"
            modules = {path.relative_to(source) for path in (source / "platform").rglob("*.py")}
            self.assertTrue({Path("platform/windows.py"), Path("platform/macos.py")} <= modules)
            self.assertTrue(modules <= set(package_deb.runtime_manifest()))
            self.assertEqual(
                {p: (actual / p).read_bytes() for p in modules},
                {p: (source / p).read_bytes() for p in modules},
            )
