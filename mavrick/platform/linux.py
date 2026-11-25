"""Linux/XDG integration, retaining the Debian Mavrick defaults."""

import ctypes
import errno
import os
import re
import shutil
from pathlib import Path

from .base import CacheTarget, Capabilities, HostPlatform, package_id

TERMINALS = ("cosmic-term", "gnome-terminal", "alacritty", "kitty", "x-terminal-emulator")
DESKTOP_ALIASES = {
    "spotify": "com.spotify.Client.desktop", "discord": "com.discordapp.Discord.desktop",
    "vlc": "org.videolan.VLC.desktop", "obs": "com.obsproject.Studio.desktop",
    "firefox": "org.mozilla.firefox.desktop", "brave": "com.brave.Browser.desktop",
    "chromium": "org.chromium.Chromium.desktop",
    "code": "com.visualstudio.code.desktop",
    "nautilus": "org.gnome.Nautilus.desktop",
    "dolphin": "org.kde.dolphin.desktop",
}

APT_ARCHIVES = "/var/cache/apt/archives"
APT_NAME = re.compile(r"[a-z0-9](?:[a-z0-9+.-]*[a-z0-9.])?")
FLATPAK_ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]*(?:\.[A-Za-z][A-Za-z0-9_-]*){2,}")


class LinuxAdapter(HostPlatform):
    name = "linux"
    capabilities = Capabilities(atomic_moves=True, cache_cleanup=True)

    def config_dir(self):
        # Preserve the existing on-disk location for Linux installations.
        return self.data_dir()

    def data_dir(self):
        return self.home / ".local" / "share" / "mavrick"

    def cache_dir(self):
        return Path(self.environ.get("XDG_CACHE_HOME") or self.home / ".cache") / "mavrick"

    def runtime_dir(self):
        return Path(self.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")

    def user_path_roots(self):
        return str(self.home.resolve()), "/tmp"

    def blocked_user_roots(self):
        return ("/etc", "/bin", "/sbin", "/usr", "/boot", "/dev", "/proc", "/sys",
                "/root", "/var", "/lib", "/lib64", "/opt")

    def is_wayland_session(self):
        return bool(self.environ.get("WAYLAND_DISPLAY") or self.environ.get("XDG_SESSION_TYPE") == "wayland")

    def is_cosmic_desktop(self):
        return "COSMIC" in self.environ.get("XDG_CURRENT_DESKTOP", "").upper() or Path("/usr/bin/cosmic-session").exists()

    def downloads_dir(self):
        location = Path(self.environ.get("XDG_CONFIG_HOME") or self.home / ".config") / "user-dirs.dirs"
        try:
            for line in location.read_text(encoding="utf-8").splitlines():
                match = re.fullmatch(r'XDG_DOWNLOAD_DIR="([^"\\]*)"', line.strip())
                if match:
                    path = match[1]
                    if path.startswith("$HOME/"):
                        return str(self.home) + path[5:]
                    if os.path.isabs(path):
                        return path
        except (OSError, UnicodeError):
            pass
        return "~/Downloads"

    def discover_apps(self):
        user = Path(self.environ.get("XDG_DATA_HOME", self.home / ".local/share"))
        system = self.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share")
        roots = [user, user / "flatpak/exports/share", *(Path(p) for p in system.split(":") if p),
                 Path("/var/lib/flatpak/exports/share")]
        ids = {}
        for root in roots:
            for entry in (root / "applications").glob("*.desktop"):
                if entry.is_file() and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", entry.stem):
                    ids.setdefault(entry.stem.lower(), entry.name)
        return ids

    def application_index(self, installed, approved):
        launcher = shutil.which("gtk-launch")
        matches = dict(installed) if launcher else {}
        for app_id in approved:
            alias = DESKTOP_ALIASES.get(app_id, "").removesuffix(".desktop").lower()
            if shutil.which(app_id) or (launcher and (app_id in installed or alias in installed)):
                matches[app_id] = app_id
        return matches

    def workspace_terminal_command(self, app, project):
        terminal = shutil.which(app)
        if not terminal:
            return None
        args = []
        if app in ("cosmic-term", "gnome-terminal", "alacritty"):
            args = ["--working-directory", project]
        elif app == "kitty":
            args = ["--directory", project]
        return [terminal, *args]

    def terminal_path(self):
        return next((path for name in TERMINALS if (path := shutil.which(name))), None)

    def terminal_command(self, terminal, arguments):
        return [terminal, "--" if os.path.basename(terminal) == "gnome-terminal" else "-e", *arguments]

    def app_command(self, app_id, installed, aliases):
        desktop_id = installed.get(app_id.removesuffix(".desktop").lower())
        alias = aliases.get(app_id)
        launcher = shutil.which("gtk-launch")
        if desktop_id and launcher:
            return [launcher, desktop_id]
        if app_id.endswith(".desktop"):
            return None
        executable = shutil.which(app_id)
        if executable:
            return [executable]
        alias_key = alias.removesuffix(".desktop").lower() if alias else ""
        if launcher and alias_key in installed:
            return [launcher, installed[alias_key]]
        return None

    def launch_app(self, app_id, *, cwd=None):
        command = self.app_command(app_id, self.discover_apps(), DESKTOP_ALIASES)
        label = "Claude Desktop" if app_id == "com.anthropic.Claude.desktop" else app_id
        return self.launch_command(command, label, cwd)

    def package_manager_name(self):
        return "apt" if shutil.which("apt-get") else "flatpak" if shutil.which("flatpak") else "unavailable"

    def package_command(self, operation, name=None, *, manager="apt", scope=None, version=None):
        if manager == "flatpak":
            if operation == "updates":
                return ["flatpak", "remote-ls", "--updates", "--columns=application,branch"]
            target = package_id(name)
            if scope not in ("user", "system") or not FLATPAK_ID.fullmatch(target):
                raise ValueError("Give the exact Flatpak ID and user or system scope.")
            subcommand = {"list": "info", "search": "search", "install": "install",
                          "remove": "uninstall", "update": "update"}.get(operation)
            if not subcommand:
                raise self.unsupported("Flatpak operation")
            return ["flatpak", subcommand, f"--{scope}", "--",
                    *(["flathub"] if operation == "install" else []), target]
        if manager != "apt":
            raise self.unsupported("Requested package manager")
        if operation == "updates":
            return ["apt", "list", "--upgradable"]
        if operation == "list" and name is None:
            return ["apt", "list", "--installed"]
        target = package_id(name)
        if not APT_NAME.fullmatch(target):
            raise ValueError("Give a valid APT package name.")
        if operation == "search":
            return ["apt-cache", "policy", "--", target]
        if operation == "list":
            return ["dpkg-query", "-W", "-f=${db:Status-Status}", "--", target]
        if operation not in ("install", "remove", "update"):
            raise self.unsupported("APT operation")
        if version is not None and not re.fullmatch(r"[A-Za-z0-9.+:~_-]+", version):
            raise ValueError("Invalid APT candidate version.")
        return ["sudo", "apt-get", "install" if operation == "update" else operation, "--",
                f"{target}={version}" if version and operation != "remove" else target]

    def get_system_info(self):
        from .linux_system import get_system_health
        return get_system_health()

    def get_cache_targets(self):
        return (CacheTarget("Thumbnails Cache", self.home / ".cache" / "thumbnails"),
                CacheTarget("APT Package Cache", Path(APT_ARCHIVES), archives=True))

    def service_status(self, name):
        result = self.run_utility(["systemctl", "--user", "is-active", "--", package_id(name)])
        return result.stdout.strip() or "unavailable"

    def open_path(self, path):
        result = self.run_utility(["xdg-open", os.path.abspath(path)])
        if result.returncode:
            raise OSError("The desktop could not open the path.")

    def copy_to_clipboard(self, text):
        command = ["wl-copy"] if self.is_wayland_session() else ["xclip", "-selection", "clipboard"]
        if self.run_utility(command, text=text).returncode:
            raise OSError("The desktop could not copy to the clipboard.")

    def pin_entry(self, parent, name):
        return os.open(name, os.O_PATH | os.O_NOFOLLOW, dir_fd=parent)

    def rename_no_replace(self, source_fd, source, dest_fd, destination):
        rename = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
        if rename is None:
            raise OSError(errno.ENOSYS, "Atomic no-replace rename is unavailable")
        if rename(source_fd, os.fsencode(source), dest_fd, os.fsencode(destination), 1):
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code), destination)
