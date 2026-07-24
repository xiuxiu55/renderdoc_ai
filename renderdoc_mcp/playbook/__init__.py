"""Shared hot-question playbook for MCP server and in-UI panel.

Add questions in ``questions.json``; wire new tools into the backend's
``call(tool, args)``; add analyzers in ``analyzers.py``.
"""

from .runtime import (  # noqa: F401
    CallableBackend,
    FrameBackend,
    describe_question,
    format_result,
    list_questions,
    load_registry,
    match_question,
    run_question,
)

__all__ = [
    "CallableBackend",
    "FrameBackend",
    "describe_question",
    "format_result",
    "list_questions",
    "load_registry",
    "match_question",
    "run_question",
]
