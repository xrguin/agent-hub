#!/usr/bin/env python3
"""Task display names: `T024` is shown as `T-collect-024`.

The number is the canonical id (file names, events, history). The category lives
in the append-only mapping `docs/agent_hub/TASK_NAMES.tsv`, one `T024<TAB>collect`
line per task, appended by `new_task.py` in the same transaction as the created
event. Legacy suffix ids such as `T005a` take the category of their base `T005`
unless they have a line of their own.
"""
from __future__ import annotations

from pathlib import Path
import re

import hubtx

CATEGORIES = ("hub", "collect", "model", "loc", "ctrl", "check")
MAPPING = hubtx.HUB + "TASK_NAMES.tsv"
_ID = re.compile(r"T([0-9]{3,})([a-z]?)\Z")
_NAME = re.compile(r"T-([A-Za-z]+)-([0-9]{3,})([a-z]?)\Z", re.I)


def _root(repo):
    return Path(repo).resolve() if repo is not None else hubtx.repo_root()


def exists(repo=None):
    """True when the repository has a mapping file (the naming scheme is in force)."""
    return (_root(repo) / MAPPING).is_file()


def load(repo=None):
    """Return {id: category} from the mapping file ({} when there is none)."""
    path = _root(repo) / MAPPING
    if not path.is_file():
        return {}
    result = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip() or raw.startswith("#"):
            continue
        parts = raw.split("\t")
        if len(parts) != 2 or not _ID.fullmatch(parts[0]) or parts[1] not in CATEGORIES:
            raise ValueError("%s line %d is malformed: %r" % (MAPPING, number, raw))
        if result.get(parts[0], parts[1]) != parts[1]:
            raise ValueError("%s line %d gives %s a second category" % (MAPPING, number, parts[0]))
        result[parts[0]] = parts[1]
    return result


def _lookup(mapping, task_id):
    if task_id in mapping:
        return mapping[task_id]
    match = _ID.fullmatch(task_id)
    return mapping.get("T" + match[1]) if match else None


def category(task_id, repo=None):
    """The category of a canonical id, or None when it is unmapped."""
    return _lookup(load(repo), task_id)


def display(task_id, repo=None, warnings=None, mapping=None):
    """`T024` -> `T-collect-024`. An unmapped id is returned unchanged and
    appended to `warnings` (a list) when one is given."""
    found = _lookup(load(repo) if mapping is None else mapping, task_id)
    match = _ID.fullmatch(task_id)
    if found is None or not match:
        if warnings is not None:
            warnings.append("task %s has no category in %s" % (task_id, MAPPING))
        return task_id
    return "T-%s-%s%s" % (found, match[1], match[2])


def canonical(name, repo=None):
    """Accept `T024`, `T-collect-024` or any-case `t-COLLECT-024`; return `T024`.

    Raises ValueError for a malformed name, an unknown category, or a category
    that differs from the mapping (or an id the mapping does not know)."""
    if not isinstance(name, str):
        raise ValueError("Task name must be a string")
    text = name.strip()
    if _ID.fullmatch(text):
        return text
    match = _NAME.fullmatch(text)
    if not match:
        raise ValueError("Malformed task name %r: expected T024 or T-<category>-024" % name)
    wanted = match[1].lower()
    if wanted not in CATEGORIES:
        raise ValueError("Unknown category %r in %r (categories: %s)" % (match[1], name, ", ".join(CATEGORIES)))
    task_id = "T" + match[2] + match[3].lower()
    actual = category(task_id, repo)
    if actual != wanted:
        raise ValueError("%s is %s, not %s" % (task_id, "category " + actual if actual else "unmapped", wanted))
    return task_id
