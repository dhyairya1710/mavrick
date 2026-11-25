"""Recognize exact single intents before any provider is selected."""

import ast
import math
import operator
import re
from dataclasses import dataclass

from .policy import APP_ALIASES
from .schema import WorkflowPlan
from .tools.apps import app_display_name, instant_app_index, resolve_cached_app
from .workflows import workflow_catalog

_MAX_EXPRESSION = 192
_MAX_NODES = 64
_MAX_DEPTH = 20
_MAX_BITS = 1024
_MAX_EXPONENT = 128
_MATH = re.compile(r"[0-9.+*/%() \t-]+")
_APP_ID = re.compile(r"[a-z0-9][a-z0-9_.-]{0,95}")
_MULTI_SPLIT = re.compile(r"\s*(?:,|[&+]|\band\b)\s*")
_LAUNCH_VERBS = ("open", "launch", "start", "run")
_NON_APP_WORDS = frozenset({
    "please", "thanks", "thank", "my", "the", "a", "an", "to", "in", "on", "at", "for",
    "from", "into", "of", "with", "about", "after", "before", "then",
    "clean", "clear", "organize", "sort", "list", "find", "check", "inspect",
    "install", "uninstall", "remove", "delete", "move", "copy", "rename",
    "set", "mute", "unmute", "pause", "play", "resume", "next", "previous", "skip",
    "lock", "undo", "history", "show", "tell", "explain", "what", "why", "how",
    "switch", "turn", "enable", "disable", "activate", "arrange", "tile", "split", "screen",
    "update", "updates", "upgrade", "close", "kill", "stop",
    "downloads", "documents", "desktop", "storage", "cache", "junk",
    "ram", "cpu", "gpu", "wifi", "network", "volume", "battery", "power", "profile",
    "workspace", "pdf", "pdfs",
})
_WORKFLOW_QUERY = re.compile(r"(/|run )([a-z0-9][a-z0-9-]{0,47})")
_BINARY = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}


class CalculationError(ValueError):
    """The expression is invalid, unsupported, or outside the arithmetic limits."""


def calculate(expression: str) -> int | float:
    text = expression.strip()
    if len(text) > _MAX_EXPRESSION or not _MATH.fullmatch(text):
        raise CalculationError("Use numbers, parentheses, and arithmetic operators only.")
    try:
        tree = ast.parse(text, mode="eval")
    except (SyntaxError, RecursionError) as error:
        raise CalculationError("Invalid arithmetic expression.") from error

    remaining = _MAX_NODES

    def visit(node: ast.AST, depth: int = 0) -> int | float:
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > _MAX_DEPTH:
            raise CalculationError("Expression is too complex.")
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            value = node.value
        elif isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
            value = _UNARY[type(node.op)](visit(node.operand, depth + 1))
        elif isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
            left, right = visit(node.left, depth + 1), visit(node.right, depth + 1)
            if isinstance(node.op, ast.Pow) and (
                abs(right) > _MAX_EXPONENT or
                (type(left) is int and type(right) is int and right > 0 and
                 max(1, left.bit_length() - 1) * right > _MAX_BITS)
            ):
                raise CalculationError("Exponent exceeds the calculator limit.")
            value = _BINARY[type(node.op)](left, right)
        else:
            raise CalculationError("Unsupported arithmetic expression.")
        if type(value) is int and value.bit_length() <= _MAX_BITS:
            return value
        if type(value) is float and math.isfinite(value):
            return value
        raise CalculationError("Result is outside the calculator range.")

    try:
        return visit(tree.body)
    except ZeroDivisionError as error:
        raise CalculationError("Cannot divide by zero.") from error
    except OverflowError as error:
        raise CalculationError("Result is outside the calculator range.") from error


@dataclass(frozen=True)
class LocalRoute:
    kind: str
    value: str = ""
    plan: WorkflowPlan | None = None
    apps: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()


def _is_non_app_clause(clause: str) -> bool:
    words = clause.split()
    if not words or len(words) > 3:
        return True
    if any(ch in clause for ch in ("?", "/", "~", "\\", ":", ";", "|", "=", "<", ">", "'", '"')):
        return True
    return any(word in _NON_APP_WORDS for word in words)


def _route_apps(phrase: str, *, cached_only: bool) -> LocalRoute | None:
    if any(marker in phrase for marker in ("://", "/", "~/", "..")) or phrase.endswith("?"):
        return None
    prefix, sep, rest = phrase.partition(" ")
    has_verb = bool(sep and prefix in _LAUNCH_VERBS and rest.strip())
    candidate = rest.strip() if has_verb else phrase
    if not candidate or re.search(r"(?:^|,|[&+]|\band\b)\s*$", candidate):
        return None
    parts = [part.strip() for part in _MULTI_SPLIT.split(candidate) if part.strip()]
    if not parts or any(_is_non_app_clause(part) for part in parts):
        return None

    index = instant_app_index(cached_only=cached_only)
    if not index:
        return None

    if len(parts) == 1:
        part = parts[0]
        if part not in APP_ALIASES and not _APP_ID.fullmatch(part.replace(" ", "-")):
            return None
        target = resolve_cached_app(part, index)
        return LocalRoute("app", target) if target else None

    matched: list[str] = []
    missing: list[str] = []
    for part in parts:
        target = resolve_cached_app(part, index)
        if target:
            if target not in matched:
                matched.append(target)
            continue
        if len(part.split()) <= 2 and _APP_ID.fullmatch(part.replace(" ", "-")):
            missing.append(part)
        else:
            return None

    if not matched:
        return None
    if len(matched) == 1 and not missing:
        return LocalRoute("app", matched[0])
    labels = ", ".join(app_display_name(app_id) for app_id in matched)
    preview = f"{labels} (not found: {', '.join(missing)})" if missing else labels
    return LocalRoute("multi_app", preview, apps=tuple(matched), missing=tuple(missing))


def route_local(query: str, *, cached_only: bool = False) -> LocalRoute | None:
    text = query.strip()
    if not text or len(text) > _MAX_EXPRESSION:
        return None
    phrase = text.casefold()
    if shortcut := _WORKFLOW_QUERY.fullmatch(phrase):
        catalog = workflow_catalog(cached_only=cached_only)
        if catalog is not None:
            name = shortcut[2]
            if plan := catalog.plans.get(name):
                return LocalRoute("workflow", name, plan)
            if shortcut[1] == "/":
                detail = "\n".join(catalog.errors)
                return LocalRoute("workflow_missing", f"No saved workflow named '{name}'.{f' {detail}' if detail else ''}")
        elif shortcut:
            return None
    if phrase in ("undo", "history"):
        return LocalRoute(phrase)
    if _MATH.fullmatch(text) and any(char.isdigit() for char in text):
        try:
            return LocalRoute("calculator", str(calculate(text)))
        except CalculationError as error:
            return LocalRoute("calculation_error", str(error))

    return _route_apps(phrase, cached_only=cached_only)
