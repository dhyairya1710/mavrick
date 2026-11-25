"""Platform-neutral entry points for system operations."""

import os
import shutil
import stat
import subprocess

from ..platform import get_platform
from ..platform import linux_system


def get_system_health():
    return get_platform().get_system_info()


def _linux_call(function, label, *args, **kwargs):
    adapter = get_platform()
    if adapter.name != "linux":
        raise adapter.unsupported(label)
    return function(*args, **kwargs)


def inspect_processes(sort_by="memory", limit=10):
    return _linux_call(linux_system.inspect_processes, "Process telemetry", sort_by, limit)


def set_volume(level=None, mute=None, delta=None):
    return _linux_call(linux_system.set_volume, "Volume control", level, mute, delta)


def lock_session():
    return _linux_call(linux_system.lock_session, "Session lock")


def get_power_profile():
    return _linux_call(linux_system.get_power_profile, "Power profile inspection")


def set_power_profile(profile):
    return _linux_call(linux_system.set_power_profile, "Power profile changes", profile)


def control_media(action="status", player=None):
    return _linux_call(linux_system.control_media, "Media controls", action, player)


def get_network_status():
    return _linux_call(linux_system.get_network_status, "Network diagnostics")


def inspect_storage_usage(category="apps", limit=10):
    return _linux_call(linux_system.inspect_storage_usage, "Detailed application storage", category, limit)

def check_updates():
    from .packages import check_updates as check
    return check()


def clean_cache(dry_run=True, thumbnail_dir=None, apt_dir=None):
    adapter = get_platform()
    if adapter.name == "linux":
        return linux_system.clean_cache(dry_run, thumbnail_dir, apt_dir)
    if not dry_run or thumbnail_dir or apt_dir:
        return False, str(adapter.unsupported("Verified cache cleanup")), {"dry_run": dry_run}
    total, count, errors = 0, 0, []
    for target in adapter.get_cache_targets():
        try:
            if os.path.lexists(target.path) and not adapter.is_private(os.lstat(target.path), directory=True):
                raise OSError(f"Refusing a linked or non-private cache: {target.path}")
            for root, dirs, files in os.walk(target.path, followlinks=False, onerror=errors.append):
                dirs[:] = [name for name in dirs if adapter.is_private(os.lstat(os.path.join(root, name)), directory=True)]
                for name in files:
                    info = os.lstat(os.path.join(root, name))
                    if stat.S_ISREG(info.st_mode):
                        total += info.st_size
                        count += 1
        except OSError as error:
            errors.append(error)
    summary = f"Cache preview: {count} files, {total} bytes in Mavrick cache targets."
    return not errors, summary + "".join(f"\n{error}" for error in errors), {
        "dry_run": True, "total_reclaimable_bytes": total, "failed": len(errors),
    }
