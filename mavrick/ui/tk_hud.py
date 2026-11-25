"""Zero-dependency cross-platform Tkinter Spotlight HUD driven by HUDController."""

from __future__ import annotations

import logging
import os
import queue
from typing import Any, Callable

from ..config import PROVIDER_LABELS, load_theme_setting
from ..executor import ActionExecutor
from ..platform import get_platform
from ..policy import WorkflowRisk
from ..schema import ActionRequest, ActionType, RiskLevel
from ..tools.files import FileReview, FileRow, display_path
from .controller import (
    HUDController,
    UIScheduler,
    close_hud_socket,
    listen_for_hud_requests,
    prepare_hud_socket,
    submit,
)
from .state import HUDState
from .styles import theme_colors

_ACTION_LABELS = {
    ActionType.INSTALL_PACKAGE: "Install",
    ActionType.UNINSTALL_PACKAGE: "Uninstall",
    ActionType.OPEN_WORKSPACE: "Open workspace",
    ActionType.SET_VOLUME: "Apply audio change",
    ActionType.SET_POWER_PROFILE: "Set power mode",
    ActionType.LOCK_SESSION: "Lock session",
    ActionType.CLEAN_CACHE: "Clean cache",
    ActionType.DEFINE_CUSTOM_COMMAND: "Save command",
    ActionType.DELETE_CUSTOM_COMMAND: "Delete command",
    ActionType.CONTROL_MEDIA: "Apply playback change",
    ActionType.MCP_TOOL: "Run MCP tool",
}

_WORKFLOW_STYLE_MAP = {
    WorkflowRisk.READ_ONLY: RiskLevel.READ_ONLY,
    WorkflowRisk.MUTATING: RiskLevel.REVERSIBLE,
    WorkflowRisk.PRIVILEGED: RiskLevel.PRIVILEGED,
    WorkflowRisk.DESTRUCTIVE: RiskLevel.DESTRUCTIVE,
}

_COMMANDS = [
    ("Organize Downloads", "organize downloads"),
    ("System health", "system health"),
    ("Coding workspace", "open my coding workspace"),
    ("Current power mode", "current power mode"),
    ("Network status", "network status"),
    ("Inspect memory usage", "what is eating my ram"),
    ("Check updates", "check updates"),
    ("Storage usage", "which app has most storage used"),
    ("Pause Spotify", "pause spotify"),
    ("Resume Spotify", "resume spotify"),
    ("Mute audio", "mute"),
    ("Preview cache cleanup", "clean cache"),
    ("Undo a file move", "undo"),
    ("Install package", "install htop"),
]


def _import_tk():
    import tkinter as tk
    return tk


def tk_available() -> bool:
    """Return True if Python's stdlib tkinter module can be imported cleanly."""
    try:
        import importlib.util
        import sys

        if "tkinter" in sys.modules:
            return sys.modules["tkinter"] is not None
        return importlib.util.find_spec("tkinter") is not None
    except (ImportError, ValueError):
        return False


def review_risk_style(risk: RiskLevel, palette: dict[str, str]) -> dict[str, str]:
    """Compute platform-agnostic card colors and focus rules for a risk level."""
    is_destructive = risk == RiskLevel.DESTRUCTIVE
    is_privileged = risk == RiskLevel.PRIVILEGED
    border = (
        palette["danger"] if is_destructive else
        palette["warning"] if is_privileged else
        palette["border"]
    )
    badge_fg = (
        palette["danger"] if is_destructive else
        palette["warning_ink"] if is_privileged else
        palette["success_ink"] if risk == RiskLevel.READ_ONLY else
        palette["muted"]
    )
    btn_bg = palette["danger_btn"] if is_destructive else palette["accent"]
    btn_fg = palette["danger_ink"] if is_destructive else palette["accent_ink"]
    return {
        "risk": risk.value,
        "badge_text": risk.value.replace("_", " ").title(),
        "border_color": border,
        "badge_fg": badge_fg,
        "confirm_bg": btn_bg,
        "confirm_fg": btn_fg,
        "is_destructive": "1" if is_destructive else "",
        "initial_focus": "cancel",
    }


def review_action_presentation(action: ActionRequest, palette: dict[str, str]) -> dict[str, Any]:
    risk = RiskLevel.DESTRUCTIVE if action.action_type == ActionType.UNINSTALL_PACKAGE else action.risk_level
    style = review_risk_style(risk, palette)
    style.update({
        "title": action.summary,
        "impact": action.impact,
        "targets": "\n".join(action.affected_targets),
        "confirm_label": _ACTION_LABELS.get(action.action_type, "Continue"),
    })
    return style


def review_workflow_presentation(review: Any, summary: str, palette: dict[str, str]) -> dict[str, Any]:
    risk = _WORKFLOW_STYLE_MAP.get(review.risk, RiskLevel.REVERSIBLE)
    style = review_risk_style(risk, palette)
    steps = []
    for step in review.steps:
        step_risk = _WORKFLOW_STYLE_MAP.get(step.risk, RiskLevel.REVERSIBLE)
        step_style = review_risk_style(step_risk, palette)
        lines = [f"{step.number}. {step.summary}"]
        if step.targets:
            lines.extend(step.targets)
        if step.error:
            lines.append(step.error)
        steps.append({
            "text": "\n".join(lines),
            "badge": step_style["badge_text"],
            "badge_fg": step_style["badge_fg"],
        })
    count = len(review.steps)
    style.update({
        "title": review.title,
        "impact": summary,
        "confirm_label": f"Run {count} {'step' if count == 1 else 'steps'}",
        "confirm_enabled": bool(review.allowed),
        "steps": steps,
    })
    return style


class TkScheduler(UIScheduler):
    """Thread-safe Tkinter callback queue drained onto the Tk main loop."""

    def __init__(self, root: Any):
        self.root = root
        self._queue: queue.SimpleQueue[tuple[Callable[..., Any], tuple[Any, ...]]] = queue.SimpleQueue()
        self._closed = False
        self._after_id: str | None = None
        self._schedule_poll()

    def _schedule_poll(self) -> None:
        if not self._closed:
            try:
                self._after_id = self.root.after(15, self._drain)
            except Exception:
                self._after_id = None

    def _drain(self) -> None:
        if self._closed:
            return
        while True:
            try:
                callback, args = self._queue.get_nowait()
            except queue.Empty:
                break
            try:
                callback(*args)
            except Exception:
                logging.debug("Tk HUD callback raised.", exc_info=True)
        self._schedule_poll()

    def call_soon(self, callback: Callable[..., Any], *args: Any) -> None:
        if not self._closed:
            self._queue.put((callback, args))

    def close(self) -> None:
        self._closed = True
        if self._after_id is not None:
            try:
                self.root.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None


class TkMavrickWindow(HUDController):
    """Cross-platform Tkinter Spotlight HUD wired into the shared HUDController."""

    def __init__(
        self,
        root: Any | None = None,
        *,
        theme: str | None = None,
        executor: ActionExecutor | None = None,
        has_activation_listener: bool = False,
    ):
        tk = _import_tk()
        self.tk = tk
        self._owns_root = root is None
        self.root = root if root is not None else tk.Tk()
        self._has_activation_listener = bool(has_activation_listener)
        self._exit_on_settle = False
        self._in_completion = False
        try:
            chosen_theme = load_theme_setting(theme)
        except ValueError:
            chosen_theme = "light"
        self.palette = theme_colors(chosen_theme)
        self.state = HUDState(theme=chosen_theme)
        self.ui_scheduler = TkScheduler(self.root)
        self.executor = executor or ActionExecutor.get_instance()
        self.send_desktop_notification = self._notify_in_background
        self.file_vars: list[Any] = []
        self._step_frames: list[Any] = []
        self._details_expanded = False
        self._visible = False
        self._review_visible = False
        self._browse_visible = False
        self._output_visible = False
        self._progress_visible = False
        self._initial_review_focus: str = ""

        self._configure_window()
        self._build_ui()
        self._bind_events()
        self.refresh_history()
        self.refresh_app_index()
        self.refresh_workflows()

    def _configure_window(self) -> None:
        self.root.title("Mavrick")
        self.root.configure(bg=self.palette["border"])
        try:
            self.root.overrideredirect(True)
        except self.tk.TclError:
            pass
        try:
            self.root.attributes("-topmost", True)
        except self.tk.TclError:
            pass
        self._position_window(expanded=False)

    def _position_window(self, *, expanded: bool) -> None:
        if self.manual_resize:
            return
        try:
            sw = self.root.winfo_screenwidth() or 1280
            sh = self.root.winfo_screenheight() or 800
        except self.tk.TclError:
            sw, sh = 1280, 800
        width = min(680, max(360, sw - 48))
        height = min(520, max(240, sh - 80)) if expanded else 190
        x = max(0, (sw - width) // 2)
        y = max(36, int(sh * 0.16))
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    def _build_ui(self) -> None:
        tk = self.tk
        c = self.palette
        self.main_box = tk.Frame(self.root, bg=c["linen"], padx=14, pady=10, highlightthickness=1,
                                 highlightbackground=c["border"])
        self.main_box.pack(fill="both", expand=True)

        # Header
        header = tk.Frame(self.main_box, bg=c["linen"])
        header.pack(fill="x", pady=(0, 8))
        tk.Label(header, text="MAVRICK", bg=c["linen"], fg=c["ink"],
                 font=("TkDefaultFont", 10, "bold")).pack(side="left")
        self.status_label = tk.Label(header, text="● Ready", bg=c["surface"], fg=c["success_ink"],
                                     padx=8, pady=2, relief="solid", bd=1, font=("TkDefaultFont", 9, "bold"))
        self.status_label.pack(side="left", padx=10)
        self.close_btn = tk.Button(header, text="✕", bg=c["linen"], fg=c["ink"], relief="flat", bd=0,
                                   command=self.hide_window)
        self.close_btn.pack(side="right")
        self.hist_btn = tk.Button(header, text="⏱ History", bg=c["linen"], fg=c["ink"], relief="flat", bd=0,
                                  command=self.toggle_history)
        self.hist_btn.pack(side="right", padx=(0, 6))

        # Entry bar
        entry_box = tk.Frame(self.main_box, bg=c["surface"], highlightthickness=1, highlightbackground=c["border"],
                             padx=8, pady=4)
        entry_box.pack(fill="x")
        tk.Label(entry_box, text="◇", bg=c["surface"], fg=c["success_ink"],
                 font=("TkDefaultFont", 12, "bold")).pack(side="left", padx=(0, 6))
        self.entry_var = tk.StringVar()
        self.entry = tk.Entry(entry_box, textvariable=self.entry_var, bg=c["surface"], fg=c["ink"],
                              insertbackground=c["ink"], relief="flat", bd=0, font=("TkDefaultFont", 13))
        self.entry.pack(side="left", fill="x", expand=True)

        self.preview_label = tk.Label(self.main_box, text="", bg=c["linen"], fg=c["success_ink"],
                                      anchor="w", font=("TkDefaultFont", 9))

        # Quick suggestion chips + Browse commands button
        self.suggestion_bar = tk.Frame(self.main_box, bg=c["linen"])
        self.suggestion_bar.pack(fill="x", pady=(8, 0))
        self.chips_box = tk.Frame(self.suggestion_bar, bg=c["linen"])
        self.chips_box.pack(side="left", fill="x", expand=True)
        for label, snippet in _COMMANDS[:3]:
            btn = tk.Button(self.chips_box, text=label, bg=c["surface"], fg=c["ink"],
                            relief="solid", bd=1, padx=8, pady=2, font=("TkDefaultFont", 9),
                            command=lambda phrase=snippet: self.apply_chip(phrase))
            btn.pack(side="left", padx=(0, 6))
        self.browse_btn = tk.Button(self.suggestion_bar, text="Browse commands", bg=c["surface"], fg=c["ink"],
                                    relief="solid", bd=1, padx=8, pady=2, font=("TkDefaultFont", 9),
                                    command=self.toggle_commands)
        self.browse_btn.pack(side="right")

        # Command browser panel
        self.browse_box = tk.Frame(self.main_box, bg=c["surface"], highlightthickness=1,
                                   highlightbackground=c["border"], padx=8, pady=8)
        self.command_search_var = tk.StringVar()
        self.command_search = tk.Entry(self.browse_box, textvariable=self.command_search_var,
                                       bg=c["inset"], fg=c["ink"], insertbackground=c["ink"], relief="solid", bd=1)
        self.command_search.pack(fill="x", pady=(0, 6))
        self.command_list = tk.Listbox(self.browse_box, bg=c["surface"], fg=c["ink"],
                                       selectbackground=c["accent"], selectforeground=c["accent_ink"],
                                       height=6, bd=0, highlightthickness=0)
        self.command_list.pack(fill="both", expand=True)
        self._filtered_phrases: list[str] = []
        self._filter_commands()

        # Review card (single action / workflow / file selection / batch undo)
        self.review_box = tk.Frame(self.main_box, bg=c["surface"], highlightthickness=2,
                                   highlightbackground=c["border"], padx=10, pady=8)
        card_top = tk.Frame(self.review_box, bg=c["surface"])
        card_top.pack(fill="x")
        self.card_title = tk.Label(card_top, text="Proposed Action", bg=c["surface"], fg=c["ink"],
                                   anchor="w", font=("TkDefaultFont", 12, "bold"))
        self.card_title.pack(side="left", fill="x", expand=True)
        self.risk_badge = tk.Label(card_top, text="Review", bg=c["inset"], fg=c["ink"],
                                   padx=8, pady=2, relief="solid", bd=1, font=("TkDefaultFont", 9, "bold"))
        self.risk_badge.pack(side="right")
        self.card_impact = tk.Label(self.review_box, text="", bg=c["surface"], fg=c["muted"],
                                    anchor="w", justify="left", wraplength=600)
        self.card_impact.pack(fill="x", pady=(4, 2))
        self.targets_scroll = tk.Frame(self.review_box, bg=c["inset"], highlightthickness=1,
                                       highlightbackground=c["border"])
        self.card_targets = tk.Text(self.targets_scroll, bg=c["inset"], fg=c["muted"], bd=0,
                                    highlightthickness=0, wrap="word", height=6, font=("TkFixedFont", 9))
        self.card_targets.configure(state="disabled")
        scrollbar_cls = getattr(tk, "Scrollbar", None)
        if scrollbar_cls is not None:
            scroll = scrollbar_cls(self.targets_scroll, orient="vertical", command=self.card_targets.yview)
            self.card_targets.configure(yscrollcommand=scroll.set)
            scroll.pack(side="right", fill="y")
        self.card_targets.pack(side="left", fill="both", expand=True)
        self.items_frame = tk.Frame(self.review_box, bg=c["surface"])
        self.items_frame.pack(fill="both", expand=True, pady=(0, 6))

        btn_row = tk.Frame(self.review_box, bg=c["surface"])
        btn_row.pack(fill="x")
        self.confirm_btn = tk.Button(btn_row, text="Continue", bg=c["accent"], fg=c["accent_ink"],
                                     relief="solid", bd=1, padx=10, pady=4, font=("TkDefaultFont", 9, "bold"),
                                     command=self.on_confirm_action)
        self.confirm_btn.pack(side="right")
        self.edit_btn = tk.Button(btn_row, text="✎ Edit", bg=c["surface"], fg=c["ink"],
                                  relief="solid", bd=1, padx=10, pady=4, command=self.on_edit_action)
        self.edit_btn.pack(side="right", padx=(0, 6))
        self.cancel_btn = tk.Button(btn_row, text="Cancel", bg=c["surface"], fg=c["ink"],
                                    relief="solid", bd=1, padx=10, pady=4, command=self.on_cancel_action)
        self.cancel_btn.pack(side="right", padx=(0, 6))

        # Progress bar
        self.progress_box = tk.Frame(self.main_box, bg=c["surface"], highlightthickness=1,
                                     highlightbackground=c["border"], padx=10, pady=8)
        self.progress_label = tk.Label(self.progress_box, text="Working…", bg=c["surface"], fg=c["ink"], anchor="w")
        self.progress_label.pack(side="left", fill="x", expand=True)
        self.stop_btn = tk.Button(self.progress_box, text="⏹ Stop", bg=c["surface"], fg=c["danger"],
                                  relief="solid", bd=1, command=self.on_stop_action)
        self.stop_btn.pack(side="right")

        # History bar
        self.history_bar = tk.Frame(self.main_box, bg=c["linen"])
        tk.Label(self.history_bar, text="PRIVACY & AUDIT LOG", bg=c["linen"], fg=c["ink"],
                 font=("TkDefaultFont", 9, "bold")).pack(side="left")

        # Output box + technical details + undo/retry bar
        self.output_scroll = tk.Frame(self.main_box, bg=c["surface"], highlightthickness=1,
                                      highlightbackground=c["border"], padx=8, pady=6)
        self.output_text = tk.Text(self.output_scroll, bg=c["surface"], fg=c["ink"], bd=0,
                                   highlightthickness=0, wrap="word", height=8, font=("TkFixedFont", 10))
        self.output_text.configure(state="disabled")
        self.output_text.pack(fill="both", expand=True)

        self.technical_toggle = tk.Button(self.main_box, text="▸ Technical details", bg=c["linen"], fg=c["muted"],
                                          relief="flat", bd=0, anchor="w", command=self._toggle_details)
        self.details_text = tk.Text(self.main_box, bg=c["inset"], fg=c["ink"], bd=1, relief="solid",
                                    height=4, wrap="word", font=("TkFixedFont", 9))
        self.details_text.configure(state="disabled")

        self.undo_box = tk.Frame(self.main_box, bg=c["linen"])
        self.undo_btn = tk.Button(self.undo_box, text="↶ Undo Last Action", bg=c["surface"], fg=c["ink"],
                                  relief="solid", bd=1, command=self.on_trigger_undo)
        self.retry_btn = tk.Button(self.undo_box, text="Review changed files", bg=c["surface"], fg=c["ink"],
                                   relief="solid", bd=1, command=self.on_review_changed)

        # Footer
        footer = tk.Frame(self.main_box, bg=c["linen"], pady=4)
        footer.pack(side="bottom", fill="x")
        shortcut = "⌘Z Undo · Esc Close" if get_platform().name == "macos" else "Ctrl+Z Undo · Esc Close"
        self.hint_label = tk.Label(footer, text=shortcut, bg=c["linen"], fg=c["muted"], font=("TkDefaultFont", 9))
        self.hint_label.pack(side="left")
        self.engine_tag = tk.Label(footer, text="agy", bg=c["surface"], fg=c["ink"],
                                   padx=8, pady=2, relief="solid", bd=1, font=("TkDefaultFont", 9, "bold"))
        self.engine_tag.pack(side="right")

    def _bind_events(self) -> None:
        self.entry.bind("<Return>", lambda _e: self.on_submit())
        self.entry.bind("<KeyRelease>", lambda _e: self.update_local_preview())
        self.entry.bind("<Up>", lambda _e: self._on_history_arrow(-1))
        self.entry.bind("<Down>", lambda _e: self._on_history_arrow(1))
        self.command_search.bind("<KeyRelease>", lambda _e: self._filter_commands())
        self.command_search.bind("<Return>", lambda _e: self._choose_selected_command())
        self.command_list.bind("<Double-Button-1>", lambda _e: self._choose_selected_command())
        self.root.bind("<Escape>", lambda _e: self._on_escape())
        self.root.bind("<Control-z>", lambda _e: self._on_undo_key())
        self.root.bind("<Control-Z>", lambda _e: self._on_undo_key())
        if get_platform().name == "macos":
            self.root.bind("<Command-z>", lambda _e: self._on_undo_key())
            self.root.bind("<Command-Z>", lambda _e: self._on_undo_key())
        self.root.bind("<Control-h>", lambda _e: self._on_history_key())
        self.root.bind("<Control-l>", lambda _e: self._on_clear_key())
        self.root.protocol("WM_DELETE_WINDOW", self.destroy)

    def _on_escape(self) -> str:
        self.handle_escape()
        return "break"

    def _on_undo_key(self) -> str:
        self.on_trigger_undo()
        return "break"

    def _on_history_key(self) -> str:
        self.toggle_history()
        return "break"

    def _on_clear_key(self) -> str:
        if not self.is_processing and not self.pending_action and not self.state.pending_workflow:
            self.history_mode = False
            self.entry_var.set("")
            self.render_welcome_state()
        return "break"

    def _on_history_arrow(self, direction: int) -> str:
        queries = self.get_history_queries()
        if not queries and direction < 0:
            return "break"
        if self.history_idx >= len(queries):
            self.history_draft = self.get_entry_text()
        new_idx = max(0, min(len(queries), self.history_idx + direction))
        self.history_idx = new_idx
        self.set_entry_text(queries[new_idx] if new_idx < len(queries) else self.history_draft)
        self.entry.icursor("end")
        return "break"

    def _hide_panels(self) -> None:
        self._review_visible = False
        self._browse_visible = False
        self._output_visible = False
        self._progress_visible = False
        for widget in (
            self.suggestion_bar, self.browse_box, self.review_box, self.progress_box,
            self.history_bar, self.output_scroll, self.technical_toggle, self.details_text,
            self.undo_box, self.preview_label,
        ):
            widget.pack_forget()

    def _toggle_details(self) -> None:
        self._details_expanded = not self._details_expanded
        if self._details_expanded:
            self.technical_toggle.configure(text="▾ Technical details")
            self.details_text.pack(fill="x", pady=(0, 6))
        else:
            self.technical_toggle.configure(text="▸ Technical details")
            self.details_text.pack_forget()
        self.adjust_window_size()

    def _filter_commands(self) -> None:
        words = self.command_search_var.get().casefold().split()
        self.command_list.delete(0, "end")
        self._filtered_phrases = []
        for label, phrase in _COMMANDS:
            if all(word in f"{label} {phrase}".casefold() for word in words):
                self.command_list.insert("end", label)
                self._filtered_phrases.append(phrase)
        if self._filtered_phrases:
            self.command_list.selection_set(0)

    def _choose_selected_command(self) -> None:
        sel = self.command_list.curselection()
        if sel and sel[0] < len(self._filtered_phrases):
            phrase = self._filtered_phrases[sel[0]]
            self.render_welcome_state()
            self.apply_chip(phrase)

    def toggle_commands(self) -> None:
        if self._browse_visible:
            self.render_welcome_state()
            return
        self._hide_panels()
        self._browse_visible = True
        self.browse_box.pack(fill="both", expand=True, pady=(8, 0))
        self.command_search.focus_set()
        self.adjust_window_size()

    def handle_escape(self) -> bool:
        if self.is_processing:
            if self.cancel_token is not None and not self.cancel_token.is_set():
                self.on_stop_action()
            else:
                self.hide_window()
        elif self._review_visible:
            self.on_cancel_action()
        elif self.history_mode:
            self.toggle_history()
        elif self._browse_visible or self._output_visible:
            self.render_welcome_state()
        elif self.get_entry_text():
            self.set_entry_text("")
        else:
            self.hide_window()
        return True

    # HUDView contract
    def get_entry_text(self) -> str:
        return self.entry_var.get()

    def set_entry_text(self, text: str) -> None:
        self.entry_var.set(text)

    def set_entry_enabled(self, enabled: bool) -> None:
        self.entry.configure(state="normal" if enabled else "disabled")

    def focus_entry(self) -> None:
        self.entry.focus_set()

    def is_review_visible(self) -> bool:
        return self._review_visible

    def is_browse_visible(self) -> bool:
        return self._browse_visible

    def show_instant_preview(self, text: str) -> None:
        self.preview_label.configure(text=text)
        self.preview_label.pack(fill="x", pady=(4, 0), before=self.suggestion_bar)

    def hide_instant_preview(self) -> None:
        self.preview_label.pack_forget()

    def set_status(self, text: str, state: str = "ready") -> None:
        fg = {
            "ready": self.palette["success_ink"],
            "done": self.palette["success_ink"],
            "review": self.palette["warning_ink"],
            "cancelled": self.palette["muted"],
            "error": self.palette["danger"],
            "executing": self.palette["ink"],
        }.get(state, self.palette["ink"])
        self.status_label.configure(text=text, fg=fg)

    def _show_health_badge(self, health: dict[str, Any]) -> None:
        if self.state.closed:
            return
        self.health_running = False
        self.state.provider_label = PROVIDER_LABELS.get(health.get("provider"), "AI")
        self.set_result_source(self.state.result_source)

    def set_result_source(self, source: str) -> None:
        self.state.result_source = source
        label = {"instant": "Instant", "instant_workflow": "Instant · Workflow"}.get(
            source, PROVIDER_LABELS.get(source, self.state.provider_label))
        fg = self.palette["success_ink"] if source in ("instant", "instant_workflow") else self.palette["ink"]
        self.engine_tag.configure(text=label, fg=fg)

    def set_history_button_label(self, text: str) -> None:
        self.hist_btn.configure(text=text)

    def set_history_enabled(self, enabled: bool) -> None:
        self.hist_btn.configure(state="normal" if enabled else "disabled")

    def set_stop_enabled(self, enabled: bool) -> None:
        self.stop_btn.configure(state="normal" if enabled else "disabled")

    def clear_output_text(self) -> None:
        self.output_text.configure(state="normal")
        self.output_text.delete("1.0", "end")
        self.output_text.configure(state="disabled")

    def show_output_view(self) -> None:
        self._output_visible = True
        self.output_scroll.pack(fill="both", expand=True, pady=(8, 4))

    def set_progress_text(self, text: str) -> None:
        self.progress_label.configure(text=text)

    def _show_progress(self, label: str) -> None:
        self.is_processing = True
        self.set_entry_enabled(False)
        self.set_history_enabled(False)
        self.set_stop_enabled(True)
        self._hide_panels()
        self._progress_visible = True
        self.set_status("● Working", "executing")
        self.progress_label.configure(text=label)
        self.progress_box.pack(fill="x", pady=(8, 0))
        self.adjust_window_size()

    def _settle_progress(self) -> None:
        self.is_processing = False
        self._progress_visible = False
        self.progress_box.pack_forget()
        self.set_entry_enabled(True)
        self.set_history_enabled(True)
        if not self._in_completion:
            self._maybe_exit_standalone()

    def on_execution_complete(self, action: ActionRequest | Any, result: Any) -> None:
        self._in_completion = True
        try:
            super().on_execution_complete(action, result)
        finally:
            self._in_completion = False
        self._maybe_exit_standalone()

    def render_welcome_state(self) -> None:
        self.clear_output_text()
        self.set_result_source("")
        self._hide_panels()
        self.suggestion_bar.pack(fill="x", pady=(8, 0))
        self.set_status("● Ready", "ready")
        self.adjust_window_size()
        self.focus_entry()

    def render_output_card(self, title: str, body: str = "", duration_ms: float = 0.0,
                           verification_status: str = "verified", is_error: bool = False) -> None:
        lines = []
        prefix = "✗ " if is_error else "✓ " if verification_status == "verified" else ""
        lines.append(f"{prefix}{title}")
        if body and body.strip():
            lines.append("─" * 54)
            lines.append(body.rstrip())
        self.output_text.configure(state="normal")
        self.output_text.delete("1.0", "end")
        self.output_text.insert("1.0", "\n".join(lines))
        self.output_text.configure(state="disabled")

    def show_processes(self, processes: list[dict[str, Any]]) -> None:
        rows = ["Process                        PID     RAM MB   CPU %"]
        for row in processes:
            rows.append(f"{row['command'][:28]:<28}  {row['pid']:<6}  {row['ram_mb']:>6.1f}   {row['cpu_pct']:>5.1f}")
        self.output_text.configure(state="normal")
        self.output_text.delete("1.0", "end")
        self.output_text.insert("1.0", "\n".join(rows))
        self.output_text.configure(state="disabled")

    def set_technical_details(self, text: str) -> None:
        self.details_text.configure(state="normal")
        self.details_text.delete("1.0", "end")
        self.details_text.insert("1.0", text)
        self.details_text.configure(state="disabled")
        self._details_expanded = False
        self.technical_toggle.configure(text="▸ Technical details")
        self.technical_toggle.pack(fill="x", pady=(2, 2))

    def show_result_actions(self, *, show_undo: bool, show_retry: bool) -> None:
        self.undo_btn.pack_forget()
        self.retry_btn.pack_forget()
        if show_undo or show_retry:
            if show_undo:
                self.undo_btn.pack(side="left", padx=(0, 6))
            if show_retry:
                self.retry_btn.pack(side="left")
            self.undo_box.pack(fill="x", pady=(2, 4))

    def set_clipboard_text(self, text: str) -> None:
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.root.update_idletasks()
        except Exception:
            get_platform().copy_to_clipboard(text)

    def _clear_review_items(self) -> None:
        self.file_vars = []
        self._step_frames = []
        for child in self.items_frame.winfo_children():
            child.destroy()

    def _present_review(self, style: dict[str, str], *, enabled: bool = True) -> None:
        self._hide_panels()
        self._review_visible = True
        self.set_entry_enabled(False)
        self.set_history_enabled(False)
        self.review_box.configure(highlightbackground=style["border_color"])
        self.risk_badge.configure(text=style["badge_text"], fg=style["badge_fg"],
                                  highlightbackground=style["border_color"])
        self.confirm_btn.configure(
            text=style["confirm_label"],
            bg=style["confirm_bg"],
            fg=style["confirm_fg"],
            state="normal" if enabled else "disabled",
        )
        self.review_box.pack(fill="both", expand=True, pady=(8, 4))
        self.adjust_window_size()
        self.cancel_btn.focus_set()
        self._initial_review_focus = "cancel"

    def _set_card_targets(self, text: str) -> None:
        self.card_targets.configure(state="normal")
        self.card_targets.delete("1.0", "end")
        if text:
            self.card_targets.insert("1.0", text)
        self.card_targets.configure(state="disabled")
        if text:
            self.targets_scroll.pack(fill="x", pady=(0, 6), before=self.items_frame)
        else:
            self.targets_scroll.pack_forget()

    def show_review_card(self, action: ActionRequest) -> None:
        self.set_status("● Review", "review")
        self._clear_review_items()
        view = review_action_presentation(action, self.palette)
        self.card_title.configure(text=view["title"])
        self.card_impact.configure(text=view["impact"])
        self._set_card_targets(view["targets"])
        self._present_review(view)

    def show_workflow_review(self, review: Any, summary: str) -> None:
        self.set_status("● Review workflow", "review")
        self._clear_review_items()
        view = review_workflow_presentation(review, summary, self.palette)
        self.card_title.configure(text=view["title"])
        self.card_impact.configure(text=view["impact"])
        self._set_card_targets("")
        for step in view["steps"]:
            row = self.tk.Frame(self.items_frame, bg=self.palette["inset"], padx=6, pady=4)
            row.pack(fill="x", pady=2)
            self.tk.Label(row, text=step["text"], bg=self.palette["inset"], fg=self.palette["ink"],
                          anchor="w", justify="left", wraplength=500).pack(side="left", fill="x", expand=True)
            self.tk.Label(row, text=step["badge"], bg=self.palette["surface"], fg=step["badge_fg"],
                          padx=6, pady=1, relief="solid", bd=1, font=("TkDefaultFont", 8, "bold")).pack(side="right")
            self._step_frames.append(row)
        self._present_review(view, enabled=view["confirm_enabled"])

    def show_file_review(self, review: FileReview, summary: str) -> None:
        self.file_review = review
        self.file_rows = list(review.rows)
        self._clear_review_items()
        undo_mode = review.action == "undo"
        self.set_status("● Review Undo" if undo_mode else "● Review files", "review")
        self.card_title.configure(text="Review batch Undo" if review.undo_id else "Review Undo" if undo_mode else "Review file moves")
        self.card_impact.configure(text=summary)
        self._set_card_targets("")
        for row in self.file_rows:
            var = self.tk.BooleanVar(value=bool(row.included and not row.problem))
            step = f"Step {row.workflow_step} · " if row.workflow_step is not None else ""
            text = f"{step}{display_path(row.source)} → {display_path(row.destination)}"
            if row.problem:
                text += f" [{display_path(row.problem)}]"
            chk = self.tk.Checkbutton(
                self.items_frame, text=text, variable=var,
                state="disabled" if row.problem else "normal",
                bg=self.palette["surface"], fg=self.palette["danger"] if row.problem else self.palette["ink"],
                anchor="w", command=self.update_file_count,
            )
            chk.pack(fill="x")
            self.file_vars.append(var)
        style = review_risk_style(RiskLevel.REVERSIBLE, self.palette)
        style["confirm_label"] = self._file_confirm_label()
        self._present_review(style, enabled=bool(self.get_selected_file_indices()))

    def _file_confirm_label(self) -> str:
        count = len(self.get_selected_file_indices())
        verb = "Restore" if self.file_review and self.file_review.action == "undo" else "Move"
        return f"{verb} {count} {'file' if count == 1 else 'files'}"

    def update_file_count(self) -> None:
        if not self.file_review:
            return
        self.confirm_btn.configure(
            text=self._file_confirm_label(),
            state="normal" if (self.get_selected_file_indices() and not self.state.renaming) else "disabled",
        )

    def get_selected_file_indices(self) -> list[int]:
        return [i for i, var in enumerate(self.file_vars) if var.get()]

    def set_review_error(self, message: str) -> None:
        self.card_impact.configure(text=message)

    def update_renamed_file_row(self, index: int, label: Any, updated: FileRow) -> None:
        if index < len(self.file_vars):
            self.file_vars[index].set(not updated.problem)

    def render_history_view(self) -> None:
        records = self.state.activity
        if not records:
            text = "Loading activity…" if self.state.activity_loading else "No activity or audit history found."
        else:
            blocks = [f"── ACTIVITY & AUDIT HISTORY ({len(records)} entries) ──"]
            for rec in reversed(records[-30:]):
                risk = str(rec.get("risk_level", "read_only")).upper()
                status = str(rec.get("status", "completed"))
                query = rec.get("query", "").strip() or str(rec.get("action") or "Action")
                blocks.append(f"[{rec.get('date_str', 'recent')}] [{risk}] {status}\n  Query: {query}")
            text = "\n\n".join(blocks)
        self.output_text.configure(state="normal")
        self.output_text.delete("1.0", "end")
        self.output_text.insert("1.0", text)
        self.output_text.configure(state="disabled")
        self.adjust_window_size()

    def toggle_history(self) -> None:
        if self.is_processing or self.pending_action or self.state.pending_workflow:
            return
        self.history_mode = not self.history_mode
        if self.history_mode:
            self._hide_panels()
            self.history_bar.pack(fill="x", pady=(8, 2))
            self.refresh_history(include_audit=True)
            self.render_history_view()
            self.show_output_view()
            self.set_history_button_label("✕ Close History")
            self.adjust_window_size()
        else:
            self.render_welcome_state()
            self.set_history_button_label("⏱ History")

    def apply_chip(self, snippet: str) -> None:
        self.set_entry_text(snippet)
        self.entry.icursor("end")
        self.history_draft = snippet
        self.history_idx = len(self.get_history_queries())
        self.focus_entry()

    def adjust_window_size(self, content_lines: int | None = None) -> None:
        self._position_window(expanded=(self._review_visible or self._output_visible or self._browse_visible))

    def is_visible(self) -> bool:
        return self._visible and not self.state.closed

    def _maybe_exit_standalone(self) -> None:
        if self._exit_on_settle and not self._has_activation_listener and not self.is_processing and not self.state.closed:
            self._exit_standalone()

    def _exit_standalone(self) -> None:
        quit_fn = getattr(self.root, "quit", None)
        if callable(quit_fn):
            try:
                quit_fn()
            except Exception:
                pass
        self.destroy()

    def hide_window(self) -> bool:
        self._visible = False
        self.root.withdraw()
        if not self._has_activation_listener:
            self._exit_on_settle = True
            if not self.is_processing:
                self._exit_standalone()
        return False

    def show_window(self) -> bool:
        if self.state.closed:
            return False
        self._visible = True
        self.root.deiconify()
        self.adjust_window_size()
        try:
            self.root.lift()
        except self.tk.TclError:
            pass
        if self._review_visible:
            self.cancel_btn.focus_set()
        elif not self.is_processing:
            self.focus_entry()
        self.refresh_health_badge(probe=False)
        self.refresh_history()
        self.history_draft = ""
        self.history_idx = len(self.get_history_queries())
        return False

    def destroy(self) -> None:
        if not self.state.closed:
            self.state.closed = True
            if self.cancel_token is not None:
                self.cancel_token.set()
            self.ui_scheduler.close()
            if self._owns_root:
                self.root.destroy()


def main() -> None:
    if not tk_available():
        raise RuntimeError(
            "Tkinter HUD backend is unavailable (install python3-tk or run CLI commands like 'mavrick --doctor')."
        )
    tk = _import_tk()
    server = None
    if get_platform().name != "windows":
        try:
            server = submit(prepare_hud_socket).result(timeout=2.0)
        except Exception:
            server = None
        else:
            if server is None:
                return
    root = tk.Tk()
    window = TkMavrickWindow(root)
    if server is not None:
        listen_for_hud_requests(window, server)
    try:
        window.show_window()
        root.mainloop()
    finally:
        window.destroy()
        if server is not None:
            close_hud_socket(server)
