"""GTK window lifecycle, keyboard navigation, and HUD entry point."""

import logging
from concurrent.futures import Future
from typing import Optional, Tuple

from gi.repository import Gdk, Gtk

from .. import ui as api
from ..executor import ActionExecutor
from .controller import (
    GLibScheduler,
    HUDController,
    close_hud_socket,
    deliver,
    listen_for_hud_requests,
    prepare_hud_socket,
    submit,
)
from .state import HUDState, state_field
from .styles import apply_styles
from .widgets import HUDWidgets


class MavrickWindow(Gtk.Window, HUDWidgets, HUDController):
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

    def __init__(self):
        super().__init__(title="Mavrick")
        self.get_style_context().add_class("mavrick-hud")
        self.set_type_hint(Gdk.WindowTypeHint.DIALOG)
        # Responsive & Resizable window
        self.set_resizable(True)
        resp_width = self.get_responsive_width()
        resp_height = self.get_responsive_height()
        self.set_default_size(resp_width, resp_height)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_decorated(False)
        self.set_keep_above(True)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.stick()

        # Transparent composited visuals
        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual and screen.is_composited():
            self.set_visual(visual)

        # CSD titlebar hidden to remove server-side decorations
        hb = Gtk.HeaderBar()
        hb.set_show_close_button(False)
        self.set_titlebar(hb)
        hb.hide()

        theme = apply_styles(screen)
        self.state = HUDState(theme=theme)
        self.ui_scheduler = GLibScheduler()
        self.executor = ActionExecutor.get_instance()
        self.file_checks: list[Gtk.CheckButton] = []
        self.send_desktop_notification = self._notify_in_background

        self.build_ui()
        self.setup_buffer_tags(theme)
        self.connect("key-press-event", self.on_key_press)
        self.entry.connect("key-press-event", self.on_entry_key_press)
        self.connect("destroy", self._mark_closed)
        self.refresh_history()
        self.refresh_app_index()
        self.refresh_workflows()

    def _mark_closed(self, _widget: Gtk.Widget) -> None:
        self.state.closed = True
        if self.cancel_token is not None:
            self.cancel_token.set()

    def get_active_monitor(self) -> Optional[Gdk.Monitor]:
        """Queries pointer coordinates via Gdk seat and returns the active Gdk.Monitor."""
        try:
            display = Gdk.Display.get_default()
            if display:
                seat = display.get_default_seat()
                if seat:
                    pointer = seat.get_pointer()
                    if pointer:
                        pos = pointer.get_position()
                        if pos and len(pos) >= 3:
                            # pos is (screen, x, y)
                            _, x, y = pos[0], pos[1], pos[2]
                            mon = display.get_monitor_at_point(x, y)
                            if mon:
                                return mon
                # Fallbacks if pointer or seat unavailable
                mon = display.get_primary_monitor()
                if mon:
                    return mon
                if display.get_n_monitors() > 0:
                    return display.get_monitor(0)
        except Exception as e:
            logging.debug(f"Error querying active monitor: {e}")
        return None

    def position_on_active_monitor(self):
        """Positions and centers the HUD on the active monitor where the mouse currently is."""
        try:
            mon = self.get_active_monitor()
            if not mon:
                return
            geom = mon.get_geometry()
            win_w, win_h = self.get_size()
            if win_w <= 1 or win_h <= 1:
                win_w = self.get_responsive_width()
                win_h = self.get_responsive_height()
            center_x = geom.x + max(0, (geom.width - win_w) // 2)
            center_y = geom.y + max(0, (geom.height - win_h) // 2)
            self.move(center_x, center_y)
        except Exception as e:
            logging.debug(f"Error positioning window on active monitor: {e}")

    def get_monitor_geometry(self) -> Tuple[int, int]:
        """Returns (width, height) of the active monitor or fallback."""
        try:
            mon = self.get_active_monitor()
            if mon:
                geom = mon.get_workarea()
                return geom.width, geom.height
            display = Gdk.Display.get_default()
            if display:
                monitor = display.get_primary_monitor()
                if not monitor and display.get_n_monitors() > 0:
                    monitor = display.get_monitor(0)
                if monitor:
                    geom = monitor.get_geometry()
                    return geom.width, geom.height
            screen = self.get_screen()
            if screen:
                return screen.get_width(), screen.get_height()
        except Exception as e:
            logging.debug(f"Monitor query error: {e}")
        return 1920, 1080

    def navigate_history(self, direction: int) -> None:
        """Cycles backwards (Up, -1) or forwards (Down, +1) through recent query history."""
        queries = self.get_history_queries()
        if not queries and direction < 0:
            return

        # If currently at draft position (not navigating), save current entry text as draft
        if self.history_idx >= len(queries):
            self.history_draft = self.entry.get_text()

        new_idx = self.history_idx + direction
        if new_idx < 0:
            new_idx = 0
        elif new_idx > len(queries):
            new_idx = len(queries)

        if new_idx == self.history_idx and new_idx != len(queries):
            return

        self.history_idx = new_idx
        if self.history_idx < len(queries):
            selected_query = queries[self.history_idx]
            self.entry.set_text(selected_query)
        else:
            # Restored draft text
            self.entry.set_text(self.history_draft)
        self.entry.set_position(-1)

    def on_entry_key_press(self, widget: Gtk.Entry, event: Gdk.EventKey) -> bool:
        """Handles key presses inside the search entry (Up/Down history cycling and Esc)."""
        if event.keyval == Gdk.KEY_Up:
            self.navigate_history(-1)
            return True
        elif event.keyval == Gdk.KEY_Down:
            self.navigate_history(1)
            return True
        elif event.keyval == Gdk.KEY_Escape:
            return self.handle_escape()
        return False

    def handle_escape(self) -> bool:
        if self.is_processing:
            if self.cancel_token is not None and not self.cancel_token.is_set():
                self.on_stop_action()
            else:
                self.hide_window()
        elif self.review_box.is_visible():
            self.on_cancel_action()
        elif self.history_mode:
            self.toggle_history()
        elif self.browse_box.get_visible() or self.output_scroll.get_visible():
            self.render_welcome_state()
        elif self.entry.get_text():
            self.entry.set_text("")
        else:
            self.hide_window()
        return True

    def on_command_search_key(self, _entry: Gtk.SearchEntry, event: Gdk.EventKey) -> bool:
        if event.keyval in (Gdk.KEY_Up, Gdk.KEY_Down):
            self.navigate_commands(-1 if event.keyval == Gdk.KEY_Up else 1)
            return True
        if event.keyval == Gdk.KEY_Escape:
            return self.handle_escape()
        return False

    def get_responsive_width(self) -> int:
        monitor_width, _ = self.get_monitor_geometry()
        return min(660, max(280, monitor_width - 32))

    def get_responsive_height(self) -> int:
        return 165

    def get_max_content_height(self) -> int:
        _, monitor_height = self.get_monitor_geometry()
        return max(70, min(460, monitor_height - 280))

    def adjust_window_size(self, content_lines: int | None = None) -> None:
        if self.manual_resize:
            return
        _, monitor_height = self.get_monitor_geometry()
        expanded = (self.review_box.get_visible() or self.output_scroll.get_visible() or
                    self.browse_box.get_visible())
        wanted = min(490, max(205, monitor_height - 36)) if expanded else self.get_responsive_height()
        self.resize(self.get_responsive_width(), wanted)

    def _resize_from_grip(self, widget: Gtk.Widget, event: Gdk.EventButton) -> bool:
        if event.button != 1:
            return False
        self.manual_resize = True
        self.begin_resize_drag(Gdk.WindowEdge.SOUTH_EAST, event.button,
                               int(event.x_root), int(event.y_root), event.time)
        return True

    def on_clear_history(self):
        """Prompts for confirmation before clearing activity history and audit logs."""
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            destroy_with_parent=True,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.OK_CANCEL,
            text="Clear all activity history and audit logs?"
        )
        dialog.format_secondary_text("This will permanently remove all recent queries, session context, and execution audit records.")
        response = dialog.run()
        dialog.destroy()

        if response != Gtk.ResponseType.OK:
            return
        self.state.history_generation += 1
        result = api.clear_history_and_context(background=True)
        if isinstance(result, Future):
            def ready(future):
                if self.state.closed:
                    return
                try:
                    cleared = future.result()
                except Exception as error:
                    logging.warning("Could not clear Mavrick history: %s", error)
                    cleared = False
                self._history_cleared(cleared)

            deliver(result, ready)
        else:
            self._history_cleared(bool(result))

    def _history_cleared(self, cleared: bool) -> None:
        if cleared:
            self.history = []
            self.state.activity = []
            self.state.activity_loading = False
            self.history_draft = ""
            self.history_idx = 0
            if self.history_mode:
                self.render_history_view()
            self.set_status("● History Cleared", "ready")
        else:
            self.set_status("● Could not clear history", "error")
            self.refresh_history(include_audit=self.history_mode)

    def toggle_history(self) -> None:
        if self.is_processing or self.pending_action or self.state.pending_workflow:
            return
        for view in (self.browse_box, self.undo_box, self.process_scroll, self.technical_details):
            view.hide()
        self.history_mode = not self.history_mode
        if self.history_mode:
            self.history_bar.show_all()
            self.refresh_history(include_audit=True)
            self.render_history_view()
            self.output_scroll.show_all()
            self.hist_btn.set_label("✕ Close History")
            self.adjust_window_size()
        else:
            self.history_bar.hide()
            self.buffer.set_text("")
            self.output_scroll.hide()
            self.adjust_window_size()
            self.hist_btn.set_label("⏱ History")

    def on_key_press(self, widget: Gtk.Widget, event: Gdk.EventKey) -> bool:
        # Escape: Dismiss HUD, cancel review, or close history
        if event.keyval == Gdk.KEY_Escape:
            return self.handle_escape()

        # Ctrl+H: Toggle History
        if (event.state & Gdk.ModifierType.CONTROL_MASK) and event.keyval in [Gdk.KEY_h, Gdk.KEY_H]:
            self.toggle_history()
            return True

        # Ctrl+Z: Undo last action
        if (event.state & Gdk.ModifierType.CONTROL_MASK) and event.keyval in [Gdk.KEY_z, Gdk.KEY_Z]:
            self.on_trigger_undo()
            return True

        # Ctrl+L clears the input when no action is active or under review.
        if (event.state & Gdk.ModifierType.CONTROL_MASK) and event.keyval in [Gdk.KEY_l, Gdk.KEY_L]:
            if not self.is_processing and not self.pending_action and not self.state.pending_workflow:
                self.history_mode = False
                self.history_bar.hide()
                self.hist_btn.set_label("⏱ History")
                self.entry.set_text("")
                self.render_welcome_state()
            return True

        return False

    def hide_window(self) -> bool:
        self.hide()
        return False

    def show_window(self) -> bool:
        if self.state.closed:
            return False
        self.stick()
        self.set_keep_above(True)
        self.position_on_active_monitor()
        self.show()
        self.adjust_window_size()
        self.present()
        if self.review_box.get_visible():
            self.cancel_btn.grab_focus()
        elif not self.is_processing:
            self.entry.grab_focus()
        self.refresh_health_badge(probe=False)
        self.refresh_history()
        self.history_draft = ""
        self.history_idx = len(self.get_history_queries())
        return False


def main() -> None:
    if api.select_ui_backend() == "tk":
        from .tk_hud import main as tk_main

        tk_main()
        return

    server = None
    startup_error = None

    def ready(future: Future) -> None:
        nonlocal server, startup_error
        try:
            server = future.result()
            if server is None:
                Gtk.main_quit()
                return
            window = api.MavrickWindow()
            window.connect("destroy", Gtk.main_quit)
            listen_for_hud_requests(window, server)
            window.show_window()
        except Exception as error:
            startup_error = error
            Gtk.main_quit()

    deliver(submit(prepare_hud_socket), ready)
    try:
        Gtk.main()
    finally:
        if server is not None:
            submit(lambda: close_hud_socket(server)).result()
    if startup_error is not None:
        raise startup_error
