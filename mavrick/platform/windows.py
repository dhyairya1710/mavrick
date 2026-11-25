"""Native Windows profile, launcher, package, and desktop integration."""

import errno
import json
import os
import re
import shutil
import stat
import struct
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

from .base import CacheTarget, Capabilities, FileLockBusy, HostPlatform, package_id

_SHLLINK_HEADER_SIZE = 0x4C
_SHLLINK_CLSID = b"\x01\x14\x02\x00\x00\x00\x00\x00\xc0\x00\x00\x00\x00\x00\x00\x46"
_UNSAFE_SHORTCUT_SUFFIXES = frozenset({
    ".bat", ".cmd", ".ps1", ".psm1", ".psd1", ".vbs", ".vbe", ".js", ".jse",
    ".wsf", ".wsh", ".hta", ".msc", ".com", ".scr", ".msi", ".msp", ".reg", ".inf", ".pif", ".cpl",
})
_UNSAFE_SHORTCUT_NAMES = frozenset({
    "cmd.exe", "powershell.exe", "pwsh.exe", "wscript.exe", "cscript.exe",
    "mshta.exe", "rundll32.exe", "regsvr32.exe", "bash.exe", "wsl.exe",
})


def _read_null_terminated(data: bytes, offset: int, *, unicode: bool = False) -> str:
    if offset <= 0 or offset >= len(data):
        return ""
    if unicode:
        end = offset
        limit = min(len(data) - 1, offset + 4096)
        while end < limit:
            if data[end] == 0 and data[end + 1] == 0:
                break
            end += 2
        return data[offset:end].decode("utf-16-le", errors="replace")
    end = data.find(b"\x00", offset, min(len(data), offset + 4096))
    if end == -1:
        end = min(len(data), offset + 4096)
    return data[offset:end].decode("utf-8", errors="replace")


def parse_windows_shortcut(path: Path) -> dict[str, str] | None:
    """Safely inspect a Windows .lnk Shell Link without executing COM or shells."""
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode) or (
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    ):
        return None
    flags_open = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags_open)
    except OSError:
        return None
    try:
        finfo = os.fstat(fd)
        if not stat.S_ISREG(finfo.st_mode) or finfo.st_size < _SHLLINK_HEADER_SIZE or finfo.st_size > 256 * 1024:
            return None
        with os.fdopen(fd, "rb") as handle:
            data = handle.read(65536 + 1)
    except OSError:
        return None
    if len(data) < _SHLLINK_HEADER_SIZE or len(data) > 65536:
        return None
    if struct.unpack_from("<I", data, 0)[0] != _SHLLINK_HEADER_SIZE or data[4:20] != _SHLLINK_CLSID:
        return None
    link_flags = struct.unpack_from("<I", data, 20)[0]
    pos = _SHLLINK_HEADER_SIZE
    if link_flags & 0x01:  # HasLinkTargetIDList
        if pos + 2 > len(data):
            return None
        id_list_size = struct.unpack_from("<H", data, pos)[0]
        pos += 2 + id_list_size
    target = ""
    if (link_flags & 0x02) and pos + 28 <= len(data):  # HasLinkInfo
        (
            link_info_size,
            link_info_hdr_size,
            link_info_flags,
            _vol_off,
            local_off,
            _net_off,
            suffix_off,
        ) = struct.unpack_from("<7I", data, pos)
        if link_info_size < 28 or pos + link_info_size > len(data):
            return None
        if link_info_hdr_size >= 0x24 and pos + 36 <= len(data):
            local_u_off, suffix_u_off = struct.unpack_from("<2I", data, pos + 28)
        else:
            local_u_off, suffix_u_off = 0, 0
        if link_info_flags & 0x01:
            local_path = (
                _read_null_terminated(data, pos + local_u_off, unicode=True)
                if local_u_off else _read_null_terminated(data, pos + local_off, unicode=False)
                if local_off else ""
            )
            suffix = (
                _read_null_terminated(data, pos + suffix_u_off, unicode=True)
                if suffix_u_off else _read_null_terminated(data, pos + suffix_off, unicode=False)
                if suffix_off else ""
            )
            target = f"{local_path}{suffix}".strip()
        pos += link_info_size
    is_unicode = bool(link_flags & 0x80)
    string_fields = {}
    for flag, field_name in ((0x04, "name"), (0x08, "relative_path"), (0x10, "working_dir"), (0x20, "arguments")):
        if not (link_flags & flag):
            continue
        if pos + 2 > len(data):
            return None
        count = struct.unpack_from("<H", data, pos)[0]
        pos += 2
        byte_len = count * 2 if is_unicode else count
        if pos + byte_len > len(data):
            return None
        text = data[pos:pos + byte_len].decode("utf-16-le" if is_unicode else "utf-8", errors="replace")
        string_fields[field_name] = text
        pos += byte_len
    if not target and string_fields.get("relative_path"):
        target = string_fields["relative_path"].strip()
    if target:
        normalized = target.replace("\\", "/")
        suffix = Path(normalized).suffix.lower()
        name = Path(normalized).name.lower()
        if suffix in _UNSAFE_SHORTCUT_SUFFIXES or name in _UNSAFE_SHORTCUT_NAMES:
            return None
        if suffix and suffix != ".exe":
            return None
    return {"target": target}


def validate_windows_acl_principals(
    owner_sid: str | None,
    current_user_sid: str | None,
    dacl_protected: bool,
    aces: list[tuple[int, str]] | tuple[tuple[int, str], ...] | None,
    *,
    system_sid: str = "S-1-5-18",
) -> bool:
    """Ensure Windows owner matches current user and protected DACL allows only owner/SYSTEM."""
    if not owner_sid or not current_user_sid or owner_sid != current_user_sid:
        return False
    if not dacl_protected or aces is None:
        return False
    allowed = {current_user_sid, system_sid}
    for ace_type, sid in aces:
        if ace_type != 0 or sid not in allowed:
            return False
    return True


class WindowsAdapter(HostPlatform):
    name = "windows"
    capabilities = Capabilities(ipc_transport="tcp-token", agy_socket_relay=True)

    def _roaming(self):
        return Path(self.environ.get("APPDATA") or self.home / "AppData" / "Roaming")

    def _local(self):
        return Path(self.environ.get("LOCALAPPDATA") or self.home / "AppData" / "Local")

    def config_dir(self):
        return self._roaming() / "Mavrick"

    def data_dir(self):
        return self._local() / "Mavrick"

    def cache_dir(self):
        return self.data_dir() / "Cache"

    def runtime_dir(self):
        return self.data_dir() / "Run"

    def agy_binary(self):
        return self.home / ".local" / "bin" / "agy.exe"

    @staticmethod
    def _app_key(value):
        return re.sub(r"[^a-z0-9_.-]+", "-", value.casefold()).strip("-")

    def _approved_apps(self) -> dict[str, str]:
        from .. import config
        from ..platform import get_platform

        raw_sources: list[object] = []
        local_cfg = self.config_dir() / "config.json"
        try:
            info = os.lstat(local_cfg)
            if self.is_private(info):
                raw_bytes = local_cfg.read_bytes()
                if len(raw_bytes) <= 256 * 1024:
                    parsed = json.loads(raw_bytes.decode("utf-8"))
                    if isinstance(parsed, dict):
                        raw_sources.append(parsed.get("approved_apps"))
        except (OSError, UnicodeError, ValueError, RecursionError):
            pass
        if get_platform() is self:
            raw_sources.append(config.load_user_config().get("approved_apps"))

        approved: dict[str, str] = {}
        for source in raw_sources:
            if not isinstance(source, dict):
                continue
            for raw_name, raw_path in source.items():
                if not isinstance(raw_name, str) or not isinstance(raw_path, str):
                    continue
                key = self._app_key(raw_name)
                if not key or not os.path.isabs(raw_path):
                    continue
                target = Path(raw_path)
                if target.suffix.lower() != ".exe" or target.name.lower() in _UNSAFE_SHORTCUT_NAMES:
                    continue
                try:
                    info = os.lstat(target)
                except OSError:
                    continue
                if self.is_private(info, directory=False):
                    approved.setdefault(key, str(target.absolute()))
        return approved

    def discover_apps(self):
        suffix = Path("Microsoft/Windows/Start Menu/Programs")
        common = Path(self.environ.get("ProgramData") or r"C:\ProgramData")
        matches = {}
        for root in (self._roaming() / suffix, common / suffix):
            try:
                root_info = os.lstat(root)
            except OSError:
                continue
            if not self.is_private(root_info, directory=True):
                continue
            for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
                current = Path(dirpath)
                safe_dirs = []
                for name in sorted(dirnames):
                    sub = current / name
                    try:
                        sub_info = os.lstat(sub)
                    except OSError:
                        continue
                    if self.is_private(sub_info, directory=True):
                        safe_dirs.append(name)
                dirnames[:] = safe_dirs
                for filename in sorted(filenames):
                    entry = current / filename
                    if entry.suffix.lower() != ".lnk":
                        continue
                    if parse_windows_shortcut(entry) is None:
                        continue
                    key = self._app_key(entry.stem)
                    if key:
                        matches.setdefault(key, str(entry.absolute()))
        for key, path in self._approved_apps().items():
            matches.setdefault(key, path)
        return matches

    def _target(self, app_id, installed):
        return installed.get(app_id.casefold().removesuffix(".exe"))

    def app_command(self, app_id, installed, aliases):
        target = self._target(app_id, installed)
        return [target] if target and Path(target).suffix.lower() == ".exe" else None

    @staticmethod
    def _path_identity(info) -> tuple[int, int, int, int]:
        return (
            info.st_dev,
            info.st_ino,
            info.st_size,
            getattr(info, "st_mtime_ns", int(info.st_mtime * 1_000_000_000)),
        )

    def resolve_pinned_target(self, app_id: str) -> dict | None:
        target = self._target(app_id, self.discover_apps())
        if not target:
            return None
        path = Path(target)
        try:
            info = os.lstat(path)
        except OSError:
            return None
        if not self.is_private(info, directory=False):
            return None
        pinned = {
            "app": app_id,
            "adapter": self.name,
            "resolved": True,
            "target_path": target,
            "target_identity": list(self._path_identity(info)),
        }
        if path.suffix.lower() == ".lnk":
            parsed = parse_windows_shortcut(path)
            if parsed is None:
                return None
            pinned["shortcut_target"] = parsed["target"]
        return pinned

    def launch_app(self, app_id, *, cwd=None, pinned_target=None):
        if pinned_target is not None:
            if not isinstance(pinned_target, dict) or not pinned_target.get("resolved"):
                return False, f"No installed launcher for {app_id}.", {"verification": "failed"}
            target = pinned_target.get("target_path", "")
            path = Path(target)
            try:
                info = os.lstat(path)
            except OSError:
                return False, f"Pinned launcher for {app_id} vanished since review.", {"verification": "failed"}
            if not self.is_private(info, directory=False) or list(self._path_identity(info)) != pinned_target.get("target_identity"):
                return False, f"Pinned launcher for {app_id} changed since review.", {"verification": "failed"}
            if path.suffix.lower() == ".lnk":
                parsed = parse_windows_shortcut(path)
                if parsed is None or parsed.get("target", "") != pinned_target.get("shortcut_target", ""):
                    return False, f"Pinned shortcut target for {app_id} changed since review.", {"verification": "failed"}
            elif path.suffix.lower() != ".exe" or path.name.lower() in _UNSAFE_SHORTCUT_NAMES:
                return False, f"Pinned launcher for {app_id} is not permitted.", {"verification": "failed"}
        else:
            target = self._target(app_id, self.discover_apps())
            if not target:
                return False, f"No installed launcher for {app_id}.", {"verification": "failed"}
        if Path(target).suffix.lower() != ".lnk":
            return self.launch_command([target], app_id, cwd)
        self.open_path(target)
        return True, f"Launch requested for {app_id}.", {"verification": "launch_requested"}

    def detached_options(self):
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200) |
                getattr(subprocess, "DETACHED_PROCESS", 0x8)}

    def package_manager_name(self):
        if shutil.which("winget"):
            return "winget"
        if self._scoop():
            return "scoop"
        return "unavailable"

    def _scoop(self):
        if path := shutil.which("scoop.exe"):
            return [path]
        script = shutil.which("scoop.ps1") or str(self.home / "scoop" / "shims" / "scoop.ps1")
        powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
        return [powershell, "-NoProfile", "-File", script] if powershell and Path(script).is_file() else None

    def package_command(self, operation, name=None, *, manager=None, **_options):
        manager = self.package_manager_name() if manager in (None, "auto") else manager
        target = package_id(name) if name is not None else None
        if operation not in ("search", "list", "install", "remove", "update", "updates"):
            raise self.unsupported("Package operation")
        if operation not in ("list", "updates") and target is None:
            raise ValueError("Give the exact package ID.")
        if manager == "winget":
            command = {"remove": "uninstall", "update": "upgrade", "updates": "upgrade"}.get(operation, operation)
            return ["winget", command, *(["--id", target, "--exact"] if target else []), "--disable-interactivity"]
        if manager == "scoop" and (command := self._scoop()):
            subcommand = {"remove": "uninstall", "updates": "status"}.get(operation, operation)
            return [*command, subcommand, *([target] if target else [])]
        raise self.unsupported("Winget or Scoop package management")

    def get_system_info(self):
        result = super().get_system_info()
        if os.name == "nt":
            import ctypes

            class MemoryStatus(ctypes.Structure):
                _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
                    (name, ctypes.c_ulonglong) for name in
                    ("total", "available", "page", "page_free", "virtual", "virtual_free", "extended_free")
                ]

            status = MemoryStatus()
            status.length = ctypes.sizeof(status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                result.update(ram_total_bytes=status.total, ram_available_bytes=status.available)
                result["report"] += f"\nAvailable RAM: {status.available / 1024**3:.1f} GiB"
        return result

    def get_cache_targets(self):
        return (CacheTarget("Mavrick Cache", self.cache_dir()), CacheTarget("Mavrick Temp", self._local() / "Temp" / "Mavrick"))

    def service_status(self, name):
        target = package_id(name)
        if shutil.which("schtasks"):
            result = self.run_utility(["schtasks", "/Query", "/TN", target, "/FO", "CSV", "/NH"])
            if result.returncode == 0:
                return "scheduled task registered"
        result = self.run_utility(["sc.exe", "query", target])
        match = re.search(r"STATE\s*:\s*\d+\s+(\w+)", result.stdout)
        return match[1].lower() if match else "unavailable"

    def open_path(self, path):
        startfile = getattr(os, "startfile", None)
        if startfile is None:
            raise self.unsupported("Windows file associations")
        startfile(os.path.abspath(path))

    def open_web(self, url):
        startfile = getattr(os, "startfile", None)
        if startfile is None:
            raise self.unsupported("Windows browser association")
        startfile(url)
        return True, "Browser launch requested.", {"verification": "launch_requested"}

    def copy_to_clipboard(self, text):
        if executable := shutil.which("clip.exe") or shutil.which("clip"):
            result = subprocess.run([executable], input=text.encode("utf-16"), capture_output=True, timeout=5)
            if result.returncode == 0:
                return
        powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
        if not powershell:
            raise self.unsupported("Windows clipboard")
        script = "[Console]::InputEncoding=[Text.Encoding]::UTF8; Set-Clipboard -Value ([Console]::In.ReadToEnd())"
        result = subprocess.run([powershell, "-NoProfile", "-Command", script],
                                input=text.encode("utf-8"), capture_output=True, timeout=5)
        if result.returncode:
            raise OSError("Windows clipboard update failed.")

    def is_private(self, info, *, directory=False):
        expected = stat.S_ISDIR if directory else stat.S_ISREG
        return expected(info.st_mode) and not (
            getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        )

    def _read_windows_security(self, *, path=None, fd=None):
        """Return (owner_sid, current_user_sid, dacl_protected, aces) on Windows, or None off-Windows."""
        if os.name != "nt":
            return None
        import ctypes
        from ctypes import wintypes

        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)

        class SID_AND_ATTRIBUTES(ctypes.Structure):
            _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

        class TOKEN_USER_STRUCT(ctypes.Structure):
            _fields_ = [("User", SID_AND_ATTRIBUTES)]

        class ACL_SIZE_INFO(ctypes.Structure):
            _fields_ = [
                ("AceCount", wintypes.DWORD),
                ("AclBytesInUse", wintypes.DWORD),
                ("AclBytesFree", wintypes.DWORD),
            ]

        class ACE_HEADER_STRUCT(ctypes.Structure):
            _fields_ = [
                ("AceType", ctypes.c_ubyte),
                ("AceFlags", ctypes.c_ubyte),
                ("AceSize", wintypes.WORD),
            ]

        advapi.GetSecurityInfo.argtypes = (
            wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
        )
        advapi.GetSecurityInfo.restype = wintypes.DWORD
        advapi.GetNamedSecurityInfoW.argtypes = (
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
        )
        advapi.GetNamedSecurityInfoW.restype = wintypes.DWORD
        advapi.GetSecurityDescriptorControl.argtypes = (
            ctypes.c_void_p, ctypes.POINTER(wintypes.WORD), ctypes.POINTER(wintypes.DWORD),
        )
        advapi.GetSecurityDescriptorControl.restype = wintypes.BOOL
        advapi.GetAclInformation.argtypes = (
            ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
        )
        advapi.GetAclInformation.restype = wintypes.BOOL
        advapi.GetAce.argtypes = (ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p))
        advapi.GetAce.restype = wintypes.BOOL
        advapi.ConvertSidToStringSidW.argtypes = (ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR))
        advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
        advapi.OpenProcessToken.argtypes = (wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE))
        advapi.OpenProcessToken.restype = wintypes.BOOL
        advapi.GetTokenInformation.argtypes = (
            wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
        )
        advapi.GetTokenInformation.restype = wintypes.BOOL
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.LocalFree.argtypes = (ctypes.c_void_p,)
        kernel.LocalFree.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle.restype = wintypes.BOOL

        def sid_to_str(sid_ptr: ctypes.c_void_p) -> str | None:
            if not sid_ptr:
                return None
            out = wintypes.LPWSTR()
            if not advapi.ConvertSidToStringSidW(sid_ptr, ctypes.byref(out)):
                return None
            try:
                return str(out.value)
            finally:
                kernel.LocalFree(out)

        # Current process user SID
        h_token = wintypes.HANDLE()
        if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(h_token)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            needed = wintypes.DWORD(0)
            advapi.GetTokenInformation(h_token, 1, None, 0, ctypes.byref(needed))
            buf = ctypes.create_string_buffer(needed.value)
            if not advapi.GetTokenInformation(h_token, 1, buf, needed.value, ctypes.byref(needed)):
                raise ctypes.WinError(ctypes.get_last_error())
            tok_user = TOKEN_USER_STRUCT.from_buffer(buf)
            current_user_sid = sid_to_str(tok_user.User.Sid)
        finally:
            kernel.CloseHandle(h_token)

        p_owner = ctypes.c_void_p()
        p_dacl = ctypes.c_void_p()
        p_sd = ctypes.c_void_p()
        sec_info = 0x00000001 | 0x00000004  # OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION
        if fd is not None:
            import msvcrt

            handle = msvcrt.get_osfhandle(fd)
            rc = advapi.GetSecurityInfo(
                handle, 1, sec_info, ctypes.byref(p_owner), None, ctypes.byref(p_dacl), None, ctypes.byref(p_sd),
            )
        else:
            rc = advapi.GetNamedSecurityInfoW(
                os.fspath(path), 1, sec_info, ctypes.byref(p_owner), None, ctypes.byref(p_dacl), None, ctypes.byref(p_sd),
            )
        if rc != 0:
            raise ctypes.WinError(rc)
        try:
            owner_sid = sid_to_str(p_owner)
            control = wintypes.WORD(0)
            revision = wintypes.DWORD(0)
            if not advapi.GetSecurityDescriptorControl(p_sd, ctypes.byref(control), ctypes.byref(revision)):
                raise ctypes.WinError(ctypes.get_last_error())
            dacl_present = bool(control.value & 0x0004)
            dacl_protected = bool(control.value & 0x1000)
            if not dacl_present or not p_dacl:
                return owner_sid, current_user_sid, dacl_protected, None
            acl_info = ACL_SIZE_INFO()
            if not advapi.GetAclInformation(p_dacl, ctypes.byref(acl_info), ctypes.sizeof(acl_info), 2):
                raise ctypes.WinError(ctypes.get_last_error())
            aces: list[tuple[int, str]] = []
            for index in range(acl_info.AceCount):
                p_ace = ctypes.c_void_p()
                if not advapi.GetAce(p_dacl, index, ctypes.byref(p_ace)):
                    raise ctypes.WinError(ctypes.get_last_error())
                header = ACE_HEADER_STRUCT.from_address(p_ace.value)
                ace_sid = sid_to_str(ctypes.c_void_p(p_ace.value + 8))
                if ace_sid is None:
                    raise OSError("Unable to decode Windows ACE SID.")
                aces.append((int(header.AceType), ace_sid))
            return owner_sid, current_user_sid, dacl_protected, aces
        finally:
            if p_sd:
                kernel.LocalFree(p_sd)

    def _verify_windows_security(self, *, path=None, fd=None) -> bool:
        try:
            security = self._read_windows_security(path=path, fd=fd)
        except OSError:
            return False
        if security is None:
            return os.name != "nt"
        return validate_windows_acl_principals(*security)

    def verify_private_path(self, path, *, directory=False):
        try:
            info = os.lstat(path)
        except OSError:
            return False
        if not self.is_private(info, directory=directory):
            return False
        return self._verify_windows_security(path=path)

    def verify_private_descriptor(self, fd, *, directory=False):
        try:
            info = os.fstat(fd)
        except OSError:
            return False
        if not self.is_private(info, directory=directory):
            return False
        return self._verify_windows_security(fd=fd)

    def restrict_permissions(self, path, *, directory=False):
        if not self.is_private(os.lstat(path), directory=directory):
            raise OSError("Refusing a Windows junction, symlink, or invalid state path.")
        if os.name == "nt":
            self._restrict_windows_acl(path, directory)

    @staticmethod
    def _restrict_windows_acl(path, directory):
        """Protect the DACL and grant full access to the owner and SYSTEM."""
        import ctypes
        from ctypes import wintypes

        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        convert = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
        convert.argtypes = (wintypes.LPCWSTR, wintypes.DWORD,
                            ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.ULONG))
        convert.restype = wintypes.BOOL
        set_security = advapi.SetFileSecurityW
        set_security.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p)
        set_security.restype = wintypes.BOOL
        local_free = kernel.LocalFree
        local_free.argtypes = (ctypes.c_void_p,)
        local_free.restype = ctypes.c_void_p
        inheritance = "OICI" if directory else ""
        descriptor = ctypes.c_void_p()
        sddl = f"D:P(A;{inheritance};FA;;;SY)(A;{inheritance};FA;;;OW)"
        if not convert(sddl, 1, ctypes.byref(descriptor), None):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            # DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION
            if not set_security(os.fspath(path), 0x80000004, descriptor):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            local_free(descriptor)

    @contextmanager
    def lock_file(self, path, *, blocking=True):
        try:
            import msvcrt
        except ImportError as error:
            raise self.unsupported("Windows file locking") from error
        if os.path.lexists(path):
            self.restrict_permissions(path)
        fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0), 0o600)
        try:
            while True:
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    break
                except OSError as error:
                    if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise
                    if not blocking:
                        raise FileLockBusy(str(path)) from error
                    time.sleep(0.05)
            yield
        finally:
            os.close(fd)

    def sync_directory(self, path):
        # There is no portable directory-FD flush on Windows.
        pass

    def sync_directory_fd(self, fd):
        # There is no portable directory-FD flush on Windows.
        pass

    def terminate_group(self, pgid):
        raise self.unsupported("Terminating a POSIX process group")

    def process_group(self, pid):
        return pid
