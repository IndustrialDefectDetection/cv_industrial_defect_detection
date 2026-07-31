"""
Shared utilities for MES and Production Meeting applications.
"""

from importlib import import_module

from .database import DatabaseManager, get_tool_config

_BEDROCK_EXPORTS = {
    "get_bedrock_client",
    "get_available_models",
    "get_best_available_model",
}
__all__ = [
    "DatabaseManager",
    "get_tool_config",
    *_BEDROCK_EXPORTS,
]


def __getattr__(name):
    """Load Bedrock helpers only when a caller explicitly requests one."""

    if name in _BEDROCK_EXPORTS:
        bedrock_utils = import_module(f"{__name__}.bedrock_utils")
        return getattr(bedrock_utils, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
