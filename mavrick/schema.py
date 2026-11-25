#!/usr/bin/env python3
"""
MAVRICK Action Schemas & Validation Layer
Defines typed tools, strict parameter validation, and model response schemas.
Eliminates arbitrary shell execution in favor of validated tool calls.
"""

import json
import re
import math
import urllib.parse
from enum import Enum
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional, Tuple

class RiskLevel(str, Enum):
    """Action risk classification governing user confirmation requirements."""
    READ_ONLY = "read_only"       # May run immediately
    REVERSIBLE = "reversible"     # Reversible local actions; review card + confirmation by default
    PRIVILEGED = "privileged"     # Elevated/apt/flatpak; confirmation required, visible terminal
    DESTRUCTIVE = "destructive"   # Process termination, irreversible file deletion; confirmation required

class ActionType(str, Enum):
    """Allowlist of supported structured tools."""
    GET_SYSTEM_HEALTH = "get_system_health"
    INSPECT_PROCESSES = "inspect_processes"
    INSPECT_STORAGE = "inspect_storage"
    OPEN_APP = "open_app"
    OPEN_APPS = "open_apps"
    OPEN_URL = "open_url"
    ARRANGE_WINDOWS = "arrange_windows"
    LIST_FILES = "list_files"
    MOVE_FILES = "move_files"
    ORGANIZE_DOWNLOADS = "organize_downloads"
    INSTALL_PACKAGE = "install_package"
    UNINSTALL_PACKAGE = "uninstall_package"
    SET_VOLUME = "set_volume"
    GET_POWER_PROFILE = "get_power_profile"
    OPEN_WORKSPACE = "open_workspace"
    SET_POWER_PROFILE = "set_power_profile"
    CONTROL_MEDIA = "control_media"
    GET_NETWORK_STATUS = "get_network_status"
    CHECK_UPDATES = "check_updates"
    CLEAN_CACHE = "clean_cache"
    LOCK_SESSION = "lock_session"
    RUN_IN_TERMINAL = "run_in_terminal"
    DEFINE_CUSTOM_COMMAND = "define_custom_command"
    DELETE_CUSTOM_COMMAND = "delete_custom_command"
    MCP_TOOL = "mcp_tool"
    UNDO = "undo"

# Mapping each ActionType to its intrinsic RiskLevel
ACTION_RISK_MAP: Dict[ActionType, RiskLevel] = {
    ActionType.GET_SYSTEM_HEALTH: RiskLevel.READ_ONLY,
    ActionType.INSPECT_PROCESSES: RiskLevel.READ_ONLY,
    ActionType.INSPECT_STORAGE: RiskLevel.READ_ONLY,
    ActionType.LIST_FILES: RiskLevel.READ_ONLY,
    ActionType.OPEN_URL: RiskLevel.READ_ONLY,
    ActionType.OPEN_APP: RiskLevel.READ_ONLY,
    ActionType.OPEN_APPS: RiskLevel.READ_ONLY,
    ActionType.RUN_IN_TERMINAL: RiskLevel.READ_ONLY,
    ActionType.ARRANGE_WINDOWS: RiskLevel.READ_ONLY,
    ActionType.GET_NETWORK_STATUS: RiskLevel.READ_ONLY,
    ActionType.CHECK_UPDATES: RiskLevel.READ_ONLY,
    ActionType.CONTROL_MEDIA: RiskLevel.READ_ONLY,
    ActionType.SET_VOLUME: RiskLevel.REVERSIBLE,
    ActionType.GET_POWER_PROFILE: RiskLevel.READ_ONLY,
    ActionType.OPEN_WORKSPACE: RiskLevel.REVERSIBLE,
    ActionType.SET_POWER_PROFILE: RiskLevel.REVERSIBLE,
    ActionType.CLEAN_CACHE: RiskLevel.REVERSIBLE,
    ActionType.ORGANIZE_DOWNLOADS: RiskLevel.REVERSIBLE,
    ActionType.MOVE_FILES: RiskLevel.REVERSIBLE,
    ActionType.DEFINE_CUSTOM_COMMAND: RiskLevel.REVERSIBLE,
    ActionType.DELETE_CUSTOM_COMMAND: RiskLevel.REVERSIBLE,
    ActionType.MCP_TOOL: RiskLevel.REVERSIBLE,
    ActionType.UNDO: RiskLevel.REVERSIBLE,
    ActionType.INSTALL_PACKAGE: RiskLevel.PRIVILEGED,
    ActionType.UNINSTALL_PACKAGE: RiskLevel.PRIVILEGED,
    ActionType.LOCK_SESSION: RiskLevel.DESTRUCTIVE,
}

@dataclass
class ActionRequest:
    """Represents a validated action requested by the AI model or user."""
    action_type: ActionType
    params: Dict[str, Any]
    summary: str
    risk_level: RiskLevel
    impact: str = ""
    affected_targets: List[str] = field(default_factory=list)
    execution_meta: Dict[str, Any] = field(default_factory=dict, compare=False, repr=False)


WORKFLOW_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,47}")
MCP_SERVER_SLUG = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
MCP_TOOL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")
MAX_WORKFLOW_STEPS = 8


@dataclass(frozen=True)
class WorkflowPlan:
    steps: tuple[ActionRequest, ...]
    name: str = ""
    description: str = ""

    def __len__(self):
        return len(self.steps)

    def __iter__(self):
        return iter(self.steps)

    def __getitem__(self, index):
        return self.steps[index]


@dataclass
class ModelResponse:
    """Structured response parsed from AI output."""
    response_type: str  # action, message, plan, calculator, history, or error
    message: Optional[str] = None
    action_request: Optional[ActionRequest] = None
    plan: Optional[WorkflowPlan] = None
    raw_text: str = ""
    validation_error: Optional[str] = None
    source: str = ""

# Dangerous characters prohibited in parameters to eliminate shell/command injection
FORBIDDEN_CHARS_PATTERN = re.compile(r"[\;&\|`\$\(\)\<\>\\!]")
PACKAGE_NAME_PATTERN = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._+-]*")
APP_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_\.\- ]+$")

def validate_action_params(action_str: str, params: Dict[str, Any]) -> Tuple[bool, Optional[str], Optional[ActionRequest]]:
    """
    Validates tool name and parameters against strict schemas.
    Returns (is_valid, error_message, validated_action_request).
    """
    if not isinstance(params, dict):
        return False, "Action parameters must be an object.", None

    # 1. Tool Allowlist Check
    try:
        action_type = ActionType(action_str)
    except ValueError:
        return False, f"Unknown or disallowed action: '{action_str}'. Must be one of {[a.value for a in ActionType]}.", None

    risk_level = ACTION_RISK_MAP[action_type]
    clean_params: Dict[str, Any] = {}
    affected: List[str] = []
    impact = ""

    # 2. Tool-Specific Parameter Schema Validation
    if action_type == ActionType.GET_SYSTEM_HEALTH:
        clean_params = {}
        summary = "Check system health (CPU, RAM, Disk, Battery)"
        impact = "Inspects local hardware and operating system resource metrics."

    elif action_type == ActionType.INSPECT_PROCESSES:
        sort_by = str(params.get("sort_by", "memory")).lower().strip()
        if sort_by not in ["memory", "cpu"]:
            return False, f"Invalid sort_by '{sort_by}'. Must be 'memory' or 'cpu'.", None
        limit = params.get("limit", 10)
        if type(limit) is not int or not 1 <= limit <= 50:
            return False, "Limit must be an integer between 1 and 50.", None

        clean_params = {"sort_by": sort_by, "limit": limit}
        summary = f"Inspect top {limit} processes sorted by {sort_by}"
        impact = f"Reads running process table ordered by {sort_by}."

    elif action_type == ActionType.INSPECT_STORAGE:
        category = str(params.get("category", "apps")).lower().strip()
        if category not in ["apps", "all", "cache", "home"]:
            return False, f"Invalid category '{category}'. Must be 'apps', 'all', 'cache', or 'home'.", None
        limit = params.get("limit", 10)
        if type(limit) is not int or not 1 <= limit <= 50:
            return False, "Limit must be an integer between 1 and 50.", None

        clean_params = {"category": category, "limit": limit}
        summary = f"Inspect storage usage ({category})"
        impact = f"Scans and reports largest storage consumers for category '{category}'."

    elif action_type in (ActionType.OPEN_APP, ActionType.RUN_IN_TERMINAL):
        terminal = action_type == ActionType.RUN_IN_TERMINAL
        key = "command" if terminal else "app_name"
        name = params.get(key, params.get("app_name") if terminal else None)
        if not isinstance(name, str) or not APP_NAME_PATTERN.fullmatch(name.strip()):
            return False, "A valid application name is required.", None
        if params.get("args", []) != []:
            return False, "Application and terminal arguments are unavailable.", None
        in_terminal = params.get("in_terminal", False)
        if not isinstance(in_terminal, bool):
            return False, "in_terminal must be a boolean.", None
        clean_params = {key: name.strip()}
        if not terminal and in_terminal:
            clean_params["in_terminal"] = True
        affected.append(name.strip())
        summary = f"Open {name.strip()}" + (" in Terminal" if terminal or in_terminal else "")
        impact = "Requests launch of an approved application."

    elif action_type == ActionType.OPEN_APPS:
        raw_apps = params.get("apps", [])
        if not isinstance(raw_apps, list) or not 1 <= len(raw_apps) <= MAX_WORKFLOW_STEPS:
            return False, "Parameter 'apps' must list one to eight applications.", None
        cleaned_apps = []
        for item in raw_apps:
            if not isinstance(item, str) or not APP_NAME_PATTERN.fullmatch(item.strip()):
                return False, "A valid application name is required.", None
            cleaned_apps.append(item.strip())
        raw_missing = params.get("missing", [])
        if not isinstance(raw_missing, list) or len(raw_missing) > MAX_WORKFLOW_STEPS:
            return False, "Parameter 'missing' must be a list of up to eight targets.", None
        cleaned_missing = []
        for item in raw_missing:
            if not isinstance(item, str) or not item.strip() or any(ord(ch) < 32 for ch in item):
                return False, "Invalid missing target label.", None
            cleaned_missing.append(item.strip())
        clean_params = {"apps": cleaned_apps}
        if cleaned_missing:
            clean_params["missing"] = cleaned_missing
        affected.extend(cleaned_apps)
        summary = f"Launch {', '.join(cleaned_apps)}"
        impact = "Requests immediate launch of approved applications."

    elif action_type == ActionType.OPEN_URL:
        url = params.get("url", "")
        browser = params.get("browser", "auto")
        try:
            url = validate_web_url(url)
            browser = validate_browser(browser)
        except ValueError as error:
            return False, str(error), None
        clean_params = {"url": url, "browser": browser}
        affected.append(url)
        summary = f"Open {url}"
        impact = f"Requests a browser launch using {browser}."

    elif action_type == ActionType.ARRANGE_WINDOWS:
        left_target = params.get("left_target")
        right_target = params.get("right_target")
        ratio = params.get("ratio", 0.5)
        if any(not isinstance(target, str) or not target.strip()
               for target in (left_target, right_target)):
            return False, "Both window targets are required.", None
        if type(ratio) not in (int, float) or not math.isfinite(ratio) or not 0.1 <= ratio <= 0.9:
            return False, "Window ratio must be between 0.1 and 0.9.", None
        try:
            browser = validate_browser(params.get("browser", "auto"))
        except ValueError as error:
            return False, str(error), None
        clean_params = {"left_target": left_target.strip(), "right_target": right_target.strip(),
                        "ratio": ratio, "browser": browser}
        affected.extend([left_target.strip(), right_target.strip()])
        summary = "Open both window targets"
        impact = "Requests both launches. Window placement is managed by the desktop."

    elif action_type == ActionType.LIST_FILES:
        path = params.get("path", "~/Downloads")
        if not isinstance(path, str) or not path or "\x00" in path:
            return False, "A valid path is required.", None
        max_depth = params.get("max_depth", 1)
        if type(max_depth) is not int or not 1 <= max_depth <= 3:
            return False, "Depth must be an integer between 1 and 3.", None
        clean_params = {"path": path, "max_depth": max_depth}
        affected.append(path)
        summary = f"List files in {path}"
        impact = f"Reads file directory structure for {path}."

    elif action_type == ActionType.ORGANIZE_DOWNLOADS:
        target_dir = params.get("target_dir", "~/Downloads")
        if not isinstance(target_dir, str) or not target_dir.strip() or "\x00" in target_dir:
            return False, "A valid Downloads directory is required.", None
        # Force dry_run=True on initial requests so file organization ALWAYS previews before executing
        clean_params = {"target_dir": target_dir, "dry_run": True}
        affected.append(target_dir)
        summary = "Organize loose downloads into categorized folders"
        impact = "Sorts loose files in Downloads into Images, Documents, Archives, Media, and Code folders. (Reversible with Undo)."

    elif action_type == ActionType.MOVE_FILES:
        sources = params.get("sources", [])
        if not isinstance(sources, list) or not sources:
            return False, "Parameter 'sources' must be a non-empty list of paths.", None
        destination = params.get("destination", "")
        if not isinstance(destination, str) or not destination or "\x00" in destination:
            return False, "Parameter 'destination' is required.", None
        for s in sources:
            if not isinstance(s, str) or not s or "\x00" in s:
                return False, f"Invalid source path '{s}'.", None
        clean_params = {"sources": sources, "destination": destination}
        affected.extend(sources)
        affected.append(destination)
        summary = f"Move {len(sources)} file(s) to {destination}"
        impact = f"Moves files into destination directory. (Reversible with Undo)."

    elif action_type in (ActionType.INSTALL_PACKAGE, ActionType.UNINSTALL_PACKAGE):
        package_name = params.get("package_name")
        manager = params.get("manager", "auto")
        if not isinstance(package_name, str) or not PACKAGE_NAME_PATTERN.fullmatch(package_name):
            return False, "A valid package name is required.", None
        if manager not in ("auto", "apt", "flatpak", "winget", "scoop", "brew"):
            return False, "Choose an approved package manager.", None
        clean_params = {"package_name": package_name, "manager": manager}
        scope = params.get("scope")
        if scope is not None:
            if manager != "flatpak" or scope not in ("user", "system"):
                return False, "Flatpak scope must be user or system.", None
            clean_params["scope"] = scope
        affected.append(package_name)
        verb = "Install" if action_type == ActionType.INSTALL_PACKAGE else "Uninstall"
        summary = f"{verb} {package_name}"
        impact = "Requests a package change using the selected package manager."

    elif action_type == ActionType.SET_VOLUME:
        level = params.get("level")
        mute = params.get("mute")
        delta = params.get("delta")
        if level is not None and (type(level) is not int or not 0 <= level <= 150):
            return False, "Volume level must be an integer between 0 and 150.", None
        if mute is not None and not isinstance(mute, bool):
            return False, "Mute must be a boolean.", None
        if delta is not None and (type(delta) is not int or not 0 < abs(delta) <= 25):
            return False, "Volume adjustment must be between 1 and 25 percentage points.", None
        if sum(value is not None for value in (level, mute, delta)) != 1:
            return False, "Choose one of level, delta, or mute.", None
        clean_params = {key: value for key, value in
                        (("level", level), ("mute", mute), ("delta", delta)) if value is not None}
        if delta is not None:
            summary = f"{'Raise' if delta > 0 else 'Lower'} volume by {abs(delta)} percentage points"
        else:
            summary = f"Set volume to {level}%" if level is not None else ("Mute audio" if mute else "Unmute audio")
        impact = "Changes the default audio output."

    elif action_type == ActionType.GET_POWER_PROFILE:
        summary = "Check current power mode"
        impact = "Reads the current system power profile."

    elif action_type == ActionType.OPEN_WORKSPACE:
        project = params.get("project")
        if project is not None and (not isinstance(project, str) or not project or "\x00" in project):
            return False, "A valid project path is required.", None
        clean_params = {"project": project} if project is not None else {}
        summary = "Open coding workspace"
        impact = "Opens the resolved project in an installed editor and terminal."

    elif action_type == ActionType.SET_POWER_PROFILE:
        profile = str(params.get("profile", "")).lower().strip()
        valid_profiles = ["battery", "balanced", "performance"]
        if profile not in valid_profiles:
            return False, f"Invalid power profile '{profile}'. Must be one of {valid_profiles}.", None
        clean_params = {"profile": profile}
        affected.append(profile)
        summary = f"Set power profile to '{profile}'"
        impact = f"Switches system power profile to '{profile}' via system76-power."

    elif action_type == ActionType.CONTROL_MEDIA:
        act = str(params.get("action", "status")).lower().strip()
        valid_actions = ["play_pause", "pause", "play", "next", "previous", "status"]
        if act not in valid_actions:
            return False, f"Invalid media action '{act}'. Must be one of {valid_actions}.", None
        player = params.get("player")
        if player is not None and (not isinstance(player, str) or not re.fullmatch(r"[a-zA-Z0-9_.-]+", player)):
            return False, "Invalid media player name.", None
        clean_params = {"action": act}
        if player:
            clean_params["player"] = player.lower()
        if act == "status":
            risk_level = RiskLevel.READ_ONLY
            summary = "Check media playback status"
            impact = "Queries active media players for current playback and track information."
        else:
            risk_level = RiskLevel.REVERSIBLE
            summary = act.replace("_", " ").capitalize() + (f" {player}" if player else " playback")
            impact = f"Controls active media playback via playerctl ({act})."

    elif action_type == ActionType.GET_NETWORK_STATUS:
        clean_params = {}
        summary = "Check network status and internet connectivity"
        impact = "Inspects network interface, local IP, default gateway, and tests internet latency."

    elif action_type == ActionType.CHECK_UPDATES:
        clean_params = {}
        summary = "Check for available system updates (APT & Flatpak)"
        impact = "Queries package managers for pending software updates."

    elif action_type == ActionType.CLEAN_CACHE:
        dry_run = params.get("dry_run", True)
        if not isinstance(dry_run, bool):
            return False, "dry_run must be a boolean.", None
        risk_level = RiskLevel.READ_ONLY if dry_run else RiskLevel.DESTRUCTIVE
        clean_params = {"dry_run": dry_run}
        summary = ("Calculate reclaimable cache space (Dry Run)" if dry_run
                   else "Permanently delete thumbnail and APT archive cache files")
        impact = ("Estimates the space used by thumbnail and package cache files." if dry_run
                  else "Deletes eligible thumbnail and APT archive files. This is irreversible.")

    elif action_type == ActionType.LOCK_SESSION:
        clean_params = {}
        summary = "Lock desktop session"
        impact = "Locks user screen immediately via loginctl."

    elif action_type == ActionType.UNDO:
        undo_id = params.get("undo_id")
        if undo_id is not None and (not isinstance(undo_id, str) or not re.fullmatch(r"legacy|[0-9a-f]{32}", undo_id)):
            return False, "Invalid Undo ID.", None
        clean_params = {"undo_id": undo_id} if undo_id else {}
        summary = "Undo last reversible action"
        impact = "Reverts previous file moves / downloads organization."

    elif action_type == ActionType.DEFINE_CUSTOM_COMMAND:
        trigger = str(params.get("trigger", "")).strip().lower()
        if not trigger or len(trigger) < 2 or len(trigger) > 60:
            return False, "Trigger phrase must be between 2 and 60 characters.", None
        if FORBIDDEN_CHARS_PATTERN.search(trigger):
            return False, f"Invalid characters in trigger phrase: '{trigger}'.", None
        target_action = str(params.get("target_action", "")).strip()
        try:
            target_act_type = ActionType(target_action)
        except ValueError:
            return False, f"Target action '{target_action}' is not a valid action tool.", None
        target_params = params.get("params", {})
        if not isinstance(target_params, dict):
            return False, "Target action params must be a dictionary.", None
        cmd_summary = str(params.get("summary", "")).strip() or f"Execute {target_action}"
        clean_params = {
            "trigger": trigger,
            "target_action": target_act_type.value,
            "params": target_params,
            "summary": cmd_summary
        }
        summary = f"Define custom command: '{trigger}' -> {target_action}"
        impact = f"Registers shortcut '{trigger}' to execute {target_action} automatically."

    elif action_type == ActionType.DELETE_CUSTOM_COMMAND:
        trigger = str(params.get("trigger", "")).strip().lower()
        if not trigger:
            return False, "Trigger phrase is required to delete a custom command.", None
        clean_params = {"trigger": trigger}
        summary = f"Delete custom command: '{trigger}'"
        impact = f"Removes custom shortcut trigger '{trigger}' from configuration."

    elif action_type == ActionType.MCP_TOOL:
        server = params.get("server")
        tool = params.get("tool")
        arguments = params.get("arguments", {})
        if not isinstance(server, str) or not MCP_SERVER_SLUG.fullmatch(server):
            return False, "A valid MCP server slug is required.", None
        if not isinstance(tool, str) or not MCP_TOOL_NAME.fullmatch(tool):
            return False, "A valid MCP tool name is required.", None
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return False, "MCP tool 'arguments' must be an object.", None
        try:
            encoded = json.dumps(arguments, ensure_ascii=False)
        except (TypeError, ValueError, RecursionError):
            return False, "MCP tool 'arguments' must be JSON-serializable.", None
        if len(encoded.encode("utf-8")) > 64 * 1024 or "\x00" in encoded:
            return False, "MCP tool arguments exceed limits or contain invalid bytes.", None
        clean_params = {"server": server, "tool": tool, "arguments": json.loads(encoded)}
        target = f"mcp · {server}/{tool}"
        affected.append(target)
        summary = target
        impact = f"Invokes external MCP tool '{tool}' on configured server '{server}'."

    req = ActionRequest(
        action_type=action_type,
        params=clean_params,
        summary=summary,
        risk_level=risk_level,
        impact=impact,
        affected_targets=affected
    )
    return True, None, req


def validate_workflow_action(action_type, params) -> ActionRequest:
    if not isinstance(action_type, str):
        raise ValueError("Each step needs an action name.")
    valid, error, action = validate_action_params(action_type, params)
    if not valid:
        raise ValueError(error)
    accepted = set(action.params)
    if action.action_type in (ActionType.OPEN_APP, ActionType.RUN_IN_TERMINAL):
        accepted.update(("args", "in_terminal"))
        if action.action_type == ActionType.RUN_IN_TERMINAL and "command" not in params:
            accepted.add("app_name")  # Legacy terminal utility alias.
    if action.action_type == ActionType.OPEN_APPS:
        accepted.add("missing")
    if action.action_type == ActionType.MCP_TOOL:
        accepted.add("arguments")
    if extras := set(params) - accepted:
        raise ValueError(f"Unexpected parameter(s): {', '.join(sorted(map(str, extras)))}.")
    if action.action_type == ActionType.ORGANIZE_DOWNLOADS and "dry_run" in params and params["dry_run"] is not True:
        raise ValueError("Review Downloads before applying it. Omit dry_run or set it to true.")
    if action.action_type == ActionType.DEFINE_CUSTOM_COMMAND:
        nested = action.params
        if nested["target_action"] in (ActionType.DEFINE_CUSTOM_COMMAND, ActionType.DELETE_CUSTOM_COMMAND):
            raise ValueError("A workflow cannot define a nested configuration edit.")
        validate_workflow_action(nested["target_action"], nested["params"])
    return action


def parse_workflow_plan(steps, *, name="", description="") -> WorkflowPlan:
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_WORKFLOW_STEPS:
        raise ValueError(f"A workflow needs one to {MAX_WORKFLOW_STEPS} action steps.")
    if not isinstance(name, str) or (name and not WORKFLOW_SLUG.fullmatch(name)):
        raise ValueError("Use a lowercase workflow slug (letters, digits, and hyphens).")
    if not isinstance(description, str) or len(description) > 240:
        raise ValueError("A workflow description must be text of at most 240 characters.")
    actions, errors = [], []
    for index, step in enumerate(steps, 1):
        try:
            if not isinstance(step, dict) or set(step) - {"type", "action", "params", "summary"} or step.get("type", "action") != "action":
                raise ValueError("Expected a structured action with typed parameters.")
            actions.append(validate_workflow_action(step.get("action"), step.get("params", {})))
        except ValueError as error:
            errors.append(f"Step {index}: {error}")
    if errors:
        raise ValueError("\n".join(errors))
    return WorkflowPlan(tuple(actions), name, description)


BROWSERS = {"auto", "brave", "brave-browser", "firefox", "google-chrome", "chromium", "xdg-open"}


def validate_browser(browser):
    if not isinstance(browser, str) or browser not in BROWSERS:
        raise ValueError("Choose an approved browser.")
    return browser


def validate_web_url(url):
    if not isinstance(url, str) or not url or any(char.isspace() or ord(char) < 32 for char in url):
        raise ValueError("Enter a valid HTTP or HTTPS URL.")
    parsed = urllib.parse.urlsplit(url)
    if not parsed.scheme and "." in url and not url.startswith(("/", "-")):
        url = "https://" + url
        parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Enter an HTTP or HTTPS URL with a host and without credentials.")
    return url
