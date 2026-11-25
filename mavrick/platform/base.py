"""Operating-system contracts for the headless Mavrick runtime."""

from __future__ import annotations

import errno
import os
import shutil
import stat
import subprocess
import tempfile
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
import re


class UnsupportedPlatformAction(ValueError):
    def __init__(self, platform: str, action: str):
        super().__init__(f"{action} is unavailable on {platform}.")
        self.platform = platform
        self.action = action


class FileLockBusy(BlockingIOError):
    """A different Mavrick process owns this lock."""


@dataclass(frozen=True)
class Capabilities:
    ipc_transport: str = "unix-socket"
    atomic_moves: bool = False
    cache_cleanup: bool = False
    agy_socket_relay: bool = False


@dataclass(frozen=True)
class CacheTarget:
    label: str
    path: Path
    archives: bool = False


class PlatformAdapter(Protocol):
    name: str
    capabilities: Capabilities

    def config_dir(self) -> Path: ...
    def data_dir(self) -> Path: ...
    def cache_dir(self) -> Path: ...
    def log_dir(self) -> Path: ...
    def runtime_dir(self) -> Path: ...
    def discover_apps(self) -> dict[str, str]: ...
    def launch_app(self, app_id: str, *, cwd: str | None = None) -> tuple[bool, str, dict]: ...
    def package_manager_name(self) -> str: ...
    def package_command(self, operation: str, name: str | None = None, **options) -> list[str]: ...
    def get_system_info(self) -> dict: ...
    def get_cache_targets(self) -> tuple[CacheTarget, ...]: ...
    def service_status(self, name: str) -> str: ...
    def open_path(self, path: str) -> None: ...
    def copy_to_clipboard(self, text: str) -> None: ...
    def lock_file(self, path: str | Path, *, blocking: bool = True) -> AbstractContextManager: ...
    def restrict_permissions(self, path: str | Path, *, directory: bool = False) -> None: ...


_PACKAGE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*")


def package_id(name: str | None) -> str:
    if not isinstance(name, str) or not _PACKAGE_ID.fullmatch(name):
        raise ValueError("Give an exact package ID without options or arguments.")
    return name


class HostPlatform:
    """Shared POSIX behavior and defaults overridden by native adapters."""

    name = "unsupported"
    capabilities = Capabilities()

    def __init__(self, home=None, environ=None):
        self._home = Path(home) if home is not None else None
        self._environ = environ

    @property
    def environ(self):
        return os.environ if self._environ is None else self._environ

    @property
    def home(self) -> Path:
        return self._home if self._home is not None else Path.home()

    def unsupported(self, action: str) -> UnsupportedPlatformAction:
        return UnsupportedPlatformAction(self.name, action)

    def temp_dir(self) -> Path:
        return Path(tempfile.gettempdir()).resolve()

    def log_dir(self) -> Path:
        return self.data_dir()

    def user_path_roots(self) -> tuple[str, ...]:
        return str(self.home.resolve()), str(self.temp_dir())

    def blocked_user_roots(self) -> tuple[str, ...]:
        return ()

    def downloads_dir(self) -> str:
        return str(self.home / "Downloads")

    def agy_binary(self) -> Path:
        return self.home / ".local" / "bin" / "agy"

    def is_wayland_session(self) -> bool:
        return False

    def is_cosmic_desktop(self) -> bool:
        return False

    def terminal_path(self) -> str | None:
        return None

    def terminal_command(self, terminal: str, arguments: list[str]) -> list[str]:
        raise self.unsupported("Launching an interactive terminal utility")

    def application_index(self, installed, approved):
        return {app_id: app_id for app_id in installed}

    def workspace_terminal_command(self, app, project):
        return None

    def normalize_file_path(self, raw):
        return os.path.abspath(os.path.expanduser(raw))

    def app_command(self, app_id: str, installed: dict[str, str], aliases: dict[str, str]) -> list[str] | None:
        return None

    def detached_options(self) -> dict:
        return {"start_new_session": True}

    def launch_command(self, command, label, cwd=None):
        # Imported lazily because the tools import this adapter at startup.
        from ..tools.apps import launch_command
        return launch_command(command, label, cwd)

    def launch_app(self, app_id: str, *, cwd: str | None = None):
        command = self.app_command(app_id, self.discover_apps(), {})
        return self.launch_command(command, app_id, cwd)

    def get_system_info(self) -> dict:
        disk = shutil.disk_usage(self.home)
        cores = os.cpu_count() or 1
        gib = 1024 ** 3
        return {
            "platform": self.name, "cpu_logical_count": cores, "disk_free_bytes": disk.free,
            "report": f"Platform: {self.name}\nLogical CPUs: {cores}\nHome volume free: {disk.free / gib:.1f} GiB",
        }

    def run_utility(self, command: list[str], *, text: str | None = None, timeout: float = 5):
        if not shutil.which(command[0]):
            raise self.unsupported(command[0])
        try:
            return subprocess.run(command, input=text, text=True, capture_output=True, timeout=timeout)
        except FileNotFoundError as error:
            raise self.unsupported(command[0]) from error

    def service_status(self, name: str) -> str:
        raise self.unsupported("Service status")

    def restrict_permissions(self, path: str | Path, *, directory: bool = False) -> None:
        info = os.lstat(path)
        mode = 0o700 if directory else 0o600
        if stat.S_ISLNK(info.st_mode) or (directory and not stat.S_ISDIR(info.st_mode)):
            raise OSError(f"Refusing to chmod a linked or invalid state path: {path}")
        if stat.S_IMODE(info.st_mode) != mode:
            os.chmod(path, mode)

    def is_private(self, info, *, directory=False) -> bool:
        expected = stat.S_ISDIR if directory else stat.S_ISREG
        return expected(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077

    def verify_private_path(self, path: str | Path, *, directory: bool = False) -> bool:
        return self.is_private(os.lstat(path), directory=directory)

    def verify_private_descriptor(self, fd: int, *, directory: bool = False) -> bool:
        return self.is_private(os.fstat(fd), directory=directory)

    @contextmanager
    def lock_file(self, path: str | Path, *, blocking: bool = True):
        try:
            import fcntl
        except ImportError as error:
            raise self.unsupported("File locking") from error
        fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
            try:
                fcntl.flock(fd, flags)
            except OSError as error:
                if not blocking and error.errno in (errno.EACCES, errno.EAGAIN):
                    raise FileLockBusy(str(path)) from error
                raise
            yield
        finally:
            os.close(fd)

    @contextmanager
    def open_parent(self, path, create=False, created=None):
        """Pin every parent without following links."""
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        fd = os.open("/", flags)
        try:
            current = ""
            for part in os.path.dirname(path).split("/")[1:]:
                if not part:
                    continue
                current += "/" + part
                try:
                    next_fd = os.open(part, flags, dir_fd=fd)
                except FileNotFoundError:
                    if not create:
                        raise
                    os.mkdir(part, dir_fd=fd)
                    next_fd = os.open(part, flags, dir_fd=fd)
                    if created is not None:
                        info = os.fstat(next_fd)
                        created[current] = info.st_dev, info.st_ino, info.st_mode
                os.close(fd)
                fd = next_fd
            yield fd
        finally:
            os.close(fd)

    def sync_directory(self, path: str | Path) -> None:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def sync_directory_fd(self, fd: int) -> None:
        os.fsync(fd)

    def terminate_group(self, pgid: int) -> None:
        import signal
        import time

        if pgid <= 1 or pgid in (os.getpgrp(), os.getpid()):
            return
        try:
            os.killpg(pgid, signal.SIGTERM)
        except OSError:
            return
        time.sleep(0.1)
        try:
            os.killpg(pgid, 0)
            os.killpg(pgid, signal.SIGKILL)
        except OSError:
            pass

    def process_group(self, pid: int) -> int:
        try:
            return os.getpgid(pid)
        except OSError:
            return pid
