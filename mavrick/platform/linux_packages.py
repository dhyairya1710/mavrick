"""Exact APT and Flatpak package lookup and interactive terminal receipts."""

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .base import FileLockBusy
from .linux import APT_NAME as _APT_NAME, FLATPAK_ID as _FLATPAK_ID, LinuxAdapter
from ..schema import PACKAGE_NAME_PATTERN
from ..tools.apps import _terminal_command


FLATHUB_APPS = {
    "vlc": "org.videolan.VLC",
    "spotify": "com.spotify.Client",
    "discord": "com.discordapp.Discord",
    "obs": "com.obsproject.Studio",
    "steam": "com.valvesoftware.Steam",
    "firefox": "org.mozilla.firefox",
}
def _installed(name, manager, scope=None):
    command = LinuxAdapter().package_command("list", name, manager=manager, scope=scope)
    if not shutil.which(command[0]):
        return False
    result = subprocess.run(command, capture_output=True, text=True, timeout=8,
                            env={**os.environ, "LC_ALL": "C"})
    if result.returncode == 0:
        return manager == "flatpak" or result.stdout.strip() == "installed"
    error = result.stderr.strip()
    if (manager == "apt" and "no packages found matching" in error
            or manager == "flatpak" and "not installed" in error):
        return False
    raise ValueError(f"{manager} could not check {name}: {error or f'exit {result.returncode}'}")


def installed_targets(name, manager, installed):
    _validate_name(name)
    matches = []
    if manager in ("auto", "apt") and _APT_NAME.fullmatch(name) and installed(name, "apt"):
        matches.append((name, "apt", None))
    flatpak_name = FLATHUB_APPS.get(name.lower(), name)
    if manager in ("auto", "flatpak") and _FLATPAK_ID.fullmatch(flatpak_name):
        for scope in ("user", "system"):
            if installed(flatpak_name, "flatpak", scope):
                matches.append((flatpak_name, "flatpak", scope))
    return matches


def _validate_name(name):
    if not isinstance(name, str) or not PACKAGE_NAME_PATTERN.fullmatch(name):
        raise ValueError("Invalid package name.")


def _package_label(name, manager, scope):
    if manager == "flatpak":
        return f"{name} ({scope} Flatpak)"
    return f"{name} (APT)"


def _flathub_scope():
    if shutil.which("flatpak"):
        for scope in ("user", "system"):
            try:
                result = subprocess.run(["flatpak", "remotes", f"--{scope}", "--columns=name"],
                                        capture_output=True, text=True, timeout=4)
                if result.returncode == 0 and "flathub" in result.stdout.splitlines():
                    return scope
            except (OSError, subprocess.SubprocessError):
                continue
    return "user"


def resolve_package(action, params, installed):
    name, manager = params["package_name"], params["manager"]
    if manager not in ("auto", "apt", "flatpak"):
        raise ValueError("Choose APT or Flatpak on Linux.")
    if action == "install_package":
        manager = ("flatpak" if _FLATPAK_ID.fullmatch(name) else "apt") if manager == "auto" else manager
        if manager == "flatpak":
            name = FLATHUB_APPS.get(name.lower(), name)
            if not _FLATPAK_ID.fullmatch(name):
                raise ValueError("Give the exact Flathub application ID.")
            scope = params.get("scope") or _flathub_scope()
            if scope not in ("user", "system"):
                raise ValueError("Choose user or system Flatpak.")
            return name, manager, scope
        if not _APT_NAME.fullmatch(name):
            raise ValueError("Give a valid APT package name.")
        return name, manager, None

    scope = params.get("scope")
    matches = installed_targets(name, manager, installed)
    if scope:
        matches = [entry for entry in matches if entry[2] == scope]
    if len(matches) > 1:
        choices = ", ".join(_package_label(*entry) for entry in matches)
        raise ValueError(f"Which installation should I remove? {choices}. Specify APT, user Flatpak, or system Flatpak.")
    if not matches:
        raise ValueError(f"No installed package matches {name} for the requested manager or scope.")
    return matches[0]


def package_title(action, name, manager, scope):
    if action == "install_package":
        if manager == "flatpak":
            display = "VLC" if name == "org.videolan.VLC" else name
            return f"Install {display} from Flathub"
        return f"Install {name} via APT"
    return f"Uninstall {_package_label(name, manager, scope)}"


# This worker is fixed application code. Package names are passed as argv to the
# package manager. The receipt lets us distinguish a terminal handoff from a
# completed package manager command even with a single-instance terminal.
_PACKAGE_WORKER = """
import fcntl
import pathlib
import subprocess
import sys

receipt = pathlib.Path(sys.argv[1])
try:
    lock = receipt.with_suffix('.lock').open('r+')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    receipt.with_suffix('.started').touch()
    if receipt.with_suffix('.cancel').exists():
        sys.exit(130)
except OSError:
    sys.exit(1)
try:
    result = subprocess.call(sys.argv[2:])
except OSError as error:
    print(error, file=sys.stderr)
    result = 1
try:
    temporary = receipt.with_suffix('.tmp')
    temporary.write_text(str(result))
    temporary.replace(receipt)
except OSError:
    pass
"""


def _apt_candidate(name):
    result = subprocess.run(LinuxAdapter().package_command("search", name), capture_output=True, text=True,
                            timeout=8, env={**os.environ, "LC_ALL": "C"})
    if result.returncode:
        raise ValueError(result.stderr.strip() or f"APT could not resolve {name}.")
    lines = result.stdout.splitlines()
    if not lines or lines[0].strip() != f"{name}:":
        raise ValueError(f"APT has no exact package named {name}.")
    for line in lines[1:]:
        if line.strip().startswith("Candidate:"):
            version = line.split(":", 1)[1].strip()
            if version and version != "(none)":
                return version
    raise ValueError(f"APT has no install candidate for {name}.")


def _worker_active(receipt):
    try:
        with LinuxAdapter().lock_file(receipt.with_suffix(".lock"), blocking=False):
            return False
    except FileLockBusy:
        return True


def _wait_for_package(receipt, cancel_token, progress):
    deadline = time.monotonic() + 8
    started = receipt.with_suffix(".started")
    stop_reported = False
    while True:
        if receipt.exists():
            return int(receipt.read_text()), ""
        if cancel_token and cancel_token.is_set() and not stop_reported:
            receipt.with_suffix(".cancel").touch()
            if progress:
                progress("Stop requested. Finish or cancel the operation in Terminal.")
            stop_reported = True
        if started.exists() and not _worker_active(receipt):
            # The worker may have completed between reading the receipt and checking the lock.
            return (int(receipt.read_text()), "") if receipt.exists() else (
                None, "Terminal closed before the package manager reported its outcome.")
        if not started.exists() and time.monotonic() > deadline and not _worker_active(receipt):
            receipt.with_suffix(".cancel").touch()
            return None, "Terminal launch requested. The package manager did not report starting."
        time.sleep(0.15)


def _package_operation(action, name, manager, scope, cancel_token, progress, *, installed, pick_terminal, launch):
    installing = action == "install_package"
    try:
        _validate_name(name)
        version = _apt_candidate(name) if installing and manager == "apt" else None
        command = LinuxAdapter().package_command("install" if installing else "remove", name,
                                                 manager=manager, scope=scope, version=version)
        before = installed(name, manager, scope)
        terminal = pick_terminal()
        if not terminal or not shutil.which(command[0]) or not shutil.which(manager if manager == "flatpak" else "apt-get"):
            return False, "The requested package manager or a supported terminal is unavailable.", {}
        title = package_title(action, name, manager, scope)
        if cancel_token and cancel_token.is_set():
            return False, "Stopped before opening Terminal.", {"cancelled": True}
        with tempfile.TemporaryDirectory(prefix="mavrick-package-") as directory:
            receipt = Path(directory, "result")
            receipt.with_suffix(".lock").touch()
            argv = [sys.executable, "-c", _PACKAGE_WORKER, str(receipt), *command]
            ok, message, details = launch(_terminal_command(terminal, argv), title)
            if not ok:
                return ok, message, details
            if progress:
                progress("Waiting for the package manager in Terminal")
            exit_code, error = _wait_for_package(receipt, cancel_token, progress)
        after = installed(name, manager, scope)
        details = {"package": name, "manager": manager, "scope": scope, "changed": before != after}
        if cancel_token and cancel_token.is_set():
            state = f"{_package_label(name, manager, scope)} is {'installed' if after else 'absent'}."
            return False, f"Stopped. {state}" + (f" {error}" if error else ""), {
                **details, "cancelled": True, "verification": "verified", "partial": before != after}
        if error:
            return False, error, {**details, "verification": "launch_requested"}
        if exit_code:
            return False, f"{title}: package manager exited with status {exit_code}. Installed state is {'present' if after else 'absent'}.", details
        if after != installing:
            return False, f"{title}: package manager exited successfully, but the requested package state could not be verified.", details
        label = _package_label(name, manager, scope)
        summary = f"Verified {'installation' if installing else 'removal'} of {label}."
        if before == after:
            summary = f"{label} was already {'installed' if installing else 'absent'}."
        return True, summary, {**details, "verification": "verified"}
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        return False, str(error), {}


def check_updates():
    """Report each backend's outcome, including failures and unavailable tools."""
    commands = {
        "APT": LinuxAdapter().package_command("updates"),
        "Flatpak": LinuxAdapter().package_command("updates", manager="flatpak")
    }
    updates = {"APT": [], "Flatpak": []}
    errors = {}
    checked = []
    for backend, command in commands.items():
        if not shutil.which(command[0]):
            errors[backend] = f"{command[0]} is unavailable"
            continue
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=10)
            if result.returncode:
                errors[backend] = result.stderr.strip() or f"exit {result.returncode}"
                continue
            checked.append(backend)
            for line in result.stdout.splitlines():
                line = line.strip()
                if line and not (backend == "APT" and line.startswith("Listing")):
                    updates[backend].append({"package": line.split("/")[0], "raw": line}
                                            if backend == "APT" else {"ref": line})
        except (OSError, subprocess.SubprocessError) as error:
            errors[backend] = str(error)
    total = sum(map(len, updates.values()))
    report = []
    for backend, items in updates.items():
        if backend in errors:
            report.append(f"{backend}: {errors[backend]}")
        elif items:
            report.append(f"{backend} ({len(items)}): " + ", ".join(
                item.get("package") or item["ref"] for item in items[:10]))
        else:
            report.append(f"{backend}: Up to date")
    return {"apt_updates": updates["APT"], "flatpak_updates": updates["Flatpak"], "errors": errors,
            "checked": checked, "total_updates": total, "report": "\n".join(report)}
