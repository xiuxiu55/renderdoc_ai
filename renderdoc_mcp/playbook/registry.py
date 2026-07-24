"""Load and query the hot-question registry (Python 3.6 compatible)."""

from __future__ import print_function

import json
import os

_REGISTRY = None
_JSON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "questions.json")


def load_registry(force=False):
    global _REGISTRY
    if _REGISTRY is not None and not force:
        return _REGISTRY
    with open(_JSON_PATH, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, list):
        raise ValueError("questions.json must be a list")
    by_id = {}
    for q in data:
        if not isinstance(q, dict) or not q.get("id"):
            continue
        by_id[q["id"]] = q
    _REGISTRY = {"list": data, "by_id": by_id}
    return _REGISTRY


def list_questions(path=None, tag=None):
    """Return questions sorted by ``hot`` desc. Filter by path/tag if given."""
    reg = load_registry()
    out = []
    tag_l = (tag or "").strip().lower()
    for q in reg["list"]:
        paths = q.get("paths") or ["mcp", "panel"]
        if path and path not in paths:
            continue
        if tag_l:
            tags = [str(t).lower() for t in (q.get("tags") or [])]
            title = (q.get("title") or "").lower()
            if tag_l not in tags and tag_l not in title and tag_l not in q["id"]:
                continue
        out.append(q)
    out.sort(key=lambda x: int(x.get("hot") or 0), reverse=True)
    return out


def get_question(question_id):
    reg = load_registry()
    return reg["by_id"].get(question_id)


def match_question(text, path=None):
    """Best-effort match free text to a question id via tags/title."""
    t = (text or "").strip().lower()
    if not t:
        return None
    best = None
    best_score = 0
    for q in list_questions(path=path):
        score = 0
        qid = q["id"].lower()
        title = (q.get("title") or "").lower()
        if qid in t or t in qid:
            score += 50
        if title and (title in t or t in title):
            score += 40
        for tag in q.get("tags") or []:
            tg = str(tag).lower()
            if tg and tg in t:
                score += 10 + min(len(tg), 8)
        score += int(q.get("hot") or 0) * 0.01
        if score > best_score:
            best_score = score
            best = q
    # Require at least one real tag/title hit (score from hot alone is tiny).
    if best is None or best_score < 10:
        return None
    return best


def describe_question(question_id):
    q = get_question(question_id)
    if q is None:
        return None
    return {
        "id": q["id"],
        "title": q.get("title"),
        "tags": q.get("tags") or [],
        "hot": q.get("hot"),
        "paths": q.get("paths") or ["mcp", "panel"],
        "collect": q.get("collect") or [],
        "analyze": q.get("analyze"),
        "params": q.get("params") or {},
        "followups": q.get("followups") or [],
    }
