"""Mavrick's public Python API."""

from importlib import import_module

__version__ = "2.0.0"

_EXPORTS = {
    "schema": ("RiskLevel", "ActionType", "ActionRequest", "ModelResponse"),
    "policy": ("requires_confirmation", "is_safe_user_path", "scrub_secrets"),
    "executor": ("ActionExecutor", "ActionResult", "JobState"),
    "ai": ("query_model", "check_ai_health"),
    "config": ("load_user_config", "ensure_secure_storage"),
}
__all__ = [name for names in _EXPORTS.values() for name in names]


def __getattr__(name):
    for module, names in _EXPORTS.items():
        if name in names:
            value = getattr(import_module(f".{module}", __name__), name)
            globals()[name] = value
            return value
    raise AttributeError(name)
