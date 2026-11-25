"""Keep test state and external commands isolated even under direct test discovery."""

import atexit
import os
import shutil
import time
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


_TEST_DIRECTORY = tempfile.TemporaryDirectory(prefix="mavrick-tests-")
TEST_ROOT = Path(_TEST_DIRECTORY.name)
for key, folder in {
    "HOME": "home", "XDG_RUNTIME_DIR": "runtime", "XDG_CONFIG_HOME": "config",
    "XDG_DATA_HOME": "home/.local/share", "XDG_DATA_DIRS": "system-data",
    "XDG_CACHE_HOME": "cache", "XDG_STATE_HOME": "state",
    "USERPROFILE": "home", "APPDATA": "config", "LOCALAPPDATA": "state", "ProgramData": "system-data",
}.items():
    path = TEST_ROOT / folder
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.environ[key] = str(path)

for key in ("DISPLAY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "XAUTHORITY"):
    os.environ.pop(key, None)

if os.environ.get("MAVRICK_TEST_GTK") == "broadway" and shutil.which("broadwayd"):
    os.environ.update(GDK_BACKEND="broadway", BROADWAY_DISPLAY=":97", NO_AT_BRIDGE="1")
    path = TEST_ROOT / "runtime" / "browser.sock"
    server = subprocess.Popen(["broadwayd", f"--unixsocket={path}", ":97"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def stop_broadway():
        server.terminate()
        server.wait(timeout=5)

    atexit.register(stop_broadway)
    for _ in range(40):
        if path.exists() or server.poll() is not None:
            break
        time.sleep(0.05)

_real_popen = subprocess.Popen


def _guarded_popen(command, *args, **kwargs):
    if isinstance(command, (tuple, list)) and command[0] in ("ps", "sleep"):
        return _real_popen(command, *args, **kwargs)
    raise AssertionError(f"Mock this external launch in the test: {command[0]!r}")


class IsolatedTestCase(unittest.TestCase):
    def run(self, result=None):
        from mavrick.config import MAVRICK_STATE_DIR, MAVRICK_SOCK, DAEMON_SOCKET, FALLBACK_DAEMON_SOCKET

        paths = (MAVRICK_STATE_DIR, MAVRICK_SOCK, DAEMON_SOCKET, FALLBACK_DAEMON_SOCKET)
        if not all(Path(path).is_relative_to(TEST_ROOT) for path in paths):
            raise RuntimeError("Mavrick was imported before test state was isolated.")
        with patch("subprocess.Popen", side_effect=_guarded_popen), \
             patch("mavrick.ai.socket.socket", side_effect=AssertionError("Mock model IPC in the test")):
            return super().run(result)
