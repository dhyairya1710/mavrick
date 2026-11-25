#!/usr/bin/env python3
"""
MAVRICK Core Module (Backward-Compatibility Bridge)
Directs legacy callers to the typed tools, ActionExecutor, and secure schemas.
All arbitrary shell execution has been completely eliminated.
"""

from .schema import RiskLevel, ActionType, ActionRequest, ModelResponse
from .policy import requires_confirmation, is_safe_user_path, scrub_secrets
from .executor import ActionExecutor, ActionResult, JobState
from .ai import query_model, check_ai_health, build_system_prompt
from .workflows import describe_workflow
from .config import (
    load_user_config,
    ensure_secure_storage,
    CONTEXT_FILE,
    HISTORY_FILE,
    AUDIT_LOG_FILE,
    AGY_BIN,
    DAEMON_SOCKET,
    FALLBACK_DAEMON_SOCKET
)
from .history import (
    load_history,
    append_history,
    load_context,
    save_context,
    clear_history_and_context,
    search_history
)
from .tools import (
    get_system_health,
    inspect_processes,
    set_volume,
    lock_session,
    open_app,
    open_url,
    arrange_windows,
    list_files,
    organize_downloads,
    undo_last_action
)

def check_antigravity():
    """Health check wrapper."""
    health = check_ai_health()
    is_ok = health.get("status") in ("connected_daemon", "fallback_cli", "connected_provider")
    return is_ok, health.get("display", "Offline")

def execute_jarvis(prompt: str, stream_callback=None, confirmed: bool = False, *, approval=None) -> dict:
    """
    Structured execution wrapper replacing old arbitrary shell runner.
    """
    model_resp = query_model(prompt, stream_callback=stream_callback)
    if model_resp.response_type == "error":
        return {
            "has_action": False,
            "is_detached": False,
            "message": f"AI Error: {model_resp.validation_error}",
            "command": None,
            "output": None
        }

    if model_resp.response_type == "plan" and model_resp.plan is not None:
        result = ActionExecutor.get_instance().execute(model_resp.plan, approval=approval,
                                                       query=prompt, preview=approval is None)
        review = result.details.get("workflow_review")
        return {"status": result.status, "command": "workflow", "summary": result.summary,
                "has_action": result.status != "workflow_review", "job_id": result.job_id,
                "output": describe_workflow(review) if review else result.stdout,
                "details": result.details, "undo_available": result.undo_available}

    if model_resp.response_type in ("message", "calculator", "plan", "history"):
        message = model_resp.message
        if model_resp.response_type == "history":
            message = "\n".join(item.get("query", "") for item in load_history()[-10:]) or "No history entries found."
        return {
            "has_action": False,
            "is_detached": False,
            "message": message or "",
            "command": None,
            "output": None
        }

    if model_resp.response_type == "action" and model_resp.action_request:
        act = model_resp.action_request
        res = ActionExecutor.get_instance().execute(act, confirmed=confirmed, approval=approval, query=prompt)
        if res.status == "requires_confirmation":
            exact = res.details["action"]
            return {"status": res.status, "action": exact.action_type.value, "summary": exact.summary,
                    "risk_level": exact.risk_level.value, "targets": exact.affected_targets,
                    "params": exact.params, "job_id": res.job_id}
        return {"has_action": True, "is_detached": res.verification_status == "launch_requested",
                "message": res.summary, "command": act.action_type.value, "output": res.stdout or res.summary,
                "status": res.status, "duration_ms": res.duration_ms, "job_id": res.job_id,
                "details": res.details, "undo_available": res.undo_available,
                "verification_status": res.verification_status}

    return {
        "has_action": False,
        "is_detached": False,
        "message": "No actionable request identified.",
        "command": None,
        "output": None
    }
