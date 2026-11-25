#!/usr/bin/env python3
"""
MAVRICK Command Line Interface (CLI)
Provides command line interaction with the unified ActionExecutor,
health diagnostics, activity history, and uninstallation.
"""

import sys
import os
import signal
import threading
import uuid
import argparse
import getpass
from importlib.util import find_spec
from typing import Optional

try:
    from .ai import query_model, check_ai_health, daemon_socket_status
    from .executor import ActionExecutor, approve_action, approve_files
    from .schema import RiskLevel, ActionType, ActionRequest
    from .tools.files import display_path, rename_review_row
    from .tools.apps import app_index_status, instant_app_index
    from .history import (
        append_history,
        load_history,
        clear_history_and_context,
        search_history,
        redact_secrets
    )
    from .config import (
        is_wayland_session,
        is_cosmic_desktop,
        ensure_secure_storage,
        load_ai_timeouts,
        load_provider_settings,
        load_ipc_settings,
        load_user_config,
        configure_user_preferences,
        PROVIDER_LABELS,
        PROVIDER_PRESETS,
        CONFIG_FILE,
        MCP_SERVERS_FILE,
    )
    from .platform import get_platform, service_template_for_platform
    from .policy import evaluate_mcp_tool_risk
    from .workflows import approve_workflow, declared_workflow_risk, describe_workflow, workflow_catalog
    from .mcp import (
        approve_mcp_server,
        discover_server_tools,
        is_grant_valid,
        load_mcp_grants,
        load_mcp_registry,
    )
except ImportError:
    parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if parent_dir not in sys.path:
        sys.path.insert(0, parent_dir)
    from mavrick.ai import query_model, check_ai_health, daemon_socket_status
    from mavrick.executor import ActionExecutor, approve_action, approve_files
    from mavrick.schema import RiskLevel, ActionType, ActionRequest
    from mavrick.tools.files import display_path, rename_review_row
    from mavrick.tools.apps import app_index_status, instant_app_index
    from mavrick.history import (
        append_history,
        load_history,
        clear_history_and_context,
        search_history,
        redact_secrets
    )
    from mavrick.config import (
        is_wayland_session,
        is_cosmic_desktop,
        ensure_secure_storage,
        load_ai_timeouts,
        load_provider_settings,
        load_ipc_settings,
        load_user_config,
        configure_user_preferences,
        PROVIDER_LABELS,
        PROVIDER_PRESETS,
        CONFIG_FILE,
        MCP_SERVERS_FILE,
    )
    from mavrick.platform import get_platform, service_template_for_platform
    from mavrick.policy import evaluate_mcp_tool_risk
    from mavrick.workflows import approve_workflow, declared_workflow_risk, describe_workflow, workflow_catalog
    from mavrick.mcp import (
        approve_mcp_server,
        discover_server_tools,
        is_grant_valid,
        load_mcp_grants,
        load_mcp_registry,
    )

CLR_RESET = "\033[0m"
CLR_BOLD = "\033[1m"
CLR_LIME = "\033[38;2;99;254;19m"
CLR_CYAN = "\033[38;2;56;189;248m"
CLR_AMBER = "\033[38;2;251;191;36m"
CLR_RED = "\033[38;2;239;68;68m"
CLR_SLATE = "\033[38;2;148;163;184m"

def print_banner():
    print(f"{CLR_BOLD}MAVRICK{CLR_RESET} · Production AI Operating System Assistant (Pop!_OS)")

def run_health_check(settings=None, timeouts=None):
    print_banner()
    print("─────────────────────────────────────────────────────────")
    health = check_ai_health(settings, timeouts) if settings or timeouts else check_ai_health()
    status = health.get("status")
    if status == "connected_daemon":
        print(f"  AI Backend:    {CLR_LIME}● Connected{CLR_RESET} ({health.get('display')})")
        print(f"  Daemon Memory: {health.get('rss_mb', 0):.1f} MB RSS")
    elif status == "fallback_cli":
        print(f"  AI Backend:    {CLR_AMBER}● CLI Fallback{CLR_RESET} ({health.get('display')})")
    elif status == "connected_provider":
        print(f"  AI Backend:    {CLR_LIME}● Connected{CLR_RESET} ({health.get('display')})")
    else:
        print(f"  AI Backend:    {CLR_RED}● Disconnected{CLR_RESET} ({health.get('error', 'Offline')})")

    # Environment
    wayland = is_wayland_session()
    cosmic = is_cosmic_desktop()
    host = get_platform().name
    session = (f"{'Wayland' if wayland else 'X11'} ({'COSMIC' if cosmic else 'Standard'})"
               if host == "linux" else host)
    print(f"  Session:       {session}")

    # Storage
    ensure_secure_storage()
    print(f"  Storage:       {CLR_LIME}✓ Verified{CLR_RESET} (Mode 0700 / 0600 enforced)")
    print("─────────────────────────────────────────────────────────")

def _mcp_doctor_summary() -> str:
    registry = load_mcp_registry(refresh=True)
    if not registry.servers and not registry.errors:
        return "none configured"
    grants = load_mcp_grants(refresh=True)
    enabled_cfgs = [cfg for cfg in registry.servers.values() if cfg.enabled]
    approved = sum(1 for cfg in enabled_cfgs if is_grant_valid(cfg, grants.get(cfg.name))[0])
    pending = len(enabled_cfgs) - approved
    trusted = sum(1 for cfg in enabled_cfgs if cfg.trusted)
    summary = f"{len(registry.servers)} configured ({len(enabled_cfgs)} enabled, {approved} approved, {trusted} trusted)"
    if pending:
        summary += f" · {pending} approval required"
    if registry.errors:
        summary += f" · {len(registry.errors)} config error(s)"
    return redact_secrets(summary)


def _list_mcp_servers() -> int:
    registry = load_mcp_registry(refresh=True)
    if not registry.servers and not registry.errors:
        print(f"No MCP servers configured ({MCP_SERVERS_FILE}).")
        return 0
    grants = load_mcp_grants(refresh=True)
    had_errors = bool(registry.errors)
    for name, cfg in sorted(registry.servers.items()):
        trust = "destructive" if cfg.destructive else ("trusted" if cfg.trusted else "untrusted")
        if not cfg.enabled:
            print(f"{name} · disabled · {trust}")
            continue
        grant = grants.get(name)
        ok, reason = is_grant_valid(cfg, grant)
        if not ok:
            print(redact_secrets(f"{name} · approval required · {trust} · {reason}"))
            continue
        snap = discover_server_tools(name, refresh=True)
        if snap.error:
            had_errors = True
            print(redact_secrets(f"{name} · error · {trust} · {snap.error}"))
            continue
        era = snap.protocol_version or snap.era or "unknown"
        print(f"{name} · {era} · {trust} · {len(snap.tools)} {'tool' if len(snap.tools) == 1 else 'tools'}")
        for tool in snap.tools:
            try:
                _risk, tier = evaluate_mcp_tool_risk(cfg, tool, grant=grant)
                tier_label = tier.value
            except ValueError:
                tier_label = "blocked"
            clean_desc = " ".join(tool.description.split()) if tool.description else ""
            suffix = f" — {clean_desc}" if clean_desc else ""
            print(redact_secrets(f"  - {tool.name} [{tier_label}]{suffix}"))
    for error in registry.errors:
        print(redact_secrets(error))
    return 1 if had_errors else 0


def _approve_mcp_server_cli(server_name: str, *, grant_env: str | None = None, grant_readonly: str | None = None) -> int:
    env_arg = None if grant_env is None else tuple(v.strip() for v in grant_env.split(",") if v.strip())
    ro_arg = None if grant_readonly is None else tuple(v.strip() for v in grant_readonly.split(",") if v.strip())
    grant = approve_mcp_server(server_name, grant_env=env_arg, approve_readonly_tools=ro_arg)
    env_desc = ",".join(grant.granted_env) if grant.granted_env else "none"
    ro_desc = ",".join(sorted(grant.approved_readonly_tools)) if grant.approved_readonly_tools else "none"
    print(redact_secrets(
        f"Approved MCP server '{grant.server}' (mode 0600): exe={grant.executable}, "
        f"env={env_desc}, readonly_tools={ro_desc}, sha256={grant.launch_hash[:12]}"
    ))
    return 0


def run_doctor(settings, timeouts) -> None:
    instant_app_index()
    daemon = daemon_socket_status()
    health = check_ai_health(settings, timeouts)
    label = PROVIDER_LABELS[settings.name]
    platform = get_platform()
    report = [
        "Mavrick doctor",
        f"Platform: {platform.name}",
        f"IPC transport: {load_ipc_settings().transport}",
        f"Package manager: {platform.package_manager_name()}",
        f"Provider: {label}",
        f"Model: {settings.model}",
        f"Timeouts: init {timeouts.init_timeout:g}s | query {timeouts.query_timeout:g}s | fallback {timeouts.fallback_timeout:g}s",
        f"Daemon socket: {daemon}",
        f"Local router cache: {app_index_status()}",
        f"Provider health: {health['display']}",
        f"MCP servers: {_mcp_doctor_summary()}",
    ]
    if settings.name == "agy":
        report.append(
            "Latency hint: agy interactive turns typically take 20-40s. "
            "Switch to groq, cerebras, openrouter, or local ollama for sub-second HUD responses."
        )
    if health.get("error"):
        report.append(f"Detail: {health['error']}")
    print(redact_secrets("\n".join(report)))


def run_setup(
    *,
    provider: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    theme: str | None = None,
    non_interactive: bool = False,
) -> int:
    current = load_user_config()
    ai_cfg = current.get("ai_provider", {}) if isinstance(current.get("ai_provider"), dict) else {}
    default_provider = str(ai_cfg.get("name") or "agy")
    default_theme = str(current.get("theme") or "light")
    chosen_provider = provider
    chosen_model = model
    chosen_theme = theme
    api_key: str | None = None

    if not non_interactive:
        if chosen_provider is None:
            choices = ", ".join(PROVIDER_PRESETS)
            raw = input(f"AI provider [{default_provider}] ({choices}): ").strip()
            chosen_provider = raw or default_provider
        preview = load_provider_settings(provider=chosen_provider, model=chosen_model, base_url=base_url)
        if chosen_model is None:
            raw_model = input(f"Model [{preview.model}]: ").strip()
            if raw_model:
                chosen_model = raw_model
        if chosen_theme is None:
            raw_theme = input(f"HUD theme [{default_theme}] (light/dark): ").strip()
            chosen_theme = raw_theme or default_theme
        if preview.name not in ("agy", "ollama"):
            entered = getpass.getpass("API key (leave blank to use env var or existing config): ").strip()
            if entered:
                api_key = entered
    else:
        if chosen_provider is None:
            chosen_provider = default_provider
        if chosen_theme is None:
            chosen_theme = default_theme

    saved = configure_user_preferences(
        provider=chosen_provider,
        model=chosen_model,
        base_url=base_url,
        theme=chosen_theme,
        api_key=api_key,
    )
    saved_ai = saved["ai_provider"]
    print(redact_secrets(
        f"Saved Mavrick setup to {CONFIG_FILE} (mode 0600): "
        f"provider={saved_ai['name']}, model={saved_ai['model']}, theme={saved['theme']}"
    ))
    run_doctor(load_provider_settings(), load_ai_timeouts())
    return 0


def show_history(search_term: Optional[str] = None):
    items = search_history(search_term) if search_term else load_history()
    if not items:
        print("No history entries found.")
        return

    print_banner()
    print(f"Recent Activity ({len(items)} entries):")
    print("─────────────────────────────────────────────────────────")
    for item in items[-25:]:
        status_clr = CLR_LIME if item.get("status") == "completed" else CLR_RED
        date_str = item.get("date_str", "")
        query = item.get("query", "")
        summary = item.get("summary", "")
        action = item.get("action", "")
        print(f"[{date_str}] {status_clr}●{CLR_RESET} {CLR_BOLD}{query}{CLR_RESET}")
        if action:
            print(f"   Action: {action} | {summary}")
    print("─────────────────────────────────────────────────────────")

def _select_files(review):
    from dataclasses import replace

    rows = list(review.rows)
    selected = {i for i, row in enumerate(rows) if row.included and not row.problem}
    while True:
        for i, row in enumerate(rows):
            mark = "x" if i in selected else " "
            warning = " [changed since preview]" if row.changed else ""
            step = f"Step {row.workflow_step} · " if row.workflow_step is not None else ""
            print(f"[{mark}] {i + 1}. {step}{display_path(row.source)} → {display_path(row.destination)}{warning}")
            if row.problem:
                print(f"       {display_path(row.problem)}")
        verb = "Restore" if review.action == "undo" else "Move"
        count = len(selected)
        noun = "file" if count == 1 else "files"
        choice = input(f"{verb} {count} {noun} [y], skip/include N, rename N NAME, or cancel [Enter]: ").strip()
        if choice.lower() in ("y", "yes") and selected:
            return approve_files(replace(review, rows=tuple(rows)), selected)
        if choice.lower() in ("", "cancel", "q", "n"):
            return None
        parts = choice.split(maxsplit=2)
        try:
            if len(parts) < 2:
                raise ValueError("Enter skip N, include N, or rename N NAME.")
            index = int(parts[1]) - 1
            if not 0 <= index < len(rows):
                raise ValueError("That row number is unavailable.")
            command = parts[0].lower()
            if command == "skip":
                selected.discard(index)
            elif command == "include" and not rows[index].problem:
                selected.add(index)
            elif command == "rename" and len(parts) == 3:
                rows[index] = rename_review_row(rows[index], parts[2])
                if rows[index].problem:
                    selected.discard(index)
                else:
                    selected.add(index)
            else:
                raise ValueError("Choose an available file or rename its destination.")
        except ValueError as error:
            print(error)


def _execute_with_stop(executor, proposal, query, job_id, *, approval=None, preview=False):
    # Python's default Ctrl+C exception could interrupt a move between rename and
    # recording Undo. During execution signal the job instead, between entries.
    cancel = threading.Event()

    def stop(_signal, _frame):
        cancel.set()

    main_thread = threading.current_thread() is threading.main_thread()
    previous = signal.signal(signal.SIGINT, stop) if main_thread else None
    try:
        options = {"preview": True} if preview else {}
        return executor.execute(proposal, approval=approval, job_id=job_id, query=query,
                                cancel_token=cancel, progress_callback=print if sys.stdout.isatty() else None,
                                **options)
    finally:
        if main_thread:
            signal.signal(signal.SIGINT, previous)


def _run_action(proposal, query, auto_confirm=False):
    executor = ActionExecutor.get_instance()
    job_id = uuid.uuid4().hex

    def execute(action, approval=None):
        return _execute_with_stop(executor, action, query, job_id, approval=approval)

    def cancel_review():
        print("Action cancelled.")
        append_history(query, proposal.action_type.value, "cancelled", "Cancelled by user", job_id=job_id)

    try:
        result = execute(proposal)
        while True:
            if result.status == "requires_confirmation":
                action = result.details["action"]
                print(f"\n{action.summary}")
                for target in action.affected_targets:
                    print(f"  {target}")
                print(action.impact)
                can_auto_confirm = (action.risk_level == RiskLevel.REVERSIBLE
                                    and action.action_type not in (ActionType.OPEN_WORKSPACE, ActionType.MCP_TOOL))
                if not (auto_confirm and can_auto_confirm) and input(f"{action.summary}? [y/N]: ").strip().lower() not in ("y", "yes"):
                    cancel_review()
                    return 0
                result = execute(action, approve_action(action))
                continue
            if result.status != "review":
                print(result.summary)
                if result.stdout and result.stdout != result.summary:
                    print(result.stdout.removeprefix(result.summary + "\n"))
                if result.undo_available:
                    print(f"Run mavrick --undo {result.details['undo_id']} to review Undo.")
                if result.verification_status == "launch_requested":
                    print("Verification: launch requested")
            review = result.details.get("review")
            if not review:
                return result.exit_code
            if result.status == "review":
                print(result.summary)
            approval = _select_files(review)
            if approval is None:
                cancel_review()
                return 0
            result = execute(proposal, approval)
    except (KeyboardInterrupt, EOFError):
        cancel_review()
        return 0


def _run_workflow(plan, query, *, dry_run=False):
    executor = ActionExecutor.get_instance()
    if dry_run:
        review = executor.preview_workflow(plan)
        print(describe_workflow(review))
        return 0 if review.allowed else 1
    job_id = uuid.uuid4().hex
    result = _execute_with_stop(executor, plan, query, job_id, preview=True)
    try:
        while result.status == "workflow_review":
            review = result.details["workflow_review"]
            print(describe_workflow(review))
            if review.requires_approval and input(f"Run {len(review.steps)} steps? [y/N]: ").strip().lower() not in ("y", "yes"):
                raise EOFError
            result = _execute_with_stop(executor, plan, query, job_id, approval=approve_workflow(review))
    except (KeyboardInterrupt, EOFError):
        print("Workflow cancelled.")
        append_history(query, "workflow", "cancelled", "Cancelled by user", job_id=job_id)
        return 0
    print(result.summary)
    if result.stdout:
        print(result.stdout)
    if result.undo_available:
        print(f"Run mavrick --undo {result.details['undo_id']} to review batch Undo.")
    return result.exit_code


def _list_workflows():
    catalog = workflow_catalog(refresh=True)
    for name, plan in sorted(catalog.plans.items()):
        risk = declared_workflow_risk(plan)
        print(f"{name} · {len(plan)} {'step' if len(plan) == 1 else 'steps'} · {risk.value}")
    for error in catalog.errors:
        print(error)
    if not catalog.plans:
        print("No saved workflows.")
    return 1 if catalog.errors else 0


def execute_cli_query(query: str, auto_confirm: bool = False, *, timeouts=None, query_limit=None, provider_settings=None):
    print(f"{CLR_SLATE}Thinking...{CLR_RESET}", end="\r", flush=True)
    request_id = uuid.uuid4().hex
    options = {"timeouts": timeouts, "query_limit": query_limit} if timeouts is not None else {}
    if provider_settings is not None:
        options["provider_settings"] = provider_settings
    resp = query_model(query, **options)
    if resp.response_type == "error":
        print(resp.validation_error or "Request failed.")
        append_history(query, status="failed", summary=resp.validation_error or "Error", job_id=request_id)
        return 1
    if resp.response_type == "history":
        show_history()
        return 0
    if resp.response_type == "plan" and resp.plan is not None:
        return _run_workflow(resp.plan, query)
    if resp.response_type in ("message", "calculator", "plan"):
        print(resp.message or "")
        append_history(query, status="completed", summary=(resp.message or "")[:80], job_id=request_id)
        return 0
    if resp.action_request:
        return _run_action(resp.action_request, query, auto_confirm)
    return 1

def run_uninstall():
    if get_platform().name != "linux":
        print("Remove Mavrick using the Python environment or package tool that installed it.")
        return
    print_banner()
    if find_spec("mavrick.install") is None:
        print("Remove the Debian package with: sudo apt remove mavrick")
        return
    confirm = input("Are you sure you want to uninstall MAVRICK? [y/N]: ").strip().lower()
    if confirm not in ["y", "yes"]:
        print("Uninstallation aborted.")
        return

    from mavrick.install import uninstall
    uninstall()

def main():
    parser = argparse.ArgumentParser(description="MAVRICK Operating System Assistant")
    parser.add_argument("query", nargs="*", help="Natural language command or question")
    parser.add_argument("-y", "--yes", action="store_true", help="Auto-confirm routine reversible actions")
    parser.add_argument("--health", action="store_true", help="Check AI daemon and system health")
    parser.add_argument("--doctor", action="store_true", help="Report provider, timeouts, socket and router status")
    parser.add_argument("--history", action="store_true", help="Display recent activity history")
    parser.add_argument("--search-history", type=str, help="Search history by query or action")
    parser.add_argument("--clear-history", action="store_true", help="Clear all request history and context")
    parser.add_argument("--undo", nargs="?", const="", metavar="ID", help="Review the last file move or the specified Undo batch")
    parser.add_argument("--uninstall", action="store_true", help="Uninstall MAVRICK")
    workflows = parser.add_mutually_exclusive_group()
    workflows.add_argument("--workflows", action="store_true", help="List saved workflows")
    workflows.add_argument("--run-workflow", metavar="NAME", help="Run an exact saved workflow")
    workflows.add_argument("--mcp", action="store_true", help="List configured MCP servers, policy trust, and discovered tools")
    workflows.add_argument("--mcp-approve", metavar="SERVER", help="Record explicit launch and env grant for an MCP server")
    parser.add_argument("--grant-env", type=str, default=None, help="Comma-separated env variables to grant with --mcp-approve")
    parser.add_argument("--grant-readonly", type=str, default=None, help="Comma-separated read-only tool names to approve")
    parser.add_argument("--dry-run", action="store_true", help="Show workflow steps and policy without running them")
    parser.add_argument("--setup", action="store_true", help="Configure AI provider, model, and HUD theme")
    parser.add_argument("--non-interactive", action="store_true", help="Run --setup without interactive prompts")
    parser.add_argument("--theme", choices=("light", "dark"), help="HUD theme for --setup (light or dark)")
    parser.add_argument("--service-template", nargs="?", const="auto", metavar="PLATFORM",
                        help="Print declarative daemon/autostart template (linux, macos, windows)")

    parser.add_argument("--init-timeout", type=float, help="Fallback worker initialization timeout")
    parser.add_argument("--query-timeout", type=float, help="HTTP query limit or maximum warm daemon query time")
    parser.add_argument("--fallback-timeout", type=float, help="One-shot fallback timeout")
    parser.add_argument("--provider", help="Select agy, groq, cerebras, openrouter, ollama, or openai_compat")
    parser.add_argument("--model", help="Override the model for this invocation")
    parser.add_argument("--base-url", help="Override the provider HTTP API base URL")
    args = parser.parse_args()
    if args.dry_run and not args.run_workflow:
        parser.error("--dry-run requires --run-workflow.")
    if args.non_interactive and not args.setup:
        parser.error("--non-interactive requires --setup.")
    if args.theme is not None and not args.setup:
        parser.error("--theme requires --setup.")
    if (args.grant_env is not None or args.grant_readonly is not None) and not args.mcp_approve:
        parser.error("--grant-env and --grant-readonly require --mcp-approve.")
    overrides = {name: getattr(args, name) for name in ("init_timeout", "query_timeout", "fallback_timeout")}
    provider_options = {name: getattr(args, name) for name in ("provider", "model", "base_url")}
    try:
        timeouts = load_ai_timeouts(**overrides) if args.doctor or any(value is not None for value in overrides.values()) else None
        settings = (load_provider_settings(**provider_options)
                    if args.doctor or any(value is not None for value in provider_options.values()) else None)
    except ValueError as error:
        parser.error(str(error))
    if (timeouts is not None or settings is not None) and not (args.query or args.doctor or args.health or args.setup):
        parser.error("Provider or timeout overrides require a query, --doctor, --health, or --setup.")

    if args.service_template is not None:
        try:
            _kind, template = service_template_for_platform(
                None if args.service_template == "auto" else args.service_template
            )
        except ValueError as error:
            parser.error(str(error))
        print(template, end="" if template.endswith("\n") else "\n")
        return 0
    if args.setup:
        try:
            return run_setup(
                provider=args.provider,
                model=args.model,
                base_url=args.base_url,
                theme=args.theme,
                non_interactive=args.non_interactive,
            )
        except (ValueError, OSError) as error:
            parser.error(str(error))

    if args.doctor:
        return run_doctor(settings, timeouts)
    if args.health:
        if settings or timeouts:
            run_health_check(settings, timeouts)
        else:
            run_health_check()
        return

    if args.history or args.search_history:
        show_history(args.search_history)
        return

    if args.clear_history:
        if clear_history_and_context():
            print("✓ History and session context cleared.")
        else:
            print("Error clearing history.")
        return

    if args.undo is not None:
        params = {"undo_id": args.undo} if args.undo else {}
        return _run_action(ActionRequest(ActionType.UNDO, params, "", RiskLevel.REVERSIBLE), "Undo")

    if args.workflows:
        return _list_workflows()
    if args.mcp:
        return _list_mcp_servers()
    if args.mcp_approve:
        try:
            return _approve_mcp_server_cli(
                args.mcp_approve,
                grant_env=args.grant_env,
                grant_readonly=args.grant_readonly,
            )
        except (ValueError, OSError) as error:
            parser.error(str(error))
    if args.run_workflow:
        catalog = workflow_catalog(refresh=True)
        name = args.run_workflow
        if (plan := catalog.plans.get(name)) is None:
            print("Saved workflow not found.")
            for error in catalog.errors:
                print(error)
            return 1
        return _run_workflow(plan, f"/{name}", dry_run=args.dry_run)

    if args.uninstall:
        run_uninstall()
        return

    if not args.query:
        try:
            from .ui import main as gui_main, select_ui_backend, unavailable_ui_guidance

            if select_ui_backend() == "none":
                print(unavailable_ui_guidance("auto"))
                return 1
            gui_main()
        except (ImportError, RuntimeError, ValueError) as error:
            print(error)
            return 1
    else:
        query_str = " ".join(args.query).strip()
        provider = {"provider_settings": settings} if settings is not None else {}
        return execute_cli_query(query_str, auto_confirm=args.yes, timeouts=timeouts,
                                 query_limit=args.query_timeout, **provider)

if __name__ == "__main__":
    sys.exit(main())
