#!/usr/bin/env python3
"""
MAVRICK Tool Implementations
Export all typed tool functions.
"""

from .system import (
    get_system_health,
    inspect_processes,
    set_volume,
    lock_session,
    set_power_profile,
    get_power_profile,
    control_media,
    get_network_status,
    clean_cache,
    inspect_storage_usage
)
from .apps import open_app, open_url, arrange_windows, run_in_terminal, detect_preferred_terminal
from .files import list_files, organize_downloads, move_files, undo_last_action
from .packages import install_package, uninstall_package, verify_package_installed, check_updates

__all__ = [
    "get_system_health",
    "inspect_processes",
    "set_volume",
    "lock_session",
    "set_power_profile",
    "get_power_profile",
    "control_media",
    "get_network_status",
    "clean_cache",
    "inspect_storage_usage",
    "open_app",
    "run_in_terminal",
    "detect_preferred_terminal",
    "open_url",
    "arrange_windows",
    "list_files",
    "move_files",
    "organize_downloads",
    "undo_last_action",
    "install_package",
    "uninstall_package",
    "verify_package_installed",
    "check_updates"
]
