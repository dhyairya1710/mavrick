"""Resolve the native adapter, with a scoped injection hook for tests."""

import os
import sys

if __name__ == "platform":
    # The repository uses a flat package-dir. When run from its source directory,
    # tools such as pip must still be able to import Python's stdlib platform module.
    from importlib.util import module_from_spec, spec_from_file_location
    from pathlib import Path

    stdlib_platform = Path(os.__file__).with_name("platform.py")
    spec = spec_from_file_location(__name__, stdlib_platform)
    module = module_from_spec(spec)
    sys.modules[__name__] = module
    spec.loader.exec_module(module)
else:
    from contextlib import contextmanager
    from contextvars import ContextVar
    from functools import lru_cache

    from .base import PlatformAdapter, UnsupportedPlatformAction
    from .linux import LinuxAdapter
    from .macos import MacOSAdapter
    from .windows import WindowsAdapter

    _override: ContextVar[PlatformAdapter | None] = ContextVar("mavrick_platform", default=None)

    @lru_cache(maxsize=3)
    def _native(host: str) -> PlatformAdapter:
        if host.startswith("linux"):
            return LinuxAdapter()
        if host == "darwin":
            return MacOSAdapter()
        if host == "win32":
            return WindowsAdapter()
        raise UnsupportedPlatformAction(host, "Mavrick runtime")

    def get_platform(host: str | None = None) -> PlatformAdapter:
        if host is None and (injected := _override.get()) is not None:
            return injected
        return _native(host or sys.platform)

    @contextmanager
    def use_platform(adapter: PlatformAdapter):
        token = _override.set(adapter)
        try:
            yield adapter
        finally:
            _override.reset(token)

    from .services import (
        linux_systemd_templates,
        macos_launchd_plist,
        service_template_for_platform,
        windows_powershell_register_command,
        windows_task_xml,
    )
