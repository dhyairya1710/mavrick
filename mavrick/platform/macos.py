"""macOS app bundles, LaunchAgents, Homebrew, and POSIX primitives."""

import ctypes
import errno
import os
import plistlib
import re
import shutil
from pathlib import Path

from .base import CacheTarget, Capabilities, HostPlatform, package_id


class MacOSAdapter(HostPlatform):
    name = "macos"
    capabilities = Capabilities(atomic_moves=True)

    def data_dir(self):
        return self.home / "Library" / "Application Support" / "Mavrick"

    def config_dir(self):
        return self.data_dir()

    def cache_dir(self):
        return self.home / "Library" / "Caches" / "Mavrick"

    def log_dir(self):
        return self.home / "Library" / "Logs" / "Mavrick"

    def runtime_dir(self):
        # Darwin sockaddr_un has a short path limit. Keep the default endpoint short.
        return Path(self.environ.get("XDG_RUNTIME_DIR") or f"/tmp/mavrick-{os.getuid()}")

    def user_path_roots(self):
        return (*super().user_path_roots(), "/private/tmp")

    def blocked_user_roots(self):
        return ("/System", "/usr", "/bin", "/sbin", "/etc", "/private/etc")

    @staticmethod
    def _app_key(value):
        return re.sub(r"[^a-z0-9_.-]+", "-", value.casefold()).strip("-")

    def discover_apps(self):
        matches = {}
        roots = (self.home / "Applications", Path("/Applications"), Path("/System/Applications"))
        for root in roots:
            for bundle in sorted({*root.glob("*.app"), *root.glob("*/*.app")}):
                try:
                    with (bundle / "Contents" / "Info.plist").open("rb") as handle:
                        info = plistlib.load(handle)
                    if not isinstance(info, dict) or not isinstance(info.get("CFBundleIdentifier"), str):
                        continue
                    names = (info["CFBundleIdentifier"], bundle.stem, info.get("CFBundleName"),
                             info.get("CFBundleDisplayName"))
                    for name in names:
                        if isinstance(name, str) and (key := self._app_key(name)):
                            matches.setdefault(key, str(bundle.absolute()))
                except (OSError, ValueError, plistlib.InvalidFileException):
                    continue
        return matches

    def app_command(self, app_id, installed, aliases):
        bundle = installed.get(app_id.casefold())
        if not bundle or not shutil.which("open"):
            return None
        return ["open", "-a", bundle]

    def package_manager_name(self):
        return "brew" if shutil.which("brew") else "unavailable"

    def package_command(self, operation, name=None, *, manager="brew", **_options):
        if manager not in ("brew", None, "auto"):
            raise self.unsupported("Requested package manager")
        if operation == "updates":
            return ["brew", "outdated", "--json=v2"]
        if operation == "list":
            return ["brew", "list", "--versions", *([package_id(name)] if name is not None else [])]
        operations = {"search": "search", "install": "install", "remove": "uninstall", "update": "upgrade"}
        if operation not in operations:
            raise self.unsupported("Homebrew operation")
        return ["brew", operations[operation], package_id(name)]

    def get_system_info(self):
        result = super().get_system_info()
        try:
            response = self.run_utility(["sysctl", "-n", "hw.memsize"])
            if response.returncode == 0:
                result["ram_total_bytes"] = int(response.stdout.strip())
                result["report"] += f"\nTotal RAM: {result['ram_total_bytes'] / 1024**3:.1f} GiB"
        except (OSError, ValueError):
            pass
        return result

    def get_cache_targets(self):
        return (CacheTarget("Mavrick Cache", self.cache_dir()),)

    def service_status(self, name):
        result = self.run_utility(["launchctl", "print", f"gui/{os.getuid()}/{package_id(name)}"])
        if result.returncode:
            return "unavailable"
        for line in result.stdout.splitlines():
            if line.strip().startswith("state = "):
                return line.strip().removeprefix("state = ")
        return "registered"

    def open_path(self, path):
        if self.run_utility(["open", os.path.abspath(path)]).returncode:
            raise OSError("macOS could not open the path.")

    def open_web(self, url):
        return self.launch_command(["open", url] if shutil.which("open") else None, "default browser")

    def copy_to_clipboard(self, text):
        if self.run_utility(["pbcopy"], text=text).returncode:
            raise OSError("macOS clipboard update failed.")

    def normalize_file_path(self, raw):
        path = super().normalize_file_path(raw)
        return "/private" + path if path == "/tmp" or path.startswith("/tmp/") else path

    def pin_entry(self, parent, name):
        return os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)

    def rename_no_replace(self, source_fd, source, dest_fd, destination):
        rename = getattr(ctypes.CDLL(None, use_errno=True), "renameatx_np", None)
        if rename is None:
            raise OSError(errno.ENOSYS, "Atomic no-replace rename is unavailable.")
        # Darwin's RENAME_EXCL makes this operation atomic and no-clobber.
        if rename(source_fd, os.fsencode(source), dest_fd, os.fsencode(destination), 4):
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code), destination)
