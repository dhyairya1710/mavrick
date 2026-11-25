"""Toolkit-agnostic HUD controller and background worker scheduling."""

from __future__ import annotations

from concurrent.futures import Future
from dataclasses import replace
import json
import logging
import os
import socket
import subprocess
import threading
from typing import TYPE_CHECKING, Any, Callable, Protocol, TypeVar

from .. import ui as api
from ..config import AUDIT_LOG_FILE, configured_api_keys, load_provider_settings
from ..platform import get_platform
from ..executor import ActionResult, approve_action, approve_files
from ..history import append_history, clear_history_and_context, load_history, redact_secrets
from ..router import route_local
from ..workflows import approve_workflow, workflow_catalog
from ..schema import ACTION_RISK_MAP, ActionRequest, ActionType, ModelResponse, RiskLevel, WorkflowPlan
from ..tools.files import rename_review_row
from ..tools.apps import app_index_status, instant_app_index

from .state import HUDState, ResultPresentation, state_field

if TYPE_CHECKING:
    from .window import MavrickWindow

Result = TypeVar("Result")


class UIScheduler(Protocol):
    """Contract for scheduling callbacks onto the active UI thread."""

    def call_soon(self, callback: Callable[..., Any], *args: Any) -> Any: ...


class InlineScheduler:
    """Execute scheduled UI callbacks synchronously (headless testing and fallback)."""

    def call_soon(self, callback: Callable[..., Any], *args: Any) -> Any:
        return callback(*args)


class GLibScheduler:
    """Dispatch callbacks onto the GTK/GLib main loop lazily without top-level gi coupling."""

    def call_soon(self, callback: Callable[..., Any], *args: Any) -> Any:
        from gi.repository import GLib

        return GLib.idle_add(callback, *args)


def default_ui_scheduler() -> UIScheduler:
    try:
        import gi  # noqa: F401
        return GLibScheduler()
    except ImportError:
        return InlineScheduler()


class HUDView(Protocol):
    """View contract driven by HUDController across GTK, Tk, or headless UI frontends."""

    state: HUDState

    def schedule_ui(self, callback: Callable[..., Any], *args: Any) -> Any: ...
    def set_status(self, text: str, state: str = "ready") -> None: ...
    def set_result_source(self, source: str) -> None: ...
    def get_entry_text(self) -> str: ...
    def set_entry_text(self, text: str) -> None: ...
    def set_entry_enabled(self, enabled: bool) -> None: ...
    def focus_entry(self) -> None: ...
    def is_review_visible(self) -> bool: ...
    def is_browse_visible(self) -> bool: ...
    def show_instant_preview(self, text: str) -> None: ...
    def hide_instant_preview(self) -> None: ...
    def set_progress_text(self, text: str) -> None: ...
    def _show_progress(self, label: str) -> None: ...
    def _settle_progress(self) -> None: ...
    def set_stop_enabled(self, enabled: bool) -> None: ...
    def set_history_enabled(self, enabled: bool) -> None: ...
    def set_history_button_label(self, text: str) -> None: ...
    def clear_output_text(self) -> None: ...
    def show_output_view(self) -> None: ...
    def render_welcome_state(self) -> None: ...
    def render_output_card(self, title: str, body: str = "", duration_ms: float = 0.0,
                           verification_status: str = "verified", is_error: bool = False) -> None: ...
    def show_processes(self, processes: list[dict[str, Any]]) -> None: ...
    def set_technical_details(self, text: str) -> None: ...
    def show_result_actions(self, *, show_undo: bool, show_retry: bool) -> None: ...
    def set_clipboard_text(self, text: str) -> None: ...
    def show_review_card(self, action: ActionRequest) -> None: ...
    def show_workflow_review(self, review: Any, summary: str) -> None: ...
    def show_file_review(self, review: Any, summary: str) -> None: ...
    def get_selected_file_indices(self) -> list[int]: ...
    def set_review_error(self, message: str) -> None: ...
    def update_renamed_file_row(self, index: int, label: Any, updated: Any) -> None: ...
    def render_history_view(self) -> None: ...
    def toggle_history(self) -> None: ...
    def adjust_window_size(self, content_lines: int | None = None) -> None: ...
    def is_visible(self) -> bool: ...
    def show_window(self) -> bool: ...
    def apply_chip(self, snippet: str) -> None: ...
    def get_history_queries(self) -> list[str]: ...


def submit(task: Callable[[], Result]) -> Future[Result]:
    future: Future[Result] = Future()

    def worker():
        if future.set_running_or_notify_cancel():
            try:
                future.set_result(task())
            except Exception as error:
                future.set_exception(error)

    threading.Thread(target=worker, daemon=True).start()
    return future


def deliver(future: Future[Result], done: Callable[[Future[Result]], None], *,
            scheduler: UIScheduler | None = None) -> None:
    target = scheduler or default_ui_scheduler()
    future.add_done_callback(lambda completed: target.call_soon(done, completed))


def erase_history() -> bool:
    cleared = clear_history_and_context()
    try:
        os.unlink(AUDIT_LOG_FILE)
    except FileNotFoundError:
        pass
    except OSError as error:
        logging.warning("Could not remove audit log: %s", error)
        cleared = False
    return cleared


class HUDController:
    pending_action = state_field("pending_action")
    current_query = state_field("current_query")
    history = state_field("history")
    history_draft = state_field("history_draft")
    history_idx = state_field("history_idx")
    is_processing = state_field("is_processing")
    history_mode = state_field("history_mode")
    request_id = state_field("request_id")
    cancel_token = state_field("cancel_token")
    file_review = state_field("file_review")
    file_rows = state_field("file_rows")
    retry_review = state_field("retry_review")
    retry_action = state_field("retry_action")
    undo_id = state_field("undo_id")
    health_running = state_field("health_running")
    manual_resize = state_field("manual_resize")

    def _ui_scheduler(self) -> UIScheduler:
        scheduler = getattr(self, "ui_scheduler", None)
        return scheduler if scheduler is not None else default_ui_scheduler()

    def schedule_ui(self, callback: Callable[..., Any], *args: Any) -> Any:
        return self._ui_scheduler().call_soon(callback, *args)

    def refresh_health_badge(self, *, probe: bool = True) -> None:
        if self.health_running:
            return
        self.health_running = True

        def worker():
            try:
                health = (api.check_ai_health() if probe else
                          {"provider": load_provider_settings().name, "status": "configured"})
            except Exception:
                health = {"status": "offline"}
            self.schedule_ui(self._show_health_badge, health)

        threading.Thread(target=worker, daemon=True).start()

    def refresh_app_index(self) -> None:
        if self.state.app_index_running or app_index_status().startswith("warm"):
            return
        self.state.app_index_running = True

        def ready(future):
            self.state.app_index_running = False
            if not self.state.closed and future.exception() is None:
                self.update_local_preview(refresh=False)

        deliver(submit(instant_app_index), ready, scheduler=self._ui_scheduler())

    def refresh_workflows(self) -> None:
        if self.state.workflow_catalog_loading or workflow_catalog(cached_only=True) is not None:
            return
        self.state.workflow_catalog_loading = True

        def ready(future):
            self.state.workflow_catalog_loading = False
            if not self.state.closed and future.exception() is None:
                self.update_local_preview(refresh=False)

        deliver(submit(workflow_catalog), ready, scheduler=self._ui_scheduler())

    def update_local_preview(self, _entry: Any = None, *, refresh: bool = True) -> None:
        if self.is_processing or self.history_mode or self.is_review_visible() or self.is_browse_visible():
            self.hide_instant_preview()
            return
        text = self.get_entry_text()
        match = route_local(text, cached_only=True)
        preview = {
            "app": "Open {} · Enter to launch",
            "multi_app": "Open {} · Enter to launch",
            "calculator": "= {} · Enter to copy",
            "undo": "Review Undo · Enter",
            "history": "Show activity history · Enter",
            "workflow": "Workflow · {} · Enter to review",
        }.get(match.kind if match else "")
        if preview:
            self.show_instant_preview("Instant · " + preview.format(match.value))
        else:
            self.hide_instant_preview()
        if refresh and text:
            self.refresh_app_index()
            if text.lstrip().lower().startswith(("/", "run ")):
                self.refresh_workflows()

    def _begin_request(self, query: str) -> None:
        self.state.begin_request(query)
        self.set_result_source("")
        self.set_history_button_label("⏱ History")
        self.clear_output_text()
        self._show_progress("Thinking…")

    def on_submit(self, entry: Any = None) -> None:
        raw = entry.get_text() if entry is not None and hasattr(entry, "get_text") else self.get_entry_text()
        query = raw.strip()
        if not query or self.is_processing or self.pending_action or self.state.pending_workflow:
            return
        instant = api.instant_response(query, cached_only=True)
        self._begin_request(query)
        if instant is not None:
            self.handle_model_response(query, instant, self.request_id)
            return
        threading.Thread(target=self.run_ai_pipeline, args=(query, self.request_id, self.cancel_token), daemon=True).start()

    def run_ai_pipeline(self, query: str, request_id: str, cancel_token: threading.Event | None = None) -> None:
        try:
            response = api.query_model(query, cancel_token=cancel_token)
        except Exception as error:
            response = ModelResponse("error", validation_error=redact_secrets(str(error)))
        self.schedule_ui(self.handle_model_response, query, response, request_id)

    def handle_model_response(self, query: str, response: ModelResponse, request_id: str | None = None) -> None:
        if self.state.closed or (request_id is not None and request_id != self.request_id):
            return
        self.set_result_source(response.source)
        if self.cancel_token is not None and self.cancel_token.is_set():
            self._settle_progress()
            self.render_welcome_state()
            self.set_status("● Stopped", "cancelled")
            self.record_history(query, status="cancelled", summary="Stopped", job_id=self.request_id)
        elif response.response_type == "history":
            self._settle_progress()
            self.set_status("● Activity", "ready")
            self.toggle_history()
        elif response.plan is not None:
            self.state.pending_workflow = response.plan
            self.execute_pending_action()
        elif response.action_request is not None:
            self.pending_action = response.action_request
            self.execute_pending_action()
        else:
            self._settle_progress()
            failed = response.response_type == "error"
            message = ((response.validation_error or "Request failed.") if failed
                       else (response.message or "No actionable response."))
            copied = response.response_type == "calculator"
            if copied:
                self.set_clipboard_text(message)
            self.set_status("● Error" if failed else "● Copied" if copied else "● Done",
                            "error" if failed else "done")
            self.render_output_card(message, verification_status="", is_error=failed)
            self.show_output_view()
            self.adjust_window_size()
            self.focus_entry()
            self.record_history(query, status="failed" if failed else "completed", summary=message, job_id=self.request_id)

    def on_confirm_action(self) -> None:
        if self.is_processing or self.state.renaming:
            return
        if self.state.workflow_review is not None:
            approval = approve_workflow(self.state.workflow_review)
        elif not self.pending_action:
            return
        elif self.file_review:
            selected = self.get_selected_file_indices()
            if not selected:
                return
            approval = approve_files(replace(self.file_review, rows=tuple(self.file_rows)), selected)
        else:
            approval = approve_action(self.pending_action)
        self.execute_pending_action(approval)

    def on_edit_action(self) -> None:
        self.on_cancel_action(record=False)
        self.apply_chip(self.current_query)

    def on_cancel_action(self, record: bool = True) -> None:
        action = self.pending_action
        action_type = "workflow" if self.state.pending_workflow else action.action_type.value if action else None
        self.pending_action = None
        self.state.pending_workflow = None
        self.state.workflow_review = None
        self.file_review = None
        self.state.renaming = False
        self.set_entry_enabled(True)
        self.set_history_enabled(True)
        self.render_welcome_state()
        self.set_status("● Cancelled", "cancelled")
        if record and self.current_query:
            self.record_history(self.current_query, action_type=action_type,
                           status="cancelled", summary="Cancelled by user", job_id=self.request_id)

    def execute_pending_action(self, approval=None) -> None:
        action = self.state.pending_workflow or self.pending_action
        if not action:
            return
        request_id, query, cancel = self.request_id, self.current_query, self.cancel_token
        self._show_progress("Applying review…" if approval else "Checking request…")

        def worker():
            try:
                options = {"preview": True} if isinstance(action, WorkflowPlan) and approval is None else {}
                result = self.executor.execute(
                    action, approval=approval, job_id=request_id, query=query, cancel_token=cancel,
                    progress_callback=lambda message: self.schedule_ui(self._update_progress, request_id, message),
                    **options)
            except Exception as error:
                result = ActionResult("failed", 1, summary=str(error), job_id=request_id,
                                      verification_status="failed")
            self.schedule_ui(self.on_execution_complete, action, result)

        threading.Thread(target=worker, daemon=True).start()

    def _update_progress(self, request_id: str, message: str) -> None:
        if not self.state.closed and self.request_id == request_id and self.is_processing:
            if not self.cancel_token.is_set() or message.startswith("Stop requested"):
                self.set_progress_text(message)

    def on_stop_action(self) -> None:
        if not self.is_processing or self.cancel_token is None or self.cancel_token.is_set():
            return
        self.cancel_token.set()
        self.set_stop_enabled(False)
        self.set_status("● Stopping", "cancelled")
        self.set_progress_text("Waiting for the active operation to settle…")
        request_id = self.request_id
        submit(lambda: self.executor.cancel_current_job(request_id))

    def send_desktop_notification(self, title: str, summary: str) -> None:
        """Sends native desktop notification via notify-send subprocess."""
        try:
            subprocess.Popen(
                ["notify-send", title, summary],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
        except Exception as e:
            logging.debug(f"Desktop notification failed: {e}")

    def _notify_in_background(self, title: str, summary: str) -> None:
        submit(lambda: HUDController.send_desktop_notification(self, title, summary))

    def on_execution_complete(self, action: ActionRequest | WorkflowPlan, result: ActionResult) -> None:
        if self.state.closed or (result.job_id and result.job_id != self.request_id):
            return
        self._settle_progress()
        if self.cancel_token and self.cancel_token.is_set() and result.status in ("requires_confirmation", "review", "workflow_review"):
            self.on_cancel_action()
            return
        if result.status == "workflow_review":
            self.state.workflow_review = result.details["workflow_review"]
            self.show_workflow_review(self.state.workflow_review, result.summary)
            return
        if result.status == "requires_confirmation":
            self.pending_action = result.details["action"]
            self.show_review_card(self.pending_action)
            return
        if result.status == "review":
            self.pending_action = action
            self.show_file_review(result.details["review"], result.summary)
            return

        self.pending_action = None
        self.state.pending_workflow = None
        self.state.workflow_review = None
        self.file_review = None
        self.retry_review = result.details.get("review")
        self.retry_action = action if self.retry_review else None
        self.undo_id = result.details.get("undo_id") if result.undo_available else None
        view = ResultPresentation.from_result(result)
        self.set_status("● " + view.status, view.style)
        processes = result.details.get("processes")
        self.render_output_card(result.summary, "" if processes is not None else view.body,
                                verification_status="", is_error=view.style == "error")
        self.show_output_view()
        if processes is not None:
            self.show_processes(processes)
        technical = "\n".join(filter(None, [f"Verification: {result.verification_status}",
                                            f"Duration: {result.duration_ms:.0f} ms",
                                            result.verification_details,
                                            f"Undo ID: {self.undo_id}" if self.undo_id else ""]))
        for step in result.details.get("steps", ()) if isinstance(action, WorkflowPlan) else ():
            output = step["output"]
            if output:
                technical += f"\n\nStep {step['number']} · {step['verification']}\n{output}"
        self.set_technical_details(technical)
        self.show_result_actions(show_undo=bool(self.undo_id), show_retry=bool(self.retry_review))
        self.adjust_window_size()
        if self.is_visible():
            self.focus_entry()
        if not self.is_visible() or result.duration_ms > 1500:
            self.send_desktop_notification("MAVRICK", result.summary or view.status)
        self.refresh_history()

    def on_review_changed(self, _button: Any = None) -> None:
        if not self.is_processing and self.retry_review:
            review, action = self.retry_review, self.retry_action
            self._begin_request(self.current_query)
            self._settle_progress()
            self.pending_action = action
            self.show_file_review(review, "Review the file rows that changed.")

    def on_trigger_undo(self) -> None:
        if self.is_processing or self.pending_action or self.state.pending_workflow:
            return
        params = {"undo_id": self.undo_id} if self.undo_id else {}
        self._begin_request("Undo")
        self.set_result_source("instant")
        self.pending_action = ActionRequest(ActionType.UNDO, params, "Undo", RiskLevel.REVERSIBLE)
        self.execute_pending_action()

    def request_file_rename(self, index: int, label: Any, name: str) -> None:
        if self.state.renaming:
            return
        review, row = self.file_review, self.file_rows[index]
        self.state.renaming = True
        self.update_file_count()

        def ready(future):
            if self.state.closed or self.file_review is not review:
                return
            self.state.renaming = False
            try:
                updated = future.result()
            except Exception as error:
                self.set_review_error(str(error))
            else:
                self.file_rows[index] = updated
                self.update_renamed_file_row(index, label, updated)
            self.update_file_count()

        deliver(submit(lambda: rename_review_row(row, name)), ready, scheduler=self._ui_scheduler())

    def record_history(self, query: str, **fields: Any) -> None:
        def save():
            append_history(query, **fields)
            return load_history()

        self._load_history_async(save)

    def get_history_queries(self) -> list[str]:
        """Return recent unique query strings in chronological order."""
        raw_hist = api.load_history(self.state.history)
        queries: list[str] = []
        for h in raw_hist:
            if isinstance(h, dict):
                q = h.get("query", "").strip()
            elif isinstance(h, str):
                q = h.strip()
            else:
                q = ""
            if q and (not queries or queries[-1] != q):
                queries.append(q)
        return queries

    def refresh_history(self, *, include_audit: bool = False) -> None:
        if include_audit:
            self.state.activity_loading = True

            def read():
                history = load_history()
                return history, self.load_combined_audit_history(history)
        else:
            read = load_history
        self._load_history_async(read, include_audit=include_audit)

    def _load_history_async(self, read, *, include_audit: bool = False) -> None:
        generation = self.state.history_generation

        def ready(future):
            if self.state.closed or generation != self.state.history_generation:
                return
            if include_audit:
                self.state.activity_loading = False
            try:
                value = future.result()
            except Exception as error:
                logging.warning("Could not read Mavrick history: %s", error)
            else:
                at_draft = self.history_idx >= len(self.get_history_queries())
                if include_audit:
                    self.history, self.state.activity = value
                else:
                    self.history = value
                if at_draft:
                    self.history_idx = len(self.get_history_queries())
            if include_audit and self.history_mode:
                self.render_history_view()

        deliver(submit(read), ready, scheduler=self._ui_scheduler())

    def load_combined_audit_history(self, history_items=None) -> list[dict[str, Any]]:
        """Loads conversational history combined with detailed audit logs."""
        audit_by_time: dict[str, dict[str, Any]] = {}
        audit_by_job: dict[str, dict[str, Any]] = {}
        audit_list: list[dict[str, Any]] = []

        if os.path.exists(AUDIT_LOG_FILE):
            try:
                known = configured_api_keys()
                with open(AUDIT_LOG_FILE, "r", encoding="utf-8") as fp:
                    for line in fp:
                        line = line.strip()
                        if line:
                            try:
                                rec = redact_secrets(json.loads(line), known)
                                audit_list.append(rec)
                                if rec.get("job_id"):
                                    audit_by_job[rec["job_id"]] = rec
                                dstr = rec.get("date_str")
                                if dstr:
                                    audit_by_time[dstr] = rec
                            except Exception:
                                pass
            except Exception as e:
                logging.debug(f"Error loading audit log: {e}")

        if history_items is None:
            history_items = load_history()
        records: list[dict[str, Any]] = []

        for h in history_items:
            date_str = h.get("date_str", "")
            action = h.get("action")
            status = h.get("status", "completed")
            query = h.get("query", "")
            summary = h.get("summary", "")

            # Match with audit log if available
            job_id = h.get("job_id")
            matched_audit = audit_by_job.get(job_id) if job_id else audit_by_time.get(date_str)
            if not job_id and not matched_audit and audit_list:
                h_ts = h.get("timestamp", 0)
                for a in reversed(audit_list):
                    if abs(a.get("timestamp", 0) - h_ts) < 5.0 and (not action or a.get("action") == action):
                        matched_audit = a
                        break

            # Risk Level
            if matched_audit and "risk_level" in matched_audit:
                risk = matched_audit["risk_level"]
            elif action:
                try:
                    act_enum = ActionType(action)
                    risk = ACTION_RISK_MAP.get(act_enum, RiskLevel.READ_ONLY).value
                except Exception:
                    risk = "read_only"
            else:
                risk = "read_only"

            # Parameters / Targets
            params = {}
            if matched_audit and "params" in matched_audit:
                params = matched_audit["params"]

            records.append({
                "date_str": date_str,
                "timestamp": h.get("timestamp", 0),
                "query": query,
                "action": action,
                "status": status,
                "risk_level": risk,
                "params": params,
                "summary": summary,
                "job_id": job_id
            })

        recorded_jobs = {r["job_id"] for r in records if r["job_id"]}
        recorded_dates = {r["date_str"] for r in records if r["date_str"]}
        for a in audit_list:
            job_id = a.get("job_id")
            if (not job_id and a.get("date_str") not in recorded_dates or
                    job_id and job_id not in recorded_jobs and audit_by_job[job_id] is a):
                records.append({
                    "date_str": a.get("date_str", ""),
                    "timestamp": a.get("timestamp", 0),
                    "query": a.get("summary", a.get("action", "")),
                    "action": a.get("action"),
                    "status": a.get("status", "success"),
                    "risk_level": a.get("risk_level", "read_only"),
                    "params": a.get("params", {}),
                    "summary": a.get("summary", ""),
                    "job_id": job_id
                })

        records.sort(key=lambda r: r.get("timestamp", 0))
        return records


def _hud_startup_lock():
    return get_platform().lock_file(api.MAVRICK_SOCK + ".lock")


def prepare_hud_socket() -> socket.socket | None:
    """Toggle an existing HUD or reserve its endpoint on the startup worker."""
    if api.client_send_toggle():
        return None
    api.ensure_secure_storage()
    with _hud_startup_lock():
        if api.client_send_toggle():
            return None
        return _bind_hud_socket()


def _bind_hud_socket() -> socket.socket:
    try:
        os.unlink(api.MAVRICK_SOCK)
    except FileNotFoundError:
        pass
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(api.MAVRICK_SOCK)
        os.chmod(api.MAVRICK_SOCK, 0o600)
        server.listen(5)
    except BaseException:
        server.close()
        raise
    return server


def close_hud_socket(server: socket.socket) -> None:
    with _hud_startup_lock():
        try:
            server.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        finally:
            server.close()
        try:
            os.unlink(api.MAVRICK_SOCK)
        except FileNotFoundError:
            pass


def listen_for_hud_requests(window: HUDView, server: socket.socket) -> None:
    if hasattr(window, "_has_activation_listener"):
        window._has_activation_listener = True

    def submit_query(prompt):
        if window.state.closed or window.is_processing or window.pending_action or window.state.pending_workflow:
            return
        window.show_window()
        window.set_entry_text(prompt)
        window.on_submit()

    def listen_loop():
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            with conn:
                conn.settimeout(0.25)
                try:
                    data = conn.recv(1024).decode("utf-8").strip()
                except (OSError, UnicodeError):
                    continue
            if data == "toggle":
                window.schedule_ui(toggle_visibility, window)
            elif data == "show":
                window.schedule_ui(window.show_window)
            elif data.startswith("query:"):
                window.schedule_ui(submit_query, data[6:].strip())

    threading.Thread(target=listen_loop, daemon=True).start()


def start_socket_listener(window: MavrickWindow) -> socket.socket:
    """Bind and start a listener for callers of the original module API."""
    server = _bind_hud_socket()
    listen_for_hud_requests(window, server)
    return server


def toggle_visibility(window: MavrickWindow) -> bool:
    if window.state.closed:
        return False
    if window.is_visible():
        window.hide_window()
    else:
        window.show_window()
    return False

def client_send_toggle() -> bool:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(0.5)
            client.connect(api.MAVRICK_SOCK)
            client.sendall(b"toggle\n")
        return True
    except (FileNotFoundError, ConnectionRefusedError):
        return False
