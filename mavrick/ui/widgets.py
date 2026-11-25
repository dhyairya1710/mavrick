"""Compose the palette and render command, review, and result widgets."""

from os.path import basename
from typing import Any

from gi.repository import Gdk, Gtk, Pango

from ..config import PROVIDER_LABELS
from ..schema import ActionRequest, ActionType, RiskLevel
from ..policy import WorkflowRisk
from ..workflows import WorkflowReview
from ..tools.files import FileReview, FileRow, display_path
from .styles import FONT_MONO, FONT_SANS, theme_colors


class HUDWidgets:
    def render_welcome_state(self):
        self.buffer.set_text("")
        self.set_result_source("")
        for view in (self.output_scroll, self.browse_box, self.undo_box,
                     self.review_box, self.technical_details, self.process_scroll, self.preview_label):
            view.hide()
        self.chips_box.show()
        self.set_status("● Ready", "ready")
        self.adjust_window_size()
        self.entry.grab_focus()

    def setup_buffer_tags(self, theme: str = "light"):
        """Configures typography and color tags for structured buffer rendering."""
        colors = theme_colors(theme)
        tag_table = self.buffer.get_tag_table()
        def add_tag(name, **kwargs):
            if not tag_table.lookup(name):
                self.buffer.create_tag(name, **kwargs)

        bold = Pango.Weight.BOLD
        add_tag("header", font=f"{FONT_SANS} 11", weight=bold, foreground=colors["muted"])
        add_tag("timestamp", font=f"{FONT_SANS} 10", foreground=colors["muted"])
        add_tag("query", font=f"{FONT_SANS} 12", weight=bold, foreground=colors["ink"])
        add_tag("summary", font=f"{FONT_SANS} 11", foreground=colors["muted"])
        add_tag("params", font=f"{FONT_MONO} 10", foreground=colors["muted"])
        add_tag("sep", foreground=colors["border"])
        add_tag("code_block", font=f"{FONT_MONO} 11", foreground=colors["ink"])
        add_tag("card_title", font=f"{FONT_SANS} 11", weight=bold, foreground=colors["ink"])
        for name, color in {
            "badge_readonly": "success_ink", "badge_reversible": "muted",
            "badge_privileged": "warning_ink", "badge_destructive": "danger",
            "status_completed": "success_ink", "status_failed": "danger",
            "status_cancelled": "muted", "status_launch": "ink",
        }.items():
            add_tag(name, font=f"{FONT_SANS} 10", weight=bold, foreground=colors[color])

    def build_ui(self) -> None:
        self.main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.main_box.set_name("main-container")
        self.add(self.main_box)

        self._build_header()
        self._build_input()
        self._build_command_browser()
        self._build_review()
        self._build_progress()
        self._build_history()
        self._build_results()
        self._build_footer()

        self.main_box.show_all()
        for view in (self.output_scroll, self.review_box, self.progress_box, self.history_bar,
                     self.browse_box, self.undo_box, self.technical_details, self.process_scroll, self.preview_label):
            view.hide()
        self.set_status("● Ready", "ready")

    def _build_header(self) -> None:
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        header.set_name("header-bar")

        title = Gtk.Label(label="MAVRICK")
        title.set_name("brand-title")
        header.pack_start(title, False, False, 0)

        # Status Capsule
        capsule = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        capsule.set_name("status-capsule")
        self.status_label = Gtk.Label(label="● Ready")
        self.status_label.set_name("status-label")
        self.status_label.set_ellipsize(Pango.EllipsizeMode.END)
        capsule.pack_start(self.status_label, False, False, 0)
        header.pack_start(capsule, False, False, 0)

        # Close Button
        close_btn = Gtk.Button(label="✕")
        close_btn.set_tooltip_text("Hide Mavrick")
        close_btn.get_style_context().add_class("icon-btn")
        close_btn.connect("clicked", lambda w: self.hide_window())
        header.pack_end(close_btn, False, False, 0)

        # History Button
        self.hist_btn = Gtk.Button(label="⏱ History")
        self.hist_btn.get_style_context().add_class("icon-btn")
        self.hist_btn.connect("clicked", lambda w: self.toggle_history())
        header.pack_end(self.hist_btn, False, False, 0)

        self.main_box.pack_start(header, False, False, 0)

    def _build_input(self) -> None:
        entry_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        entry_box.set_name("entry-box")

        glyph = Gtk.Label(label="◇")
        glyph.set_name("prompt-glyph")
        entry_box.pack_start(glyph, False, False, 0)

        self.entry = Gtk.Entry()
        self.entry.set_name("prompt-entry")
        self.entry.set_placeholder_text("Type a command or ask Mavrick")
        self.entry.connect("activate", self.on_submit)
        self.entry.connect("changed", self.update_local_preview)
        entry_box.pack_start(self.entry, True, True, 0)

        self.main_box.pack_start(entry_box, False, False, 0)
        self.preview_label = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, margin_start=16, margin_end=16)
        self.preview_label.set_name("instant-preview")
        self.preview_label.set_no_show_all(True)
        self.main_box.pack_start(self.preview_label, False, False, 0)

    def _build_command_browser(self) -> None:
        self.chips_scroll = Gtk.ScrolledWindow()
        self.chips_scroll.set_name("chips-scroll")
        self.chips_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        self.chips_scroll.set_propagate_natural_height(True)
        self.chips_scroll.set_kinetic_scrolling(True)
        self.chips_scroll.set_overlay_scrolling(True)
        self.chips_scroll.set_shadow_type(Gtk.ShadowType.NONE)

        self.chips_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.chips_box.set_name("chips-box")
        self.suggestion_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.chips_box.connect("show", lambda _widget: self.suggestion_bar.show())
        self.chips_box.connect("hide", lambda _widget: self.suggestion_bar.hide())

        self.commands = [
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
            ("Install VLC from Flathub", "install VLC from Flathub"),
        ]
        for label, snippet in self.commands[:3]:
            btn = Gtk.Button(label=label)
            btn.get_style_context().add_class("chip-btn")
            btn.connect("clicked", lambda _button, phrase=snippet: self.apply_chip(phrase))
            self.chips_box.pack_start(btn, False, False, 0)
        self.browse_btn = Gtk.Button(label="Browse commands", margin_end=12)
        self.browse_btn.connect("clicked", self.toggle_commands)
        self.chips_scroll.add(self.chips_box)
        self.suggestion_bar.pack_start(self.chips_scroll, True, True, 0)
        self.suggestion_bar.pack_end(self.browse_btn, False, False, 0)
        self.main_box.pack_start(self.suggestion_bar, False, False, 0)

        self.browse_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=12)
        self.command_search = Gtk.SearchEntry(placeholder_text="Search commands")
        self.command_search.connect("search-changed", self.filter_commands)
        self.command_search.connect("activate", self.choose_first_command)
        self.command_search.connect("key-press-event", self.on_command_search_key)
        self.browse_box.pack_start(self.command_search, False, False, 0)
        self.command_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.command_list.set_name("command-list")
        self.command_list.connect("row-activated", self.choose_command)
        browser_scroll = Gtk.ScrolledWindow()
        browser_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        browser_scroll.add(self.command_list)
        self.command_list.set_adjustment(browser_scroll.get_vadjustment())
        self.browse_box.pack_start(browser_scroll, True, True, 0)
        self.main_box.pack_start(self.browse_box, True, True, 0)
        self.filter_commands(self.command_search)

    def _build_review(self) -> None:
        self.review_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.review_box.set_name("action-card")

        card_top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.card_title = Gtk.Label(label="Proposed Action")
        self.card_title.set_name("card-title")
        self.card_title.set_xalign(0.0)
        self.card_title.set_line_wrap(True)
        self.card_title.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        card_top.pack_start(self.card_title, True, True, 0)

        self.risk_badge = Gtk.Label()
        self.risk_badge.get_style_context().add_class("risk-badge")
        card_top.pack_end(self.risk_badge, False, False, 0)
        self.review_box.pack_start(card_top, False, False, 0)

        self.card_impact = Gtk.Label(label="Impact explanation")
        self.card_impact.set_name("card-impact")
        self.card_impact.set_line_wrap(True)
        self.card_impact.set_xalign(0.0)
        self.review_box.pack_start(self.card_impact, False, False, 0)

        self.card_targets = Gtk.Label(label="")
        self.card_targets.set_name("card-targets")
        self.card_targets.set_line_wrap(True)
        self.card_targets.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.card_targets.set_xalign(0.0)
        self.card_targets.set_selectable(True)
        self.targets_scroll = Gtk.ScrolledWindow()
        self.targets_scroll.set_name("card-targets-scroll")
        self.targets_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.targets_scroll.set_shadow_type(Gtk.ShadowType.NONE)
        self.targets_scroll.set_propagate_natural_height(True)
        self.targets_scroll.set_max_content_height(140)
        self.targets_scroll.add(self.card_targets)
        self.review_box.pack_start(self.targets_scroll, False, False, 0)

        self.file_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.file_list.set_name("file-list")
        self.file_scroll = Gtk.ScrolledWindow()
        self.file_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.file_scroll.set_min_content_height(68)
        self.file_scroll.add(self.file_list)
        self.review_box.pack_start(self.file_scroll, True, True, 0)

        # Review Actions
        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.confirm_btn = Gtk.Button(label="Continue")
        self.confirm_btn.get_style_context().add_class("btn-primary")
        self.confirm_btn.connect("clicked", lambda w: self.on_confirm_action())
        btn_row.pack_end(self.confirm_btn, False, False, 0)

        # [✎ Edit] Button
        self.edit_btn = Gtk.Button(label="✎ Edit")
        self.edit_btn.get_style_context().add_class("btn-secondary")
        self.edit_btn.connect("clicked", lambda w: self.on_edit_action())
        btn_row.pack_end(self.edit_btn, False, False, 0)

        self.cancel_btn = Gtk.Button(label="Cancel")
        self.cancel_btn.get_style_context().add_class("btn-secondary")
        self.cancel_btn.connect("clicked", lambda w: self.on_cancel_action())
        btn_row.pack_end(self.cancel_btn, False, False, 0)

        self.review_box.pack_start(btn_row, False, False, 0)
        self.main_box.pack_start(self.review_box, True, True, 0)
        self.review_box.hide()

    def _build_progress(self) -> None:
        self.progress_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.progress_box.set_name("progress-box")

        self.spinner = Gtk.Spinner()
        self.progress_box.pack_start(self.spinner, False, False, 0)

        self.progress_label = Gtk.Label(label="Executing action...")
        self.progress_label.set_name("progress-label")
        self.progress_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.progress_box.pack_start(self.progress_label, True, True, 0)

        self.stop_btn = Gtk.Button(label="⏹ Stop Action")
        self.stop_btn.get_style_context().add_class("btn-danger")
        self.stop_btn.connect("clicked", lambda w: self.on_stop_action())
        self.progress_box.pack_end(self.stop_btn, False, False, 0)

        self.main_box.pack_start(self.progress_box, False, False, 0)
        self.progress_box.hide()

    def _build_history(self) -> None:
        self.history_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.history_bar.set_name("history-bar")

        hist_title = Gtk.Label(label="PRIVACY & AUDIT LOG")
        hist_title.get_style_context().add_class("history-title")
        self.history_bar.pack_start(hist_title, False, False, 0)

        self.clear_hist_btn = Gtk.Button(label="🗑 Clear History")
        self.clear_hist_btn.get_style_context().add_class("btn-danger")
        self.clear_hist_btn.connect("clicked", lambda w: self.on_clear_history())
        self.history_bar.pack_end(self.clear_hist_btn, False, False, 0)

        self.main_box.pack_start(self.history_bar, False, False, 0)
        self.history_bar.hide()

    def _build_results(self) -> None:
        self.output_scroll = Gtk.ScrolledWindow()
        self.output_scroll.set_name("output-scroll")
        self.output_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.output_scroll.set_propagate_natural_height(True)
        self.output_scroll.set_kinetic_scrolling(True)
        self.output_scroll.set_overlay_scrolling(True)
        self.output_scroll.set_shadow_type(Gtk.ShadowType.NONE)
        self.output_scroll.set_min_content_height(50)
        self.output_scroll.set_max_content_height(self.get_max_content_height())

        self.text_view = Gtk.TextView()
        self.text_view.set_name("output-view")
        self.text_view.set_editable(False)
        self.text_view.set_cursor_visible(False)
        self.text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.text_view.set_left_margin(4)
        self.text_view.set_right_margin(4)
        self.buffer = self.text_view.get_buffer()

        self.output_scroll.add(self.text_view)
        self.main_box.pack_start(self.output_scroll, True, True, 0)
        self.output_scroll.hide()

        self.process_scroll = Gtk.ScrolledWindow()
        self.process_scroll.set_name("process-scroll")
        self.process_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.main_box.pack_start(self.process_scroll, True, True, 0)
        self.technical_details = Gtk.Expander(label="Technical details", margin_start=20, margin_end=20)
        details_scroll = Gtk.ScrolledWindow()
        details_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        details_scroll.set_min_content_height(62)
        self.details_view = Gtk.TextView(editable=False, cursor_visible=False, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.details_view.set_name("details-view")
        details_scroll.add(self.details_view)
        self.technical_details.add(details_scroll)
        self.main_box.pack_start(self.technical_details, False, False, 0)

        # Undo Button Box (Optional strip under output)
        self.undo_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.undo_box.set_margin_start(20)
        self.undo_box.set_margin_end(20)
        self.undo_box.set_margin_bottom(8)
        self.undo_btn = Gtk.Button(label="↶ Undo Last Action [Ctrl+Z]")
        self.undo_btn.get_style_context().add_class("btn-secondary")
        self.undo_btn.connect("clicked", lambda w: self.on_trigger_undo())
        self.undo_box.pack_start(self.undo_btn, False, False, 0)
        self.retry_btn = Gtk.Button(label="Review changed files")
        self.retry_btn.connect("clicked", self.on_review_changed)
        self.undo_box.pack_start(self.retry_btn, False, False, 0)
        self.main_box.pack_start(self.undo_box, False, False, 0)
        self.undo_box.hide()

    def _build_footer(self) -> None:
        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        footer.set_name("footer-bar")

        self.hint_label = Gtk.Label(label="Super+M  Toggle")
        self.hint_label.set_name("hint-label")
        footer.pack_start(self.hint_label, False, False, 0)

        self.engine_tag = Gtk.Label(label="agy")
        self.engine_tag.set_name("engine-tag")
        grip = Gtk.EventBox()
        grip.add(Gtk.Label(label="◢"))
        grip.set_tooltip_text("Drag to resize")
        grip.connect("button-press-event", self._resize_from_grip)
        footer.pack_end(grip, False, False, 0)
        footer.pack_end(self.engine_tag, False, False, 0)

        self.main_box.pack_start(footer, False, False, 0)

    def get_entry_text(self) -> str:
        return self.entry.get_text()

    def set_entry_text(self, text: str) -> None:
        self.entry.set_text(text)

    def set_entry_enabled(self, enabled: bool) -> None:
        self.entry.set_sensitive(enabled)

    def focus_entry(self) -> None:
        self.entry.grab_focus()

    def is_review_visible(self) -> bool:
        return self.review_box.is_visible()

    def is_browse_visible(self) -> bool:
        return self.browse_box.is_visible()

    def show_instant_preview(self, text: str) -> None:
        self.preview_label.set_text(text)
        self.preview_label.show()

    def hide_instant_preview(self) -> None:
        self.preview_label.hide()

    def set_history_button_label(self, text: str) -> None:
        self.hist_btn.set_label(text)

    def set_history_enabled(self, enabled: bool) -> None:
        self.hist_btn.set_sensitive(enabled)

    def set_stop_enabled(self, enabled: bool) -> None:
        self.stop_btn.set_sensitive(enabled)

    def clear_output_text(self) -> None:
        self.buffer.set_text("")

    def show_output_view(self) -> None:
        self.output_scroll.show_all()

    def set_progress_text(self, text: str) -> None:
        self.progress_label.set_text(text)

    def set_clipboard_text(self, text: str) -> None:
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(text, -1)

    def set_technical_details(self, text: str) -> None:
        self.details_view.get_buffer().set_text(text)
        self.technical_details.set_expanded(False)
        self.technical_details.show_all()

    def show_result_actions(self, *, show_undo: bool, show_retry: bool) -> None:
        self.undo_box.show_all()
        self.undo_btn.set_visible(show_undo)
        self.retry_btn.set_visible(show_retry)
        self.undo_box.set_visible(show_undo or show_retry)

    def get_selected_file_indices(self) -> list[int]:
        return [i for i, check in enumerate(self.file_checks) if check.get_active()]

    def set_review_error(self, message: str) -> None:
        self.card_impact.set_text(message)

    def update_renamed_file_row(self, index: int, label: Any, updated: Any) -> None:
        self._set_file_label(label, updated)
        self.file_checks[index].set_sensitive(not updated.problem)
        self.file_checks[index].set_active(not updated.problem)

    def set_status(self, text: str, state: str = "ready") -> None:
        self.status_label.set_text(text)
        ctx = self.status_label.get_style_context()
        for s in ["status-ready", "status-thinking", "status-review", "status-executing", "status-done", "status-cancelled", "status-error"]:
            ctx.remove_class(s)
        ctx.add_class(f"status-{state}")

    def _show_health_badge(self, health: dict[str, Any]) -> None:
        if self.state.closed:
            return
        self.health_running = False
        self.state.provider_label = PROVIDER_LABELS.get(health.get("provider"), "AI")
        style = {
            "connected_daemon": "engine-connected", "connected_provider": "engine-connected",
            "fallback_cli": "engine-fallback", "offline": "engine-offline",
        }.get(health.get("status"), "")
        ctx = self.engine_tag.get_style_context()
        for klass in ("engine-connected", "engine-fallback", "engine-offline"):
            ctx.remove_class(klass)
        if style:
            ctx.add_class(style)
        self.engine_tag.set_tooltip_text(health.get("display") or "Configured AI provider")
        self.set_result_source(self.state.result_source)

    def set_result_source(self, source: str) -> None:
        self.state.result_source = source
        label = {"instant": "Instant", "instant_workflow": "Instant · Workflow"}.get(
            source, PROVIDER_LABELS.get(source, self.state.provider_label))
        self.engine_tag.set_text(label)
        context = self.engine_tag.get_style_context()
        if source in ("instant", "instant_workflow"):
            context.add_class("engine-instant")
        else:
            context.remove_class("engine-instant")

    def filter_commands(self, entry: Gtk.SearchEntry) -> None:
        words = entry.get_text().casefold().split()
        if words == self.state.command_filter:
            return
        self.state.command_filter = words
        for row in self.command_list.get_children():
            self.command_list.remove(row)
        for label, phrase in self.commands:
            if all(word in f"{label} {phrase}".casefold() for word in words):
                row = Gtk.ListBoxRow()
                row.phrase = phrase
                row.add(Gtk.Label(label=label, xalign=0, margin=8))
                self.command_list.add(row)
        self.command_list.show_all()
        self.command_list.select_row(self.command_list.get_row_at_index(0))

    def toggle_commands(self, _button=None):
        if self.browse_box.get_visible():
            self.render_welcome_state()
            return
        self.browse_box.show_all()
        self.preview_label.hide()
        self.command_search.grab_focus()
        self.adjust_window_size()

    def choose_first_command(self, entry: Gtk.SearchEntry) -> None:
        self.filter_commands(entry)
        selected = self.command_list.get_selected_row()
        if selected:
            self.choose_command(self.command_list, selected)

    def navigate_commands(self, direction: int) -> None:
        self.filter_commands(self.command_search)
        selected = self.command_list.get_selected_row()
        if selected is None:
            return
        row = self.command_list.get_row_at_index(max(0, selected.get_index() + direction))
        if row is None:
            return
        self.command_list.select_row(row)
        area = row.get_allocation()
        adjustment = self.command_list.get_adjustment()
        adjustment.clamp_page(area.y, area.y + area.height)

    def choose_command(self, _list, row):
        self.browse_box.hide()
        self.apply_chip(row.phrase)
        self.adjust_window_size()

    def apply_chip(self, snippet):
        self.entry.set_text(snippet)
        self.entry.set_position(-1)
        self.history_draft = snippet
        self.history_idx = len(self.get_history_queries())
        self.entry.grab_focus()

    def _show_progress(self, label):
        self.is_processing = True
        self.entry.set_sensitive(False)
        self.hist_btn.set_sensitive(False)
        self.stop_btn.set_sensitive(True)
        for view in (self.chips_box, self.review_box, self.undo_box, self.output_scroll,
                     self.technical_details, self.process_scroll, self.browse_box, self.history_bar, self.preview_label):
            view.hide()
        self.set_status("● Working", "executing")
        self.progress_label.set_text(label)
        self.spinner.start()
        self.progress_box.show_all()
        self.adjust_window_size()

    def _settle_progress(self):
        self.is_processing = False
        self.progress_box.hide()
        self.spinner.stop()
        self.entry.set_sensitive(True)
        self.hist_btn.set_sensitive(True)

    def show_review_card(self, action: ActionRequest) -> None:
        self.set_status("● Review", "review")
        self.card_title.set_text(action.summary)
        self.card_impact.set_text(action.impact)
        targets_text = "\n".join(action.affected_targets)
        self.card_targets.set_text(targets_text)
        risk = RiskLevel.DESTRUCTIVE if action.action_type == ActionType.UNINSTALL_PACKAGE else action.risk_level
        self._set_review_style(risk)
        labels = {
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
        self.confirm_btn.set_label(labels.get(action.action_type, "Continue"))
        self.confirm_btn.set_sensitive(True)
        self._show_review()
        self.file_scroll.hide()
        if not targets_text:
            self.targets_scroll.hide()

    def _set_review_style(self, risk: RiskLevel) -> None:
        badge = self.risk_badge.get_style_context()
        card = self.review_box.get_style_context()
        for level in RiskLevel:
            badge.remove_class(f"badge-{level.value}")
            card.remove_class(f"review-{level.value}")
        badge.add_class(f"badge-{risk.value}")
        card.add_class(f"review-{risk.value}")
        self.risk_badge.set_text(risk.value.replace("_", " ").title())
        button = self.confirm_btn.get_style_context()
        button.remove_class("btn-primary")
        button.remove_class("btn-destructive")
        button.add_class("btn-destructive" if risk == RiskLevel.DESTRUCTIVE else "btn-primary")

    def _show_review(self):
        for view in (self.chips_box, self.output_scroll, self.process_scroll, self.technical_details, self.undo_box):
            view.hide()
        self.entry.set_sensitive(False)
        self.hist_btn.set_sensitive(False)
        self.review_box.show_all()
        self.adjust_window_size()
        self.cancel_btn.grab_focus()

    def show_workflow_review(self, review: WorkflowReview, summary: str) -> None:
        styles = {WorkflowRisk.READ_ONLY: RiskLevel.READ_ONLY, WorkflowRisk.MUTATING: RiskLevel.REVERSIBLE,
                  WorkflowRisk.PRIVILEGED: RiskLevel.PRIVILEGED, WorkflowRisk.DESTRUCTIVE: RiskLevel.DESTRUCTIVE}
        self.file_review = None
        self.file_checks = []
        for child in self.file_list.get_children():
            self.file_list.remove(child)
        self.set_status("● Review workflow", "review")
        self.card_title.set_text(review.title)
        self.card_impact.set_text(summary)
        self.card_targets.set_text("")
        self._set_review_style(styles[review.risk])
        self.risk_badge.set_text(review.risk.value.replace("_", " ").title())
        for step in review.steps:
            line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, margin=8)
            text = f"{step.number}. {step.summary}"
            if step.targets:
                text += "\n" + "\n".join(step.targets)
            if step.error:
                text += "\n" + step.error
            label = Gtk.Label(label=text, xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, selectable=True)
            line.pack_start(label, True, True, 0)
            badge = Gtk.Label(label=step.risk.value.replace("_", " ").title(), valign=Gtk.Align.START)
            for name in ("risk-badge", f"badge-{styles[step.risk].value}"):
                badge.get_style_context().add_class(name)
            line.pack_end(badge, False, False, 0)
            self.file_list.add(line)
        count = len(review.steps)
        self.confirm_btn.set_label(f"Run {count} {'step' if count == 1 else 'steps'}")
        self.confirm_btn.set_sensitive(review.allowed)
        self._show_review()
        self.targets_scroll.hide()

    def show_file_review(self, review: FileReview, summary: str) -> None:
        self.file_review = review
        self.file_rows = list(review.rows)
        self.file_checks = []
        for child in self.file_list.get_children():
            self.file_list.remove(child)
        self.set_status("● Review files", "review")
        self.card_title.set_text("Review batch Undo" if review.workflow_batch else
                                 "Review Undo" if review.action == "undo" else "Review file moves")
        self.card_impact.set_text(summary + " Select files to include. Rename a destination to resolve a conflict.")
        self.card_targets.set_text("")
        self._set_review_style(RiskLevel.REVERSIBLE)
        for index, row in enumerate(self.file_rows):
            line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, margin=5)
            check = Gtk.CheckButton()
            check.get_accessible().set_name(f"Include {display_path(row.source)}")
            check.set_active(row.included and not row.problem)
            check.set_sensitive(not row.problem)
            check.connect("toggled", lambda _button: self.update_file_count())
            self.file_checks.append(check)
            line.pack_start(check, False, False, 0)
            path = Gtk.Label(xalign=0, selectable=True, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR)
            path.get_style_context().add_class("file-path")
            line.pack_start(path, True, True, 0)
            rename = Gtk.Button(label="Rename")
            rename.set_sensitive(row.source_valid)
            rename.connect("clicked", self.rename_file_row, index, path)
            line.pack_end(rename, False, False, 0)
            self._set_file_label(path, row)
            self.file_list.add(line)
        self._show_review()
        self.targets_scroll.hide()
        self.update_file_count()

    def _set_file_label(self, label: Gtk.Label, row: FileRow) -> None:
        step = f"Step {row.workflow_step} · " if row.workflow_step is not None else ""
        lines = [step + display_path(row.source), "→ " + display_path(row.destination)]
        if row.changed:
            lines.append("Changed since preview")
        if row.problem:
            lines.append(display_path(row.problem))
        context = label.get_style_context()
        if row.problem:
            context.add_class("file-problem")
        else:
            context.remove_class("file-problem")
        label.set_text("\n".join(lines))

    def rename_file_row(self, _button: Gtk.Button, index: int, label: Gtk.Label) -> None:
        row = self.file_rows[index]
        dialog = Gtk.Dialog(title="Choose a new file name", transient_for=self, modal=True)
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Use name", Gtk.ResponseType.OK)
        entry = Gtk.Entry(text=display_path(basename(row.destination)), margin=12, activates_default=True)
        dialog.get_content_area().add(entry)
        dialog.set_default_response(Gtk.ResponseType.OK)
        dialog.show_all()
        response = dialog.run()
        name = entry.get_text()
        dialog.destroy()
        if response != Gtk.ResponseType.OK:
            return
        self.request_file_rename(index, label, name)

    def update_file_count(self):
        count = sum(check.get_active() for check in self.file_checks)
        verb = "Restore" if self.file_review.action == "undo" else "Move"
        self.confirm_btn.set_label(f"{verb} {count} {'file' if count == 1 else 'files'}")
        self.confirm_btn.set_sensitive(count > 0 and not self.state.renaming)

    def render_output_card(self, title: str, body: str = "", duration_ms: float = 0.0,
                           verification_status: str = "verified", is_error: bool = False):
        """Renders a high-contrast formatted monospace card into the buffer with smooth scrolling."""
        self.buffer.set_text("")
        it = self.buffer.get_end_iter()

        status_tag = "status_failed" if is_error else (
            "status_completed" if verification_status == "verified" else "status_launch"
        )
        icon = "✗ " if is_error else "✓ " if verification_status == "verified" else ""
        self.buffer.insert_with_tags_by_name(it, f"{icon}{title}\n", status_tag)

        if body and body.strip():
            it = self.buffer.get_end_iter()
            sep = "─────────────────────────────────────────────────────────────────\n"
            self.buffer.insert_with_tags_by_name(it, sep, "sep")
            it = self.buffer.get_end_iter()
            body_clean = body.rstrip() + "\n"
            self.buffer.insert_with_tags_by_name(it, body_clean, "code_block")
            it = self.buffer.get_end_iter()
            self.buffer.insert_with_tags_by_name(it, sep, "sep")

        if duration_ms > 0 or verification_status:
            it = self.buffer.get_end_iter()
            footer_parts = []
            if duration_ms > 0:
                footer_parts.append(f"⏱ Duration: {duration_ms:.0f}ms")
            if verification_status:
                footer_parts.append(f"Verification: {verification_status}")
            footer = "   │   ".join(footer_parts) + "\n"
            self.buffer.insert_with_tags_by_name(it, footer, "timestamp")

        v_adj = self.output_scroll.get_vadjustment()
        if v_adj:
            v_adj.set_value(0.0)

    def show_processes(self, processes: list[dict[str, Any]]) -> None:
        for child in self.process_scroll.get_children():
            self.process_scroll.remove(child)
        store = Gtk.ListStore(str, str, str, str)
        for row in processes:
            store.append((row["command"], str(row["pid"]), f"{row['ram_mb']:.1f}", f"{row['cpu_pct']:.1f}"))
        table = Gtk.TreeView(model=store)
        for i, title in enumerate(("Process", "PID", "RAM (MB)", "CPU %")):
            renderer = Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.END)
            column = Gtk.TreeViewColumn(title, renderer, text=i)
            column.set_resizable(True)
            column.set_expand(i == 0)
            table.append_column(column)
        self.process_scroll.add(table)
        self.process_scroll.show_all()

    def render_history_view(self):
        """Renders enhanced audit and history entries with badges, timestamps, and parameters."""
        self.buffer.set_text("")
        records = self.state.activity

        if not records:
            it = self.buffer.get_end_iter()
            text = "Loading activity…" if self.state.activity_loading else "No activity or audit history found."
            self.buffer.insert_with_tags_by_name(it, text + "\n", "summary")
            self.adjust_window_size()
            return

        it = self.buffer.get_end_iter()
        self.buffer.insert_with_tags_by_name(
            it, f"── ACTIVITY & AUDIT HISTORY ({len(records)} entries) ──────────────────────────────────\n\n", "header"
        )

        for rec in reversed(records[-30:]):
            it = self.buffer.get_end_iter()

            # 1. Timestamp
            date_text = f"[{rec.get('date_str', 'recent')}] "
            self.buffer.insert_with_tags_by_name(it, date_text, "timestamp")

            # 2. Risk Badge
            risk = str(rec.get("risk_level", "read_only")).lower()
            badge_map = {
                "read_only": ("[READ-ONLY]", "badge_readonly"),
                "reversible": ("[REVERSIBLE]", "badge_reversible"),
                "mutating": ("[MUTATING]", "badge_reversible"),
                "privileged": ("[PRIVILEGED]", "badge_privileged"),
                "destructive": ("[DESTRUCTIVE]", "badge_destructive")
            }
            badge_label, tag_name = badge_map.get(risk, (f"[{risk.upper()}]", "badge_readonly"))
            it = self.buffer.get_end_iter()
            self.buffer.insert_with_tags_by_name(it, f"{badge_label} ", tag_name)

            # 3. Status Badge
            raw_st = str(rec.get("status", "completed")).lower()
            if "fail" in raw_st or "error" in raw_st:
                st_label, st_tag = "● Failed", "status_failed"
            elif "cancel" in raw_st:
                st_label, st_tag = "● Cancelled", "status_cancelled"
            elif raw_st == "partial":
                st_label, st_tag = "● Partial result", "status_failed"
            elif raw_st == "busy":
                st_label, st_tag = "● Busy", "status_cancelled"
            elif "launch" in raw_st or "terminal" in raw_st:
                st_label, st_tag = "● Launch Requested", "status_launch"
            else:
                st_label, st_tag = "● Completed", "status_completed"
            it = self.buffer.get_end_iter()
            self.buffer.insert_with_tags_by_name(it, f"{st_label}\n", st_tag)

            # 4. Query / Heading
            query = rec.get("query", "").strip()
            act_name = rec.get("action")
            heading = query or (f"Action: {act_name}" if act_name else "Action")
            it = self.buffer.get_end_iter()
            self.buffer.insert_with_tags_by_name(it, f"  Query:  {heading}\n", "query")

            # 5. Target Parameters
            params = rec.get("params", {})
            if params:
                param_strs = [f"{k}={repr(v)}" for k, v in params.items()]
                targets_line = f"  Params: {', '.join(param_strs)}\n"
            elif act_name:
                targets_line = f"  Action: {act_name}\n"
            else:
                targets_line = ""

            if targets_line:
                it = self.buffer.get_end_iter()
                self.buffer.insert_with_tags_by_name(it, targets_line, "params")

            # Summary if distinct from query
            summary = rec.get("summary", "").strip()
            if summary and summary != query:
                it = self.buffer.get_end_iter()
                self.buffer.insert_with_tags_by_name(it, f"  Detail: {summary}\n", "summary")

            # Separator
            it = self.buffer.get_end_iter()
            self.buffer.insert_with_tags_by_name(it, "  ─────────────────────────────────────────────────────────────\n", "sep")

        self.adjust_window_size()
