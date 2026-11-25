#!/usr/bin/env python3
"""
MAVRICK Shared Action Executor & Job Management
One unified execution engine used by both CLI and GUI.
Provides explicit job states, cancellation, outcome verification, and audit logging.
"""

import os
import re
import json
import time
import uuid
import logging
import threading
from enum import Enum
from dataclasses import asdict, dataclass, field
from typing import Dict, Any, Optional, Callable, List

from .schema import (ActionType, RiskLevel, ActionRequest, WorkflowPlan, MAX_WORKFLOW_STEPS,
                     validate_action_params, validate_workflow_action)
from .platform import get_platform
from .platform.base import FileLockBusy, UnsupportedPlatformAction
from .policy import (WorkflowRisk, aggregate_workflow_risk, enforce_mcp_access, enforce_workflow_step,
                     evaluate_mcp_tool_risk, is_safe_user_path, mcp_policy_fingerprint,
                     requires_confirmation, validate_app_target, validate_mcp_arguments, workflow_risk)
from .mcp import MCPError, call_mcp_tool, discover_server_tools, load_mcp_grants, load_mcp_registry
from .config import (MAVRICK_STATE_DIR, configured_api_keys, ensure_secure_storage, load_user_config,
                     set_custom_command, delete_custom_command)
from .history import append_audit_log, append_history, redact_secrets, save_context
from .tools.apps import (_UNSET, resolve_pinned_app, resolve_window_target, resolve_workspace,
                         open_workspace, launch_apps)
from .tools.files import FileReview, review_files, apply_file_review, begin_workflow_batch
from .tools.packages import resolve_package, package_title, _package_label, _package_operation
from .tools import (
    get_system_health,
    inspect_processes,
    set_volume,
    get_power_profile,
    set_power_profile,
    control_media,
    get_network_status,
    check_updates,
    clean_cache,
    inspect_storage_usage,
    lock_session,
    open_app,
    run_in_terminal,
    open_url,
    arrange_windows,
    list_files,
)
from .workflows import (WorkflowReview, WorkflowStepReport, WorkflowStepReview, approve_workflow,
                        describe_workflow, file_step_targets, overlaps_prior_file_step)

class JobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    REQUIRES_CONFIRMATION = "requires_confirmation"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    STOPPING = "stopping"

@dataclass
class ActionResult:
    status: str                       # "success", "failed", "cancelled", "requires_confirmation"
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    start_time: float = 0.0
    end_time: float = 0.0
    duration_ms: float = 0.0
    summary: str = ""
    undo_available: bool = False
    undo_metadata: Optional[Dict[str, Any]] = None
    verification_status: str = "unverified"  # "verified", "launch_requested", "failed", "unverified"
    verification_details: str = ""
    details: Dict[str, Any] = field(default_factory=dict)
    job_id: str = ""

@dataclass
class Job:
    id: str
    action: ActionRequest | WorkflowPlan
    state: JobState
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    result: Optional[ActionResult] = None
    cancellation_requested: bool = False
    _cancel_event: threading.Event = field(default_factory=threading.Event)
    pgids: List[int] = field(default_factory=list)
    pids: List[int] = field(default_factory=list)
    batch_id: str = ""
    workflow_step: int | None = None
    created_move_directories: dict = field(default_factory=dict)

FILE_ACTIONS = {ActionType.ORGANIZE_DOWNLOADS, ActionType.MOVE_FILES, ActionType.UNDO}
PACKAGE_ACTIONS = {ActionType.INSTALL_PACKAGE, ActionType.UNINSTALL_PACKAGE}
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b.")
_MAX_MCP_OUTPUT_CHARS = 64 * 1024


@dataclass(frozen=True)
class ActionApproval:
    signature: str


@dataclass(frozen=True)
class FileApproval:
    review: FileReview
    selected: tuple[int, ...]


def approve_action(action):
    """Bind an explicit confirmation to the displayed action, risk, target, and pinned policy metadata."""
    risk = action.risk_level.value if isinstance(action.risk_level, RiskLevel) else str(action.risk_level)
    meta = action.execution_meta if isinstance(getattr(action, "execution_meta", None), dict) else {}
    return ActionApproval(json.dumps([
        action.action_type.value,
        risk,
        action.params,
        action.summary,
        action.affected_targets,
        meta,
    ], sort_keys=True, ensure_ascii=True))


def approve_files(review, selected):
    indices = tuple(sorted(set(selected)))
    if not indices or any(type(index) is not int or not 0 <= index < len(review.rows) for index in indices):
        raise ValueError("Choose at least one reviewed file.")
    return FileApproval(review, indices)


class ActionExecutor:
    """
    Central thread-safe action executor with cancellation and verification.
    """
    _instance: Optional['ActionExecutor'] = None
    _lock = threading.Lock()

    def __init__(self):
        self.current_job: Optional[Job] = None
        self.active_pgids: List[int] = []
        self.active_pids: List[int] = []
        self.job_lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> 'ActionExecutor':
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def _kill_process_group(self, pgid: int):
        try:
            get_platform().terminate_group(pgid)
        except UnsupportedPlatformAction:
            pass

    def terminate_process_group_for_pid(self, pid: int):
        self._kill_process_group(get_platform().process_group(pid))

    def register_pgid(self, pgid: int, pid: Optional[int] = None):
        """Registers a process group ID and optional PID with the active job/executor."""
        with self.job_lock:
            if pgid and pgid not in self.active_pgids:
                self.active_pgids.append(pgid)
            if pid and pid not in self.active_pids:
                self.active_pids.append(pid)
            if self.current_job:
                if pgid and pgid not in self.current_job.pgids:
                    self.current_job.pgids.append(pgid)
                if pid and pid not in self.current_job.pids:
                    self.current_job.pids.append(pid)

    def register_process(self, proc: Any):
        """Registers a spawned subprocess with the active job/executor."""
        pid = getattr(proc, "pid", None)
        if pid:
            try:
                pgid = get_platform().process_group(pid)
            except OSError:
                pgid = pid
            self.register_pgid(pgid, pid)

    def cancel_current_job(self, job_id=None) -> bool:
        """Requests cancellation of the currently running job and terminates all spawned process groups."""
        with self.job_lock:
            job = self.current_job
            if not job or (job_id and job.id != job_id) or job.state not in (JobState.RUNNING, JobState.QUEUED):
                return False
            job.cancellation_requested = True
            job._cancel_event.set()
            job.state = JobState.STOPPING
            groups = set(job.pgids) | set(self.active_pgids)
        for group in groups:
            self._kill_process_group(group)
        return True

    def get_active_job(self) -> Optional[Job]:
        with self.job_lock:
            return self.current_job

    def prepare(self, proposal):
        """Rebuild the request from typed values and resolve real execution targets."""
        valid, error, action = validate_action_params(proposal.action_type, proposal.params)
        if not valid:
            raise ValueError(error)
        kind, params = action.action_type, action.params
        if kind in (ActionType.OPEN_APP, ActionType.RUN_IN_TERMINAL):
            key = "app_name" if kind == ActionType.OPEN_APP else "command"
            in_terminal = kind == ActionType.RUN_IN_TERMINAL or params.get("in_terminal", False)
            valid, target = validate_app_target(params[key], in_terminal)
            if not valid:
                raise ValueError(target)
            params[key] = target
            action.affected_targets = [target]
            action.execution_meta = {"pinned_apps": {target: resolve_pinned_app(target, in_terminal=in_terminal)}}
        elif kind == ActionType.OPEN_APPS:
            resolved = []
            for item in params["apps"]:
                valid, target = validate_app_target(item)
                if not valid:
                    raise ValueError(target)
                if target not in resolved:
                    resolved.append(target)
            params["apps"] = resolved
            action.affected_targets = list(resolved)
            action.execution_meta = {"pinned_apps": {target: resolve_pinned_app(target) for target in resolved}}
        elif kind == ActionType.ARRANGE_WINDOWS:
            pinned_apps = {}
            for key in ("left_target", "right_target"):
                target_kind, resolved_value = resolve_window_target(params[key])
                params[key] = resolved_value
                if target_kind == "app":
                    pinned_apps[resolved_value] = resolve_pinned_app(resolved_value)
            action.affected_targets = [params["left_target"], params["right_target"]]
            if pinned_apps:
                action.execution_meta = {"pinned_apps": pinned_apps}
        elif kind in PACKAGE_ACTIONS:
            name, manager, scope = resolve_package(kind.value, params)
            action.params = {"package_name": name, "manager": manager}
            if scope:
                action.params["scope"] = scope
            action.summary = package_title(kind.value, name, manager, scope)
            action.affected_targets = [_package_label(name, manager, scope)]
        elif kind == ActionType.OPEN_WORKSPACE:
            project, steps = resolve_workspace(params, load_user_config())
            action.params = {"project": project}
            action.affected_targets = [step["label"] for step in steps]
        elif kind == ActionType.MCP_TOOL:
            server_name = params["server"]
            tool_name = params["tool"]
            arguments = params.get("arguments", {})
            server_cfg = load_mcp_registry().servers.get(server_name)
            grant = load_mcp_grants().get(server_name)
            enforce_mcp_access(server_cfg, tool_name, grant=grant)
            snapshot = discover_server_tools(server_name)
            if snapshot.error:
                raise ValueError(snapshot.error)
            tool_spec = next((item for item in snapshot.tools if item.name == tool_name), None)
            if tool_spec is None:
                raise ValueError(f"MCP tool '{server_name}/{tool_name}' was not found on server '{server_name}'.")
            risk_level, _ = evaluate_mcp_tool_risk(server_cfg, tool_spec, grant=grant)
            validate_mcp_arguments(tool_spec, arguments, strict=(risk_level != RiskLevel.READ_ONLY))
            known = configured_api_keys()
            args_preview = (
                redact_secrets(json.dumps(arguments, indent=2, sort_keys=True, ensure_ascii=False), known)
                if arguments else ""
            )
            target = f"mcp · {server_name}/{tool_name}"
            action.risk_level = risk_level
            action.summary = target
            action.affected_targets = [target] + ([f"Args:\n{args_preview}"] if args_preview else [])
            action.execution_meta = {
                "mcp_fingerprint": mcp_policy_fingerprint(server_cfg, grant, tool_spec, risk_level),
            }
            trust_note = (
                "Destructive MCP tool requires explicit confirmation."
                if risk_level == RiskLevel.DESTRUCTIVE else
                "Approved read-only MCP tool."
                if risk_level == RiskLevel.READ_ONLY else
                ("Untrusted MCP server requires review before calling."
                 if not server_cfg.trusted else "Mutating MCP tool requires review before calling.")
            )
            desc = redact_secrets(tool_spec.description, known)
            action.impact = f"{trust_note} {desc}".strip()
        return action

    def needs_approval(self, action):
        if action.action_type in FILE_ACTIONS:
            return False  # The file scan is read-only. Apply requires an explicit row selection.
        if action.action_type == ActionType.OPEN_WORKSPACE:
            return True
        if action.action_type == ActionType.MCP_TOOL:
            return action.risk_level != RiskLevel.READ_ONLY
        return requires_confirmation(action, load_user_config().get("trusted_actions", []))

    def execute(self, action: ActionRequest | WorkflowPlan, confirmed: bool = False,
                progress_callback: Optional[Callable[[str], None]] = None, *,
                approval=None, job_id=None, query="", cancel_token=None, preview=False) -> ActionResult:
        """Admit one job and keep it active through auditing and history persistence."""
        job_id = job_id or uuid.uuid4().hex
        job = Job(id=job_id, action=action, state=JobState.QUEUED)
        if cancel_token is not None:
            job._cancel_event = cancel_token
        with self.job_lock:
            if self.current_job is not None:
                return ActionResult("busy", 1, summary="A job is still active.", job_id=job_id)
            self.current_job = job

        start = time.time()
        lock = None
        try:
            ensure_secure_storage()
            operation_lock = get_platform().lock_file(os.path.join(MAVRICK_STATE_DIR, "execution.lock"), blocking=False)
            try:
                operation_lock.__enter__()
            except FileLockBusy:
                return ActionResult("busy", 1, summary="A job is still active in another Mavrick session.", job_id=job_id)
            lock = operation_lock
            try:
                if isinstance(action, WorkflowPlan):
                    result = self._execute_workflow(action, job, approval, progress_callback, preview)
                    if result.status == "workflow_review":
                        return result
                elif job._cancel_event.is_set():
                    result = ActionResult("cancelled", -15, summary="Stopped before execution.")
                else:
                    result = self._execute_action(action, job, approval, confirmed, progress_callback)
                    if result.status == "requires_confirmation":
                        return result
            except (Exception, KeyboardInterrupt) as error:
                stopped = isinstance(error, KeyboardInterrupt)
                result = ActionResult("cancelled" if stopped else "failed", -15 if stopped else 1,
                                      summary="Stopped." if stopped else str(error),
                                      verification_status="cancelled" if stopped else "failed")
            if job._cancel_event.is_set() and result.status not in ("cancelled", "failed", "partial"):
                result.status = "cancelled"
                result.exit_code = -15
                result.summary = "Stop requested. " + result.summary
                result.details.pop("review", None)
            result.job_id = job.id
            result.start_time = start
            result.end_time = time.time()
            result.duration_ms = (result.end_time - start) * 1000
            job.result = result
            job.completed_at = result.end_time
            job.state = {"success": JobState.COMPLETED, "cancelled": JobState.CANCELLED,
                         "review": JobState.REQUIRES_CONFIRMATION}.get(result.status, JobState.FAILED)
            try:
                self._record(job, result, query)
            except OSError as error:
                logging.warning("Could not persist Mavrick history: %s", error)
            return result
        except OSError as error:
            return ActionResult("failed", 1, summary=f"Mavrick state is unavailable: {error}",
                                verification_status="failed", job_id=job.id)
        finally:
            if lock is not None:
                lock.__exit__(None, None, None)
            with self.job_lock:
                self.current_job = None
                self.active_pgids.clear()
                self.active_pids.clear()

    def _mark_running(self, job: Job) -> None:
        with self.job_lock:
            if job.state != JobState.STOPPING:
                job.state = JobState.RUNNING
            job.started_at = time.time()

    def _execute_action(self, action, job, approval, confirmed, progress):
        prior_pinned = (
            action.execution_meta.get("pinned_apps")
            if isinstance(getattr(action, "execution_meta", None), dict)
            else None
        )
        prepared = self.prepare(action)
        job.action = prepared
        if prior_pinned is not None and prepared.execution_meta.get("pinned_apps") != prior_pinned:
            return ActionResult(
                "failed",
                1,
                summary="Pinned launcher target changed since review.",
                verification_status="failed",
            )
        if job._cancel_event.is_set():
            return ActionResult("cancelled", -15, summary="Stopped before execution.")
        if isinstance(approval, FileApproval) and (
            prepared.action_type not in FILE_ACTIONS or approval.review.action != prepared.action_type.value
        ):
            return ActionResult("failed", 1, summary="This file review belongs to a different action.")
        if self.needs_approval(prepared) and approval != approve_action(prepared) and not (
            confirmed and prepared.risk_level == RiskLevel.REVERSIBLE
            and prepared.action_type not in (ActionType.OPEN_WORKSPACE, ActionType.MCP_TOOL)
        ):
            job.state = JobState.REQUIRES_CONFIRMATION
            return ActionResult("requires_confirmation", summary=prepared.summary,
                                details={"action": prepared}, job_id=job.id)
        self._mark_running(job)
        if progress:
            progress(prepared.summary)
        return self._dispatch_tool(prepared, job, approval, progress)

    def preview_workflow(self, plan: WorkflowPlan, cancel_token=None) -> WorkflowReview:
        """Resolve and check every action before anything in the plan can run."""
        known = configured_api_keys()
        title = redact_secrets(f"Workflow · {plan.name}" if plan.name else "Workflow plan", known)
        if not 1 <= len(plan) <= MAX_WORKFLOW_STEPS:
            error = f"A workflow needs one to {MAX_WORKFLOW_STEPS} action steps."
            invalid = WorkflowStepReview(1, WorkflowRisk.READ_ONLY, "Invalid workflow", error=error)
            return WorkflowReview(plan, (invalid,), title, invalid.risk)
        reviews, earlier_files = [], []
        for index, proposal in enumerate(plan, 1):
            risk, summary, action, files, targets, error = WorkflowRisk.READ_ONLY, "Invalid action", None, None, (), ""
            try:
                if cancel_token and cancel_token.is_set():
                    raise ValueError("Stopped during preflight.")
                if not isinstance(proposal, ActionRequest):
                    raise ValueError("Expected a typed action.")
                action = validate_workflow_action(proposal.action_type, proposal.params)
                risk, summary = workflow_risk(action), action.summary
                action = self.prepare(action)
                risk, summary = workflow_risk(action), action.summary
                enforce_workflow_step(action)
                summary, targets = action.summary, tuple(action.affected_targets)
                if action.action_type in FILE_ACTIONS:
                    files = review_files(action.action_type.value, action.params, cancel_token)
                    targets = file_step_targets(files)
                    if problems := [row.problem for row in files.rows if row.problem]:
                        raise ValueError(problems[0])
                    if len({row.destination for row in files.rows}) != len(files.rows):
                        raise ValueError("Two files in this step have the same destination.")
                    if overlaps_prior_file_step(files, earlier_files):
                        raise ValueError("This file step overlaps an earlier move. Review those moves separately.")
                    earlier_files.append(files)
                elif action.action_type == ActionType.LIST_FILES:
                    safe, reason, path = is_safe_user_path(action.params["path"])
                    if not safe:
                        raise ValueError(reason)
                    targets = (path,)
            except (ValueError, OSError) as failure:
                error = str(failure)
            summary, targets, error = redact_secrets((summary, targets, error), known)
            reviews.append(WorkflowStepReview(index, risk, summary, targets, action, files, error))
        return WorkflowReview(plan, tuple(reviews), title, aggregate_workflow_risk(step.risk for step in reviews))

    def _execute_workflow(self, plan, job, approval, progress, preview):
        review = self.preview_workflow(plan, job._cancel_event)
        if job._cancel_event.is_set():
            reports = [self._step_report(step, "skipped", "Stopped before execution.") for step in review.steps]
            return self._workflow_result(job, review, reports, False, stopped=True)
        if not review.allowed:
            reports = [self._step_report(step, "failed" if step.error else "skipped",
                                         step.error or "Blocked by workflow preflight.") for step in review.steps]
            return ActionResult("failed", 1, summary="Workflow blocked before Step 1.",
                                stdout=describe_workflow(review), job_id=job.id, verification_status="failed",
                                details={"workflow_review": review, "risk": review.risk.value,
                                         "steps": [asdict(report) for report in reports]})
        if preview or (approval is not None or review.requires_approval) and approval != approve_workflow(review):
            job.state = JobState.REQUIRES_CONFIRMATION
            summary = "Workflow changed. Review the updated steps." if approval is not None else "Workflow ready to review."
            return ActionResult("workflow_review", summary=summary, job_id=job.id,
                                details={"workflow_review": review, "risk": review.risk.value})

        self._mark_running(job)
        batch_id = uuid.uuid4().hex
        begin_workflow_batch(batch_id)
        job.batch_id = batch_id
        reports, undo_available = [], False
        stopped, has_partial_effects = False, False
        known = configured_api_keys()
        for step in review.steps:
            if stopped or any(report.status == "failed" for report in reports):
                reports.append(self._step_report(step, "skipped", "Not run."))
                continue
            if job._cancel_event.is_set():
                stopped = True
                reports.append(self._step_report(step, "cancelled", "Stopped before this step started."))
                continue
            job.workflow_step = step.number
            label = f"Step {step.number} of {len(review.steps)} · "

            def step_progress(message, label=label):
                if progress:
                    progress(label + message)

            step_progress(step.summary)
            try:
                if job._cancel_event.is_set():
                    result = ActionResult("cancelled", -15, summary="Stopped before this step started.")
                elif step.files is not None and not step.files.rows:
                    result = ActionResult("success", summary="No files to move.", verification_status="verified")
                else:
                    approved = approve_files(step.files, range(len(step.files.rows))) if step.files else None
                    result = self._dispatch_tool(step.action, job, approved, step_progress)
            except (Exception, KeyboardInterrupt) as error:
                interrupted = isinstance(error, KeyboardInterrupt)
                result = ActionResult("cancelled" if interrupted else "failed", -15 if interrupted else 1,
                                      summary="Stopped." if interrupted else str(error))
            status = {"success": "completed", "cancelled": "cancelled"}.get(result.status, "failed")
            message = ("Files changed since review. Review the workflow again."
                       if result.status == "review" else result.summary)
            message, output = redact_secrets((message, result.stdout or result.stderr), known)
            reports.append(self._step_report(step, status, message, result.verification_status, output))
            undo_available |= result.undo_available
            has_partial_effects |= result.status == "partial" or bool(result.details.get("executed_count"))
            stopped = status == "cancelled" or job._cancel_event.is_set()
            try:
                self._record_verified_change(step.action, result)
            except OSError as error:
                logging.warning("Could not persist workflow step context: %s", error)
        return self._workflow_result(job, review, reports, undo_available, stopped, has_partial_effects)

    @staticmethod
    def _step_report(step, status, summary, verification="", output=""):
        action = step.action.action_type.value if step.action is not None else ""
        return WorkflowStepReport(step.number, action, step.risk.value, status, summary, verification, output)

    @staticmethod
    def _workflow_result(job, review, reports, undo_available, stopped, partial=False):
        counts = {status: sum(report.status == status for report in reports)
                  for status in ("completed", "failed", "skipped", "cancelled")}
        if stopped:
            status = "cancelled"
        elif counts["failed"]:
            status = "partial" if counts["completed"] or partial else "failed"
        else:
            status = "success"
        prefix = "Workflow stopped" if stopped else "Workflow"
        summary = f"{prefix}: " + ", ".join(f"{count} {name}" for name, count in counts.items()) + "."
        verified = status == "success" and all(report.verification == "verified" for report in reports)
        body = "\n".join(report.line() for report in reports)
        return ActionResult(status, -15 if stopped else 0 if status == "success" else 1,
                            summary=summary, stdout=body, undo_available=undo_available,
                            verification_status="verified" if verified else "unverified" if status == "success" else status,
                            details={"batch_id": job.batch_id or None, "undo_id": job.batch_id if undo_available else None,
                                     "risk": review.risk.value, "steps": [asdict(report) for report in reports]})

    def _record(self, job, result, query):
        action = job.action
        if isinstance(action, WorkflowPlan):
            append_history(query, "workflow", "completed" if result.status == "success" else result.status,
                           result.summary, job_id=job.id)
            append_audit_log({"job_id": job.id, "action": "workflow", "workflow": action.name,
                              "batch_id": result.details.get("batch_id"), "status": result.status,
                              "risk_level": result.details.get("risk", ""), "summary": result.summary,
                              "steps": result.details.get("steps", [])})
            return
        kind = action.action_type
        if result.status != "review":
            status = ("completed" if result.verification_status == "verified" else result.verification_status
                      if result.verification_status in ("launch_requested", "terminal_launched") else "executed"
                      ) if result.status == "success" else result.status
            append_history(query, str(getattr(kind, "value", kind)), status, result.summary, job_id=job.id)
        append_audit_log({
            "job_id": job.id, "action": str(getattr(kind, "value", kind)), "status": result.status,
            "exit_code": result.exit_code, "duration_ms": result.duration_ms, "summary": result.summary,
            "risk_level": job.action.risk_level.value if isinstance(job.action.risk_level, RiskLevel) else ""
        })
        self._record_verified_change(action, result)

    @staticmethod
    def _record_verified_change(action, result):
        kind = action.action_type
        if result.status == "success" and result.verification_status == "verified":
            is_package_change = kind in PACKAGE_ACTIONS and result.details.get("changed")
            is_change = (is_package_change or result.details.get("executed_count") or
                         (kind in {ActionType.SET_VOLUME, ActionType.SET_POWER_PROFILE, ActionType.CLEAN_CACHE,
                                   ActionType.DEFINE_CUSTOM_COMMAND, ActionType.DELETE_CUSTOM_COMMAND}
                          and not action.params.get("dry_run", False)) or
                         (kind == ActionType.CONTROL_MEDIA and action.params.get("action") != "status"))
            if is_change:
                change = {"action": kind.value, "summary": result.summary}
                context = {"last_verified_change": change}
                if is_package_change:
                    change.update(package_name=result.details["package"], manager=result.details["manager"],
                                  scope=result.details.get("scope"))
                    context["last_verified_package"] = change
                save_context(context)

    def _dispatch_tool(self, action: ActionRequest, job: Job, approval=None, progress=None) -> ActionResult:
        start_time = time.time()
        atype = action.action_type
        params = action.params

        if atype == ActionType.GET_SYSTEM_HEALTH:
            health = get_system_health()
            report = health.get("report", "")
            return ActionResult(
                status="success",
                exit_code=0,
                stdout=report,
                start_time=start_time,
                end_time=time.time(),
                duration_ms=round((time.time() - start_time) * 1000, 1),
                summary="System health report generated.",
                verification_status="verified",
                verification_details=("Direct /proc kernel metrics gathered." if get_platform().name == "linux"
                                      else "System metrics gathered using the active platform adapter.")
            )

        elif atype == ActionType.INSPECT_PROCESSES:
            sort_by = params.get("sort_by", "memory")
            limit = params.get("limit", 10)
            procs, table = inspect_processes(sort_by, limit)
            return ActionResult(
                status="success",
                exit_code=0,
                stdout=table,
                start_time=start_time,
                end_time=time.time(),
                duration_ms=round((time.time() - start_time) * 1000, 1),
                summary=f"Top {len(procs)} processes sorted by {sort_by}.",
                verification_status="verified",
                verification_details=f"Read {len(procs)} processes from process table.",
                details={"processes": procs}
            )

        elif atype == ActionType.INSPECT_STORAGE:
            category = params.get("category", "apps")
            limit = params.get("limit", 10)
            ok, report, details = inspect_storage_usage(category=category, limit=limit)
            return ActionResult(
                status="success" if ok else "failed",
                exit_code=0 if ok else 1,
                stdout=report,
                start_time=start_time,
                end_time=time.time(),
                duration_ms=round((time.time() - start_time) * 1000, 1),
                summary="Application storage breakdown generated." if ok else "Could not inspect application storage.",
                verification_status="verified" if ok else "failed",
                verification_details=f"Scanned {len(details.get('items', []))} storage consumers across Flatpak, Native, and User data."
            )

        elif atype in {ActionType.OPEN_APP, ActionType.OPEN_APPS, ActionType.RUN_IN_TERMINAL, ActionType.OPEN_URL,
                       ActionType.ARRANGE_WINDOWS, ActionType.OPEN_WORKSPACE}:
            pinned_targets = action.execution_meta.get("pinned_apps") if isinstance(action.execution_meta, dict) else None
            if atype == ActionType.OPEN_APP:
                pinned = pinned_targets.get(params["app_name"], _UNSET) if isinstance(pinned_targets, dict) else _UNSET
                ok, summary, details = open_app(params["app_name"], in_terminal=params.get("in_terminal", False), pinned=pinned)
            elif atype == ActionType.OPEN_APPS:
                ok, summary, details = launch_apps(
                    params["apps"], params.get("missing", ()),
                    cancel_token=job._cancel_event, pinned_targets=pinned_targets,
                )
            elif atype == ActionType.RUN_IN_TERMINAL:
                pinned = pinned_targets.get(params["command"], _UNSET) if isinstance(pinned_targets, dict) else _UNSET
                ok, summary, details = run_in_terminal(params["command"], pinned=pinned)
            elif atype == ActionType.OPEN_URL:
                ok, summary, details = open_url(params["url"], params["browser"])
            elif atype == ActionType.ARRANGE_WINDOWS:
                ok, summary, details = arrange_windows(
                    **params, cancel_token=job._cancel_event, pinned_targets=pinned_targets,
                )
            else:
                project, steps = resolve_workspace(params, load_user_config())
                if [step["label"] for step in steps] != action.affected_targets:
                    return ActionResult("failed", 1, summary="Workspace apps changed. Review the workspace again.")
                ok, summary, details = open_workspace(project, steps, job._cancel_event)
            steps = details.get("steps", [])
            if steps and job._cancel_event.is_set():
                summary = f"{sum(step['ok'] for step in steps)} launch request(s) sent before Stop."
            messages = [step["message"] for step in steps]
            if details.get("placement_hint"):
                messages.append(details["placement_hint"])
            body = details.get("report") or "\n".join(messages)
            return ActionResult("success" if ok else "partial" if details.get("partial") else "failed",
                                0 if ok else 1, stdout=body, summary=summary,
                                verification_status=details.get("verification", "launch_requested" if ok else "failed"),
                                details=details)

        elif atype == ActionType.LIST_FILES:
            cancel_token = job._cancel_event
            ok, report, details = list_files(
                params.get("path", "~/Downloads"),
                params.get("max_depth", 1),
                cancel_token=cancel_token
            )
            is_cancelled = details.get("cancelled", False) or job.cancellation_requested
            return ActionResult(
                status="cancelled" if is_cancelled else ("success" if ok else "failed"),
                exit_code=-15 if is_cancelled else (0 if ok else 1),
                stdout=report if (ok or is_cancelled) else "",
                stderr="" if (ok or is_cancelled) else report,
                start_time=start_time,
                end_time=time.time(),
                duration_ms=round((time.time() - start_time) * 1000, 1),
                summary="List files cancelled." if is_cancelled else (
                    f"Found {details.get('total_files', 0)} files in {details.get('path', '')}." if ok else "List files failed."
                ),
                details=details,
                verification_status="cancelled" if is_cancelled else ("verified" if ok else "failed")
            )

        elif atype in FILE_ACTIONS:
            if not isinstance(approval, FileApproval):
                review = review_files(atype.value, params, job._cancel_event)
                if job._cancel_event.is_set():
                    return ActionResult("cancelled", -15, summary="Stopped scanning files.")
                if not review.rows:
                    return ActionResult("success", summary="No files to move.", verification_status="verified")
                return ActionResult("review", summary=f"{len(review.rows)} file(s) to review.",
                                    details={"review": review}, verification_status="verified")
            grouped = ({"batch_id": job.batch_id, "workflow_step": job.workflow_step,
                        "created": job.created_move_directories} if job.batch_id else {})
            ok, report, details = apply_file_review(approval.review, approval.selected, job._cancel_event,
                                                    progress, **grouped)
            summary = report.splitlines()[0]
            status = ("cancelled" if details.get("cancelled") else "review" if details.get("review_required") else
                      "success" if ok else "partial" if details.get("executed_count") else "failed")
            return ActionResult(status, 0 if ok or status == "review" else -15 if status == "cancelled" else 1,
                                stdout=report, summary=summary, details=details,
                                undo_available=details.get("undo_available", False),
                                verification_status="verified" if ok or status == "partial" else status)

        elif atype in PACKAGE_ACTIONS:
            ok, summary, details = _package_operation(
                atype.value, params["package_name"], params["manager"], params.get("scope"),
                cancel_token=job._cancel_event, progress=progress)
            status = "cancelled" if details.get("cancelled") else "success" if ok else "failed"
            return ActionResult(status, -15 if status == "cancelled" else 0 if ok else 1,
                                summary=summary, details=details,
                                verification_status=details.get("verification", "unverified" if ok else "failed"))

        elif atype == ActionType.GET_POWER_PROFILE:
            ok, profile = get_power_profile()
            return ActionResult("success" if ok else "failed", 0 if ok else 1,
                                summary=f"Current power mode is {profile}." if ok else profile,
                                verification_status="verified" if ok else "failed")

        elif atype == ActionType.SET_VOLUME:
            ok, msg, ver = set_volume(**params)
            return ActionResult(
                status="success" if ok else "failed",
                exit_code=0 if ok else 1,
                stdout=ver if ver and ver != msg else "",
                stderr="" if ok else msg,
                start_time=start_time,
                end_time=time.time(),
                duration_ms=round((time.time() - start_time) * 1000, 1),
                summary=msg,
                verification_status="verified" if ok else "failed",
                verification_details=ver
            )

        elif atype == ActionType.LOCK_SESSION:
            ok, msg = lock_session()
            return ActionResult(
                status="success" if ok else "failed",
                exit_code=0 if ok else 1,
                stdout="",
                stderr="" if ok else msg,
                start_time=start_time,
                end_time=time.time(),
                duration_ms=round((time.time() - start_time) * 1000, 1),
                summary=msg,
                verification_status="launch_requested" if ok else "failed"
            )

        elif atype == ActionType.SET_POWER_PROFILE:
            ok, msg, ver = set_power_profile(params["profile"])
            return ActionResult(
                status="success" if ok else "failed",
                exit_code=0 if ok else 1,
                stdout=ver if ver and ver != msg else "",
                stderr="" if ok else msg,
                start_time=start_time,
                end_time=time.time(),
                duration_ms=round((time.time() - start_time) * 1000, 1),
                summary=msg,
                verification_status="verified" if ok else "failed",
                verification_details=ver
            )

        elif atype == ActionType.CONTROL_MEDIA:
            ok, msg, details = control_media(**params)
            status_text = details.get("status", "")
            artist = details.get("artist", "")
            title = details.get("title", "")
            track = f"{artist} - {title}" if (artist and title) else (artist or title or "")
            stdout_text = f"{status_text}: {track}" if track else (status_text or msg)
            return ActionResult(
                status="success" if ok else "failed",
                exit_code=0 if ok else 1,
                stdout=stdout_text,
                stderr="" if ok else msg,
                start_time=start_time,
                end_time=time.time(),
                duration_ms=round((time.time() - start_time) * 1000, 1),
                summary=msg,
                details=details,
                verification_status="verified" if ok else "failed",
                verification_details=stdout_text
            )

        elif atype == ActionType.GET_NETWORK_STATUS:
            net = get_network_status()
            report = net.get("report", "")
            ping = net.get("dns_latency_ms") or net.get("ping_ms")
            ping_str = f"{ping} ms" if ping is not None else "unreachable"
            summary = f"Network: {net.get('interface', 'unknown')} ({net.get('ip', 'no IP')}), Ping: {ping_str}"
            return ActionResult(
                status="success",
                exit_code=0,
                stdout=report,
                start_time=start_time,
                end_time=time.time(),
                duration_ms=round((time.time() - start_time) * 1000, 1),
                summary=summary,
                details=net,
                verification_status="verified",
                verification_details=f"Gateway: {net.get('gateway')}, Ping: {ping_str}"
            )

        elif atype == ActionType.CHECK_UPDATES:
            updates = check_updates()
            errors = updates.get("errors", {})
            status = "partial" if errors and updates.get("checked") else "failed" if errors else "success"
            total = updates.get("total_updates", 0)
            qualifier = " from the available results" if errors else ""
            summary = ("Update check failed." if status == "failed" else
                       "Package manager update report available." if total is None else
                       f"Found {total} pending system update(s){qualifier}.")
            return ActionResult(status, 0 if status == "success" else 1, stdout=updates.get("report", ""),
                                summary=summary, details=updates,
                                verification_status="verified" if status == "success" else status)

        elif atype == ActionType.CLEAN_CACHE:
            dry_run = params.get("dry_run", True)
            ok, report, details = clean_cache(dry_run=dry_run)
            status = ("partial" if details.get("deleted") and not ok or details.get("apt_sudo_needed")
                      else "success" if ok else "failed")
            return ActionResult(
                status=status,
                exit_code=0 if status == "success" else 1,
                stdout=report,
                stderr="" if status == "success" else report,
                start_time=start_time,
                end_time=time.time(),
                duration_ms=round((time.time() - start_time) * 1000, 1),
                summary=report.splitlines()[0] if report else ("Cache preview ready." if dry_run else "Cache cleaned."),
                details=details,
                verification_status="verified" if status == "success" else status,
                verification_details=f"Reclaimable: {details.get('total_reclaimable_mb', 0)} MB"
            )

        elif atype == ActionType.DEFINE_CUSTOM_COMMAND:
            trig = params.get("trigger", "")
            target_act = params.get("target_action", "")
            target_par = params.get("params", {})
            summ = params.get("summary", "")
            set_custom_command(trig, target_act, target_par, summ)
            msg = f"✓ Custom command registered: '{trig}' -> {target_act}"
            return ActionResult(
                status="success",
                exit_code=0,
                stdout=f"Shortcut '{trig}' successfully bound to action '{target_act}'.\nYou can now type '{trig}' anytime to execute this action instantly.",
                start_time=start_time,
                end_time=time.time(),
                duration_ms=round((time.time() - start_time) * 1000, 1),
                summary=msg,
                details={"trigger": trig, "action": target_act, "params": target_par},
                verification_status="verified",
                verification_details="Saved to user configuration."
            )

        elif atype == ActionType.DELETE_CUSTOM_COMMAND:
            trig = params.get("trigger", "")
            deleted = delete_custom_command(trig)
            if deleted:
                summary_msg = f"Deleted custom command '{trig}'."
                stdout_msg = f"Shortcut trigger '{trig}' has been removed from configuration."
            else:
                summary_msg = f"Custom command '{trig}' was not found."
                stdout_msg = f"No custom command found with trigger '{trig}'."
            return ActionResult(
                status="success" if deleted else "failed",
                exit_code=0 if deleted else 1,
                stdout=stdout_msg,
                start_time=start_time,
                end_time=time.time(),
                duration_ms=round((time.time() - start_time) * 1000, 1),
                summary=summary_msg,
                verification_status="verified" if deleted else "failed",
                verification_details=f"Deleted: {deleted}"
            )

        elif atype == ActionType.MCP_TOOL:
            server_name = params["server"]
            tool_name = params["tool"]
            arguments = params.get("arguments", {})
            known = configured_api_keys()
            expected_fp = (
                action.execution_meta.get("mcp_fingerprint", "")
                if isinstance(getattr(action, "execution_meta", None), dict)
                else ""
            )
            try:
                registry = load_mcp_registry(refresh=True)
                grants = load_mcp_grants(refresh=True)
                server_cfg = registry.servers.get(server_name)
                grant = grants.get(server_name)
                enforce_mcp_access(server_cfg, tool_name, grant=grant)
                snapshot = discover_server_tools(server_name, refresh=False)
                if snapshot.error:
                    raise ValueError(snapshot.error)
                tool_spec = next((item for item in snapshot.tools if item.name == tool_name), None)
                if tool_spec is None:
                    raise ValueError(f"MCP tool '{server_name}/{tool_name}' was not found on server '{server_name}'.")
                current_risk, _ = evaluate_mcp_tool_risk(server_cfg, tool_spec, grant=grant)
                validate_mcp_arguments(tool_spec, arguments, strict=(current_risk != RiskLevel.READ_ONLY))
                current_fp = mcp_policy_fingerprint(server_cfg, grant, tool_spec, current_risk)
                if not expected_fp or current_fp != expected_fp or current_risk != action.risk_level:
                    raise ValueError(
                        f"MCP tool '{server_name}/{tool_name}' policy or metadata changed since review. Review again."
                    )
            except (ValueError, OSError) as error:
                msg = redact_secrets(str(error), known)
                return ActionResult(
                    status="failed",
                    exit_code=1,
                    stderr=msg,
                    start_time=start_time,
                    end_time=time.time(),
                    duration_ms=round((time.time() - start_time) * 1000, 1),
                    summary=msg,
                    verification_status="failed",
                )
            try:
                mcp_result = call_mcp_tool(server_name, tool_name, arguments, cancel_token=job._cancel_event)
            except MCPError as error:
                is_cancelled = error.code == "cancelled" or job._cancel_event.is_set()
                msg = redact_secrets(str(error), known)
                return ActionResult(
                    status="cancelled" if is_cancelled else "failed",
                    exit_code=-15 if is_cancelled else 1,
                    stderr=msg,
                    start_time=start_time,
                    end_time=time.time(),
                    duration_ms=round((time.time() - start_time) * 1000, 1),
                    summary=f"mcp · {server_name}/{tool_name} {'stopped' if is_cancelled else 'failed'}: {msg}",
                    verification_status="cancelled" if is_cancelled else "failed",
                )
            clean_text = _ANSI_ESCAPE.sub("", mcp_result.text or "")
            clean_text = redact_secrets(clean_text, known)[:_MAX_MCP_OUTPUT_CHARS]
            ok = not mcp_result.is_error
            summary = f"mcp · {server_name}/{tool_name} {'completed' if ok else 'reported an error'}."
            return ActionResult(
                status="success" if ok else "failed",
                exit_code=0 if ok else 1,
                stdout=clean_text if ok else "",
                stderr="" if ok else clean_text,
                start_time=start_time,
                end_time=time.time(),
                duration_ms=round((time.time() - start_time) * 1000, 1),
                summary=summary,
                details={
                    "server": server_name,
                    "tool": tool_name,
                    "era": mcp_result.era,
                    "protocol_version": mcp_result.protocol_version,
                    "untrusted_tool_output": True,
                },
                verification_status="server_reported_success" if ok else "failed",
                verification_details=f"MCP ({mcp_result.era} / {mcp_result.protocol_version})",
            )

        return ActionResult(
            status="failed",
            exit_code=1,
            stderr=f"No dispatcher implementation for action {atype.value}",
            summary="Unimplemented action."
        )
