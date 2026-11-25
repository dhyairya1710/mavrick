"""Resolve and verify exact package changes through the active platform."""

import json
import re
import shutil
import subprocess

from ..platform import get_platform, linux_packages
from ..platform.base import package_id
from .apps import detect_preferred_terminal, launch_command

FLATHUB_APPS = linux_packages.FLATHUB_APPS
_LABELS = {"apt": "APT", "flatpak": "Flatpak", "winget": "Winget", "scoop": "Scoop", "brew": "Homebrew"}


def _package_label(name, manager, scope):
    if manager in ("apt", "flatpak"):
        return linux_packages._package_label(name, manager, scope)
    return f"{name} ({_LABELS[manager]})"


def _installed(name, manager, scope=None):
    adapter = get_platform()
    if adapter.name == "linux":
        return linux_packages._installed(name, manager, scope)
    command = adapter.package_command("list", name, manager=manager)
    result = subprocess.run(command, capture_output=True, text=True, timeout=10)
    output = result.stdout.strip()
    if result.returncode:
        detail = (result.stdout + result.stderr).casefold()
        if any(message in detail for message in ("no installed package found", "no such keg", "no package found")):
            return False
        raise ValueError(f"{_LABELS[manager]} could not verify {name} (exit {result.returncode}).")
    if manager == "winget":
        pattern = rf"(?:^|\s){re.escape(name)}(?=\s|$)"
        return any(re.search(pattern, line, re.IGNORECASE) for line in output.splitlines())
    return any(line.split() and line.split()[0].casefold() == name.casefold() for line in output.splitlines())


def _native_manager(adapter, manager):
    if manager == "auto":
        manager = adapter.package_manager_name()
    if manager not in ({"winget", "scoop"} if adapter.name == "windows" else {"brew"}):
        raise adapter.unsupported("Requested package manager")
    if manager != adapter.package_manager_name() and not (
        manager == "scoop" and adapter.name == "windows" and adapter._scoop()
    ):
        raise adapter.unsupported(_LABELS[manager])
    return manager


def installed_targets(name, manager="auto"):
    adapter = get_platform()
    if adapter.name == "linux":
        return linux_packages.installed_targets(name, manager, _installed)
    name = package_id(name)
    manager = _native_manager(adapter, manager)
    return [(name, manager, None)] if _installed(name, manager) else []


def verify_package_installed(package_name):
    try:
        matches = installed_targets(package_name)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        return False, str(error)
    return bool(matches), ", ".join(_package_label(*target) for target in matches) or "No matching package is installed."


def resolve_package(action, params):
    adapter = get_platform()
    if adapter.name == "linux":
        return linux_packages.resolve_package(action, params, _installed)
    name = package_id(params["package_name"])
    manager = _native_manager(adapter, params["manager"])
    if action == "uninstall_package" and not _installed(name, manager):
        raise ValueError(f"No installed package matches {name} via {_LABELS[manager]}.")
    return name, manager, None


def package_title(action, name, manager, scope):
    if manager in ("apt", "flatpak"):
        return linux_packages.package_title(action, name, manager, scope)
    return f"{'Install' if action == 'install_package' else 'Uninstall'} {name} via {_LABELS[manager]}"


def _package_operation(action, name, manager, scope, cancel_token=None, progress=None):
    adapter = get_platform()
    if adapter.name == "linux":
        return linux_packages._package_operation(action, name, manager, scope, cancel_token, progress,
                                                  installed=_installed, pick_terminal=detect_preferred_terminal,
                                                  launch=launch_command)
    installing = action == "install_package"
    try:
        command = adapter.package_command("install" if installing else "remove", name, manager=manager)
        before = _installed(name, manager)
        if cancel_token and cancel_token.is_set():
            return False, "Stopped before starting the package manager.", {"cancelled": True}
        if progress:
            progress(f"Waiting for {_LABELS[manager]}")
        with subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as process:
            while True:
                try:
                    status = process.wait(timeout=0.15)
                    break
                except subprocess.TimeoutExpired:
                    if cancel_token and cancel_token.is_set():
                        try:
                            process.terminate()
                            status = process.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            status = process.wait(timeout=2)
                        break
        after = _installed(name, manager)
        details = {"package": name, "manager": manager, "changed": before != after}
        if cancel_token and cancel_token.is_set():
            return False, f"Stopped. {_package_label(name, manager, scope)} is {'installed' if after else 'absent'}.", {
                **details, "cancelled": True, "partial": before != after, "verification": "verified",
            }
        if status or after != installing:
            return False, f"{_LABELS[manager]} returned {status}. Desired package state could not be confirmed.", details
        return True, f"Verified {'installation' if installing else 'removal'} of {_package_label(name, manager, scope)}.", {
            **details, "verification": "verified",
        }
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        return False, str(error), {}


def check_updates():
    adapter = get_platform()
    if adapter.name == "linux":
        return linux_packages.check_updates()
    manager = adapter.package_manager_name()
    label = _LABELS.get(manager, "Package manager")
    try:
        command = adapter.package_command("updates")
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
        if result.returncode:
            raise ValueError(f"{label} exited with status {result.returncode}.")
        output = result.stdout.strip()
        if manager == "brew":
            data = json.loads(output)
            count = sum(len(data[key]) for key in ("formulae", "casks"))
        else:
            count = None  # Winget and Scoop present a human-readable report.
        return {"errors": {}, "checked": [label], "total_updates": count,
                "report": f"{label}: {output or 'No update entries returned.'}"}
    except (OSError, KeyError, ValueError, subprocess.SubprocessError) as error:
        return {"errors": {label: str(error)}, "checked": [], "total_updates": 0, "report": str(error)}


def _request_package(action, package_name, manager, scope, approval):
    from ..executor import ActionExecutor
    from ..schema import ActionRequest, ActionType, RiskLevel

    params = {"package_name": package_name, "manager": manager}
    if scope:
        params["scope"] = scope
    request = ActionRequest(ActionType(action), params, "", RiskLevel.PRIVILEGED)
    result = ActionExecutor.get_instance().execute(request, approval=approval)
    return result.status == "success", result.summary, {
        **result.details, "status": result.status, "verification": result.verification_status}


def install_package(package_name, manager="auto", *, scope=None, approval=None):
    return _request_package("install_package", package_name, manager, scope, approval)


def uninstall_package(package_name, manager="auto", *, scope=None, approval=None):
    return _request_package("uninstall_package", package_name, manager, scope, approval)


