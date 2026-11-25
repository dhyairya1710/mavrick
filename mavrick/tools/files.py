#!/usr/bin/env python3
"""
MAVRICK File Tools
Safe directory listing, controlled file organization, path verification, and undo.
Never modifies system files. All moves record reversible metadata.
"""

import copy
import os
import re
import json
import stat
import uuid
from dataclasses import dataclass, replace
from typing import Dict, Any, List, Optional, Tuple

try:
    from ..platform import get_platform
    from ..config import UNDO_STATE_FILE, ensure_secure_storage
    from ..history import write_undo_journal
    from ..policy import is_safe_user_path
except (ImportError, ValueError):
    from mavrick.platform import get_platform
    from mavrick.config import UNDO_STATE_FILE, ensure_secure_storage
    from mavrick.history import write_undo_journal
    from mavrick.policy import is_safe_user_path

DOWNLOAD_CATEGORIES = {
    "Images": [".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp"],
    "Documents": [".pdf", ".docx", ".doc", ".txt", ".md", ".csv", ".xlsx", ".pptx", ".odt"],
    "Archives": [".zip", ".tar", ".gz", ".deb", ".7z", ".iso", ".bz2", ".xz"],
    "Media": [".mp4", ".mkv", ".webm", ".mp3", ".wav", ".ogg", ".flac"],
    "Code": [".py", ".rs", ".js", ".ts", ".html", ".css", ".json", ".sh", ".c", ".cpp"]
}

def _is_cancelled(cancel_token: Optional[Any]) -> bool:
    """Helper to evaluate cancellation token or callable."""
    if cancel_token is None:
        return False
    if callable(cancel_token):
        return bool(cancel_token())
    if hasattr(cancel_token, "is_set"):
        return cancel_token.is_set()
    return bool(cancel_token)

def list_files(path: str = "~/Downloads", max_depth: int = 1, cancel_token: Optional[Any] = None) -> Tuple[bool, str, Dict[str, Any]]:
    """
    Safely lists directory contents with file sizes and counts.
    Strictly constrained to user home directory with bounded traversal.
    """
    if _is_cancelled(cancel_token):
        return False, "Listing cancelled by user.", {"cancelled": True}

    safe, err, canonical_path = is_safe_user_path(path, must_exist=True)
    if not safe:
        return False, err, {}

    if not os.path.isdir(canonical_path):
        return False, f"Path '{path}' is not a directory.", {}

    results: List[Dict[str, Any]] = []
    total_size = 0
    total_files = 0
    total_dirs = 0

    try:
        flags = getattr(os, "O_RDONLY", 0) | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        can_use_dir_fd = os.name == "posix" and hasattr(os, "O_DIRECTORY") and hasattr(os, "O_NOFOLLOW")

        def record_dir_group(prefix: str, subdir_names: list[str], file_rows: list[dict[str, Any]]):
            results.append({
                "folder": prefix or os.path.basename(canonical_path),
                "subfolders": subdir_names,
                "files": file_rows,
            })

        def process_entries(it, current_depth: int, prefix: str):
            nonlocal total_size, total_files, total_dirs
            subdirs = []
            files = []
            for entry in it:
                if _is_cancelled(cancel_token):
                    return None, None
                if entry.name.startswith("."):
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISLNK(info.st_mode) or (
                    getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                ):
                    continue
                if stat.S_ISDIR(info.st_mode):
                    subdirs.append((entry.name, (info.st_dev, info.st_ino)))
                    total_dirs += 1
                elif stat.S_ISREG(info.st_mode):
                    total_files += 1
                    total_size += info.st_size
                    files.append((entry.name, info.st_size))

            subdirs.sort(key=lambda item: item[0].lower())
            files.sort(key=lambda item: item[0].lower())
            file_rows = [
                {
                    "name": name,
                    "size_bytes": sz,
                    "size_display": f"{sz/(1024*1024):.1f} MB" if sz >= 1048576 else f"{sz/1024:.1f} KB",
                }
                for name, sz in files
            ]
            record_dir_group(prefix, [name for name, _ in subdirs], file_rows)
            return subdirs, current_depth

        def walk_dir_fd(dir_fd: int, prefix: str, current_depth: int):
            if _is_cancelled(cancel_token) or current_depth > max_depth:
                return
            with os.scandir(dir_fd) as it:
                subdirs, _ = process_entries(it, current_depth, prefix)
            if subdirs is None or current_depth >= max_depth:
                return
            for name, expected_id in subdirs:
                if _is_cancelled(cancel_token):
                    return
                try:
                    child_fd = os.open(name, flags, dir_fd=dir_fd)
                except OSError:
                    continue
                try:
                    child_info = os.fstat(child_fd)
                    if not stat.S_ISDIR(child_info.st_mode) or (child_info.st_dev, child_info.st_ino) != expected_id:
                        continue
                    child_prefix = os.path.join(prefix, name) if prefix else name
                    walk_dir_fd(child_fd, child_prefix, current_depth + 1)
                finally:
                    os.close(child_fd)

        def walk_dir_lstat(current_dir: str, prefix: str, current_depth: int):
            if _is_cancelled(cancel_token) or current_depth > max_depth:
                return
            with os.scandir(current_dir) as it:
                subdirs, _ = process_entries(it, current_depth, prefix)
            if subdirs is None or current_depth >= max_depth:
                return
            for name, expected_id in subdirs:
                if _is_cancelled(cancel_token):
                    return
                child_path = os.path.join(current_dir, name)
                try:
                    child_info = os.lstat(child_path)
                except OSError:
                    continue
                if (
                    stat.S_ISLNK(child_info.st_mode)
                    or not stat.S_ISDIR(child_info.st_mode)
                    or (child_info.st_dev, child_info.st_ino) != expected_id
                    or (getattr(child_info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
                ):
                    continue
                child_prefix = os.path.join(prefix, name) if prefix else name
                walk_dir_lstat(child_path, child_prefix, current_depth + 1)

        if can_use_dir_fd:
            root_fd = os.open(canonical_path, flags)
            try:
                walk_dir_fd(root_fd, "", 1)
            finally:
                os.close(root_fd)
        else:
            root_info = os.lstat(canonical_path)
            if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
                return False, f"Path '{path}' is not a directory.", {}
            walk_dir_lstat(canonical_path, "", 1)

        if _is_cancelled(cancel_token):
            return False, "Listing cancelled by user.", {"cancelled": True}

        # Format user-readable tree report
        report_lines = [f"📂 Directory: {canonical_path} ({total_files} files, {total_dirs} folders)"]
        for group in results:
            folder_title = group["folder"]
            report_lines.append(f"\n📁 {folder_title}/")
            if not group["files"] and not group["subfolders"]:
                report_lines.append("   (empty)")
            for d in group["subfolders"]:
                report_lines.append(f"   📁 {d}/")
            for f in group["files"]:
                report_lines.append(f"   📄 {f['name']:<34} ({f['size_display']})")

        return True, "\n".join(report_lines), {
            "path": canonical_path,
            "total_files": total_files,
            "total_dirs": total_dirs,
            "total_size_mb": round(total_size / (1024*1024), 2)
        }
    except Exception as e:
        return False, f"Failed to list directory: {e}", {}

@dataclass(frozen=True)
class FileRow:
    source: str
    destination: str
    source_state: tuple
    destination_state: tuple
    problem: str = ""
    changed: bool = False
    source_valid: bool = True
    included: bool = True
    workflow_step: int | None = None


@dataclass(frozen=True)
class FileReview:
    action: str
    rows: tuple[FileRow, ...]
    undo_id: str = ""
    skipped: int = 0
    workflow_batch: bool = False


def _safe_path(raw):
    # Keep the final component literal, including a possible dangling symlink.
    path = get_platform().normalize_file_path(raw)
    parent = os.path.dirname(path)
    safe, error, canonical = is_safe_user_path(parent)
    if (get_platform().name == "linux" and parent == "/tmp") or path == str(get_platform().home.resolve()):
        safe = True  # Allow immediate children of /tmp and the home directory itself.
    if not safe or canonical != parent:
        raise ValueError(error or f"Symlinked directory in {path}")
    safe, error, _ = is_safe_user_path(path) if not os.path.lexists(path) else (True, "", "")
    if not safe:
        raise ValueError(error)
    return path


def _parent(path, create=False, created=None):
    adapter = get_platform()
    if not adapter.capabilities.atomic_moves:
        raise adapter.unsupported("Descriptor-pinned file operations")
    return adapter.open_parent(path, create, created)


def _identity(info):
    return info.st_dev, info.st_ino, info.st_mode


def _signature(info):
    return (*_identity(info), info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _snapshot(path):
    """Record existing ancestor identities and the literal entry, if present."""
    chain = []
    for parent in reversed(_path_parts(os.path.dirname(path))):
        try:
            info = os.lstat(parent)
        except FileNotFoundError:
            return tuple(chain), None
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"Destination parent is not a directory: {parent}")
        chain.append(_identity(info))
    try:
        return tuple(chain), _signature(os.lstat(path))
    except FileNotFoundError:
        return tuple(chain), None


def _path_parts(path):
    parents = []
    while path != os.path.dirname(path):
        parents.append(path)
        path = os.path.dirname(path)
    return parents


def _row(source, destination, expected=None):
    try:
        source = _safe_path(source)
        if source == os.path.realpath(os.path.expanduser("~")):
            raise ValueError("The home directory cannot be moved.")
        src = _snapshot(source)
        if src[1] is None or not (stat.S_ISREG(src[1][2]) or stat.S_ISDIR(src[1][2])):
            raise ValueError(f"Source is missing or is not a regular file or directory: {source}")
        if expected and tuple(expected[:2]) != src[1][:2]:
            raise ValueError(f"A different entry is at the moved file's path: {source}")
    except (OSError, ValueError) as error:
        return FileRow(source, destination, (), (), str(error), source_valid=False)
    dst = ()
    try:
        destination = _safe_path(destination)
        dst = _snapshot(destination)
        if dst[1] is not None:
            raise FileExistsError(f"Destination already exists: {destination}")
    except (OSError, ValueError) as error:
        return FileRow(source, destination, src, dst, str(error))
    return FileRow(source, destination, src, dst)


def display_path(path):
    """Render Unix paths with controls or undecodable bytes as escaped literals."""
    if not path.isprintable():
        return repr(path)
    return path


def rename_review_row(row, name):
    if not name or name in (".", "..") or "/" in name or "\x00" in name:
        raise ValueError("Enter a single file name for the reviewed destination folder.")
    if not row.source_valid:
        raise ValueError(row.problem)
    destination = os.path.join(os.path.dirname(row.destination), name)
    updated = _row(row.source, destination)
    if updated.source_state != row.source_state:
        raise ValueError("The source changed. Apply or reopen the review to refresh it.")
    return replace(updated, changed=row.changed, included=row.included, workflow_step=row.workflow_step)


def _downloads_path():
    return get_platform().downloads_dir()


def review_files(action, params, cancel_token=None):
    if action == "undo":
        with _undo_lock():
            batches = _read_undo()
            batch = next((item for item in reversed(batches)
                          if item["moves"] and (not params.get("undo_id") or item["id"] == params["undo_id"])), None)
            if batch is None:
                raise ValueError("No recorded moves available to undo.")
            grouped = batch.get("kind") == "workflow"
            moves = reversed(batch["moves"]) if grouped else batch["moves"]
            rows = tuple(replace(_row(move["current"], move["original"], move.get("signature")),
                                 workflow_step=move.get("step")) for move in moves)
            return FileReview(action, rows, batch["id"], workflow_batch=grouped)
    if action == "organize_downloads":
        root = params.get("target_dir", "~/Downloads")
        root = _safe_path(_downloads_path() if root == "~/Downloads" else root)
        adapter = get_platform()
        if adapter.capabilities.atomic_moves:
            scan = _parent(os.path.join(root, "."))
        else:
            from contextlib import nullcontext
            scan = nullcontext(root)
        with scan as location, os.scandir(location) as entries:
            names = []
            for entry in entries:
                if _is_cancelled(cancel_token):
                    break
                try:
                    if not entry.name.startswith(".") and entry.is_file(follow_symlinks=False):
                        names.append(entry.name)
                except FileNotFoundError:
                    continue
        pairs = []
        for name in sorted(names, key=str.casefold):
            ext = os.path.splitext(name)[1].lower()
            folder = next((cat for cat, extensions in DOWNLOAD_CATEGORIES.items()
                           if ext in extensions), "Other_Files")
            pairs.append((os.path.join(root, name), os.path.join(root, folder, name)))
    else:
        sources = list(dict.fromkeys(os.path.abspath(os.path.expanduser(path)) for path in params["sources"]))
        destination = _safe_path(params["destination"])
        # Preserve the single-source filename shorthand, including when that name exists.
        is_file = (len(sources) == 1 and not os.path.isdir(destination)
                   and bool(os.path.lexists(destination) or os.path.splitext(destination)[1]))
        pairs = [(src, destination if is_file else os.path.join(destination, os.path.basename(src)))
                 for src in sources]
    return FileReview(action, tuple(_row(src, dst) for src, dst in pairs))


def _undo_lock():
    ensure_secure_storage()
    return get_platform().lock_file(UNDO_STATE_FILE + ".lock")


class IndeterminateMoveError(OSError):
    """Raised when a rename succeeded or partially completed before parent fsync durability failed."""

    def __init__(self, message: str, *, source: str, destination: str, signature=None):
        super().__init__(message)
        self.source = source
        self.destination = destination
        self.signature = signature


def _read_undo():
    try:
        with open(UNDO_STATE_FILE, encoding="utf-8") as handle:
            state = json.load(handle)
    except FileNotFoundError:
        return []
    if "batches" in state:
        batches = state["batches"]
        pending = state.get("pending")
        if pending:
            # Recover the receipt if a process exited between rename and the
            # final Undo write. Only accept the entry with the recorded inode.
            info = _lstat_if_present(pending["destination"])
            source = _lstat_if_present(pending["source"])
            identity = tuple(pending["identity"])
            if info and _identity(info) == identity and (source is None or _identity(source) != identity):
                _record_move(batches, pending["id"], pending["undo"], pending["source"],
                             pending["destination"], _signature(info), pending.get("step"))
                _write_undo(batches)
            elif pending.get("indeterminate") or pending.get("state") == "indeterminate":
                # Keep recovery metadata for both paths intact when durability
                # could not be verified across source and destination.
                return batches
            else:
                _write_undo(batches)
        return batches
    if state.get("moves"):
        return [{"id": "legacy", "moves": state["moves"]}]
    return []


def _lstat_if_present(path):
    try:
        return os.lstat(_safe_path(path))
    except FileNotFoundError:
        return None


def _write_undo(batches, pending=None):
    write_undo_journal(UNDO_STATE_FILE, batches, pending)


def begin_workflow_batch(batch_id: str) -> None:
    with _undo_lock():
        batches = _read_undo()
        empty = [batch["id"] for batch in batches if batch.get("kind") == "workflow" and not batch["moves"]]
        expired = set(empty[:-127])
        batches = [batch for batch in batches if batch["id"] not in expired]
        batches.append({"id": batch_id, "kind": "workflow", "moves": []})
        _write_undo(batches)


def _rename_no_replace(source_fd, source, dest_fd, destination):
    get_platform().rename_no_replace(source_fd, source, dest_fd, destination)


def _record_move(batches, batch_id, undoing, source, destination, signature, step=None):
    batch = next((item for item in batches if item["id"] == batch_id), None)
    if undoing:
        batch["moves"] = [move for move in batch["moves"] if move["current"] != source]
        if not batch["moves"] and batch.get("kind") != "workflow":
            batches.remove(batch)
    else:
        if batch is None:
            batch = {"id": batch_id, "moves": []}
            batches.append(batch)
        move = {"original": source, "current": destination, "signature": signature}
        if step is not None:
            move["step"] = step
        batch["moves"].append(move)


def _move_row(row, created):
    source_name = os.path.basename(row.source)
    dest_name = os.path.basename(row.destination)
    # Recheck parents and source immediately before the no-replace operation.
    with _parent(row.source) as src_fd, _parent(row.destination, create=True, created=created) as dst_fd:
        current = _snapshot(row.destination)
        live = replace(row, destination_state=current)
        if (not _matches_created_destination(row, live, created)
                or current[0][-1] != _identity(os.fstat(dst_fd))):
            raise ValueError("Destination folder or entry changed")
        if _identity(os.fstat(src_fd)) != row.source_state[0][-1]:
            raise ValueError("Source folder changed")
        # Keep the reviewed inode alive across the rename to prevent reuse if
        # another process unlinks it between the final check and the syscall.
        entry_fd = get_platform().pin_entry(src_fd, source_name)
        try:
            if _signature(os.fstat(entry_fd)) != row.source_state[1]:
                raise ValueError("Source changed")
            return _move_pinned_entry(row, src_fd, dst_fd, source_name, dest_name)
        finally:
            os.close(entry_fd)


def _move_pinned_entry(row, src_fd, dst_fd, source_name, dest_name):
    _rename_no_replace(src_fd, source_name, dst_fd, dest_name)
    try:
        moved = _signature(os.stat(dest_name, dir_fd=dst_fd, follow_symlinks=False))
    except FileNotFoundError as error:
        raise ValueError("The destination entry disappeared during rename. Check both paths.") from error
    if moved[:2] != row.source_state[1][:2]:
        # A writer replaced the source after stat and before rename. Return
        # the observed entry only if it remains in place and nothing now
        # occupies the source name. Never journal it as the reviewed file.
        try:
            current = os.stat(dest_name, dir_fd=dst_fd, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != moved[:2]:
                raise ValueError("The destination entry also changed.")
            _rename_no_replace(dst_fd, dest_name, src_fd, source_name)
            restored = os.stat(source_name, dir_fd=src_fd, follow_symlinks=False)
            if (restored.st_dev, restored.st_ino) != moved[:2]:
                raise ValueError("The returned entry could not be verified.")
        except (OSError, ValueError) as error:
            raise ValueError(f"Source identity changed during rename. Check both paths. "
                             f"Could not return the replacement: {error}") from error
        raise ValueError("Source identity changed during rename. The replacement was returned to the source path.")
    adapter = get_platform()
    try:
        adapter.sync_directory_fd(src_fd)
        src_dir = os.fstat(src_fd)
        dst_dir = os.fstat(dst_fd)
        if (dst_dir.st_dev, dst_dir.st_ino) != (src_dir.st_dev, src_dir.st_ino):
            adapter.sync_directory_fd(dst_fd)
    except OSError as error:
        raise IndeterminateMoveError(
            f"Directory fsync failed after rename; inspect both {row.source} and {row.destination}: {error}",
            source=row.source,
            destination=row.destination,
            signature=moved,
        ) from error
    return moved


def apply_file_review(review, selected, cancel_token=None, progress=None, rereview=True,
                      *, batch_id=None, workflow_step=None, created=None):
    """Apply only the reviewed rows. A stale selection returns to review before moving."""
    adapter = get_platform()
    if not adapter.capabilities.atomic_moves:
        return False, str(adapter.unsupported("Verified file move and Undo apply")), {}
    selected = set(selected)
    rows = review.rows
    undoing = review.action == "undo"
    workflow_apply = not undoing and batch_id is not None
    batch_id = review.undo_id if undoing else batch_id or uuid.uuid4().hex
    errors = []
    moved = []
    skipped = review.skipped + len(rows) - len(selected)
    changed_rows = list(rows)
    created = {} if created is None else created
    grouped = undoing and review.workflow_batch

    def failure(row, problem):
        step = f"Step {row.workflow_step} · " if row.workflow_step is not None else ""
        return f"{step}{display_path(row.source)} → {display_path(row.destination)}: {display_path(str(problem))}"

    if grouped:
        blocked = [row for i, row in enumerate(rows) if i not in selected and row.problem and row.included]
        errors.extend(failure(row, row.problem) for row in blocked)
        skipped -= len(blocked)
    with _undo_lock():
        batches = _read_undo()
        batch = next((item for item in batches if item["id"] == batch_id), None)
        if undoing and batch is None:
            return False, "These Undo records are no longer available.", {}
        records = {move["current"]: move for move in batch["moves"]} if undoing else {}
        destinations = {}
        for index in sorted(selected):
            row = rows[index]
            expected = records.get(row.source, {}).get("signature")
            fresh = replace(_row(row.source, row.destination, expected), workflow_step=row.workflow_step)
            if undoing and row.source not in records:
                fresh = replace(fresh, source_valid=False, problem="This Undo record was already restored.")
            if row.destination in destinations:
                first = destinations[row.destination]
                changed_rows[first] = replace(changed_rows[first], problem="Another selected row has this destination.", changed=True)
                fresh = replace(fresh, problem="Another selected row has this destination.")
            destinations[row.destination] = index
            changed_destination = (fresh.destination_state != row.destination_state and
                                   not _matches_created_destination(row, fresh, created))
            if (fresh.source_state != row.source_state or changed_destination or fresh.problem != row.problem):
                changed_rows[index] = replace(fresh, changed=True, included=True)
        def recheck(indices, carry_skipped):
            updated = tuple(replace(changed_rows[i], included=i in indices) for i in range(len(rows)))
            return replace(review, rows=updated, skipped=carry_skipped)

        if rereview and not grouped and not _is_cancelled(cancel_token) and any(
            changed_rows[i] != rows[i] or rows[i].problem for i in selected
        ):
            return False, "Review the highlighted rows before applying.", {
                "review": recheck(selected, review.skipped), "review_required": True
            }

        retry = set()
        attempted = 0
        indeterminate_paths = []
        for index in sorted(selected):
            if _is_cancelled(cancel_token):
                break
            row = rows[index]
            attempted += 1
            if progress:
                step = f"Step {row.workflow_step} · " if row.workflow_step is not None else ""
                progress(f"{step}{'Restoring' if undoing else 'Moving'} {display_path(os.path.basename(row.source))}")
            pending = False
            receipt = None
            try:
                if changed_rows[index].problem:
                    raise ValueError(changed_rows[index].problem)
                if grouped and changed_rows[index] != row:
                    raise ValueError("Entry changed since the batch preview.")
                live = _row(row.source, row.destination)
                if (live.problem or live.source_state != row.source_state or
                    live.destination_state != row.destination_state and
                    not _matches_created_destination(row, live, created)):
                    raise ValueError(live.problem or "Source or destination changed")
                receipt = {"id": batch_id, "undo": undoing, "source": row.source,
                           "destination": row.destination, "identity": row.source_state[1][:3]}
                if workflow_step is not None:
                    receipt["step"] = workflow_step
                _write_undo(batches, receipt)
                pending = True
                signature = _move_row(row, created)
            except IndeterminateMoveError as error:
                recovery = {
                    **(receipt or {}),
                    "state": "indeterminate",
                    "indeterminate": True,
                    "error": str(error),
                    "check_paths": [error.source, error.destination],
                    "destination_signature": list(error.signature) if error.signature else None,
                }
                try:
                    _write_undo(batches, recovery)
                except OSError:
                    pass
                indeterminate_paths.append({"source": error.source, "destination": error.destination})
                errors.append(failure(row, error))
                break
            except (OSError, ValueError) as error:
                if pending:
                    batches = _read_undo()
                errors.append(failure(row, error))
                if rereview and (isinstance(error, (ValueError, FileExistsError, FileNotFoundError))):
                    retry.add(index)
                    changed_rows[index] = replace(
                        _row(row.source, row.destination, records.get(row.source, {}).get("signature")),
                        changed=True, workflow_step=row.workflow_step)
                if workflow_apply:
                    break
                continue

            moved.append({"original": row.source, "current": row.destination, "signature": signature})
            next_batches = copy.deepcopy(batches)
            _record_move(next_batches, batch_id, undoing, row.source, row.destination, signature, workflow_step)
            try:
                _write_undo(next_batches)
                batches = next_batches
            except OSError as error:
                recovery = {
                    **(receipt or {}),
                    "state": "indeterminate",
                    "indeterminate": True,
                    "error": f"Could not finalize Undo records cleanly: {error}",
                    "check_paths": [row.source, row.destination],
                    "destination_signature": list(signature),
                }
                try:
                    _write_undo(batches, recovery)
                except OSError:
                    pass
                indeterminate_paths.append({"source": row.source, "destination": row.destination})
                errors.append(f"Could not finalize Undo records: {error}")
                break

        stopped = _is_cancelled(cancel_token)
        skipped += len(selected) - attempted
        verb = "restored" if undoing else "moved"
        summary = f"{len(moved)} {verb}, {skipped} skipped, {len(errors)} failed."
        if stopped:
            summary = "Stopped. " + summary
        details = {"cancelled": stopped, "executed_count": len(moved), "skipped_count": skipped,
                   "failed_count": len(errors), "errors": errors, "moves": moved,
                   "restored_count": len(moved) if undoing else 0,
                   "undo_available": any(item["id"] == batch_id and item["moves"] for item in batches),
                   "undo_id": batch_id}
        if indeterminate_paths:
            details["indeterminate"] = indeterminate_paths
        if retry and not stopped:
            remaining = tuple(changed_rows[i] for i in sorted(retry))
            details["review"] = replace(review, rows=remaining, skipped=0)
        return not stopped and not errors, "\n".join([summary, *errors]), details


def _matches_created_destination(row, live, created):
    if not row.destination_state or not live.destination_state:
        return False
    old_chain, old_entry = row.destination_state
    new_chain, new_entry = live.destination_state
    paths = list(reversed(_path_parts(os.path.dirname(row.destination))))
    return (old_entry == new_entry and new_chain[:len(old_chain)] == old_chain and
            all(created.get(path) == identity for path, identity in
                zip(paths[len(old_chain):], new_chain[len(old_chain):])))


def _preview_result(review, cancel_token):
    cancelled = _is_cancelled(cancel_token)
    rows = [{"source": row.source, "destination": row.destination, **({"problem": row.problem} if row.problem else {})}
            for row in review.rows]
    lines = [f"{len(rows)} file(s) to review."]
    lines.extend(f"{display_path(row['source'])} → {display_path(row['destination'])}" for row in rows)
    return not cancelled, "\n".join(lines), {
        "dry_run": True, "planned_count": len(rows), "preview": rows,
        "review": review, "cancelled": cancelled
    }


def organize_downloads(target_dir="~/Downloads", dry_run=True, cancel_token=None):
    try:
        return _preview_result(review_files("organize_downloads", {"target_dir": target_dir}, cancel_token), cancel_token)
    except (OSError, ValueError) as error:
        return False, str(error), {}


def move_files(sources, destination, cancel_token=None):
    try:
        review = review_files("move_files", {"sources": sources, "destination": destination})
        return apply_file_review(review, range(len(review.rows)), cancel_token, rereview=False)
    except (OSError, ValueError) as error:
        return False, str(error), {}


def undo_last_action(cancel_token=None):
    try:
        review = review_files("undo", {})
        return apply_file_review(review, range(len(review.rows)), cancel_token, rereview=False)
    except (OSError, ValueError) as error:
        return False, str(error), {}
