"""Launch approved applications and report the launcher outcome."""

import difflib
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from ..platform import get_platform
from ..platform.linux import DESKTOP_ALIASES, TERMINALS
from ..config import is_cosmic_desktop, is_wayland_session
from ..policy import (APP_ALIASES, APPLICATION_ALLOWLIST, TERMINAL_CLI_APPS,
                      validate_app_target, desktop_app_ids, is_safe_user_path)
from ..schema import validate_browser, validate_web_url


APP_INDEX_TTL = 30
_index_lock = threading.Lock()
_index = (None, 0.0, MappingProxyType({}))
_norm_cache: tuple[object, dict[str, str], tuple[str, ...]] = (None, {}, ())
_UNSET = object()

_PRIORITY_ALIASES = {
    "files": ("cosmic-files", "nautilus", "org.gnome.nautilus", "dolphin", "org.kde.dolphin", "thunar"),
    "file manager": ("cosmic-files", "nautilus", "org.gnome.nautilus", "dolphin", "org.kde.dolphin", "thunar"),
    "vscode": ("code", "com.visualstudio.code"),
    "vs code": ("code", "com.visualstudio.code"),
    "visual studio code": ("code", "com.visualstudio.code"),
    "code": ("code", "com.visualstudio.code"),
    "vlc": ("vlc", "org.videolan.vlc"),
    "terminal": ("cosmic-term", "gnome-terminal", "alacritty", "kitty", "x-terminal-emulator"),
    "browser": ("brave", "brave-browser", "com.brave.browser", "firefox", "org.mozilla.firefox", "google-chrome", "chromium"),
}

_APP_LABELS = {
    "cosmic-files": "Files",
    "nautilus": "Files",
    "org.gnome.nautilus": "Files",
    "dolphin": "Files",
    "org.kde.dolphin": "Files",
    "thunar": "Files",
    "vlc": "VLC",
    "org.videolan.vlc": "VLC",
    "code": "VS Code",
    "com.visualstudio.code": "VS Code",
    "firefox": "Firefox",
    "org.mozilla.firefox": "Firefox",
    "brave": "Brave",
    "brave-browser": "Brave",
    "com.brave.browser": "Brave",
    "google-chrome": "Chrome",
    "chromium": "Chromium",
    "org.chromium.chromium": "Chromium",
    "spotify": "Spotify",
    "com.spotify.client": "Spotify",
    "discord": "Discord",
    "com.discordapp.discord": "Discord",
    "obs": "OBS",
    "com.obsproject.studio": "OBS",
    "cosmic-term": "Terminal",
    "gnome-terminal": "Terminal",
    "alacritty": "Alacritty",
    "kitty": "Kitty",
    "x-terminal-emulator": "Terminal",
    "com.anthropic.claude": "Claude Desktop",
    "cursor": "Cursor",
    "subl": "Sublime Text",
    "gimp": "GIMP",
    "steam": "Steam",
}


def app_display_name(app_id: str) -> str:
    clean = app_id.strip().casefold().removesuffix(".desktop")
    if clean in _APP_LABELS:
        return _APP_LABELS[clean]
    leaf = clean.rsplit(".", 1)[-1]
    if leaf in _APP_LABELS:
        return _APP_LABELS[leaf]
    return leaf.replace("-", " ").replace("_", " ").title()


def _desktop_environment() -> tuple[str | None, ...]:
    adapter = get_platform()
    return (adapter.name, str(adapter.home), *(adapter.environ.get(name) for name in
            ("XDG_DATA_HOME", "XDG_DATA_DIRS", "PATH", "APPDATA", "LOCALAPPDATA", "ProgramData")))


def app_index_status() -> str:
    key, expiry, entries = _index
    if key != _desktop_environment():
        return "cold"
    return f"{'warm' if time.monotonic() < expiry else 'stale'} ({len(entries)} names)"


def instant_app_index(*, cached_only: bool = False) -> Mapping[str, str]:
    """Cache installed exact IDs and known aliases. The executor revalidates launches."""
    global _index
    key = _desktop_environment()
    cached_key, expiry, entries = _index
    if key == cached_key and time.monotonic() < expiry:
        return entries
    if cached_only:
        return MappingProxyType({})
    with _index_lock:
        cached_key, expiry, entries = _index
        if key == cached_key and time.monotonic() < expiry:
            return entries
        installed = desktop_app_ids()
        adapter = get_platform()
        matches = adapter.application_index(installed, APPLICATION_ALLOWLIST - TERMINAL_CLI_APPS)
        for stem in installed:
            leaf = stem.rsplit(".", 1)[-1]
            if leaf and leaf not in matches and leaf in APPLICATION_ALLOWLIST - TERMINAL_CLI_APPS:
                matches[leaf] = stem
        matches.update({alias: matches[target] for alias, target in APP_ALIASES.items() if target in matches})
        for alias, candidates in _PRIORITY_ALIASES.items():
            if alias not in matches:
                found = next((matches[candidate] for candidate in candidates if candidate in matches), None)
                if found:
                    matches[alias] = found
        entries = MappingProxyType(matches)
        _index = (key, time.monotonic() + APP_INDEX_TTL, entries)
        return entries


def _normalized_lookup(index: Mapping[str, str]) -> tuple[dict[str, str], tuple[str, ...]]:
    global _norm_cache
    cached_ref, norm_map, fuzzy_keys = _norm_cache
    if cached_ref is index:
        return norm_map, fuzzy_keys
    built: dict[str, str] = {}
    simple: list[str] = []
    for key, target in index.items():
        clean = key.casefold().removesuffix(".desktop")
        norm = re.sub(r"[\s_-]+", "", clean)
        built.setdefault(norm, target)
        if "." not in clean and len(norm) >= 2 and norm not in simple:
            simple.append(norm)
    keys_tuple = tuple(simple)
    _norm_cache = (index, built, keys_tuple)
    return built, keys_tuple


def resolve_cached_app(query: str, index: Mapping[str, str], *, allow_fuzzy: bool = True) -> str | None:
    if not index:
        return None
    phrase = query.strip().casefold().removesuffix(".desktop")
    if not phrase:
        return None
    if phrase in index:
        return index[phrase]
    norm_map, fuzzy_keys = _normalized_lookup(index)
    norm_query = re.sub(r"[\s_-]+", "", phrase)
    if norm_query in norm_map:
        return norm_map[norm_query]
    if allow_fuzzy and len(norm_query) >= 3 and len(phrase.split()) <= 2 and re.fullmatch(r"[a-z0-9._ -]+", phrase):
        close = difflib.get_close_matches(norm_query, fuzzy_keys, n=1, cutoff=0.82)
        if close:
            return norm_map[close[0]]
    return None


def detect_preferred_terminal():
    return get_platform().terminal_path()


def _terminal_command(terminal, arguments):
    return get_platform().terminal_command(terminal, arguments)


def launch_command(command, label, cwd=None):
    if not command:
        return False, f"No installed launcher for {label}.", {"verification": "failed"}
    try:
        proc = subprocess.Popen(command, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                **get_platform().detached_options())
        try:
            _, error = proc.communicate(timeout=0.2)
        except subprocess.TimeoutExpired:
            # Drain output and reap the child without holding up the palette.
            threading.Thread(target=proc.communicate, daemon=True).start()
            return True, f"Launch requested for {label}.", {"verification": "launch_requested"}
        if proc.returncode:
            reason = error.decode(errors="replace").strip() if error else f"exit {proc.returncode}"
            return False, f"{label}: {reason}", {"verification": "failed"}
    except OSError as error:
        return False, f"{label}: {error}", {"verification": "failed"}
    return True, f"Launch requested for {label}.", {"verification": "launch_requested"}


def run_in_terminal(command, args=None, *, pinned=_UNSET):
    if args:
        return False, "Terminal utility arguments are unavailable.", {}
    valid, target = validate_app_target(command, terminal=True)
    if not valid:
        return False, target, {}
    is_claude = target == "claude-code"
    label = "Claude Code" if is_claude else target
    if pinned is not _UNSET:
        if isinstance(pinned, dict) and not pinned.get("resolved"):
            return False, pinned.get("reason") or f"{label} executable is unavailable.", {"verification": "failed"}
        ok, reason = verify_pinned_app(pinned, target=target)
        if not ok:
            return False, reason, {"verification": "failed"}
        return launch_command(list(pinned["command"]), f"{label} in Terminal")
    binary = "claude" if is_claude else target
    executable = shutil.which(binary)
    if not executable:
        return False, f"{label} executable ({binary}) is unavailable.", {"verification": "failed"}
    term = detect_preferred_terminal()
    if not term:
        return False, "No supported terminal emulator is installed.", {"verification": "failed"}
    return launch_command(_terminal_command(term, [executable]), f"{label} in Terminal")


def _app_command(target):
    return get_platform().app_command(target, desktop_app_ids(), DESKTOP_ALIASES)


def _path_identity(path: str | Path) -> list[int] | None:
    try:
        info = os.lstat(path)
    except OSError:
        return None
    return [
        info.st_dev,
        info.st_ino,
        info.st_size,
        getattr(info, "st_mtime_ns", int(info.st_mtime * 1_000_000_000)),
    ]


def _find_linux_desktop_file(desktop_id: str) -> str | None:
    adapter = get_platform()
    user = Path(adapter.environ.get("XDG_DATA_HOME", adapter.home / ".local/share"))
    system = adapter.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share")
    roots = [
        user,
        user / "flatpak/exports/share",
        *(Path(p) for p in system.split(":") if p),
        Path("/var/lib/flatpak/exports/share"),
    ]
    for root in roots:
        candidate = root / "applications" / desktop_id
        if candidate.is_file():
            return str(candidate)
    return None


def resolve_pinned_app(app_name: str, *, in_terminal: bool = False) -> dict:
    """Resolve and snapshot exact app launcher target details during prepare()."""
    valid, target = validate_app_target(app_name, terminal=in_terminal)
    if not valid:
        return {"resolved": False, "app": app_name, "reason": str(target)}
    adapter = get_platform()
    if in_terminal or target in TERMINAL_CLI_APPS:
        is_claude = target == "claude-code"
        binary = "claude" if is_claude else target
        label = "Claude Code" if is_claude else target
        executable = shutil.which(binary)
        if not executable:
            return {
                "resolved": False,
                "app": target,
                "adapter": adapter.name,
                "in_terminal": True,
                "reason": f"{label} executable ({binary}) is unavailable.",
            }
        term = detect_preferred_terminal()
        if not term:
            return {
                "resolved": False,
                "app": target,
                "adapter": adapter.name,
                "in_terminal": True,
                "reason": "No supported terminal emulator is installed.",
            }
        return {
            "resolved": True,
            "app": target,
            "adapter": adapter.name,
            "in_terminal": True,
            "command": list(_terminal_command(term, [executable])),
            "exe_path": executable,
            "exe_identity": _path_identity(executable),
            "terminal_path": term,
            "terminal_identity": _path_identity(term),
        }
    label = "Claude Desktop" if target == "com.anthropic.Claude.desktop" else target
    if adapter.name == "windows" and hasattr(adapter, "resolve_pinned_target"):
        pinned = adapter.resolve_pinned_target(target)
        return (
            pinned
            if pinned is not None
            else {"resolved": False, "app": target, "adapter": "windows", "reason": f"No installed launcher for {label}."}
        )
    command = _app_command(target)
    if not command:
        return {
            "resolved": False,
            "app": target,
            "adapter": adapter.name,
            "in_terminal": False,
            "reason": f"No installed launcher for {label}.",
        }
    exe = command[0]
    pinned = {
        "resolved": True,
        "app": target,
        "adapter": adapter.name,
        "in_terminal": False,
        "command": list(command),
        "exe_path": exe,
        "exe_identity": _path_identity(exe),
    }
    if adapter.name == "linux" and len(command) >= 2 and os.path.basename(exe) == "gtk-launch":
        desktop_path = _find_linux_desktop_file(command[1])
        if desktop_path:
            pinned["desktop_path"] = desktop_path
            pinned["desktop_identity"] = _path_identity(desktop_path)
    elif adapter.name == "macos" and len(command) >= 3 and command[0] == "open" and command[1] == "-a":
        pinned["bundle_path"] = command[2]
        pinned["bundle_identity"] = _path_identity(command[2])
    return pinned


def verify_pinned_app(pinned: object, *, target: str) -> tuple[bool, str]:
    """Re-verify a pinned launcher command/target against its prepared identity."""
    if not isinstance(pinned, dict) or not pinned.get("resolved") or pinned.get("app") != target:
        return False, f"No installed launcher for {target}."
    checks = (
        ("exe_path", "exe_identity", "executable"),
        ("terminal_path", "terminal_identity", "terminal"),
        ("desktop_path", "desktop_identity", "desktop entry"),
        ("bundle_path", "bundle_identity", "application bundle"),
    )
    for path_key, id_key, label in checks:
        recorded_id = pinned.get(id_key)
        path_val = pinned.get(path_key)
        if recorded_id is not None:
            current_id = _path_identity(path_val) if path_val else None
            if current_id is None:
                return False, f"Pinned {label} for {target} vanished since review."
            if current_id != recorded_id:
                return False, f"Pinned {label} for {target} changed since review."
    if not pinned.get("command"):
        return False, f"Pinned launcher command for {target} is missing."
    return True, ""


def open_app(app_name, args=None, in_terminal=False, *, pinned=_UNSET):
    if args:
        return False, "Application arguments are unavailable.", {}
    valid, target = validate_app_target(app_name, terminal=in_terminal)
    if not valid:
        return False, target, {}
    if in_terminal or target in TERMINAL_CLI_APPS:
        return run_in_terminal(target, pinned=pinned)
    adapter = get_platform()
    label = "Claude Desktop" if target == "com.anthropic.Claude.desktop" else target
    if pinned is not _UNSET:
        if adapter.name == "windows" and hasattr(adapter, "launch_app"):
            return adapter.launch_app(target, pinned_target=pinned)
        if not isinstance(pinned, dict) or not pinned.get("resolved"):
            reason = pinned.get("reason") if isinstance(pinned, dict) else ""
            return False, reason or f"No installed launcher for {label}.", {"verification": "failed"}
        ok, reason = verify_pinned_app(pinned, target=target)
        if not ok:
            return False, reason, {"verification": "failed"}
        return launch_command(list(pinned["command"]), label)
    if adapter.name != "linux":
        return adapter.launch_app(target)
    return launch_command(_app_command(target), label)


def launch_apps(app_names, missing=(), cancel_token=None, *, pinned_targets=None):
    """Launch multiple validated applications immediately and report missing targets."""
    targets = list(dict.fromkeys(app_names))
    missing_clean = list(dict.fromkeys(str(item).strip() for item in missing if str(item).strip()))
    steps, launched, failed = [], [], []
    cancelled = False
    for app_id in targets:
        if cancel_token and cancel_token.is_set():
            cancelled = True
            break
        pinned = pinned_targets.get(app_id, _UNSET) if isinstance(pinned_targets, dict) else _UNSET
        ok, message, _ = open_app(app_id, pinned=pinned)
        label = app_display_name(app_id)
        steps.append({"target": app_id, "label": label, "ok": ok, "message": message})
        (launched if ok else failed).append(label)
    parts = []
    if launched:
        parts.append("Launched: " + ", ".join(launched))
    if missing_clean:
        parts.append("Not found: " + ", ".join(missing_clean))
    if failed:
        parts.append("Failed: " + ", ".join(failed))
    summary = " · ".join(parts) if parts else "No applications to launch."
    lines = list(parts)
    for step in steps:
        if step["message"]:
            lines.append(step["message"])
    ok = bool(launched) and not failed and not cancelled
    details = {
        "steps": steps,
        "launched": launched,
        "missing": missing_clean,
        "failed": failed,
        "report": "\n".join(lines),
        "verification": "launch_requested" if launched else "failed",
        "partial": bool(launched and failed),
        "cancelled": cancelled,
    }
    return ok, summary, details


def open_url(url, browser="auto"):
    try:
        url = validate_web_url(url)
        browser = validate_browser(browser)
    except ValueError as error:
        return False, str(error), {}
    adapter = get_platform()
    if adapter.name != "linux" and browser in ("auto", "xdg-open"):
        return adapter.open_web(url)
    if browser in ("auto", "xdg-open"):
        executable = shutil.which("xdg-open")
        command = [executable] if executable else None
    else:
        command = _app_command(browser)
        if not command and browser == "brave":
            command = _app_command("brave-browser")
    return launch_command([*command, url] if command else None, url)


def resolve_window_target(target):
    # Recognize registered desktop IDs before treating dotted strings as domains.
    valid, app = validate_app_target(target)
    if valid:
        return "app", app
    if "://" not in target and (target.lower().endswith(".desktop") or "." not in target or " " in target):
        raise ValueError(f"Could not resolve window target {target!r}. {app}")
    return "url", validate_web_url(target)


def arrange_windows(left_target, right_target, ratio=0.5, browser="auto", cancel_token=None, *, pinned_targets=None):
    try:
        validate_browser(browser)
        targets = [resolve_window_target(target) for target in (left_target, right_target)]
    except ValueError as error:
        return False, str(error), {}
    steps = []
    for kind, target in targets:
        if cancel_token and cancel_token.is_set():
            break
        if kind == "url":
            ok, message, _ = open_url(target, browser)
        else:
            pinned = pinned_targets.get(target, _UNSET) if isinstance(pinned_targets, dict) else _UNSET
            ok, message, _ = open_app(target, pinned=pinned)
        steps.append({"target": target, "ok": ok, "message": message})
    ok, summary, details = _launch_steps(steps, "window targets")
    if ok and is_wayland_session() and is_cosmic_desktop():
        details["placement_hint"] = "For side-by-side placement, use the COSMIC panel’s Tile current workspace control."
    return ok, summary, details


def resolve_workspace(params, config):
    workspace = config.get("coding_workspace", {})
    if not isinstance(workspace, dict):
        raise ValueError("coding_workspace must be an object in Mavrick's configuration.")
    raw = params.get("project") or workspace.get("project")
    if raw is None:
        candidates = sorted({project.resolve() for folder in ("Projects", "Code", "Developer")
                             for project in (Path.home() / folder).glob("*")
                             if (project / ".git").exists()})
        if len(candidates) != 1:
            choices = ", ".join(map(str, candidates))
            raise ValueError("Which project folder should I open? Give its path." +
                             (f" Found {choices}." if choices else ""))
        raw = str(candidates[0])
    if not isinstance(raw, str):
        raise ValueError("Give a project folder path.")
    safe, error, project = is_safe_user_path(raw, must_exist=True)
    if not safe or not os.path.isdir(project):
        raise ValueError(error or f"Project is not a directory: {project}")
    editors = ("code", "cursor", "subl", "antigravity")
    configured = workspace.get("editor")
    if configured is not None:
        valid, editor = validate_app_target(configured)
        if not valid or editor not in editors:
            raise ValueError("Choose Code, Cursor, Sublime Text, or Antigravity as the workspace editor.")
        editors = (editor,)
    editor = next((name for name in editors if _app_command(name)), None)
    terminal = detect_preferred_terminal()
    steps = []
    if editor:
        steps.append({"app": editor, "label": f"Open {project} in {editor}"})
    else:
        steps.append({"app": "", "label": "Editor unavailable"})
    if terminal:
        steps.append({"app": os.path.basename(terminal), "label": f"Open Terminal in {project}"})
    else:
        steps.append({"app": "", "label": "Terminal unavailable"})
    if not editor and not terminal:
        raise ValueError("No supported workspace editor or terminal is installed.")
    return project, steps


def open_workspace(project, steps, cancel_token):
    outcomes = []
    for index, step in enumerate(steps):
        if cancel_token.is_set():
            break
        app = step["app"]
        command = _app_command(app) if app and index == 0 else None
        if index == 0 and command:
            # Project is validated and supplied as one positional argument.
            command = [*command, project]
        elif index == 1 and app:
            command = get_platform().workspace_terminal_command(app, project)
        ok, message, _ = launch_command(command, step["label"], cwd=project)
        outcomes.append({"target": step["label"], "ok": ok, "message": message})
    return _launch_steps(outcomes, "workspace steps")


def _launch_steps(steps, description):
    count = sum(step["ok"] for step in steps)
    return bool(steps) and count == len(steps), f"{count} of {len(steps)} {description} requested.", {
        "steps": steps, "verification": "launch_requested" if count else "failed", "partial": 0 < count < len(steps)
    }
