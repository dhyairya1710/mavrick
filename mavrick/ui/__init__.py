"""Mavrick desktop HUD package with toolkit-agnostic backend selection (GTK3 / Tk)."""

from __future__ import annotations

import importlib.util
import os
import subprocess  # Retained for backwards-compatible mavrick.ui namespace callers/tests.
import sys
import threading  # Retained for backwards-compatible mavrick.ui namespace callers/tests.
from typing import Any

from ..ai import check_ai_health, instant_response, query_model
from ..config import AUDIT_LOG_FILE, HISTORY_FILE, MAVRICK_SOCK, ensure_secure_storage
from ..executor import ActionExecutor, ActionResult, approve_action, approve_files
from .. import history as _history
from ..history import append_history, search_history
from ..platform import get_platform
from ..schema import ACTION_RISK_MAP, ActionRequest, ActionType, ModelResponse, RiskLevel
from ..tools.files import display_path, rename_review_row
from .controller import (
    GLibScheduler,
    HUDController,
    HUDView,
    InlineScheduler,
    UIScheduler,
    client_send_toggle,
    default_ui_scheduler,
    deliver,
    erase_history,
    start_socket_listener,
    submit,
    toggle_visibility,
)
from .styles import DARK_CSS, LIGHT_CSS, MAVRICK_CSS, theme_colors
from .tk_hud import TkMavrickWindow, TkScheduler, tk_available

UI_BACKENDS = ("auto", "gtk", "tk")


def gtk_available() -> bool:
    """Return True when PyGObject (gi) appears importable without raising on headless hosts."""
    try:
        if "gi" in sys.modules:
            return sys.modules["gi"] is not None
        return importlib.util.find_spec("gi") is not None
    except (ImportError, ValueError):
        return False


def select_ui_backend(backend: str | None = None, *, platform_name: str | None = None) -> str:
    """Resolve active UI backend: 'gtk', 'tk', or 'none'."""
    raw = backend if backend is not None else os.environ.get("MAVRICK_UI_BACKEND", "auto")
    if not isinstance(raw, str):
        raise ValueError("Invalid MAVRICK_UI_BACKEND. Use auto, gtk, or tk.")
    chosen = raw.strip().lower() or "auto"
    if chosen not in UI_BACKENDS:
        raise ValueError("Invalid MAVRICK_UI_BACKEND. Use auto, gtk, or tk.")
    if chosen in ("gtk", "tk"):
        return chosen
    host = platform_name or get_platform().name
    if host == "linux" and gtk_available():
        return "gtk"
    if tk_available():
        return "tk"
    if gtk_available():
        return "gtk"
    return "none"


def unavailable_ui_guidance(requested: str = "auto") -> str:
    if requested == "gtk":
        return (
            "GTK3 HUD backend is unavailable. Install PyGObject/GTK3 "
            "(e.g. 'sudo apt install python3-gi gir1.2-gtk-3.0') or run with MAVRICK_UI_BACKEND=tk / CLI mode."
        )
    if requested == "tk":
        return (
            "Tkinter HUD backend is unavailable. Install Python's tkinter package "
            "(e.g. 'sudo apt install python3-tk') or run CLI commands like 'mavrick --doctor'."
        )
    return (
        "No GUI toolkit is available (neither GTK3 nor Tkinter). "
        "Install python3-gi / python3-tk, or use Mavrick via CLI (e.g. 'mavrick --doctor' or 'mavrick \"system health\"')."
    )


def _headless_main() -> None:
    requested = (os.environ.get("MAVRICK_UI_BACKEND", "auto") or "auto").strip().lower()
    backend = select_ui_backend()
    if backend == "tk":
        from .tk_hud import main as tk_main

        tk_main()
        return
    if backend == "gtk":
        try:
            import gi

            gi.require_version("Gtk", "3.0")
            gi.require_version("Gdk", "3.0")
            from .window import main as gtk_main
        except (ImportError, ValueError) as error:
            raise ImportError(unavailable_ui_guidance(requested)) from error
        gtk_main()
        return
    raise ImportError(unavailable_ui_guidance(requested))


Gdk: Any = None
GLib: Any = None
Gtk: Any = None
Pango: Any = None
apply_styles: Any = None
MavrickWindow: Any = TkMavrickWindow
main = _headless_main

_initial_backend = "none"
try:
    _initial_backend = select_ui_backend()
except ValueError:
    _initial_backend = "auto"

if _initial_backend in ("gtk", "auto") and gtk_available():
    try:
        import gi

        gi.require_version("Gtk", "3.0")
        gi.require_version("Gdk", "3.0")
        from gi.repository import Gdk, GLib, Gtk, Pango

        from .styles import apply_styles
        from .window import MavrickWindow, main
    except (ImportError, ValueError):
        Gdk = GLib = Gtk = Pango = None
        apply_styles = None
        MavrickWindow = TkMavrickWindow
        main = _headless_main


def load_history(snapshot=None):
    """Return the HUD's in-memory snapshot when supplied, or read on explicit calls."""
    return _history.load_history() if snapshot is None else snapshot


def clear_history_and_context(*, background: bool = False):
    """Keep the synchronous public API while scheduling deletion for the HUD."""
    return submit(erase_history) if background else _history.clear_history_and_context()
