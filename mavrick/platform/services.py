"""Declarative cross-platform background daemon and HUD startup templates."""

from __future__ import annotations

import os
import plistlib
import re
import stat
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from . import get_platform

_VALID_ID = re.compile(r"[A-Za-z0-9._-]+")
_TASK_NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"


def _validate_executable(executable: str | None, *, default: str | None = None) -> str:
    chosen = (executable or default or sys.executable or "").strip()
    if not chosen or any(ch in chosen for ch in ("\x00", "\r", "\n")):
        raise ValueError("Python executable path contains invalid control characters.")
    return chosen


def _normalize_target_platform(name: str | None) -> str:
    raw = (name or get_platform().name).strip().lower()
    aliases = {"darwin": "macos", "osx": "macos", "win32": "windows", "win": "windows"}
    chosen = aliases.get(raw, raw)
    if chosen not in ("linux", "macos", "windows"):
        raise ValueError(f"Unsupported service platform '{name}'. Choose linux, macos, or windows.")
    return chosen


def linux_systemd_templates(*, python_executable: str | None = None) -> dict[str, str]:
    """Return declarative systemd --user unit templates for Linux."""
    exe = _validate_executable(python_executable, default="/usr/bin/python3")
    env_path = 'Environment="PATH=%h/.npm-global/bin:%h/.local/bin:/usr/local/bin:/usr/bin:/bin"'
    env_py = 'Environment="PYTHONPATH=/usr/lib/mavrick:/usr/lib:%h/.local/share/mavrick"'
    return {
        "mavrickd.service": "\n".join([
            "[Unit]",
            "Description=MAVRICK AI Intelligence Daemon (mavrickd)",
            "After=default.target",
            "",
            "[Service]",
            "Type=simple",
            env_path,
            env_py,
            f"ExecStart={exe} -m mavrick.daemon",
            "Restart=always",
            "RestartSec=2",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]),
        "mavrick.service": "\n".join([
            "[Unit]",
            "Description=MAVRICK Desktop AI Operating System Assistant",
            "After=graphical-session.target",
            "",
            "[Service]",
            "Type=simple",
            env_path,
            env_py,
            f"ExecStart={exe} -m mavrick.ui",
            "Restart=on-failure",
            "RestartSec=2",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]),
    }


def ensure_macos_log_dir(log_dir: str | Path | None = None) -> Path:
    """Ensure the macOS LaunchAgent log directory exists with private 0700 permissions."""
    target = Path(log_dir) if log_dir is not None else (get_platform("darwin").log_dir())
    if os.path.lexists(target):
        info = os.lstat(target)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise OSError(f"Refusing a linked or non-directory macOS log path: {target}")
    os.makedirs(target, mode=0o700, exist_ok=True)
    info = os.lstat(target)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise OSError(f"Refusing a linked or non-directory macOS log path: {target}")
    if os.name == "posix":
        if info.st_uid != os.getuid():
            raise OSError(f"Refusing macOS log directory not owned by the current user: {target}")
        if stat.S_IMODE(info.st_mode) != 0o700:
            os.chmod(target, 0o700)
    return target


def macos_launchd_plist(
    *,
    python_executable: str | None = None,
    log_dir: Path | None = None,
    label: str = "com.mavrick.daemon",
    create_log_dir: bool | None = None,
) -> str:
    """Build an XML LaunchAgent plist for macOS using stdlib plistlib."""
    if not _VALID_ID.fullmatch(label):
        raise ValueError("Invalid launchd label.")
    exe = _validate_executable(python_executable)
    logs = Path(log_dir) if log_dir is not None else (Path.home() / "Library" / "Logs" / "Mavrick")
    should_create = (get_platform().name == "macos") if create_log_dir is None else bool(create_log_dir)
    if should_create:
        logs = ensure_macos_log_dir(logs)
    payload = {
        "Label": label,
        "ProgramArguments": [exe, "-m", "mavrick.daemon"],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ProcessType": "Background",
        "StandardOutPath": str(logs / "mavrickd.out.log"),
        "StandardErrorPath": str(logs / "mavrickd.err.log"),
    }
    return plistlib.dumps(payload, sort_keys=False).decode("utf-8")


def write_macos_launchd_plist(
    plist_path: str | Path,
    *,
    python_executable: str | None = None,
    log_dir: str | Path | None = None,
    label: str = "com.mavrick.daemon",
) -> Path:
    """Create the private macOS LaunchAgent log dir (0700) and atomically write the plist."""
    logs = ensure_macos_log_dir(log_dir)
    rendered = macos_launchd_plist(
        python_executable=python_executable,
        log_dir=logs,
        label=label,
        create_log_dir=True,
    )
    target = Path(plist_path)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temp_file = tempfile.mkstemp(prefix=".launchd-", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_file, 0o644)
        os.replace(temp_file, target)
    finally:
        if os.path.exists(temp_file):
            os.unlink(temp_file)
    return target


def _windows_pythonw(executable: str) -> str:
    path = Path(executable)
    if path.name.lower() == "python.exe":
        candidate = path.with_name("pythonw.exe")
        if candidate.is_file():
            return str(candidate)
    return executable


def windows_task_xml(
    *,
    python_executable: str | None = None,
    task_name: str = "MavrickDaemon",
) -> str:
    """Build a safe, shell-free Windows Task Scheduler XML template for mavrickd."""
    if not _VALID_ID.fullmatch(task_name):
        raise ValueError("Invalid Windows scheduled task name.")
    exe = _windows_pythonw(_validate_executable(python_executable))

    root = ET.Element("Task", version="1.2", xmlns=_TASK_NS)
    reg = ET.SubElement(root, "RegistrationInfo")
    ET.SubElement(reg, "Description").text = f"MAVRICK Background AI Daemon ({task_name})"

    triggers = ET.SubElement(root, "Triggers")
    logon = ET.SubElement(triggers, "LogonTrigger")
    ET.SubElement(logon, "Enabled").text = "true"

    principals = ET.SubElement(root, "Principals")
    principal = ET.SubElement(principals, "Principal", id="Author")
    ET.SubElement(principal, "LogonType").text = "InteractiveToken"
    ET.SubElement(principal, "RunLevel").text = "LeastPrivilege"

    settings = ET.SubElement(root, "Settings")
    ET.SubElement(settings, "MultipleInstancesPolicy").text = "IgnoreNew"
    ET.SubElement(settings, "DisallowStartIfOnBatteries").text = "false"
    ET.SubElement(settings, "StopIfGoingOnBatteries").text = "false"
    ET.SubElement(settings, "StartWhenAvailable").text = "true"
    ET.SubElement(settings, "AllowHardTerminate").text = "true"
    ET.SubElement(settings, "ExecutionTimeLimit").text = "PT0S"
    restart = ET.SubElement(settings, "RestartOnFailure")
    ET.SubElement(restart, "Interval").text = "PT1M"
    ET.SubElement(restart, "Count").text = "3"

    actions = ET.SubElement(root, "Actions", Context="Author")
    exec_el = ET.SubElement(actions, "Exec")
    ET.SubElement(exec_el, "Command").text = exe
    ET.SubElement(exec_el, "Arguments").text = "-m mavrick.daemon"

    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="unicode", xml_declaration=True) + "\n"


def windows_powershell_register_command(xml_path: str | Path, *, task_name: str = "MavrickDaemon") -> str:
    """Return a PowerShell command that registers a Task Scheduler XML with literal quoting."""
    if not _VALID_ID.fullmatch(task_name):
        raise ValueError("Invalid Windows scheduled task name.")
    raw = os.fspath(xml_path)
    if any(ch in raw for ch in ("\x00", "\r", "\n")):
        raise ValueError("Task XML path contains invalid control characters.")
    escaped_path = raw.replace("'", "''")
    return f"Register-ScheduledTask -TaskName '{task_name}' -Xml (Get-Content -LiteralPath '{escaped_path}' -Raw) -Force"


def service_template_for_platform(
    platform_name: str | None = None,
    *,
    python_executable: str | None = None,
) -> tuple[str, str]:
    """Return (kind_label, template_content) for linux, macos, or windows."""
    target = _normalize_target_platform(platform_name)
    if target == "linux":
        units = linux_systemd_templates(python_executable=python_executable)
        rendered = (
            "# ~/.config/systemd/user/mavrickd.service\n"
            + units["mavrickd.service"]
            + "\n# ~/.config/systemd/user/mavrick.service\n"
            + units["mavrick.service"]
            + "\n# Enable with: systemctl --user enable --now mavrickd.service mavrick.service\n"
        )
        return "systemd", rendered
    if target == "macos":
        logs = get_platform().log_dir() if get_platform().name == "macos" else get_platform("darwin").log_dir()
        return "launchd", macos_launchd_plist(python_executable=python_executable, log_dir=logs)
    return "task-scheduler", windows_task_xml(python_executable=python_executable)
