"""Typed workflow reviews, exact approvals, and the local recipe catalog."""

import json
import os
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from . import config
from .history import redact_secrets
from .policy import WorkflowRisk, aggregate_workflow_risk, workflow_risk
from .schema import WorkflowPlan, WORKFLOW_SLUG, parse_workflow_plan
from .tools.files import FileReview, display_path
from .schema import ActionRequest

_MAX_CATALOG_BYTES = 256 * 1024
_MAX_RECIPES = 128
_REFRESH_INTERVAL = 1.0


@dataclass(frozen=True)
class WorkflowStepReview:
    number: int
    risk: WorkflowRisk
    summary: str
    targets: tuple[str, ...] = ()
    action: ActionRequest | None = None
    files: FileReview | None = None
    error: str = ""


@dataclass(frozen=True)
class WorkflowReview:
    plan: WorkflowPlan
    steps: tuple[WorkflowStepReview, ...]
    title: str
    risk: WorkflowRisk

    @property
    def allowed(self) -> bool:
        return all(not step.error for step in self.steps)

    @property
    def requires_approval(self) -> bool:
        return self.risk != WorkflowRisk.READ_ONLY


@dataclass(frozen=True)
class WorkflowApproval:
    signature: str


def approve_workflow(review: WorkflowReview) -> WorkflowApproval:
    if not review.allowed:
        raise ValueError("A blocked workflow cannot be approved.")
    return WorkflowApproval(json.dumps(asdict(review), sort_keys=True, ensure_ascii=True))


@dataclass(frozen=True)
class WorkflowStepReport:
    number: int
    action: str
    risk: str
    status: str
    summary: str
    verification: str = ""
    output: str = ""

    def line(self) -> str:
        return f"Step {self.number} [{self.status}]: {self.summary}"


def describe_workflow(review: WorkflowReview) -> str:
    verdict = ("blocked" if not review.allowed else "approval required" if review.requires_approval else
               "allowed (read-only)")
    lines = [review.title, f"Aggregate risk: {review.risk.value}", f"Policy: {verdict}"]
    for step in review.steps:
        lines.append(f"{step.number}. [{step.risk.value}] {step.summary}")
        lines.extend(f"   {target}" for target in step.targets)
        if step.error:
            lines.append(f"   Blocked: {step.error}")
    return "\n".join(lines)


@dataclass(frozen=True)
class WorkflowCatalog:
    plans: Mapping[str, WorkflowPlan]
    errors: tuple[str, ...] = ()


_cache_lock = threading.Lock()
_cache = ("", 0.0, None, WorkflowCatalog(MappingProxyType({})))


def _read_catalog(path: str) -> WorkflowCatalog:
    entries = {}
    errors = []
    try:
        with open(path, "rb") as handle:
            raw = handle.read(_MAX_CATALOG_BYTES + 1)
        if len(raw) > _MAX_CATALOG_BYTES:
            raise ValueError("workflows.json exceeds the 256 KiB limit.")
        data = json.loads(raw)
        records = data.get("workflows") if isinstance(data, dict) else data
        if not isinstance(records, list) or len(records) > _MAX_RECIPES:
            raise ValueError(f"workflows.json needs an array of up to {_MAX_RECIPES} workflows.")
    except FileNotFoundError:
        return WorkflowCatalog(MappingProxyType({}))
    except (OSError, UnicodeError, ValueError, RecursionError) as error:
        return WorkflowCatalog(MappingProxyType({}), (redact_secrets(f"Cannot read workflows.json. {error}"),))

    duplicates = set()
    for index, item in enumerate(records, 1):
        try:
            if not isinstance(item, dict) or set(item) - {"name", "description", "steps"}:
                raise ValueError("Expected name, description, and steps.")
            name = item.get("name")
            if not isinstance(name, str) or not WORKFLOW_SLUG.fullmatch(name):
                raise ValueError("Use a lowercase workflow slug.")
            if name in entries or name in duplicates:
                entries.pop(name, None)
                duplicates.add(name)
                raise ValueError(f"Duplicate workflow name '{name}'.")
            entries[name] = parse_workflow_plan(item.get("steps"), name=name,
                                                description=item.get("description", ""))
        except ValueError as error:
            errors.append(f"Workflow {index}: {error}")
    return WorkflowCatalog(MappingProxyType(entries), redact_secrets(tuple(errors)) if errors else ())


def workflow_catalog(*, cached_only: bool = False, refresh: bool = False) -> WorkflowCatalog | None:
    """Serve an immutable index to GTK and refresh it only off the GTK thread."""
    global _cache
    path = os.fspath(config.WORKFLOWS_FILE)
    key, expires, stamp, cached = _cache
    if cached_only:
        return cached if key == path and time.monotonic() < expires else None
    if not refresh and key == path and time.monotonic() < expires:
        return cached
    with _cache_lock:
        key, expires, stamp, cached = _cache
        if not refresh and key == path and time.monotonic() < expires:
            return cached
        try:
            info = Path(path).stat()
            updated = (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size)
        except FileNotFoundError:
            updated = None
        except OSError:
            updated = object()
        if path != key or stamp != updated:
            cached = _read_catalog(path)
        _cache = (path, time.monotonic() + _REFRESH_INTERVAL, updated, cached)
        return cached


def declared_workflow_risk(plan: WorkflowPlan) -> WorkflowRisk:
    return aggregate_workflow_risk(workflow_risk(step) for step in plan)


def file_step_targets(review: FileReview) -> tuple[str, ...]:
    return tuple(f"{display_path(row.source)} → {display_path(row.destination)}" for row in review.rows)


def overlaps_prior_file_step(review: FileReview, earlier: list[FileReview]) -> bool:
    def paths(file_review):
        return [Path(path) for row in file_review.rows for path in (row.source, row.destination)]

    current = paths(review)
    return any(left.is_relative_to(right) or right.is_relative_to(left)
               for previous in earlier for right in paths(previous) for left in current)
