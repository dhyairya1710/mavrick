#!/usr/bin/env python3
"""
MAVRICK System Tools
Structured inspection and management of system health, processes, audio, and session.
Never invokes shell=True; uses direct argument vectors and kernel proc interfaces.
"""

import os
import stat
import re
import glob
import json
import shutil
import logging
import subprocess
from typing import Dict, Any, List, Optional, Tuple

from ..tools.files import _parent

def get_system_health() -> Dict[str, Any]:
    """
    Collects structured system metrics: RAM, Disk, CPU load, and Battery.
    Returns both raw metric data and a human-readable summary.
    """
    metrics: Dict[str, Any] = {}

    # 1. RAM Inspection via /proc/meminfo
    try:
        meminfo = {}
        with open("/proc/meminfo", "r") as fp:
            for line in fp:
                parts = line.split(":")
                if len(parts) == 2:
                    key = parts[0].strip()
                    val = parts[1].strip().split()[0]
                    meminfo[key] = int(val) # in kB

        total_kb = meminfo.get("MemTotal", 0)
        avail_kb = meminfo.get("MemAvailable", 0)
        used_kb = total_kb - avail_kb
        mem_pct = round((used_kb / total_kb) * 100, 1) if total_kb else 0

        metrics["memory"] = {
            "total_mb": round(total_kb / 1024, 1),
            "used_mb": round(used_kb / 1024, 1),
            "available_mb": round(avail_kb / 1024, 1),
            "used_pct": mem_pct
        }
    except Exception as e:
        metrics["memory"] = {"error": str(e)}

    # 2. Disk Usage
    try:
        root_usage = shutil.disk_usage("/")
        metrics["disk"] = {
            "total_gb": round(root_usage.total / (1024**3), 1),
            "used_gb": round(root_usage.used / (1024**3), 1),
            "free_gb": round(root_usage.free / (1024**3), 1),
            "used_pct": round((root_usage.used / root_usage.total) * 100, 1)
        }
    except Exception as e:
        metrics["disk"] = {"error": str(e)}

    # 3. CPU Load
    try:
        load1, load5, load15 = os.getloadavg()
        cpu_count = os.cpu_count() or 1
        metrics["cpu"] = {
            "cores": cpu_count,
            "load_1m": round(load1, 2),
            "load_5m": round(load5, 2),
            "load_15m": round(load15, 2)
        }
    except Exception as e:
        metrics["cpu"] = {"error": str(e)}

    # 4. Battery / Power
    try:
        bat_capacity_file = "/sys/class/power_supply/BAT0/capacity"
        bat_status_file = "/sys/class/power_supply/BAT0/status"
        if os.path.exists(bat_capacity_file):
            with open(bat_capacity_file) as f:
                cap = int(f.read().strip())
            status = "Unknown"
            if os.path.exists(bat_status_file):
                with open(bat_status_file) as f:
                    status = f.read().strip()
            metrics["battery"] = {"percentage": cap, "status": status}
        else:
            metrics["battery"] = {"status": "AC Power (No Battery Detected)"}
    except Exception as e:
        metrics["battery"] = {"status": f"Unavailable: {e}"}

    # 5. GPU Telemetry (NVIDIA / AMD)
    try:
        gpu_info = {}
        if shutil.which("nvidia-smi"):
            nv_cmd = [
                "nvidia-smi",
                "--query-gpu=utilization.gpu,memory.total,memory.used,temperature.gpu",
                "--format=csv,noheader,nounits"
            ]
            nv_res = subprocess.run(nv_cmd, capture_output=True, text=True, timeout=2.0)
            if nv_res.returncode == 0 and nv_res.stdout.strip():
                first_line = nv_res.stdout.strip().splitlines()[0]
                parts = [p.strip() for p in first_line.split(",")]
                if len(parts) >= 4:
                    gpu_info = {
                        "vendor": "NVIDIA",
                        "utilization_pct": float(parts[0]),
                        "memory_total_mb": float(parts[1]),
                        "memory_used_mb": float(parts[2]),
                        "temperature_c": float(parts[3])
                    }
        if not gpu_info:
            amd_cards = sorted(glob.glob("/sys/class/drm/card*/device/gpu_busy_percent"))
            for busy_file in amd_cards:
                try:
                    with open(busy_file, "r") as f:
                        busy_val = float(f.read().strip())
                    dev_dir = os.path.dirname(busy_file)
                    vram_total_file = os.path.join(dev_dir, "mem_info_vram_total")
                    vram_used_file = os.path.join(dev_dir, "mem_info_vram_used")
                    vtotal = 0.0
                    vused = 0.0
                    if os.path.exists(vram_total_file) and os.path.exists(vram_used_file):
                        with open(vram_total_file, "r") as f:
                            vtotal = round(int(f.read().strip()) / (1024 * 1024), 1)
                        with open(vram_used_file, "r") as f:
                            vused = round(int(f.read().strip()) / (1024 * 1024), 1)

                    temp_c = None
                    hwmon_files = sorted(glob.glob(os.path.join(dev_dir, "hwmon", "hwmon*", "temp*_input")))
                    for tf in hwmon_files:
                        try:
                            with open(tf, "r") as f:
                                millic = int(f.read().strip())
                                temp_c = round(millic / 1000.0, 1)
                                break
                        except Exception:
                            continue

                    gpu_info = {
                        "vendor": "AMD",
                        "utilization_pct": busy_val,
                        "memory_total_mb": vtotal,
                        "memory_used_mb": vused
                    }
                    if temp_c is not None:
                        gpu_info["temperature_c"] = temp_c
                    break
                except Exception:
                    continue

        if gpu_info:
            metrics["gpu"] = gpu_info
        else:
            metrics["gpu"] = {"status": "No dedicated GPU detected"}
    except Exception as e:
        metrics["gpu"] = {"status": f"Unavailable: {e}"}

    # Format user-readable report
    mem = metrics.get("memory", {})
    disk = metrics.get("disk", {})
    cpu = metrics.get("cpu", {})
    bat = metrics.get("battery", {})
    gpu = metrics.get("gpu", {})

    gpu_line = ""
    if "vendor" in gpu:
        vram_str = f" | {gpu.get('memory_used_mb', 0):.0f} / {gpu.get('memory_total_mb', 0):.0f} MB VRAM" if gpu.get("memory_total_mb") else ""
        temp_str = f" | {gpu.get('temperature_c')}°C" if "temperature_c" in gpu else ""
        gpu_line = f"\n  • GPU:  {gpu['vendor']} ({gpu.get('utilization_pct', 0):.0f}% util{vram_str}{temp_str})"
    elif "status" in gpu and gpu["status"]:
        gpu_line = f"\n  • GPU:  {gpu['status']}"

    report = (
        f"🖥️ System Health Dashboard:\n"
        f"  • RAM:  {mem.get('used_mb', 0):.0f} MB / {mem.get('total_mb', 0):.0f} MB in use ({mem.get('used_pct', 0)}%)\n"
        f"  • Disk: {disk.get('free_gb', 0):.1f} GB free of {disk.get('total_gb', 0):.1f} GB ({disk.get('used_pct', 0)}% used)\n"
        f"  • CPU:  {cpu.get('cores', 1)} cores | Load Avg: {cpu.get('load_1m', 0)} (1m), {cpu.get('load_5m', 0)} (5m)\n"
        f"  • Power: {bat.get('percentage', '')}{'%' if 'percentage' in bat else ''} {bat.get('status', 'OK')}"
        f"{gpu_line}"
    )

    return {"metrics": metrics, "report": report}

def inspect_processes(sort_by: str = "memory", limit: int = 10) -> Tuple[List[Dict[str, Any]], str]:
    """
    Inspects top running processes sorted by memory (RSS) or CPU.
    Uses direct parameter vectors to ps, never shell=True.
    """
    sort_flag = "--sort=-rss" if sort_by == "memory" else "--sort=-%cpu"
    cmd = ["ps", "-eo", "comm,pid,rss,%mem,%cpu", sort_flag]
    
    proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
    lines = proc.stdout.strip().splitlines()
    if not lines:
        return [], "No process data available."

    processes: List[Dict[str, Any]] = []
    # Skip header
    for line in lines[1:limit + 1]:
        parts = line.rsplit(maxsplit=4)
        if len(parts) == 5:
            comm = parts[0]
            pid = parts[1]
            rss_kb = float(parts[2])
            mem_pct = float(parts[3])
            cpu_pct = float(parts[4])
            processes.append({
                "command": comm,
                "pid": pid,
                "ram_mb": round(rss_kb / 1024, 1),
                "mem_pct": mem_pct,
                "cpu_pct": cpu_pct
            })

    # Render formatted table
    header = f"{'PROCESS':<22} {'PID':<8} {'RAM (MB)':<12} {'% MEM':<8} {'% CPU'}"
    div = "─" * len(header)
    row_strs = [header, div]
    for p in processes:
        row_strs.append(f"{p['command'][:20]:<22} {p['pid']:<8} {p['ram_mb']:<12.1f} {p['mem_pct']:<8.1f} {p['cpu_pct']:.1f}%")

    return processes, "\n".join(row_strs)

def set_volume(level=None, mute=None, delta=None):
    """Adjust the default sink and read back the resulting level and mute state."""
    if not shutil.which("wpctl"):
        return False, "wpctl is not installed on this system.", ""
    sink = "@DEFAULT_AUDIO_SINK@"
    if sum(value is not None for value in (level, mute, delta)) != 1:
        return False, "Choose one volume or mute operation.", ""
    if delta is not None:
        command = ["wpctl", "set-volume", sink, f"{abs(delta)}%{'+' if delta > 0 else '-'}", "--limit", "1.5"]
    elif level is not None:
        command = ["wpctl", "set-volume", sink, f"{level}%"]
    else:
        command = ["wpctl", "set-mute", sink, "1" if mute else "0"]
    result = subprocess.run(command, capture_output=True, text=True, timeout=5)
    if result.returncode:
        return False, result.stderr.strip() or f"wpctl returned {result.returncode}", ""
    result = subprocess.run(["wpctl", "get-volume", sink], capture_output=True, text=True, timeout=5)
    state = result.stdout.strip()
    match = re.search(r"Volume:\s+([0-9.]+)", state)
    if result.returncode or not match:
        return False, "Audio change requested. The resulting volume could not be verified.", result.stderr.strip()
    actual = round(float(match[1]) * 100)
    muted = "[MUTED]" in state
    if (level is not None and level != actual) or (mute is not None and mute != muted):
        return False, f"Audio readback returned {actual}%" + (" (muted)." if muted else "."), state
    return True, f"Audio volume is {actual}%" + (" (muted)." if muted else "."), state

def lock_session() -> Tuple[bool, str]:
    """Locks the desktop session using loginctl lock-session."""
    if not shutil.which("loginctl"):
        return False, "loginctl is not available."
    res = subprocess.run(["loginctl", "lock-session"], capture_output=True, text=True)
    if res.returncode == 0:
        return True, "Session lock requested."
    return False, f"Failed to lock session: {res.stderr.strip()}"

def get_power_profile() -> Tuple[bool, str]:
    """Query the running power-profile backend without changing the profile."""
    errors = []
    for command in (["system76-power", "profile"], ["powerprofilesctl", "get"]):
        if not shutil.which(command[0]):
            errors.append(f"{command[0]} unavailable")
            continue
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError) as error:
            errors.append(f"{command[0]}: {error}")
            continue
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                profile = line.partition(":")[2].strip().lower() if "Power Profile:" in line else line.strip().lower()
                profile = {"power-saver": "battery", "high performance": "performance"}.get(profile, profile)
                if profile in ("battery", "balanced", "performance"):
                    return True, profile
        errors.append(f"{command[0]}: {result.stderr.strip() or 'unrecognized power profile'}")
    return False, ", ".join(errors)

def set_power_profile(profile: str) -> Tuple[bool, str, str]:
    """
    Sets Pop!_OS power profile ('battery', 'balanced', 'performance') using system76-power
    with powerprofilesctl fallback.
    Returns (success, message, verification_details).
    """
    valid_profiles = ["battery", "balanced", "performance"]
    clean_profile = profile.strip().lower()
    if clean_profile not in valid_profiles:
        return False, f"Invalid power profile '{profile}'. Must be one of {valid_profiles}.", ""

    if shutil.which("system76-power"):
        res = subprocess.run(["system76-power", "profile", clean_profile], capture_output=True, text=True)
        if res.returncode == 0:
            ok, cur = get_power_profile()
            ver = f"Current profile: {cur}" if ok else "Verification unavailable"
            return ok and cur == clean_profile, f"Requested {clean_profile} via system76-power. {ver}.", ver
        return False, f"Failed to set profile with system76-power: {res.stderr.strip()}", ""

    if shutil.which("powerprofilesctl"):
        ctl_profile = "power-saver" if clean_profile == "battery" else clean_profile
        res = subprocess.run(["powerprofilesctl", "set", ctl_profile], capture_output=True, text=True)
        if res.returncode == 0:
            ok, cur = get_power_profile()
            ver = f"Current profile: {cur}" if ok else "Verification unavailable"
            return ok and cur == clean_profile, f"Requested {clean_profile} via powerprofilesctl. {ver}.", ver
        return False, f"Failed to set profile with powerprofilesctl: {res.stderr.strip()}", ""

    return False, "Power profile management tools (system76-power / powerprofilesctl) not available.", ""

def control_media(action="status", player=None):
    """Control one player. Pause and play are idempotent MPRIS operations."""
    if not shutil.which("playerctl"):
        return False, "playerctl is not installed on this system.", {}
    if action not in ("play_pause", "pause", "play", "next", "previous", "status"):
        return False, "Invalid media action.", {}
    if player and not re.fullmatch(r"[A-Za-z0-9_.-]+", player):
        return False, "Invalid media player name.", {}
    command = ["playerctl", "--player", player] if player else ["playerctl"]
    if action != "status":
        result = subprocess.run([*command, action.replace("_", "-")], capture_output=True, text=True, timeout=5)
        if result.returncode:
            error = result.stderr.strip() or f"playerctl exited with status {result.returncode}"
            return False, f"{player or 'Media player'}: {error}", {}
    result = subprocess.run([*command, "metadata", "--format", "{{artist}}\t{{title}}\t{{status}}"],
                            capture_output=True, text=True, timeout=5)
    artist = title = status = ""
    if result.returncode == 0:
        artist, title, status = (result.stdout.rstrip("\n").split("\t") + ["", "", ""])[:3]
    if not status:
        result = subprocess.run([*command, "status"], capture_output=True, text=True, timeout=5)
        status = result.stdout.strip() if result.returncode == 0 else ""
    if not status:
        return False, f"Could not read playback status for {player or 'the active player'}.", {}
    details = {"action": action, "artist": artist, "title": title, "status": status, "player": player}
    expected = {"pause": "Paused", "play": "Playing"}.get(action)
    if expected and status != expected and not (action == "pause" and status == "Stopped"):
        return False, f"{player or 'Media player'} reported {status}.", details
    track = f" {title} by {artist}" if title and artist else (f" {title or artist}" if title or artist else "")
    return True, f"{player or 'Media player'}: {status}.{track}", details

def get_network_status() -> Dict[str, Any]:
    """
    Reads network diagnostics: default route IP, local interface, and ping test latency (1 ping to 1.1.1.1).
    Returns structured metrics and human-readable report.
    """
    dev = "unknown"
    local_ip = "not assigned"
    gateway = "unknown"
    dns_latency_ms: Optional[float] = None
    connected = False

    # 1. Read default route and interface via ip route
    if shutil.which("ip"):
        try:
            res = subprocess.run(["ip", "-j", "route", "show", "default"], capture_output=True, text=True, timeout=2.0)
            if res.returncode == 0 and res.stdout.strip():
                routes = json.loads(res.stdout.strip())
                if routes and isinstance(routes, list):
                    r = routes[0]
                    dev = r.get("dev", dev)
                    gateway = r.get("gateway", gateway)
                    local_ip = r.get("prefsrc", local_ip)
            else:
                txt_res = subprocess.run(["ip", "route", "show", "default"], capture_output=True, text=True, timeout=2.0)
                if txt_res.returncode == 0:
                    m_dev = re.search(r"dev\s+(\S+)", txt_res.stdout)
                    m_gw = re.search(r"via\s+(\S+)", txt_res.stdout)
                    m_src = re.search(r"src\s+(\S+)", txt_res.stdout)
                    if m_dev:
                        dev = m_dev.group(1)
                    if m_gw:
                        gateway = m_gw.group(1)
                    if m_src:
                        local_ip = m_src.group(1)
        except Exception as e:
            logging.debug(f"ip route check error: {e}")

    # 2. Ping test (1 ping to 1.1.1.1)
    if shutil.which("ping"):
        try:
            ping_res = subprocess.run(["ping", "-c", "1", "-W", "2", "1.1.1.1"], capture_output=True, text=True, timeout=3.0)
            if ping_res.returncode == 0:
                connected = True
                m_lat = re.search(r"time=([\d\.]+)\s*ms", ping_res.stdout)
                if m_lat:
                    dns_latency_ms = float(m_lat.group(1))
                else:
                    m_avg = re.search(r"rtt min/avg/max/mdev = [\d\.]+/([\d\.]+)", ping_res.stdout)
                    if m_avg:
                        dns_latency_ms = float(m_avg.group(1))
        except Exception as e:
            logging.debug(f"ping check error: {e}")

    status_str = f"Connected ({dns_latency_ms} ms to 1.1.1.1)" if connected and dns_latency_ms is not None else ("Connected" if connected else "Disconnected / Unreachable")
    report = (
        f"🌐 Network Status:\n"
        f"  • Interface: {dev}\n"
        f"  • Local IP:  {local_ip}\n"
        f"  • Gateway:   {gateway}\n"
        f"  • Internet:  {status_str}"
    )

    return {
        "interface": dev,
        "ip": local_ip,
        "gateway": gateway,
        "dns_latency_ms": dns_latency_ms,
        "ping_ms": dns_latency_ms,
        "connected": connected,
        "report": report
    }

from .linux import APT_ARCHIVES as _APT_ARCHIVES, LinuxAdapter


def _scan_cache(root, dry_run, *, archives=False):
    """Inspect one pinned cache root and unlink eligible files by directory FD."""
    result = {"count": 0, "bytes": 0, "deleted": 0, "deleted_bytes": 0,
              "skipped": 0, "errors": [], "needs_privileges": False}
    root = os.path.abspath(root)
    home = os.path.abspath(os.path.expanduser("~"))
    user_cache = os.path.join(home, ".cache")
    system_archives = archives and root == _APT_ARCHIVES
    user_cache_subdirectory = root.startswith(user_cache + os.sep)
    temporary_subdirectory = root.startswith("/tmp/") and root not in (home, user_cache)
    if not (system_archives or user_cache_subdirectory or temporary_subdirectory):
        result["errors"].append(f"Refusing to clean outside a cache subdirectory: {root}")
        return result

    def scan(directory_fd, display, device, deletable):
        with os.scandir(directory_fd) as entries:
            for entry in entries:
                path = os.path.join(display, entry.name)
                try:
                    info = entry.stat(follow_symlinks=False)
                    if info.st_dev != device:
                        result["skipped"] += 1
                    elif stat.S_ISDIR(info.st_mode) and not archives:
                        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                        child_fd = os.open(entry.name, flags, dir_fd=directory_fd)
                        try:
                            actual = os.fstat(child_fd)
                            if (actual.st_dev, actual.st_ino) != (info.st_dev, info.st_ino):
                                raise OSError("Cache directory changed while opening it.")
                            scan(child_fd, path, device, deletable)
                        finally:
                            os.close(child_fd)
                    elif stat.S_ISREG(info.st_mode) and (not archives or entry.name.endswith(".deb")):
                        result["count"] += 1
                        result["bytes"] += info.st_size
                        if not dry_run:
                            if deletable:
                                os.unlink(entry.name, dir_fd=directory_fd)
                                result["deleted"] += 1
                                result["deleted_bytes"] += info.st_size
                            else:
                                result["skipped"] += 1
                                result["needs_privileges"] = True
                    else:
                        result["skipped"] += 1
                except OSError as error:
                    result["errors"].append(f"{path}: {error}")

    try:
        # _parent opens each component with O_NOFOLLOW. The final '.' makes
        # the cache root itself part of that descriptor-relative walk.
        with _parent(os.path.join(root, ".")) as directory_fd:
            try:
                info = os.fstat(directory_fd)
                if not system_archives and info.st_uid != os.geteuid():
                    raise PermissionError("Cache directory is not owned by the current user.")
                deletable = not archives or os.access(".", os.W_OK, dir_fd=directory_fd)
                scan(directory_fd, root, info.st_dev, deletable)
            except OSError as error:
                result["errors"].append(f"{root}: {error}")
    except FileNotFoundError:
        pass  # An uncreated cache has no entries to clean.
    except OSError as error:
        result["errors"].append(f"Cannot open cache directory {root} without following symlinks: {error}")
    return result


def clean_cache(dry_run: bool = True, thumbnail_dir: Optional[str] = None,
                apt_dir: Optional[str] = None) -> Tuple[bool, str, Dict[str, Any]]:
    """Clean the thumbnail cache and regular .deb files in APT's archives directory."""
    thumb, apt = LinuxAdapter().get_cache_targets()
    thumb_path = thumbnail_dir or str(thumb.path)
    apt_path = apt_dir or str(apt.path)
    thumbnails = _scan_cache(thumb_path, dry_run)
    archives = _scan_cache(apt_path, dry_run, archives=True)
    errors = thumbnails["errors"] + archives["errors"]
    deleted = thumbnails["deleted"] + archives["deleted"]
    skipped = thumbnails["skipped"] + archives["skipped"]
    failed = len(errors)
    total_bytes = thumbnails["bytes"] + archives["bytes"]
    freed_bytes = thumbnails["deleted_bytes"] + archives["deleted_bytes"]

    def mb(size):
        return round(size / (1024 * 1024), 2)

    if dry_run:
        title = "Cache Clean Preview (Dry Run)" if not failed else "Cache Clean Preview Incomplete"
    else:
        title = ("Cache Clean Completed" if not failed and not archives["needs_privileges"]
                 else "Cache Clean Incomplete")
    lines = [f"🧹 {title}:"]
    if dry_run:
        lines.extend([
            f"  • Thumbnails Cache: {mb(thumbnails['bytes'])} MB ({thumbnails['count']} files) in {thumb_path}",
            f"  • APT Package Cache: {mb(archives['bytes'])} MB ({archives['count']} files) in {apt_path}",
            f"  • Total Reclaimable: {mb(total_bytes)} MB",
        ])
    else:
        lines.extend([
            f"  • Thumbnails: Cleaned {mb(thumbnails['deleted_bytes'])} MB ({thumbnails['deleted']} files)",
            f"  • APT archives: Cleaned {mb(archives['deleted_bytes'])} MB ({archives['deleted']} files)",
            f"  • Total Freed: {mb(freed_bytes)} MB",
        ])
        if archives["needs_privileges"]:
            lines.append("  • Skipped APT archives because the directory is not writable.")
    lines.append(f"  • Deleted: {deleted}, skipped: {skipped}, failed: {failed}")
    lines.extend(f"  • {error}" for error in errors)
    return not failed and not archives["needs_privileges"], "\n".join(lines), {
        "dry_run": dry_run, "deleted": deleted, "skipped": skipped, "failed": failed, "errors": errors,
        "thumbnails_mb": mb(thumbnails["bytes"]), "thumbnails_count": thumbnails["count"],
        "apt_mb": mb(archives["bytes"]), "apt_count": archives["count"],
        "total_reclaimable_mb": mb(total_bytes), "total_reclaimable_bytes": total_bytes,
        "thumbnails_cleaned_mb": mb(thumbnails["deleted_bytes"]),
        "thumbnails_cleaned_count": thumbnails["deleted"],
        "apt_sudo_needed": archives["needs_privileges"], "total_freed_mb": mb(freed_bytes),
    }


def inspect_storage_usage(category: str = "apps", limit: int = 10) -> Tuple[bool, str, Dict[str, Any]]:
    """
    Scans and reports storage utilization for applications, user app data, packages, and caches.
    Returns (success, formatted_report, structured_details).
    """
    import glob
    import shlex

    def format_size(bytes_val: int) -> str:
        if bytes_val >= 1024 * 1024 * 1024:
            return f"{bytes_val / (1024**3):.1f} GB"
        elif bytes_val >= 1024 * 1024:
            return f"{bytes_val / (1024**2):.1f} MB"
        elif bytes_val >= 1024:
            return f"{bytes_val / 1024:.1f} KB"
        return f"{bytes_val} B"

    items: List[Dict[str, Any]] = []

    # 1. Flatpak Applications
    try:
        p = subprocess.run(["flatpak", "list", "--app", "--columns=name,application,size"],
                           capture_output=True, text=True, timeout=5)
        if p.returncode == 0:
            for line in p.stdout.strip().split("\n")[1:]:
                parts = [x.strip() for x in line.split("\t") if x.strip()]
                if len(parts) >= 3:
                    name, app_id, sz_str = parts[0], parts[1], parts[2]
                    sz_str_clean = sz_str.replace("\xa0", " ")
                    val_unit = sz_str_clean.split(" ")
                    if len(val_unit) == 2:
                        try:
                            v = float(val_unit[0])
                            u = val_unit[1].upper()
                            sz_bytes = int(v * (1024**3 if "GB" in u else (1024**2 if "MB" in u else 1024)))
                            items.append({
                                "name": name,
                                "identifier": app_id,
                                "type": "Flatpak App",
                                "bytes": sz_bytes,
                                "size_str": format_size(sz_bytes)
                            })
                        except (ValueError, TypeError):
                            pass
    except Exception:
        pass

    # 2. User App Data in ~/.var/app/* (Flatpak storage / caches)
    var_apps = glob.glob(os.path.expanduser("~/.var/app/*"))
    if var_apps:
        try:
            p = subprocess.run(["du", "-sb"] + var_apps, capture_output=True, text=True, timeout=5)
            if p.returncode == 0:
                for line in p.stdout.strip().split("\n"):
                    parts = line.split("\t")
                    if len(parts) == 2 and parts[0].isdigit():
                        sz_bytes = int(parts[0])
                        app_id = os.path.basename(parts[1])
                        if sz_bytes > 30 * 1024 * 1024:  # > 30 MB
                            name_map = {
                                "com.spotify.Client": "Spotify (Data)",
                                "com.usebottles.bottles": "Bottles (Data)",
                                "com.brave.Browser": "Brave (Flatpak Data)",
                            }
                            display_name = name_map.get(app_id, f"{app_id.split('.')[-1].capitalize()} (Data)")
                            items.append({
                                "name": display_name,
                                "identifier": app_id,
                                "type": "User App Data",
                                "bytes": sz_bytes,
                                "size_str": format_size(sz_bytes)
                            })
        except Exception:
            pass

    # 3. Native App Profiles in ~/.config and ~/.local/share
    profile_dirs = [
        (os.path.expanduser("~/.config/BraveSoftware"), "Brave Browser (Profile)", "App Profile"),
        (os.path.expanduser("~/.local/share/waydroid"), "Waydroid (Android OS)", "User Runtime"),
        (os.path.expanduser("~/.local/share/flatpak"), "Flatpak Runtimes", "App Runtimes"),
        (os.path.expanduser("~/.config/google-chrome"), "Google Chrome (Profile)", "App Profile"),
        (os.path.expanduser("~/.config/Antigravity"), "Antigravity / IDE", "App Profile"),
        (os.path.expanduser("~/.config/Codex"), "Codex", "App Profile"),
    ]
    for pdir, pname, ptype in profile_dirs:
        if os.path.exists(pdir):
            try:
                p = subprocess.run(["du", "-sb", pdir], capture_output=True, text=True, timeout=3)
                if p.returncode == 0:
                    sz_bytes = int(p.stdout.split("\t")[0])
                    if sz_bytes > 50 * 1024 * 1024:
                        items.append({
                            "name": pname,
                            "identifier": os.path.basename(pdir),
                            "type": ptype,
                            "bytes": sz_bytes,
                            "size_str": format_size(sz_bytes)
                        })
            except Exception:
                pass

    # 4. APT / Debian Installed Packages
    try:
        cmd = shlex.split("dpkg-query -W -f='${Installed-Size}\t${Package}\n'")
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        if p.returncode == 0:
            for line in p.stdout.strip().split("\n"):
                parts = line.split("\t")
                if len(parts) == 2 and parts[0].isdigit():
                    sz_kb = int(parts[0])
                    pkg = parts[1]
                    sz_bytes = sz_kb * 1024
                    is_system = pkg.startswith(("lib", "linux-", "fonts-", "mesa-"))
                    if sz_bytes >= 100 * 1024 * 1024:
                        if category == "apps" and is_system:
                            continue
                        name_map = {
                            "chatgpt": "ChatGPT Desktop",
                            "code": "VS Code (code)",
                            "brave-browser": "Brave Browser",
                            "google-chrome-stable": "Google Chrome",
                            "firefox": "Firefox",
                            "thunderbird": "Thunderbird",
                            "libreoffice-core": "LibreOffice",
                        }
                        display_name = name_map.get(pkg, pkg)
                        items.append({
                            "name": display_name,
                            "identifier": pkg,
                            "type": "Debian App" if not is_system else "System Package",
                            "bytes": sz_bytes,
                            "size_str": format_size(sz_bytes)
                        })
    except Exception:
        pass

    # Sort descending by size
    items.sort(key=lambda x: x["bytes"], reverse=True)

    # Disk usage summary
    total, used, free = shutil.disk_usage("/")
    used_pct = (used / total) * 100

    report_lines = [
        "💾 Application Storage Usage:\n",
        f"  Rank  Application / Component        Type               Storage Used",
        f"  ─────────────────────────────────────────────────────────────────"
    ]
    top_items = items[:limit]
    for idx, it in enumerate(top_items, 1):
        report_lines.append(f"  {idx:2d}.   {it['name']:<29}  {it['type']:<17}  {it['size_str']:>10}")

    report_lines.append(f"  ─────────────────────────────────────────────────────────────────")
    report_lines.append(f"  • System Disk: {free / (1024**3):.1f} GB free of {total / (1024**3):.1f} GB ({used_pct:.1f}% used)")
    if top_items:
        report_lines.append(f"  • Top Consumer: {top_items[0]['name']} ({top_items[0]['size_str']})")
    report_lines.append(f"  • Hint: Run 'clean cache' to reclaim cache, or 'open baobab' for GUI disk map.")

    details = {
        "category": category,
        "items": top_items,
        "disk_total_gb": round(total / (1024**3), 1),
        "disk_free_gb": round(free / (1024**3), 1),
        "disk_used_pct": round(used_pct, 1)
    }
    return True, "\n".join(report_lines), details

