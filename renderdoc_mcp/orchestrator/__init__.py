"""Intent -> Plan -> Execute -> Analyze orchestration (Panel + MCP).

Local-first: hot playbook and rule plans call RenderDoc tools automatically;
LLM is only used by the panel as a narrative layer when evidence is ready.
"""

from __future__ import print_function

from .pipeline import answer, format_report  # noqa: F401

__all__ = ["answer", "format_report"]
