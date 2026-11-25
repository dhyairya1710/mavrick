#!/usr/bin/env python3
"""
Route exact commands, query the configured provider, and validate its proposals.
"""

import re
import json
import socket
import time
from typing import Dict, Any, Optional, Callable

from dataclasses import asdict

from .config import (AGY_BIN, AITimeouts, DAEMON_SOCKET, FALLBACK_DAEMON_SOCKET, ProviderSettings,
                     configured_api_keys, get_custom_command, list_custom_commands, load_ai_timeouts,
                     load_provider_settings, validate_timeout)
from .schema import ModelResponse, parse_workflow_plan, validate_action_params
from .history import load_context, redact_secrets
from .ipc import FrameReader, RequestCancelled
from .platform import get_platform
from .policy import APP_ALIASES
from .providers import create_provider
from .providers import agy
from .providers.base import ProviderError, unwrap_json
from .router import route_local
from .mcp import mcp_prompt_summary


_PLAN_INSTRUCTIONS = (
    '\nFor multiple distinct actions, use {"type":"plan","plan":'
    '[{"action":"<tool>","params":{}}]}. Mavrick validates every step and shows the workflow for review.'
)


def daemon_socket_status() -> str:
    return agy.probe_daemon((DAEMON_SOCKET, FALLBACK_DAEMON_SOCKET), reader_type=FrameReader)[0]


def check_ai_health(settings: ProviderSettings | None = None, timeouts: AITimeouts | None = None) -> Dict[str, Any]:
    name = "agy"
    try:
        config = settings or load_provider_settings()
        name = config.name
        provider = _provider(config, timeouts or load_ai_timeouts())
        try:
            return {**asdict(provider.health_check()), "provider": name}
        finally:
            provider.close()
    except (ValueError, ProviderError) as error:
        return {"status": "offline", "display": "AI configuration error", "provider": name,
                "error": redact_secrets(str(error))}


def build_system_prompt() -> str:
    """
    Generates strict system prompt requiring structured JSON action tools.
    Explicitly forbids raw shell code execution.
    """
    ctx = redact_secrets(load_context())
    change = ctx.get("last_verified_change", {})
    ctx_str = json.dumps(change) if change else "No verified change"
    mcp_desc = mcp_prompt_summary()
    if mcp_desc:
        mcp_desc = "\n<MCP_UNTRUSTED_TOOL_DECLARATIONS>\n" + mcp_desc + "\n</MCP_UNTRUSTED_TOOL_DECLARATIONS>\n"
    adapter = get_platform()
    if adapter.name != "linux":
        package_manager = adapter.package_manager_name()
        moves = ("move_files {sources, destination}, undo {}" if adapter.capabilities.atomic_moves
                 else "File move and undo apply are unavailable.")
        return f"""You are MAVRICK, a local desktop assistant for {adapter.name}.
Current context: {ctx_str}.
Return a single JSON object with type "message" and a message, or type "action" with an action name and params object.
Use only these actions: get_system_health {{}}, open_app {{app_name}}, open_url {{url}}, arrange_windows
{{left_target, right_target}}, list_files {{path, max_depth}}, organize_downloads {{}}, {moves}
Use clean_cache {{dry_run: true}} to preview Mavrick's cache. For package work use check_updates {{}},
install_package {{package_name, manager}}, or uninstall_package {{package_name, manager}}.
The available package manager is {package_manager}. Use an exact package ID and ask when the target is ambiguous.
Use only installed app IDs and validated parameters. Never suggest shell commands, executable paths or interpreter arguments.
Explain when a requested operation is unavailable.{mcp_desc}""" + _PLAN_INSTRUCTIONS

    tools_desc = """Supported Structured Tools:
1. get_system_health: {}
   Inspects CPU, RAM, Disk, Battery, and GPU (NVIDIA/AMD) telemetry.
2. inspect_storage: {"category": "apps"|"all"|"cache"|"home", "limit": 1-50}
   Inspects disk space and storage usage of installed applications, flatpaks, packages, and user app data. Use this whenever the user asks "which app has most storage", "largest apps", "storage breakdown", "what is taking up storage", or "app disk usage".
3. inspect_processes: {"sort_by": "memory"|"cpu", "limit": 1-50}
   Inspects active processes.
4. open_app: {"app_name": "<approved app ID>", "in_terminal": false|true}
   Launches an application. Use "claude desktop" for the Claude graphical app.
5. run_in_terminal: {"command": "<approved utility ID e.g. agy, htop, btop, ncdu>"}
   Opens an interactive terminal running an approved utility. Use command "claude-code" for Claude Code in Terminal.
6. open_url: {"url": "<valid http/https URL>", "browser": "auto"|"brave"|"firefox"}
   Opens web address.
7. arrange_windows: {"left_target": "<url or app>", "right_target": "<url or app>", "ratio": 0.5}
   Requests two app or browser launches. Window positioning is left to the desktop.
8. list_files: {"path": "<path e.g. ~/Downloads>", "max_depth": 1}
   Lists directory contents safely with file sizes and counts.
9. organize_downloads: {}
   Previews loose files in Downloads into categorized folders. (Always dry_run: true first).
10. move_files: {"sources": ["<path>"], "destination": "<path>"}
   Moves files safely within user home.
11. install_package: {"package_name": "<package>", "manager": "auto"|"apt"|"flatpak"}
    Installs software package via interactive terminal.
12. uninstall_package: {"package_name": "<package>", "manager": "auto"|"apt"|"flatpak"}
    Uninstalls software package via interactive terminal.
13. set_volume: one of {"level": 0-150}, {"delta": -5}, or {"mute": true|false}
    Sets audio sink volume level.
14. set_power_profile: {"profile": "battery"|"balanced"|"performance"}
    Sets Pop!_OS power profile via system76-power (or powerprofilesctl).
15. control_media: {"action": "pause"|"play"|"play_pause"|"next"|"previous"|"status", "player": "<optional player name>"}
    Controls media playback via playerctl or inspects current song and artist.
16. get_network_status: {}
    Checks network interface, local IP, gateway, and ping test latency.
17. check_updates: {}
    Checks for available APT and Flatpak package updates.
18. clean_cache: {"dry_run": true|false}
    Calculates reclaimable space or cleans thumbnails and APT cache.
19. lock_session: {}
    Locks desktop screen.
20. define_custom_command: {"trigger": "<short_phrase>", "target_action": "<tool_name>", "params": {...}, "summary": "..."}
    Teaches MAVRICK a custom command or creates a shortcut alias. Use when user says "when I say X do Y", "teach mavrick: alias X to Y", or "create shortcut X for Y".
21. delete_custom_command: {"trigger": "<phrase>"}
    Removes a custom command shortcut.
22. undo: {}
    Reviews file moves to restore.
23. get_power_profile: {}
    Reads the current power mode. Use for questions about the current setting.
24. open_workspace: {"project": "<optional project folder path>"}
    Resolve a project folder, editor and terminal, and show the launch steps before opening."""

    customs = list_custom_commands()
    custom_desc = ""
    if customs:
        custom_lines = ["Active User Custom Shortcuts (Fast Path):"]
        for trig, cdef in customs.items():
            custom_lines.append(f"  • '{trig}' -> {cdef.get('action')} ({cdef.get('summary', '')})")
        custom_desc = "\n" + "\n".join(custom_lines) + "\n"

    human_intent_guide = """NATURAL LANGUAGE HUMAN INTENT TRAINING EXAMPLES:
1. System & Performance:
   - "why is my laptop heating up" / "why is my fan loud" / "cpu spikes" -> {"action": "inspect_processes", "params": {"sort_by": "cpu", "limit": 10}, "summary": "Checking top CPU-consuming processes causing fan noise"}
   - "what is eating my ram" / "memory hogs" / "why is memory full" -> {"action": "inspect_processes", "params": {"sort_by": "memory", "limit": 10}, "summary": "Inspecting top memory-consuming processes"}
   - "how much free disk do i have" / "how is my system doing" / "system stats" -> {"action": "get_system_health", "params": {}, "summary": "Gathering system health telemetry"}
   - "which app have most storage used" / "largest apps" / "what is taking up storage" -> {"action": "inspect_storage", "params": {"category": "apps", "limit": 10}, "summary": "Inspecting application storage breakdown"}
   - "free up some disk space" / "clear cache" / "clean junk" -> {"action": "clean_cache", "params": {"dry_run": true}, "summary": "Calculating reclaimable cache and junk space"}

2. Hardware & Power:
   - "game mode" / "high performance" / "max power" -> {"action": "set_power_profile", "params": {"profile": "performance"}, "summary": "Switching to performance power profile"}
   - "battery saver" / "conserve battery" / "quiet mode" -> {"action": "set_power_profile", "params": {"profile": "battery"}, "summary": "Switching to battery saver profile"}
   - "balanced power" / "normal power mode" -> {"action": "set_power_profile", "params": {"profile": "balanced"}, "summary": "Switching to balanced power profile"}
   - "gpu stats" / "graphics card temp" -> {"action": "get_system_health", "params": {}, "summary": "Checking GPU and system telemetry"}

3. Media & Audio:
   - "pause Spotify" -> {"action": "control_media", "params": {"action": "pause", "player": "spotify"}}
   - "resume Spotify" -> {"action": "control_media", "params": {"action": "play", "player": "spotify"}}
   - "skip this song" / "next track" -> {"action": "control_media", "params": {"action": "next"}, "summary": "Skipping to next track"}
   - "what song is playing" / "current track" -> {"action": "control_media", "params": {"action": "status"}, "summary": "Inspecting current media playback"}
   - "turn down the volume" / "lower volume" -> {"action": "set_volume", "params": {"delta": -5}, "summary": "Lowering system audio volume"}
   - "mute" / "silence" -> {"action": "set_volume", "params": {"mute": true}, "summary": "Muting system audio"}
   - "crank volume to 80" -> {"action": "set_volume", "params": {"level": 80}, "summary": "Setting volume to 80%"}

4. Network & Updates:
   - "check wifi" / "why is internet slow" / "am i connected to internet" / "ping test" -> {"action": "get_network_status", "params": {}, "summary": "Checking network interface, local IP, and ping latency"}
   - "any updates" / "update my system" / "check packages" -> {"action": "check_updates", "params": {}, "summary": "Checking for APT and Flatpak updates"}

5. Desktop & Workflows:
   - "clean up my downloads folder" / "downloads is messy" -> {"action": "organize_downloads", "params": {"dry_run": true}, "summary": "Generating preview of categorized downloads"}
   - "put brave on left and terminal on right" -> {"action": "arrange_windows", "params": {"left_target": "brave", "right_target": "cosmic-term"}}
   - "open files and Claude in split screen" -> {"action": "arrange_windows", "params": {"left_target": "files", "right_target": "claude"}}
   - For split-screen requests, keep both targets in one arrange_windows action.
   - "open terminal running htop" / "run agy in terminal" -> {"action": "run_in_terminal", "params": {"command": "htop", "args": []}, "summary": "Opening terminal with requested CLI utility"}
   - "lock my pc" / "lock screen" / "stepping away" -> {"action": "lock_session", "params": {}, "summary": "Locking desktop screen"}
   - "undo that" / "revert last move" -> {"action": "undo", "params": {}, "summary": "Reverting last file operation"}
   - "when i say 'chill' switch to battery saver" -> {"action": "define_custom_command", "params": {"trigger": "chill", "target_action": "set_power_profile", "params": {"profile": "battery"}, "summary": "Switch to battery saver"}, "summary": "Registered custom shortcut 'chill'"}"""

    return f"""You are MAVRICK, a production AI operating system assistant for Pop!_OS (COSMIC Desktop, Linux x86_64, Wayland).
Current Context: [{ctx_str}].

CRITICAL SECURITY RULES:
1. You must ALWAYS return a single valid JSON object.
2. NEVER output raw bash, shell scripts, terminal commands, or markdown-wrapped shell code.
3. Every operation on the computer MUST use one of the supported structured tools listed below.
4. When asked to run a command or tool in terminal (e.g. "run agy in terminal", "open agy", "run htop"), use "run_in_terminal" with command "agy" or the tool name.

{tools_desc}
{mcp_desc}{custom_desc}
{human_intent_guide}

JSON Output Format:
If an action should be taken:
{{
  "type": "action",
  "action": "<tool_name>",
  "params": {{ ... }},
  "summary": "<Concise 1-sentence description of what will be done>"
}}

If purely informational or conversational (e.g. answering a question, greeting, general knowledge):
{{
  "type": "message",
  "message": "<Direct, polite, concise answer>"
}}

Anaphoric Context Resolution:
Ask for clarification when a target is ambiguous. Never infer a package name from an app launch or an unsuccessful change.

Output ONLY the raw JSON object. Do not add conversational text outside the JSON.""" + _PLAN_INSTRUCTIONS

def parse_model_response(raw_text: str, *, allow_plain_message: bool = True) -> ModelResponse:
    """
    Parses and validates model response against strict JSON schema.
    Treats all model output as untrusted data.
    """
    text = unwrap_json(raw_text)
    if not text:
        return ModelResponse(response_type="error", validation_error="Empty model response.", raw_text=raw_text)

    try:
        data = json.loads(text)
    except (ValueError, RecursionError) as e:
        # Check if the model generated a message directly without JSON wrapping
        if allow_plain_message and "{" not in text:
            return ModelResponse(response_type="message", message=text, raw_text=raw_text)
        return ModelResponse(
            response_type="error",
            validation_error=f"Model response was not valid JSON: {e}",
            raw_text=raw_text
        )

    if not isinstance(data, dict):
        return ModelResponse(response_type="error", validation_error="JSON root must be an object.", raw_text=raw_text)

    rtype = data.get("type", "message")
    if not allow_plain_message and "type" not in data:
        return ModelResponse("error", validation_error="JSON response is missing its type.", raw_text=raw_text)
    if rtype == "message":
        msg = data.get("message", "")
        if not allow_plain_message and not isinstance(data.get("message"), str):
            return ModelResponse("error", validation_error="Message response needs text.", raw_text=raw_text)
        return ModelResponse(response_type="message", message=str(msg), raw_text=raw_text)

    if rtype == "action":
        if not isinstance(data.get("action"), str):
            return ModelResponse("error", validation_error="Each action needs a tool name.", raw_text=raw_text)
        valid, error, action = validate_action_params(data["action"], data.get("params", {}))
        if not valid:
            return ModelResponse("error", validation_error=f"Action validation rejected: {error}", raw_text=raw_text)
        return ModelResponse("action", action_request=action, raw_text=raw_text)
    if rtype == "plan":
        try:
            plan = parse_workflow_plan(data.get("plan"))
        except ValueError as error:
            return ModelResponse("error", validation_error=str(error), raw_text=raw_text)
        message = "Proposed steps\n" + "\n".join(f"{i}. {action.summary}" for i, action in enumerate(plan, 1))
        return ModelResponse("plan", message=message, plan=plan, raw_text=raw_text)

    return ModelResponse(
        response_type="error",
        validation_error=f"Unknown response type '{rtype}'.",
        raw_text=raw_text
    )

LOCAL_COMMANDS = {
    "organize downloads": ("organize_downloads", {}),
    "undo": ("undo", {}),
    "undo that": ("undo", {}),
    "current power mode": ("get_power_profile", {}),
    "what power mode am i on": ("get_power_profile", {}),
    "turn it down a little": ("set_volume", {"delta": -5}),
    "turn it up a little": ("set_volume", {"delta": 5}),
    "mute": ("set_volume", {"mute": True}),
    "mute all": ("set_volume", {"mute": True}),
    "unmute": ("set_volume", {"mute": False}),
    "open my coding workspace": ("open_workspace", {}),
    "system health": ("get_system_health", {}),
    "gpu stats": ("get_system_health", {}),
    "what is eating my ram": ("inspect_processes", {"sort_by": "memory"}),
    "network status": ("get_network_status", {}),
    "check updates": ("check_updates", {}),
}


def _local_intent(prompt):
    text = prompt.strip().rstrip("!")
    phrase = " ".join(text.lower().rstrip("?.").split())
    if phrase in LOCAL_COMMANDS:
        return LOCAL_COMMANDS[phrase]
    # Handle explicit two-target requests before a model can reduce them to one launch.
    pair = re.fullmatch(r"open\s+(.+?)\s+and\s+(.+?)\s+in\s+split[ -]+screen[?.]?", text, re.IGNORECASE)
    if not pair:
        pair = re.fullmatch(r"split[ -]+screen\s+(?:with\s+)?(.+?)\s+and\s+(.+)", text, re.IGNORECASE)
    if pair:
        left, right = pair.groups()
        browser = re.fullmatch(
            r"(.+?)\s+in\s+(brave|brave-browser|firefox|google-chrome|chromium)[?.]?",
            right, re.IGNORECASE)
        websites = {"youtube": "https://www.youtube.com", "reddit": "https://www.reddit.com"}
        if browser:
            right = browser[1]
        right = right.rstrip("?.") if "://" not in right else right
        if any(len(target.split()) > 1 and target.casefold() not in APP_ALIASES for target in (left, right)):
            return None
        params = {"left_target": websites.get(left.casefold(), left),
                  "right_target": websites.get(right.casefold(), right)}
        if browser:
            params["browser"] = browser[2].lower()
        return "arrange_windows", params
    claude = re.fullmatch(
        r"(open|launch|start|run) (?:claude|calude)(?: (app|desktop|code|terminal))?( in(?: the)? terminal)?",
        phrase)
    if claude:
        verb, variant, terminal = claude.groups()
        if terminal or variant in ("code", "terminal") or (variant is None and verb == "run"):
            return "run_in_terminal", {"command": "claude-code"}
        return "open_app", {"app_name": "claude desktop"}
    media = re.fullmatch(r"(pause|resume|play)(?: ([a-z0-9][a-z0-9_.-]*))?", phrase)
    if media:
        verb, player = media.groups()
        details = {"action": "pause" if verb == "pause" else "play"}
        if player and player not in ("music", "media", "playback"):
            details["player"] = player
        return "control_media", details
    if phrase in ("uninstall that", "uninstall it", "remove that app", "uninstall the app"):
        previous = load_context().get("last_verified_change", {})
        if previous.get("action") != "install_package" or not previous.get("package_name"):
            return None, {}
        params = {key: previous[key] for key in ("package_name", "manager") if key in previous}
        if previous.get("scope"):
            params["scope"] = previous["scope"]
        return "uninstall_package", params
    package = re.fullmatch(r"(install|uninstall) ([A-Za-z0-9][A-Za-z0-9._+-]*)(?: (?:from|via) (flathub|apt|user flatpak|system flatpak))?",
                           prompt.strip().rstrip("?.!"), re.IGNORECASE)
    if package:
        verb, name, backend = package.groups()
        if name.lower() not in ("that", "it", "this", "the"):
            name = "code" if name.lower() in ("vscode", "vs-code") else name
            params = {"package_name": name.lower() if "." not in name else name}
            if backend:
                backend = backend.lower()
                params["manager"] = "apt" if backend == "apt" else "flatpak"
                if backend.endswith(" flatpak"):
                    params["scope"] = backend.split()[0]
            return f"{verb.lower()}_package", params
    workspace = re.fullmatch(r"open (?:my )?coding workspace (?:at|in) (.+)", prompt.strip(), re.IGNORECASE)
    if workspace:
        project = workspace[1]
        if not re.search(r"\s(?:and|then)\s+(?:open|close|clean|move|delete|install|uninstall|find)\s", project, re.I):
            return "open_workspace", {"project": project}
    return None


def _read_events(connection, started, deadline, cancelled, *, negotiate):
    """Compatibility entry for callers exercising the NDJSON timeout handshake."""
    return agy.read_events(connection, started, deadline, cancelled, negotiate=negotiate, reader_type=FrameReader)


def _query_oneshot(prompt, system_prompt, stream_callback, cancelled, timing: AITimeouts):
    """Compatibility adapter for the Antigravity CLI fallback."""
    try:
        result = agy.query_oneshot(prompt, system_prompt, stream_callback, cancelled, timing, binary=AGY_BIN)
    except ProviderError as error:
        return ModelResponse("error", validation_error=str(error), source="agy")
    return parse_model_response(result.text)


def instant_response(prompt: str, *, cached_only: bool = False) -> ModelResponse | None:
    match = route_local(prompt, cached_only=cached_only)
    if not match:
        return None
    if match.kind == "app":
        result = parse_model_response(json.dumps({"type": "action", "action": "open_app",
                                                  "params": {"app_name": match.value}}))
    elif match.kind == "multi_app":
        params = {"apps": list(match.apps)}
        if match.missing:
            params["missing"] = list(match.missing)
        result = parse_model_response(json.dumps({"type": "action", "action": "open_apps", "params": params}))
    elif match.kind == "undo":
        result = parse_model_response('{"type": "action", "action": "undo"}')
    elif match.kind == "workflow":
        return ModelResponse("plan", plan=match.plan, source="instant_workflow")
    elif match.kind in ("calculation_error", "workflow_missing"):
        result = ModelResponse("error", validation_error=match.value)
    else:
        result = ModelResponse(match.kind, message=match.value)
    result.source = "instant"
    return result


def _provider(config, timing, daemon_timeout=None, query_limit=None):
    if config.name == "agy":
        return create_provider(config, timing, binary=AGY_BIN,
                               paths=(DAEMON_SOCKET, FALLBACK_DAEMON_SOCKET), reader_type=FrameReader,
                               daemon_timeout=daemon_timeout, query_limit=query_limit, force_model=config.require_model)
    return create_provider(config, timing)


def query_model(prompt: str, stream_callback: Optional[Callable[[str], None]] = None, *,
                cancel_token=None, daemon_timeout: float | None = None,
                timeouts: AITimeouts | None = None, query_limit: float | None = None,
                provider_settings: ProviderSettings | None = None) -> ModelResponse:
    """Resolve exact intents or validate one provider's reply against the action schema."""
    cancelled = cancel_token.is_set if cancel_token is not None else None
    name = "agy"
    secrets = None
    try:
        if cancelled and cancelled():
            raise RequestCancelled("AI request cancelled.")
        instant = instant_response(prompt)
        if instant is not None:
            return instant
        direct = _local_intent(prompt)
        if direct:
            action, params = direct
            if action is None:
                return ModelResponse("message", message="Which package should I uninstall? Give its name and package manager.",
                                     source="instant")
            result = parse_model_response(json.dumps({"type": "action", "action": action, "params": params}))
            result.source = "instant"
            return result
        custom = get_custom_command(prompt)
        if custom:
            result = parse_model_response(json.dumps({"type": "action", **custom}))
            result.source = "instant"
            return result

        config = provider_settings or load_provider_settings()
        name = config.name
        secrets = (*configured_api_keys(), config.api_key) if config.api_key else configured_api_keys()
        timing = timeouts or load_ai_timeouts()
        legacy_timeout = validate_timeout(daemon_timeout, "daemon_timeout") if daemon_timeout is not None else None
        provider = _provider(config, timing, legacy_timeout, query_limit)
        try:
            callback = (lambda text: stream_callback(redact_secrets(text, secrets))) if stream_callback else None
            instructions = build_system_prompt()
            provider_prompt = redact_secrets(prompt, secrets) if name != "agy" else prompt
            reply = provider.query(provider_prompt, instructions, stream_callback=callback, cancelled=cancelled)
        finally:
            provider.close()
        result = parse_model_response(reply.text, allow_plain_message=name == "agy")
        result.source = name
        result.raw_text, result.message, result.validation_error = redact_secrets(
            (result.raw_text, result.message, result.validation_error), secrets
        )
        return result
    except (ValueError, ProviderError, RequestCancelled) as error:
        return ModelResponse("error", validation_error=redact_secrets(str(error), secrets), source=name)
