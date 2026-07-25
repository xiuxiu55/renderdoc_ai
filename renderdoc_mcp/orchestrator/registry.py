"""Tool catalog loader: tools_catalog.json is the single source of truth.

Python 3.6 compatible.
"""

from __future__ import print_function

import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_CATALOG_PATH = os.path.join(_HERE, "tools_catalog.json")

_CATALOG = None
_TOOLS_BY_ID = None
_INTENT_PLANS = None
_INTENT_META = None
_COMMON_TOOLS = None
_MCP_ONLY_TOOLS = None
_PANEL_TOOLS = None


def _load_catalog():
    global _CATALOG, _TOOLS_BY_ID, _INTENT_PLANS, _INTENT_META
    global _COMMON_TOOLS, _MCP_ONLY_TOOLS, _PANEL_TOOLS
    if _CATALOG is not None:
        return _CATALOG
    with open(_CATALOG_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    _CATALOG = data
    by_id = {}
    for t in data.get("tools") or []:
        tid = t.get("id")
        if tid:
            by_id[tid] = t
    _TOOLS_BY_ID = by_id

    common = set()
    mcp_only = set()
    panel = set()
    for tid, t in by_id.items():
        paths = set(t.get("paths") or [])
        if "panel" in paths:
            panel.add(tid)
        if "mcp" in paths and "panel" in paths:
            common.add(tid)
        elif "mcp" in paths:
            mcp_only.add(tid)
    _COMMON_TOOLS = frozenset(common)
    _MCP_ONLY_TOOLS = frozenset(mcp_only)
    _PANEL_TOOLS = frozenset(panel)

    plans = {}
    meta = {}
    for name, spec in (data.get("intents") or {}).items():
        plans[name] = dict(spec.get("plan") or {})
        meta[name] = {
            "priority": int(spec.get("priority") or 0),
            "keywords": list(spec.get("keywords") or []),
        }
    _INTENT_PLANS = plans
    _INTENT_META = meta
    return _CATALOG


def reload_catalog():
    """Force reload (tests / hot-edit)."""
    global _CATALOG, _TOOLS_BY_ID, _INTENT_PLANS, _INTENT_META
    global _COMMON_TOOLS, _MCP_ONLY_TOOLS, _PANEL_TOOLS
    _CATALOG = None
    _TOOLS_BY_ID = None
    _INTENT_PLANS = None
    _INTENT_META = None
    _COMMON_TOOLS = None
    _MCP_ONLY_TOOLS = None
    _PANEL_TOOLS = None
    return _load_catalog()


def get_catalog():
    return _load_catalog()


def list_tools(path=None):
    """Return tool dicts, optionally filtered by path (mcp|panel)."""
    _load_catalog()
    out = []
    for tid, t in _TOOLS_BY_ID.items():
        paths = t.get("paths") or []
        if path and path not in paths:
            continue
        out.append(t)
    return out


def tool_info(tool_id):
    _load_catalog()
    return _TOOLS_BY_ID.get(tool_id)


def native_not_exposed():
    _load_catalog()
    return list((_CATALOG or {}).get("native_not_exposed") or [])


def intent_meta():
    _load_catalog()
    return dict(_INTENT_META or {})


def tools_for_path(path):
    """Return frozenset of tools allowed on this path."""
    _load_catalog()
    if path == "mcp":
        return _COMMON_TOOLS | _MCP_ONLY_TOOLS
    return _PANEL_TOOLS


def get_intent_plan(intent):
    _load_catalog()
    plans = _INTENT_PLANS or {}
    return plans.get(intent) or plans.get("general") or {
        "steps": [],
        "analyze": None,
        "explain_with_llm": False,
    }


_load_catalog()
COMMON_TOOLS = _COMMON_TOOLS
MCP_ONLY_TOOLS = _MCP_ONLY_TOOLS
INTENT_PLANS = _INTENT_PLANS
