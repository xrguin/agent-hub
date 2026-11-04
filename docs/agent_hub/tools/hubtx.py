"""Cooperative, standard-library Git transactions for protocol-v2 tools.

Public API is plan §3: transaction(tag, task), Tx.append/write_new/commit,
owns(tag, path), config(), repo_root(), now(), and the HubError subclasses.
All target paths are repo-relative POSIX paths. No reset/stash/checkout is used.
Hooks inspect their inherited candidate index and never acquire this lock again.

T013 may use hub_events' documented dict grammar and scopes_overlap helper.
Only an explicit release/cancel ends a claim; stale claims never grant takeover.
No hook is installed and no configuration is changed merely by importing this.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from fnmatch import fnmatchcase

import hub_events as events

HUB = "docs/agent_hub/"
DEFAULTS = dict(author_per_agent=False, state_maintainer=None, memory_export=False,
                shared_tree=True, v2_in_force=False, task_branch=None, commit_guard="full")
_local = threading.local()


class HubError(Exception):
    exit_code = 6


class Busy(HubError):
    exit_code = 2


class Unauthorized(HubError):
    exit_code = 3


class DirtyTarget(HubError):
    exit_code = 4


class GitFailure(HubError):
    exit_code = 5


class BadInput(HubError):
    exit_code = 6


def git(root, *args, check=True, env=None, data=None):
    environment = os.environ.copy()
    environment["GIT_LITERAL_PATHSPECS"] = "1"
    if env:
        environment.update(env)
    try:
        p = subprocess.run(["git", "-C", str(root), *args], input=data,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment)
    except OSError as exc:
        raise GitFailure(str(exc)) from exc
    if check and p.returncode:
        raise GitFailure(p.stderr.decode(errors="replace").strip() or p.stdout.decode(errors="replace").strip())
    return p


def repo_root():
    return Path(git(Path.cwd(), "rev-parse", "--show-toplevel").stdout.decode().strip()).resolve()


def now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def normalize(root, path):
    if not isinstance(path, (str, os.PathLike)):
        raise BadInput("Expected a repo-relative POSIX path")
    try:
        value = events.pattern(os.fspath(path))
    except ValueError as exc:
        raise BadInput(str(exc)) from exc
    if value.split("/")[0] == ".git":
        raise BadInput("Git metadata is not a transaction target")
    target = root/value
    if not target.resolve().is_relative_to(root):
        raise BadInput("Path resolves outside the repository: " + value)
    # Even internal symlinks alias a different pathname and bypass scope checks.
    if any(p.is_symlink() for p in (target, *target.parents) if p.is_relative_to(root)):
        raise BadInput("Symlink targets/parents are not supported: " + value)
    return value


def _blob(root, path, source="HEAD"):
    spec = ":"+path if source == "index" else source+":"+path
    result = git(root, "show", spec, check=False)
    return result.stdout if result.returncode == 0 else None


def _read_config(root):
    # Uncommitted options must not silently activate --author or a maintainer.
    data = _blob(root, HUB+"hub_config.json")
    result = DEFAULTS.copy()
    if data is not None:
        try:
            update = json.loads(data)
        except (ValueError, UnicodeError) as exc:
            raise BadInput("Invalid committed hub_config.json: " + str(exc)) from exc
        _validate_config(update)
        result.update(update)
    return result


def _validate_config(value):
    if not isinstance(value, dict):
        raise BadInput("hub_config.json must be an object")
    for key in ("author_per_agent", "memory_export", "shared_tree", "v2_in_force"):
        if key in value and type(value[key]) is not bool:
            raise BadInput(key+" must be Boolean")
    maintainer = value.get("state_maintainer")
    if maintainer is not None and (not isinstance(maintainer,str) or maintainer not in events.TAGS):
        raise BadInput("Invalid state_maintainer")
    if "task_branch" in value and (not isinstance(value["task_branch"], str) or not value["task_branch"].strip()):
        raise BadInput("task_branch must be a non-empty string")
    if "commit_guard" in value and (not isinstance(value["commit_guard"], str) or value["commit_guard"] not in ("full", "scoped")):
        raise BadInput('commit_guard must be "full" or "scoped"')


def config():
    return _read_config(repo_root())


def policy_config(root):
    """hub_config.json as committed at the main checkout's HEAD (the HEAD of the common git
    directory).  The guard mode and `task_branch` are the repository's policy, so a linked worktree
    on a branch from before a key existed gets the same policy as main (T046 F1: new_task.py read
    the worktree's own config, so an old branch could allocate a colliding task id).  Git exports
    GIT_DIR and GIT_INDEX_FILE to hooks; they are cleared for this read, or it would see the worktree."""
    out = git(root, "rev-parse", "--git-common-dir", check=False)
    if out.returncode:
        return _read_config(root)
    common = Path(out.stdout.decode().strip())
    if not common.is_absolute():
        common = Path(root) / common
    env = {k: v for k, v in os.environ.items()
           if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_PREFIX")}
    shown = subprocess.run(["git", "--git-dir", str(common.resolve()), "show", "HEAD:" + HUB + "hub_config.json"],
                           capture_output=True, env=env)
    if shown.returncode:
        return dict(DEFAULTS)
    try:
        data = json.loads(shown.stdout)
    except (ValueError, UnicodeError) as exc:
        raise BadInput("Invalid committed hub_config.json in the main checkout: " + str(exc)) from exc
    _validate_config(data)
    return DEFAULTS | data


def _status_path(task):
    return HUB+"tasks/"+task+".status.md"


def _metadata(root, task, source):
    path = _status_path(task)
    data = (root/path).read_bytes() if source == "worktree" and (root/path).is_file() else _blob(root, path, source)
    records = events.parse_text(data.decode("utf-8", errors="surrogateescape"), path)[0] if data is not None else []
    owner, creator, reviewers, holder, base = None, None, [], None, None
    for record in records:
        tag, kind, fields = record["tag"], record["event"], record["fields"]
        if kind == "created" and owner is None:
            owner, creator, reviewers = fields["owner"], tag, fields["reviewers"]
            base = owner
        elif kind == "claim" and holder is None and owner in ("open", tag) and tag not in reviewers:
            owner, holder = tag, tag
        elif kind == "release" and tag == holder:
            holder = None
            if base == "open":  # T-hub-028
                owner = "open"
    return owner, creator, reviewers, records


def _claims(root, source):
    # HEAD claims cannot be erased by an uncommitted rewrite of a status file.
    return events.active_claims(root, "HEAD") + events.active_claims(root, source)


def _foreign(root, tag, path, source):
    return any(c["tag"] != tag and any(fnmatchcase(path, g) for g in c["scope"])
               for c in _claims(root, source))


def append_only(path):
    return path in {HUB+"DECISIONS.md", HUB+"INDEX.md", HUB+"QUESTIONS.md", HUB+"TASK_NAMES.tsv"} or bool(
        re.fullmatch(HUB+r"tasks/T\d{3,}[a-z]?\.status\.md", path))


def _owns(root, tag, path, source="worktree", allow_append=False):
    if tag not in events.TAGS:
        raise BadInput("Unknown agent tag")
    path = normalize(root, path)
    if _foreign(root, tag, path, source):
        return False
    if append_only(path):
        return allow_append
    if path in {HUB+"logs/"+t+".md" for t in events.TAGS}:
        return path == HUB+"logs/"+tag+".md"
    if path.startswith(HUB+"reviews/"):
        return bool(re.fullmatch(re.escape(HUB)+r"reviews/[^/]+_"+tag+r"\.md", path))
    task_match = re.fullmatch(re.escape(HUB)+r"tasks/(T\d{3,}[a-z]?)_[^/]+\.md", path)
    if task_match:
        task = task_match[1]
        owner, _, _, _ = _metadata(root, task, "HEAD")
        if owner is None:
            old = _blob(root, path)
            if old is not None:  # safe adapter until T014 supplies status files
                m = re.search(rb"^Owner:\s*\[?(claude|gpt|deepseek)\]?", old, re.M)
                return bool(m and m[1].decode() == tag)
            owner, creator, _, _ = _metadata(root, task, source)
            return owner == tag or creator == tag
        return owner == tag
    if path == HUB+"STATE.md" and _read_config(root)["state_maintainer"] is not None:
        return _read_config(root)["state_maintainer"] == tag
    return any(c["tag"] == tag and any(fnmatchcase(path, g) for g in c["scope"])
               for c in _claims(root, source))


def owns(tag, path):
    tx = getattr(_local, "tx", None)
    root = tx.root if tx is not None and tx.pid == os.getpid() else repo_root()
    appended = tx is not None and (str(path) in tx.appends or str(path) in tx.new)
    return _owns(root, tag, path, allow_append=appended)


def _decision_exists(root, identifier):
    text = events.without_comments((_blob(root, HUB+"DECISIONS.md") or b"").decode("utf-8"))
    blocks = re.findall(r"^###\s+"+re.escape(identifier)+r"\b[^\n]*\n(.*?)(?=^### |\Z)", text, re.M|re.S)
    if len(blocks) != 1:
        return False
    quote = re.search(r'^- quote:\s*"(.+)"\s*$',blocks[0],re.M)
    return bool(quote and quote[1].strip())


def _authorize_event(root, tag, path, record, prior, source="worktree"):
    if record["tag"] != tag:
        raise Unauthorized("Cannot write another agent's event")
    canonical = events.format_event(tag,record["event"],record["fields"],record["note"],timestamp=record["time"])
    if any(canonical == events.format_event(r["tag"],r["event"],r["fields"],r["note"],timestamp=r["time"]) for r in prior):
        raise BadInput("Duplicate event line")
    task = Path(path).name.split(".")[0]
    owner, reviewers, holder, base = None, [], None, None
    for old in prior:
        who, kind, fields = old["tag"], old["event"], old["fields"]
        if kind == "created" and owner is None:
            owner, reviewers = fields["owner"], fields["reviewers"]
            base = owner
        elif kind == "claim" and holder is None and owner in ("open", who) and who not in reviewers:
            owner, holder = who, who
        elif kind == "release" and who == holder:
            holder = None
            if base == "open":  # T-hub-028: a released open-base task is claimable by anyone again
                owner = "open"
    kind, fields = record["event"], record["fields"]
    permitted = False
    if kind == "created":
        permitted = owner is None
    elif kind == "claim":
        permitted = owner in ("open", tag) and holder is None and tag not in reviewers
        if permitted:
            for claim in _claims(root, source):
                if claim["tag"] != tag and any(events.scopes_overlap(a,b) for a in fields["scope"] for b in claim["scope"]):
                    raise Unauthorized("Scope overlaps " + claim["task"] + " held by " + claim["tag"])
    elif kind == "release":
        permitted = holder == tag
    elif kind in ("submit", "response"):
        permitted = owner == tag
    elif kind in ("review", "request_changes", "accept"):
        permitted = tag in reviewers and tag != owner
    elif kind == "disposition":
        permitted = fields["finding"].split("#")[0].endswith("_"+tag+".md")
    elif kind == "dispute":
        permitted = owner == tag or tag in reviewers  # full exchange check belongs to T013
    elif kind == "user":
        permitted = _decision_exists(root, fields["decision"])
        if "task" in fields:
            permitted &= fields["task"] == task
    if not permitted:
        raise Unauthorized("Unauthorized " + kind + " event in " + path)


def _entry(root, path, index=False):
    args = ("ls-files", "--stage", "-z", "--", path) if index else ("ls-tree", "-z", "HEAD", "--", path)
    p = git(root, *args, check=False)
    entries = [x for x in p.stdout.split(b"\0") if x]
    if not entries:
        return None
    if len(entries) != 1:
        raise DirtyTarget("Unmerged index target: " + path)
    meta, filename = entries[0].split(b"\t", 1)
    parts = meta.split()
    if index and parts[2] != b"0":
        raise DirtyTarget("Unmerged index target: " + path)
    if filename.decode() != path:
        raise BadInput("Path did not name one exact file")
    return (parts[0], parts[1] if index else parts[2])


def _clean(root, path):
    head, index = _entry(root, path), _entry(root, path, index=True)
    if head != index:
        raise DirtyTarget("Target has staged changes: " + path)
    target = root/path
    if head is None:
        if target.exists():
            raise DirtyTarget("Untracked target already exists: " + path)
        return b""
    if not target.is_file() or head[0] not in (b"100644", b"100755"):
        raise DirtyTarget("Target is not the HEAD regular file: " + path)
    before = _blob(root, path)
    mode = b"100755" if target.stat().st_mode & 0o111 else b"100644"
    if target.read_bytes() != before or mode != head[0]:
        raise DirtyTarget("Target has working-tree changes: " + path)
    return before


def _sha(data):
    return hashlib.sha256(data).hexdigest()


class Tx:
    def __init__(self, root, tag, task):
        self.root, self.tag, self.task = root, tag, task
        self.pid, self.thread = os.getpid(), threading.get_ident()
        self.appends, self.new = {}, {}
        self.active = True

    def _check(self):
        if not self.active or self.pid != os.getpid() or self.thread != threading.get_ident():
            raise BadInput("Transaction is not active in this thread/process")

    def append(self, path, line):
        self._check(); path = normalize(self.root, path)
        if not isinstance(line, str) or not line or "\0" in line:
            raise BadInput("Append needs nonempty text without NUL")
        if not _owns(self.root, self.tag, path, allow_append=True):
            raise Unauthorized("Not permitted to append " + path)
        before = _clean(self.root, path)
        if before and not before.endswith(b"\n"):
            raise BadInput("Clean append target needs a final newline")
        if path.endswith(".status.md"):
            parsed, warnings = events.parse_text(line, path)
            if warnings or len(parsed) != 1 or "\n" in line or "\r" in line:
                raise BadInput("Status append needs exactly one valid event")
            prior = events.parse_text(before.decode("utf-8",errors="surrogateescape"), path)[0]
            _authorize_event(self.root, self.tag, path, parsed[0], prior)
        after = before + (line+"\n").encode("utf-8")
        target = self.root/path
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("ab" if target.exists() else "xb") as f:
            f.write(after[len(before):]); f.flush(); os.fsync(f.fileno())
        if target.read_bytes() != after:
            raise DirtyTarget("Target changed during append: " + path)
        self.appends[path] = dict(before=_sha(before), after=_sha(after))

    def write_new(self, path, text):
        self._check(); path = normalize(self.root, path)
        if not isinstance(text, str):
            raise BadInput("New file text must be a string")
        target = self.root/path
        if target.exists():
            raise FileExistsError(path)
        if _foreign(self.root, self.tag, path, "worktree"):
            raise Unauthorized("Another agent claims " + path)
        task_card = bool(re.fullmatch(re.escape(HUB)+r"tasks/T\d{3,}[a-z]?_[^/]+\.md", path))
        if path.endswith(".status.md") and append_only(path):
            parsed, warnings = events.parse_text(text, path)
            if warnings or not parsed or parsed[0]["event"] != "created":
                raise BadInput("New status file must start with a valid created event")
            prior = []
            for record in parsed:
                _authorize_event(self.root, self.tag, path, record, prior)
                prior.append(record)
        elif not (task_card and _entry(self.root,path) is None) and not _owns(self.root, self.tag, path, allow_append=append_only(path)):
            raise Unauthorized("Not permitted to create " + path)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        with os.fdopen(fd, "wb") as f:
            data = text.encode("utf-8"); f.write(data); f.flush(); os.fsync(f.fileno())
        self.new[path] = _sha(data)

    def commit(self, paths, message):
        self._check()
        if isinstance(paths, (str, bytes)) or not paths or not isinstance(message, str) or not message.strip():
            raise BadInput("Commit needs concrete file paths and a message")
        paths = list(dict.fromkeys(normalize(self.root, p) for p in paths))
        if not paths:
            raise BadInput("Commit needs at least one file")
        for path in paths:
            if (self.root/path).is_dir():
                raise BadInput("Commit names files, not directories: " + path)
            if not _owns(self.root, self.tag, path, allow_append=path in self.appends or path in self.new):
                raise Unauthorized("Not permitted to commit " + path)
            if append_only(path) or path in self.appends:
                expected = self.appends.get(path)
                data = (self.root/path).read_bytes() if (self.root/path).is_file() else None
                if expected:
                    if _sha(_blob(self.root, path) or b"") != expected["before"] or data is None or _sha(data) != expected["after"] or _entry(self.root,path) != _entry(self.root,path,index=True):
                        raise DirtyTarget("Append changed before commit: " + path)
                    head_mode = _entry(self.root,path)
                    work_mode = b"100755" if (self.root/path).stat().st_mode & 0o111 else b"100644"
                    if work_mode != (head_mode[0] if head_mode is not None else b"100644"):
                        raise DirtyTarget("Append target mode changed: " + path)
                elif path not in self.new or data is None or _sha(data) != self.new[path]:
                    raise Unauthorized("Append-only path must use this transaction's append/write_new")
        if HUB+"hub_config.json" in paths:
            _check_config_change(self.root, "worktree")
        # New files need staging; --only excludes every unrelated staged path.
        new_paths = [p for p in paths if _entry(self.root,p) is None and (self.root/p).is_file()]
        if new_paths:
            git(self.root, "add", "--", *new_paths)
        environment = {"HUB_TX_APPENDS": json.dumps({p:self.appends[p] for p in paths if p in self.appends}),
                       "HUB_TX_NEW": json.dumps({p:self.new[p] for p in paths if p in self.new})}
        args = ["commit", "--only", "-m", f"[{self.tag}] {self.task}: {message}"]
        if _read_config(self.root)["author_per_agent"]:
            args += ["--author", f"{self.tag}-agent <{self.tag}@agents.local>"]
        git(self.root, *args, "--", *paths, env=environment)
        for path in paths:
            self.appends.pop(path, None); self.new.pop(path, None)
        return git(self.root, "rev-parse", "HEAD").stdout.decode().strip()


@contextmanager
def transaction(tag, task, *, timeout=None):
    if tag not in events.TAGS or not isinstance(task, str) or not events.TASK_RE.fullmatch(task):
        raise BadInput("Expected agent tag and T### task id")
    existing = getattr(_local, "tx", None)
    if existing is not None and existing.active and existing.pid == os.getpid():
        raise BadInput("Nested transactions are not permitted")
    try:
        timeout = float(os.environ.get("HUB_LOCK_TIMEOUT", "30") if timeout is None else timeout)
    except ValueError as exc:
        raise BadInput("Invalid lock timeout") from exc
    if not 0 <= timeout <= 30:
        raise BadInput("Lock timeout must be between 0 and 30 seconds")
    root = repo_root()
    git_dir = Path(git(root, "rev-parse", "--absolute-git-dir").stdout.decode().strip())
    with (git_dir/"hub.lock").open("a+b") as lock:
        deadline = time.monotonic()+timeout
        while True:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB); break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise Busy("Hub transaction busy; live holders are never displaced")
                time.sleep(min(.025, max(0, deadline-time.monotonic())))
        tx = Tx(root, tag, task); _local.tx = tx
        try:
            lock.seek(0); lock.truncate()
            lock.write(json.dumps(dict(tag=tag, task=task, pid=os.getpid(), time=now())).encode()); lock.flush()
            yield tx
        finally:
            tx.active = False; _local.tx = None
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            # Never unlink: waiters must keep locking the same inode.


def _check_config_change(root, source):
    raw = (root/(HUB+"hub_config.json")).read_bytes() if source == "worktree" else _blob(root,HUB+"hub_config.json","index")
    try:
        after = json.loads(raw)
    except (ValueError, TypeError, UnicodeError) as exc:
        raise BadInput("Invalid new config") from exc
    _validate_config(after)
    before = _read_config(root); effective = DEFAULTS | after
    latest = {}
    order = {sha:i for i,sha in enumerate(git(root,"rev-list","--reverse","HEAD",check=False).stdout.decode().splitlines())}
    for path, records, _ in events.task_records(root, "HEAD"):
        for record in records:
            f = record["fields"]
            if record["event"] == "user" and f["action"] == "set_config" and _decision_exists(root,f["decision"]):
                # Across files, append order comes from the serialized commits,
                # not filename sorting or wall clocks. Within one file use line
                # order; conflicting approvals in separate files in one commit
                # have no defined order and are refused.
                blame = git(root,"blame","--line-porcelain","-L",f"{record['line']},{record['line']}","HEAD","--",path).stdout
                sha = blame.split()[0].decode().lstrip("^")
                item = (order[sha],path,record["line"],f["value"])
                old = latest.get(f["key"])
                if old is not None and item[0] == old[0] and item[1] != old[1] and item[3] != old[3]:
                    raise Unauthorized("Conflicting same-commit config approvals: "+f["key"])
                if old is None or item[0] > old[0] or (item[0] == old[0] and item[1] == old[1] and item[2] > old[2]):
                    latest[f["key"]] = item
    for key in set(before) | set(effective):
        if before.get(key) != effective.get(key):
            try:
                value = json.loads(latest[key][3])
            except (KeyError, ValueError) as exc:
                raise Unauthorized("Config change needs committed user action=set_config: " + key) from exc
            if value != effective.get(key) or type(value) is not type(effective.get(key)):
                raise Unauthorized("Config value differs from user event: " + key)


def candidate_paths(root):
    raw = git(root, "diff", "--cached", "--name-status", "-z", "--find-renames").stdout.split(b"\0")
    result, i = [], 0
    while i < len(raw) and raw[i]:
        kind = raw[i].decode(); i += 1
        count = 2 if kind[0] in "RC" else 1
        for _ in range(count):
            result.append(raw[i].decode()); i += 1
    return list(dict.fromkeys(result))


def check_candidate(tag, root=None):
    """Read-only hook check under Git's inherited GIT_INDEX_FILE; no flock."""
    root = root or repo_root()
    try:
        proofs = json.loads(os.environ.get("HUB_TX_APPENDS", "{}"))
        new = json.loads(os.environ.get("HUB_TX_NEW", "{}"))
    except ValueError as exc:
        raise BadInput("Malformed transaction proof") from exc
    for path in candidate_paths(root):
        if not _owns(root, tag, path, "index", allow_append=path in proofs or path in new):
            raise Unauthorized("Candidate commit includes unowned path: " + path)
        if append_only(path) or path in proofs:
            before, after = _blob(root,path) or b"", _blob(root,path,"index")
            if path in proofs:
                proof = proofs[path]
                if after is None or _sha(before) != proof.get("before") or _sha(after) != proof.get("after") or not after.startswith(before) or len(after) <= len(before) or not after.endswith(b"\n"):
                    raise DirtyTarget("Candidate is not the exact authorized append: " + path)
            elif path in new:
                if _blob(root,path) is not None or after is None or _sha(after) != new[path]:
                    raise DirtyTarget("Candidate new append-only file differs: " + path)
            head_mode, index_mode = _entry(root,path), _entry(root,path,index=True)
            if index_mode is None or index_mode[0] != (head_mode[0] if head_mode else b"100644"):
                raise DirtyTarget("Append candidate changed file mode: " + path)
            if path.endswith(".status.md"):
                prior = events.parse_text(before.decode("utf-8",errors="surrogateescape"),path)[0]
                added, warnings = events.parse_text(after[len(before):].decode("utf-8"),path)
                if warnings:
                    raise BadInput("Malformed candidate status event")
                for record in added:
                    _authorize_event(root,tag,path,record,prior,"index"); prior.append(record)
        if path == HUB+"hub_config.json":
            _check_config_change(root,"index")


def scoped_tag(message):
    """The agent tag of a commit message subject (`[tag] ...`), or None for a user commit."""
    for line in message.splitlines():
        if line.startswith("#"):  # comment lines of an editor template
            continue
        match = re.match(r"\[(claude|gpt|deepseek)\]", line)
        return match[1] if match else None
    return None


def check_scoped(tag, root=None):
    """Read-only hook check of the scoped commit guard (commit_guard = "scoped").

    `tag` is the committing agent, or None for a user commit. Append-only integrity and status-line
    parsing apply to everyone; claims, foreign logs/reviews, task cards and the config apply to agent
    commits only; every other path is allowed. Claims are read once per commit."""
    root = root or repo_root()
    if tag is not None and tag not in events.TAGS:
        raise BadInput("Unknown agent tag")
    try:
        proofs = json.loads(os.environ.get("HUB_TX_APPENDS", "{}"))
        new = json.loads(os.environ.get("HUB_TX_NEW", "{}"))
    except ValueError as exc:
        raise BadInput("Malformed transaction proof") from exc
    paths = candidate_paths(root)
    claims = _live_claims(root) if tag is not None else []
    for path in paths:
        if append_only(path) or path in proofs:  # hub_append may append to any file it owns
            _scoped_append_only(root, tag, path, proofs, new)
        if tag is None:
            continue
        for claim in claims:
            if claim["tag"] != tag and any(fnmatchcase(path, g) for g in claim["scope"]):
                raise Unauthorized("Path is inside the active claim of " + claim["tag"] + " (" + claim["task"] + "): " + path)
        log = re.fullmatch(re.escape(HUB)+r"logs/(claude|gpt|deepseek)\.md", path)
        review = re.fullmatch(re.escape(HUB)+r"reviews/[^/]+_(claude|gpt|deepseek)\.md", path)
        if (log or review) and (log or review)[1] != tag:
            raise Unauthorized("Another agent's file: " + path)
        card = re.fullmatch(re.escape(HUB)+r"tasks/(T\d{3,}[a-z]?)_[^/]+\.md", path)
        if card:
            _scoped_card(root, tag, card[1], path)
        if path == HUB+"hub_config.json":
            _check_config_change(root, "index")


def _live_claims(root):
    """Claims that still guard their files: those of tasks not yet accepted or cancelled.

    A claim lasts until it is released, also after its task is accepted; README §4 protects the
    files of a *claimed* task, so the scoped guard ignores the claims of finished tasks.

    T046 F2: hub_state.derive reads the status file and the review files from the working tree, and
    the hook validates only what is staged.  A task counts as finished only when every file its
    derivation read (status file, review files) has the same bytes in the working tree as in the
    candidate index, so an unstaged status line or review edit cannot release a committed claim.
    (Deliverables do not matter here: derive hashes them at HEAD.)
    T046 round 3 (F8): "every file its derivation read" is recorded by hub_state.recording_reads,
    so it also covers the dependency tasks' status and review files and DECISIONS.md (D### prerequisites)."""
    import hub_state  # lazy: hub_state does not import hubtx
    base = os.path.abspath(root)
    states = {}
    live = []
    for claim in _claims(root, "index"):
        task = claim["task"]
        if task not in states:
            try:
                with hub_state.recording_reads() as reads:
                    d = hub_state.derive(root, task)
                finished = d["accepted"] or d["state"] in ("accepted", "done", "cancelled")
                read = {d["status_path"], *d["review_files"], *d["review_files_all"]}
                read |= {Path(os.path.relpath(p, base)).as_posix() for p in reads}
                # a read outside the repository cannot be checked against the index: keep the claim
                states[task] = not (finished and all(not p.startswith("../") and _staged_as_on_disk(root, p)
                                                     for p in read))
            except Exception:  # an unreadable task keeps its claim (the safe side)
                states[task] = True
        if states[task]:
            live.append(claim)
    return live


def _staged_as_on_disk(root, path):
    """True when `path` has the same bytes in the candidate index as in the working tree
    (both absent counts as the same)."""
    disk = Path(root) / path
    return (disk.read_bytes() if disk.is_file() else None) == _blob(root, path, "index")


def _scoped_append_only(root, tag, path, proofs, new):
    before, after = _blob(root, path) or b"", _blob(root, path, "index")
    if after is None:
        raise DirtyTarget("Append-only file removed or renamed: " + path)
    if not after.startswith(before) or (after != before and not after.endswith(b"\n")):
        raise DirtyTarget("Append-only file does not extend HEAD (or lacks a final newline): " + path)
    if before and after != before and not before.endswith(b"\n"):
        raise DirtyTarget("Append-only file continues an unterminated last line: " + path)
    if path in proofs:
        proof = proofs[path]
        if _sha(before) != proof.get("before") or _sha(after) != proof.get("after") or len(after) <= len(before):
            raise DirtyTarget("Candidate is not the exact authorized append: " + path)
    elif path in new:
        if _blob(root, path) is not None or _sha(after) != new[path]:
            raise DirtyTarget("Candidate new append-only file differs: " + path)
    head_mode, index_mode = _entry(root, path), _entry(root, path, index=True)
    if index_mode is None or index_mode[0] != (head_mode[0] if head_mode else b"100644"):
        raise DirtyTarget("Append candidate changed file mode: " + path)
    if path.endswith(".status.md"):
        prior = events.parse_text(before.decode("utf-8", errors="surrogateescape"), path)[0]
        added, warnings = events.parse_text(after[len(before):].decode("utf-8", errors="surrogateescape"), path)
        if warnings:
            raise BadInput("Malformed candidate status event in " + path)
        if not before and (not added or added[0]["event"] != "created"):
            raise BadInput("New status file must start with a valid created event: " + path)
        for record in added:
            if tag is not None:
                _authorize_event(root, tag, path, record, prior, "index")
            prior.append(record)


def _scoped_card(root, tag, task, path):
    owner, creator, _, _ = _metadata(root, task, "index")
    old = _blob(root, path)
    # T046 F5: a legacy card (in HEAD, no status file in HEAD) keeps its `Owner:` line as the
    # authority; a status file added in the same commit cannot make the committer its owner.
    # T046 round 3 (F7): nor one added in an earlier commit, after the card was already committed.
    named = re.search(rb"^Owner:\s*\[?(claude|gpt|deepseek)\]?", old, re.M) if old is not None else None
    if named and (owner is None or _legacy_card(root, task, path)):
        if named[1].decode() != tag:
            raise Unauthorized("Only the owner may change " + path)
        return
    if owner is not None:  # a card with no named owner falls back to its status file
        if tag in (owner, creator):
            return
        raise Unauthorized("Only the owner or the creator may change " + path)
    # a new card with no status file yet, or an old one that names no owner: allowed, as before


def _legacy_card(root, task, path):
    """True when the card at `path` (in HEAD) was committed before its task had a status file:
    the status file is not in HEAD, or the commit that first added it already had the card.
    new_task.py commits a card and its status file together, so its cards are never legacy."""
    status = _status_path(task)
    if _blob(root, status) is None:
        return True
    added = git(root, "log", "--diff-filter=A", "--format=%H", "HEAD", "--", status, check=False).stdout.split()
    if not added:
        return False
    first = added[-1].decode()
    return git(root, "cat-file", "-e", first + "^:" + path, check=False).returncode == 0


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise BadInput(message)


def cli(operation):
    try:
        operation(); return 0
    except HubError as exc:
        print(str(exc), file=sys.stderr); return exc.exit_code
    except FileExistsError as exc:
        print(str(exc), file=sys.stderr); return 6
    except (ValueError, TypeError) as exc:
        print(str(exc), file=sys.stderr); return 6
    except OSError as exc:
        print(str(exc), file=sys.stderr); return 5
