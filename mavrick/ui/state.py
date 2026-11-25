"""Data behind one HUD window, independent of GTK widgets."""

from dataclasses import dataclass, field
from threading import Event
from typing import Any
import uuid

from ..executor import ActionResult
from ..schema import ActionRequest, WorkflowPlan
from ..workflows import WorkflowReview
from ..tools.files import FileReview, FileRow


@dataclass
class HUDState:
    pending_action: ActionRequest | None = None
    pending_workflow: WorkflowPlan | None = None
    workflow_review: WorkflowReview | None = None
    workflow_catalog_loading: bool = False
    current_query: str = ""
    history: list[dict[str, Any] | str] = field(default_factory=list)
    history_draft: str = ""
    history_idx: int = 0
    history_mode: bool = False
    command_filter: list[str] | None = None
    history_generation: int = 0
    activity: list[dict[str, Any]] = field(default_factory=list)
    activity_loading: bool = False
    is_processing: bool = False
    request_id: str | None = None
    cancel_token: Event | None = None
    file_review: FileReview | None = None
    file_rows: list[FileRow] = field(default_factory=list)
    retry_review: FileReview | None = None
    retry_action: ActionRequest | None = None
    undo_id: str | None = None
    health_running: bool = False
    app_index_running: bool = False
    provider_label: str = "agy"
    result_source: str = ""
    theme: str = "light"
    manual_resize: bool = False
    renaming: bool = False
    closed: bool = False

    def begin_request(self, query: str) -> None:
        self.current_query = query
        self.request_id = uuid.uuid4().hex
        self.cancel_token = Event()
        self.pending_action = None
        self.pending_workflow = None
        self.workflow_review = None
        self.file_review = None
        self.retry_review = None
        self.history_mode = False
        self.renaming = False


@dataclass(frozen=True)
class ResultPresentation:
    status: str
    style: str
    body: str

    @classmethod
    def from_result(cls, result: ActionResult) -> "ResultPresentation":
        status = {
            "success": "Done", "failed": "Failed", "partial": "Partial result",
            "cancelled": "Stopped", "busy": "Busy",
        }.get(result.status, "Done")
        if result.status == "success" and result.verification_status == "launch_requested":
            status = "Launch requested"
        style = ("error" if result.status in ("failed", "partial", "busy")
                 else "cancelled" if result.status == "cancelled" else "done")
        body = result.stdout or result.stderr
        if body == result.summary:
            body = ""
        return cls(status, style, body.removeprefix(result.summary + "\n"))


def state_field(name: str) -> property:
    """Maintain direct window attributes used by existing callers."""
    return property(lambda window: getattr(window.state, name),
                    lambda window, value: setattr(window.state, name, value))
