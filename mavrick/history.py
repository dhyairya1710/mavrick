#!/usr/bin/env python3
"""
MAVRICK History, Activity Log & Context Management
Maintains secure state files with strict permissions (0700 directory, 0600 files).
Keeps audit logs separate from conversational request history.
Redacts all sensitive tokens and credentials.
"""

import os
import json
import time
import logging
import tempfile
from typing import List, Dict, Any, Optional

from .config import (
    HISTORY_FILE,
    AUDIT_LOG_FILE,
    CONTEXT_FILE,
    configured_api_keys,
    ensure_secure_storage
)
from .policy import scrub_secrets
from .platform import get_platform

_SECRET_FIELDS = {
    "apikey", "auth", "authorization", "token", "accesstoken", "refreshtoken",
    "password", "passwd", "secret", "clientsecret", "credentials",
}


def redact_secrets(value, known: tuple[str, ...] | None = None):
    """Return a redacted copy of JSON-compatible data, including nested values."""
    if known is None:
        known = configured_api_keys()
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if isinstance(key, str):
                field = key.lower().replace("_", "").replace("-", "").replace(" ", "")
                result[scrub_secrets(key, known)] = (
                    "[REDACTED_SECRET]" if field in _SECRET_FIELDS else redact_secrets(item, known)
                )
            else:
                result[key] = redact_secrets(item)
        return result
    if isinstance(value, list):
        return [redact_secrets(item, known) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_secrets(item, known) for item in value)
    return scrub_secrets(value, known) if isinstance(value, str) else value


def append_history(query: str, action_type: Optional[str] = None,
                   status: str = "completed", summary: str = "", job_id: Optional[str] = None) -> None:
    """
    Appends a user request to conversational history with secret scrubbing.
    Maintains a maximum history buffer.
    """
    if not query.strip():
        return
    ensure_secure_storage()
    clean_query, clean_summary = redact_secrets((query.strip(), summary.strip()))

    entry = {
        "timestamp": time.time(),
        "date_str": time.strftime("%Y-%m-%d %H:%M:%S"),
        "query": clean_query,
        "action": action_type,
        "status": status,
        "summary": clean_summary
    }

    if job_id:
        entry["job_id"] = job_id
    with _locked_state(HISTORY_FILE):
        history = load_history()
        if job_id or not history or history[-1].get("query") != clean_query:
            history.append(entry)
            _write_secure_json(HISTORY_FILE, history[-200:])


def load_history() -> List[Dict[str, Any]]:
    """Loads request history, normalizing any legacy string entries to dicts."""
    ensure_secure_storage()
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as fp:
                data = json.load(fp)
                if isinstance(data, list):
                    normalized = []
                    for item in data:
                        if isinstance(item, dict):
                            normalized.append(item)
                        elif isinstance(item, str):
                            normalized.append({
                                "timestamp": 0.0,
                                "date_str": "legacy",
                                "query": item,
                                "action": None,
                                "status": "completed",
                                "summary": item
                            })
                    return redact_secrets(normalized)
        except Exception as e:
            logging.warning(f"Error reading history: {e}")
    return []

def clear_history_and_context() -> bool:
    """
    Clears all history and context files securely.
    """
    ensure_secure_storage()
    cleared = True
    for f in [HISTORY_FILE, CONTEXT_FILE]:
        with _locked_state(f):
            try:
                os.unlink(f)
            except FileNotFoundError:
                pass
            except OSError as e:
                logging.error(f"Failed to remove {f}: {e}")
                cleared = False
    return cleared

def search_history(keyword: str) -> List[Dict[str, Any]]:
    """Searches history for matching queries or actions."""
    clean_kw = keyword.lower().strip()
    history = load_history()
    if not clean_kw:
        return list(reversed(history))
    matches = [
        item for item in history
        if clean_kw in item.get("query", "").lower() or clean_kw in str(item.get("action", "")).lower()
    ]
    return list(reversed(matches))

def append_audit_log(entry: Dict[str, Any]) -> None:
    """
    Appends a record to the separate audit log with mode 0600.
    """
    ensure_secure_storage()
    entry["timestamp"] = time.time()
    entry["date_str"] = time.strftime("%Y-%m-%d %H:%M:%S")

    sanitized_entry = redact_secrets(entry)

    try:
        with open(AUDIT_LOG_FILE, "a", encoding="utf-8") as fp:
            fp.write(json.dumps(sanitized_entry) + "\n")
        # Ensure 0600
        get_platform().restrict_permissions(AUDIT_LOG_FILE)
    except Exception as e:
        logging.error(f"Failed to write audit log: {e}")

def load_context() -> Dict[str, Any]:
    """Loads anaphoric context state (last action, targets)."""
    ensure_secure_storage()
    if os.path.exists(CONTEXT_FILE):
        try:
            with open(CONTEXT_FILE, "r", encoding="utf-8") as fp:
                return json.load(fp)
        except Exception:
            pass
    return {}

def save_context(data: Dict[str, Any]) -> None:
    """Saves session context securely."""
    ensure_secure_storage()
    with _locked_state(CONTEXT_FILE):
        ctx = load_context()
        ctx.update(data)
        ctx["last_updated"] = time.time()
        _write_secure_json(CONTEXT_FILE, ctx)


def _locked_state(path):
    return get_platform().lock_file(path + ".lock")


def _write_secure_json(path, data):
    fd, temp = tempfile.mkstemp(dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(redact_secrets(data), handle, indent=2)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def write_undo_journal(path, batches, pending=None) -> None:
    """Commit operational Undo receipts, including exact paths and inode signatures."""
    directory = os.path.dirname(path)
    fd, temp = tempfile.mkstemp(dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"batches": batches, "pending": pending}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        get_platform().sync_directory(directory)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
