"""Protocol-v2 wire grammar, independent of T013's lifecycle reducer.

format_event(tag, event, fields, note='', *, timestamp=None) returns one line
without a trailing newline. List fields accept lists/tuples, not comma-joined
strings. parse_file(path) returns (events, warnings). Each event is a dict with
time, tag, event, fields, note, line (1-based), path and raw. Scalar field values
are strings; list field values are lists of strings. Warnings are strings.
active_claims(repo, source='worktree') returns dicts with task, tag, scope, path
and line. Claims persist until release/cancel; quiet/stale claims never expire.

Parsing checks syntax, not the full lifecycle. T013 owns lifecycle, revision,
review quorum, dependencies and finding semantics. Basic claim-holder and owner
checks here keep malformed/unauthorized claim records from granting file access.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
import re
import subprocess
from urllib.parse import unquote_to_bytes

TAGS = frozenset(("claude", "gpt", "deepseek"))
KINDS = frozenset(("numerical", "formulation", "code", "documentation", "protocol", "review"))
TASK_RE = re.compile(r"T[0-9]{3,}[a-z]?\Z")
LIST_FIELDS = frozenset(("reviewers", "deliverables", "depends_on", "scope"))
STATES = frozenset(("open", "proposed", "awaiting_user", "claimed", "review",
                    "changes_requested", "disputed", "accepted", "cancelled"))
FIELDS = {
    "created": {"owner", "reviewers", "required", "kind", "deliverables", "depends_on", "initial"},
    "claim": {"scope"}, "release": set(), "submit": {"rev"},
    "review": {"file", "rev"}, "request_changes": {"file"},
    "accept": {"rev"}, "response": {"finding"},
    "disposition": {"finding", "result"}, "dispute": {"reproduction"},
    "user": {"decision", "action"},
}
OPTIONAL_CREATED = frozenset(("job",))
def without_comments(text):
    """Text without HTML comments: an example block in the DECISIONS.md template is no decision."""
    return re.sub(r"<!--.*?-->", "", text, flags=re.S)


def next_decision(text):
    """The next free D### id of a DECISIONS.md text."""
    numbers = [int(n) for n in re.findall(r"^###\s+D(\d{3,})\b", without_comments(text), re.M)]
    return "D%03d" % (max(numbers, default=0) + 1)


USER_FIELDS = {
    "open": {"task"}, "cancel": {"task"},
    "accept_limitation": {"finding"}, "resolve_dispute": {"task", "to"},
    "approve_batch": {"task", "scope", "limits"}, "set_config": {"key", "value"},
}


def pattern(value):
    """Normalize a repo-relative POSIX path/glob; reject traversal/magic roots."""
    if not isinstance(value, str) or not value or "\0" in value or "\\" in value:
        raise ValueError("Expected a nonempty POSIX path/glob")
    p = PurePosixPath(value)
    if p.is_absolute() or ".." in p.parts or str(p) == "." or ":" in p.parts[0]:
        raise ValueError("Path must stay inside the repository: " + value)
    return p.as_posix()


def _encode(value):
    value = str(value)
    return "".join("%%%02X" % ord(c) if c in "%;=,\t\r\n" else c for c in value)


def _decode(value):
    if re.search(r"%(?![0-9A-Fa-f]{2})", value):
        raise ValueError("Invalid percent escape")
    return unquote_to_bytes(value).decode("utf-8", errors="strict")


def _validate(tag, event, fields, timestamp):
    if tag not in TAGS or event not in FIELDS:
        raise ValueError("Unknown agent tag or event")
    if not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:Z|[+-]\d\d:\d\d)", timestamp):
        raise ValueError("Timestamp needs seconds and UTC offset")
    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timestamp has no UTC offset")
    required = FIELDS[event]
    if event == "user":
        action = fields.get("action")
        if action not in USER_FIELDS:
            raise ValueError("Unknown user action")
        required = required | USER_FIELDS[action]
    if event == "created":
        # T038: a created event may also name the job the task belongs to (optional).
        if not required <= set(fields) <= required | OPTIONAL_CREATED:
            raise ValueError("Wrong event fields; expected " + ",".join(sorted(required))
                             + " and optionally " + ",".join(sorted(OPTIONAL_CREATED)))
    elif set(fields) != required:
        raise ValueError("Wrong event fields; expected " + ",".join(sorted(required)))
    for key, value in fields.items():
        if key in LIST_FIELDS:
            if not isinstance(value, (list, tuple)) or any(not isinstance(x, str) or not x for x in value):
                raise ValueError(key + " must be a list of nonempty strings")
            if len(set(value)) != len(value):
                raise ValueError("Duplicate list item in " + key)
        elif not isinstance(value, str) or not value:
            raise ValueError(key + " must be a nonempty string")
    if event == "created":
        if fields["owner"] not in TAGS | {"open"}:
            raise ValueError("Invalid owner")
        if not set(fields["reviewers"]) <= TAGS or fields["owner"] in fields["reviewers"]:
            raise ValueError("Invalid reviewer or owner self-review")
        if fields["required"] not in ("all", "any") or fields["initial"] not in ("open", "proposed", "awaiting_user"):
            raise ValueError("Invalid reviewer quorum or initial state")
        if fields["kind"] not in KINDS:
            raise ValueError("Unknown task kind")
        if "job" in fields and not (fields["job"] == "none" or re.fullmatch(r"job-[1-9][0-9]*", fields["job"])):
            raise ValueError("Invalid job (job-<n> or none)")
        if not fields["deliverables"]:
            raise ValueError("Empty deliverables")
        for dependency in fields["depends_on"]:
            if not (TASK_RE.fullmatch(dependency) or re.fullmatch(r"D\d{3,}", dependency)):
                raise ValueError("Invalid dependency")
    if event == "claim" and not fields["scope"]:
        raise ValueError("Empty claim scope")
    for key in ("deliverables", "scope"):
        for value in fields.get(key, []):
            if pattern(value) != value:
                raise ValueError("Unnormalized path/glob")
    for key in ("file", "reproduction"):
        if key in fields and pattern(fields[key]) != fields[key]:
            raise ValueError("Unnormalized path")
    if "rev" in fields and not re.fullmatch(r"[0-9a-f]{64}", fields["rev"]):
        raise ValueError("Revision must be a lowercase SHA-256")
    if "finding" in fields:
        finding = re.fullmatch(r"(.+)#F([1-9][0-9]*)", fields["finding"])
        if not finding or pattern(finding[1]) != finding[1]:
            raise ValueError("Finding must be <review path>#F<n>")
    if event == "disposition" and fields["result"] not in ("resolved", "upheld"):
        raise ValueError("Invalid disposition")
    if event == "user":
        if not re.fullmatch(r"D\d{3,}", fields["decision"]):
            raise ValueError("User event must cite a D###")
        if "task" in fields and not TASK_RE.fullmatch(fields["task"]):
            raise ValueError("Invalid task id")
        if "to" in fields and fields["to"] not in STATES:
            raise ValueError("Invalid dispute resolution state")


def format_event(tag, event, fields=None, note="", *, timestamp=None):
    timestamp = timestamp or datetime.now().astimezone().isoformat(timespec="seconds")
    fields = dict(fields or {})
    _validate(tag, event, fields, timestamp)
    pairs = []
    for key, value in sorted(fields.items()):
        encoded = ",".join(_encode(x) for x in value) if key in LIST_FIELDS else _encode(value)
        pairs.append(key + "=" + encoded)
    note = re.sub(r"[\t\r\n]", " ", str(note))
    return "\t".join((timestamp, tag, event, ";".join(pairs), note))


def parse_text(text, path="<text>"):
    events, warnings, seen = [], [], set()
    for number, line in enumerate(text.split("\n"), 1):
        line = line.removesuffix("\r")
        if not line.strip():
            continue
        try:
            line.encode("utf-8", errors="strict")
            columns = line.split("\t")
            if len(columns) != 5:
                raise ValueError("Expected five TAB-separated columns")
            timestamp, tag, event, arguments, note = columns
            fields = {}
            for pair in arguments.split(";") if arguments else []:
                if pair.count("=") != 1:
                    raise ValueError("Malformed key=value")
                key, value = pair.split("=", 1)
                if key in fields:
                    raise ValueError("Duplicate field")
                fields[key] = ([_decode(x) for x in value.split(",")] if value else []) if key in LIST_FIELDS else _decode(value)
            _validate(tag, event, fields, timestamp)
            canonical = format_event(tag, event, fields, note, timestamp=timestamp)
            if canonical in seen:
                raise ValueError("Duplicate event line")
            seen.add(canonical)
            events.append(dict(time=timestamp, tag=tag, event=event, fields=fields,
                               note=note, line=number, path=str(path), raw=line))
        except (ValueError, UnicodeError, TypeError) as exc:
            warnings.append(f"{path}:{number}: {exc}")
    return events, warnings


def parse_file(path):
    try:
        return parse_text(Path(path).read_bytes().decode("utf-8", errors="surrogateescape"), path)
    except FileNotFoundError:
        return [], []


def _git(repo, *args):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True)
    return result.stdout if result.returncode == 0 else b""


def task_records(repo, source="worktree"):
    """Yield (repo-relative status path, events, warnings), for T012/T013."""
    repo = Path(repo)
    prefix = "docs/agent_hub/tasks/"
    if source == "worktree":
        paths = sorted(p.relative_to(repo).as_posix() for p in (repo/prefix).glob("T*.status.md"))
    elif source in ("HEAD", "index"):
        args = ("ls-files", "-z", "--", prefix) if source == "index" else ("ls-tree", "-r", "--name-only", "-z", "HEAD", "--", prefix)
        paths = sorted(x.decode() for x in _git(repo, *args).split(b"\0") if x.endswith(b".status.md"))
    else:
        raise ValueError("Unknown status source")
    for path in paths:
        if not re.fullmatch(prefix + r"T\d{3,}[a-z]?\.status\.md", path):
            continue
        if source == "worktree":
            events, warnings = parse_file(repo/path)
        else:
            spec = ":"+path if source == "index" else "HEAD:"+path
            events, warnings = parse_text(_git(repo, "show", spec).decode("utf-8", errors="surrogateescape"), path)
        yield path, events, warnings


def active_claims(repo, source="worktree"):
    result = []
    for path, events, _ in task_records(repo, source):
        owner, holder, scope, line, created, reviewers, base = None, None, [], 0, False, [], None
        task = Path(path).name.split(".")[0]
        for record in events:
            kind, tag, fields = record["event"], record["tag"], record["fields"]
            if kind == "created" and not created:
                owner, created, reviewers = fields["owner"], True, fields["reviewers"]
                base = owner
            elif kind == "claim" and created and holder is None and owner in ("open", tag) and tag not in reviewers:
                owner, holder, scope, line = tag, tag, list(fields["scope"]), record["line"]
            elif kind == "release" and holder == tag:
                holder, scope = None, []
                if base == "open":  # T-hub-028: a released open-base task is claimable by anyone again
                    owner = "open"
            elif kind == "user" and fields["action"] == "cancel" and fields["task"] == task:
                holder, scope = None, []
        if holder:
            result.append(dict(task=task, tag=holder, scope=scope, path=path, line=line))
    return result


def _tokens(glob):
    tokens, i = [], 0
    while i < len(glob):
        c = glob[i]
        if c == "[":
            end = i+1
            if end < len(glob) and glob[end] == "!":
                end += 1
            if end < len(glob) and glob[end] == "]":
                end += 1
            end = glob.find("]", end)
            if end >= 0:
                tokens.append(glob[i:end+1]); i = end+1; continue
        tokens.append(c); i += 1
    return tokens


def scopes_overlap(left, right):
    """Glob-language intersection, using Python fnmatchcase (* includes /).

    Membership changes only at literal/class range boundaries. Testing those
    boundaries and their neighbours supplies a finite alphabet for a product
    automaton, including Unicode classes. This is not filesystem sampling.
    """
    left, right = pattern(left), pattern(right)
    a, b = _tokens(left), _tokens(right)
    points = {1, 32, 47, 126, 0x10ffff}
    for c in left+right:
        points.update(n for n in (ord(c)-1, ord(c), ord(c)+1) if 0 < n <= 0x10ffff)
    alphabet = [chr(n) for n in points]
    def matches(token, c):
        return token in ("*", "?") or (fnmatchcase(c, token) if token.startswith("[") and len(token)>1 else token == c)
    pending, seen = deque([(0,0)]), set()
    while pending:
        i,j = pending.popleft()
        if (i,j) in seen:
            continue
        seen.add((i,j))
        if i == len(a) and j == len(b):
            return True
        if i < len(a) and a[i] == "*":
            pending.append((i+1,j))
        if j < len(b) and b[j] == "*":
            pending.append((i,j+1))
        if i < len(a) and j < len(b) and any(matches(a[i], c) and matches(b[j], c) for c in alphabet):
            pending.append((i if a[i] == "*" else i+1, j if b[j] == "*" else j+1))
    return False
