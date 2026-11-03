"""T013: derive protocol-v2 task state from append-only status events and reviews.

Read-only over the hub tree. Writers (`new_task.py`, `claim.py`) use hubtx's
transaction directly; this module supplies the lifecycle, revision, review-table,
finding and queue logic. The grammar belongs to hub_events.py (T012).

Public API:
    load_tasks(repo) -> dict[str, Task]
    derive(repo, task) -> dict          # one task's full state
    status_view(repo, tag=None) -> dict # everything hub_status.py prints
    parse_review(path) -> list[dict]    # review-table rows
    revision(repo, deliverables) -> str # sha256 over sorted path/NUL/hash pairs
    queue(repo, tag) -> dict
    new_since(repo, tag) -> dict
    open_findings(repo, task, events=None, review_files=None) -> list[dict]
        # a blocking row closes by its author's `disposition resolved`, by the author's accept
        # (T054, user decision 2026-11-03: the latest verdict is an accept, no later user `open` or
        # `upheld`), or, UNVERIFIABLE only, by `user action=accept_limitation`; see closed_by

Only standard library. No writes, no lock, no network.
"""
from __future__ import annotations

import contextlib
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import subprocess

import hub_events as events

HUB = "docs/agent_hub/"
STATUS_RE = re.compile(re.escape(HUB) + r"tasks/(T[0-9]{3,}[a-z]?)\.status\.md")
CARD_RE = re.compile(re.escape(HUB) + r"tasks/(T[0-9]{3,}[a-z]?)_[^/]+\.md")
REVIEW_RE = re.compile(re.escape(HUB) + r"reviews/[^/]+_([a-z]+)\.md")
VERDICTS = ("CONFIRMED", "WRONG", "UNVERIFIED", "UNVERIFIABLE")
BLOCKING_VERDICTS = frozenset(("WRONG", "UNVERIFIED", "UNVERIFIABLE"))
SEVERITIES = ("blocking", "informational")
STALE_HOURS = 12.0


# ---------------------------------------------------------------- files/ids
_READS = None  # T046 F8: while recording, the absolute path of every hub file a derivation reads


@contextlib.contextmanager
def recording_reads():
    """Collect the absolute path of every file read (or looked for) inside the block.

    hubtx._live_claims uses it to trust a derived "finished" state only when every input the
    derivation read, dependencies and DECISIONS.md included, is staged as it is on disk."""
    global _READS
    outer, _READS = _READS, set()
    try:
        yield _READS
    finally:
        if outer is not None:
            outer |= _READS
        _READS = outer


def _note_read(path):
    if _READS is not None:
        _READS.add(os.path.abspath(path))


def read_text(path):
    _note_read(path)
    try:
        return Path(path).read_bytes().decode("utf-8", errors="surrogateescape")
    except FileNotFoundError:
        return ""


def status_path(task):
    return HUB + "tasks/" + task + ".status.md"


def task_ids(repo):
    """Every task id mentioned by a status file or a task card."""
    repo = Path(repo)
    ids = set()
    for pattern in ("tasks/T*.status.md", "tasks/T*_*.md"):
        for path in (repo / HUB).glob(pattern):
            match = re.match(r"(T[0-9]{3,}[a-z]?)", path.name)
            if match:
                ids.add(match[1])
    return sorted(ids)


def next_task_id(repo):
    nums = [int(re.match(r"T([0-9]+)", t)[1]) for t in task_ids(repo)]
    return "T%03d" % (max(nums, default=0) + 1)


def _git(repo, *args):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True)
    return result.stdout if result.returncode == 0 else b""


# ---------------------------------------------------------------- revision
def _head_bytes(repo, path):
    result = subprocess.run(["git", "-C", str(repo), "show", "HEAD:" + path], capture_output=True)
    return result.stdout if result.returncode == 0 else b""


def revision(repo, deliverables, source="HEAD"):
    """sha256 over sorted (path, sha256(file bytes)) pairs, as plan §2.1.

    The default source is HEAD, so the acceptance state depends on committed
    deliverables only; an uncommitted edit cannot flip an accepted task.  Callers
    that intentionally hash the working tree can pass source="worktree".
    Missing deliverables hash as the empty file and are reported by callers.
    """
    repo = Path(repo)
    digest = hashlib.sha256()
    for value in sorted(deliverables):
        data = _head_bytes(repo, value) if source == "HEAD" else (
            (repo / value).read_bytes() if (repo / value).is_file() else b"")
        digest.update(value.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(data).hexdigest().encode("ascii"))
    return digest.hexdigest()


def missing_deliverables(repo, deliverables):
    repo = Path(repo)
    return [value for value in deliverables if not (repo / value).is_file()]


# ---------------------------------------------------------------- reviews
def _cells(line):
    return [cell.strip().strip("*").strip() for cell in line.strip().strip("|").split("|")]


def _is_separator(line):
    text = line.strip()
    if not text.startswith("|"):
        return False
    return all(re.fullmatch(r":?-{2,}:?", cell.replace(" ", "")) for cell in text.strip("|").split("|"))


def _header_index(header):
    looking = [cell.lower() for cell in header]
    index = {}
    for key in ("id", "claim", "verdict", "severity", "evidence"):
        for i, cell in enumerate(looking):
            first = cell.split("(")[0].strip()
            if key == "id":
                match = re.fullmatch(r"id", first)
            else:
                match = first.startswith(key)
            if match:
                index[key] = i
                break
    return index


def parse_review(path):
    """Parse every Markdown review table that has a Verdict column.

    New rows carry ID and Severity; legacy rows without them fall back to
    `F<data-row>` and to "WRONG -> blocking, otherwise informational" (plan §2.3).
    Verdict cells may hold prose; the first whole verdict token wins. Bold/plain
    are equivalent. Rows with no verdict token are ignored (not findings).
    """
    lines = read_text(path).splitlines()
    rows, header, index = [], None, {}
    in_table, data_row = False, 0
    for number, line in enumerate(lines, 1):
        if line.lstrip().startswith("|"):
            if not in_table:
                candidate = _cells(line)
                candidate_index = _header_index(candidate)
                if "verdict" in candidate_index:
                    header, index = candidate, candidate_index
                    in_table, data_row = True, 0
                continue
            if _is_separator(line):
                continue
            if in_table and "verdict" in index:
                cells = _cells(line)
                if len(cells) < len(header):
                    cells += [""] * (len(header) - len(cells))
                verdict_cell = cells[index["verdict"]] if index["verdict"] < len(cells) else ""
                verdict = None
                for token in VERDICTS:
                    if re.search(r"\b" + token + r"\b", verdict_cell, re.IGNORECASE):
                        verdict = token
                        break
                if verdict is None:
                    continue
                data_row += 1
                identifier = cells[index["id"]] if "id" in index and index["id"] < len(cells) else ""
                identifier = identifier.strip().lstrip("#")
                if not identifier:
                    identifier = "F%d" % data_row
                severity = ""
                if "severity" in index and index["severity"] < len(cells):
                    severity = cells[index["severity"]].lower()
                if "blocking" in severity:
                    severity = "blocking"
                elif "informational" in severity:
                    severity = "informational"
                elif verdict == "WRONG":
                    severity = "blocking"   # legacy fallback (plan §2.3)
                else:
                    severity = "informational"
                rows.append(dict(id=identifier, claim=cells[index["claim"]] if "claim" in index else "",
                                 verdict=verdict, severity=severity,
                                 evidence=cells[index["evidence"]] if "evidence" in index else "",
                                 line=number, file=str(path)))
        else:
            in_table, header, index, data_row = False, None, {}, 0
    return rows


def _review_files(status_events):
    files = []
    for record in status_events:
        if record["event"] in ("review", "request_changes") and "file" in record["fields"]:
            if record["fields"]["file"] not in files:
                files.append(record["fields"]["file"])
    return files


def review_author(path):
    match = re.search(r"_([a-z]+)\.md$", Path(path).name)
    return match[1] if match and match[1] in events.TAGS else None


def _accept_closes(status_events, author, key):
    """T054 (user decision 2026-11-03): does `author`'s accept close its own finding `key`?

    True when the author's latest verdict event (accept or request_changes) is an accept, no user
    `open` follows it (T-hub-028: the user's "not OK" voids earlier accepts) and the author has not
    upheld the finding since.  "After" is file order in the status file, not a timestamp.
    """
    last = None
    for position, record in enumerate(status_events):
        if record["tag"] == author and record["event"] in ("accept", "request_changes"):
            last = position if record["event"] == "accept" else None
    if last is None:
        return False
    for record in status_events[last + 1:]:
        fields = record["fields"]
        if record["event"] == "user" and fields.get("action") == "open":
            return False
        if (record["event"] == "disposition" and record["tag"] == author
                and fields.get("result") == "upheld" and fields.get("finding") == key):
            return False
    return True


def open_findings(repo, status_events, review_files=None):
    """Blocking findings of one task with their closure (plan §2.3; T054 added "accept").

    Each finding carries closed_by: "disposition" (its review's author wrote `disposition resolved`),
    "accept" (T054: the author's latest verdict is an accept, see _accept_closes; WRONG and UNVERIFIED
    rows only), "limitation" (UNVERIFIABLE row, `user action=accept_limitation`) or None (open).
    """
    files = list(dict.fromkeys(review_files if review_files is not None else _review_files(status_events)))
    resolved_by, limited = [], set()
    responses = set()
    for record in status_events:
        fields = record["fields"]
        if record["event"] == "response":
            responses.add(fields["finding"])
        elif record["event"] == "disposition" and fields["result"] == "resolved":
            resolved_by.append((record["tag"], fields["finding"]))
        elif record["event"] == "user" and fields["action"] == "accept_limitation":
            limited.add(fields["finding"])
    found = []
    for name in files:
        path = Path(repo) / name
        _note_read(path)  # also when absent: an unstaged deletion must not go unseen
        if not path.is_file():
            continue
        author = review_author(name)
        for row in parse_review(path):
            if row["verdict"] not in BLOCKING_VERDICTS or row["severity"] != "blocking":
                continue
            key = name + "#" + row["id"]
            closed_by = None
            if row["verdict"] == "UNVERIFIABLE":
                if key in limited:
                    closed_by = "limitation"
            elif any(tag == author and finding == key for tag, finding in resolved_by):
                closed_by = "disposition"
            elif _accept_closes(status_events, author, key):
                closed_by = "accept"  # T054 (user decision 2026-11-03): a reviewer's accept closes its own findings
            found.append(dict(finding=key, file=name, id=row["id"], verdict=row["verdict"],
                              severity=row["severity"], closed=bool(closed_by), closed_by=closed_by,
                              responded=key in responses, claim=row["claim"], line=row["line"]))
    return found


# ---------------------------------------------------------------- lifecycle
def _load_status(repo, task):
    path = Path(repo) / status_path(task)
    _note_read(path)
    if not path.is_file():
        return [], ["missing status file for " + task]
    return events.parse_file(path)


def _timestamp(record):
    try:
        return datetime.fromisoformat(record["time"].replace("Z", "+00:00"))
    except ValueError:
        return None


def _effective_owner(base_owner, reviewer_tags, status_events):
    """An open task is owned by its current holder; the holder's release returns it to "open".

    T-hub-028: before, a release kept the first claimant as owner for good, so a timed-out
    wake could never be taken over. A task with a named base owner keeps that owner.
    """
    if base_owner != "open":
        return base_owner
    owner, holder = "open", None
    for record in status_events:
        tag = record["tag"]
        if record["event"] == "claim" and holder is None and tag not in reviewer_tags and tag in events.TAGS:
            owner = holder = tag
        elif record["event"] == "release" and tag == holder:
            owner, holder = "open", None
    return owner


_STACK = set()


def _actors(value):
    """Split a derived next-actor string into agent tags (ignore none/any)."""
    value = re.sub(r"\s*\(via [^)]*\)", "", value or "")
    return [part for part in value.split(",") if part in events.TAGS]


def derive(repo, task):
    """Derive one task's state, acceptance, findings, dependencies and next actor.

    A dependency cycle is reported as blocked instead of recursing forever.
    """
    key = (str(Path(repo).resolve()), task)
    if key in _STACK:
        return dict(task=task, state="blocked", owner=None, base_owner=None, reviewers=[],
                    required=None, kind=None, deliverables=[], depends_on=[], initial=None,
                    revision=None, accepted_revision=None, accepted=False, claims=[],
                    next_actor="user", blocked_by=["cycle"], open_findings=[], all_findings=[],
                    warnings=["dependency cycle"], status_path=status_path(task), events=[],
                    review_files=[], review_files_all=[], submitted_revision=None, latest_accepts={},
                    stale_claims=[], holder=None, claim_since=None, last_review_file=None,
                    depends_on_met=False, next_via=["dependency cycle"])
    _STACK.add(key)
    try:
        return _derive(repo, task)
    finally:
        _STACK.discard(key)


def _derive(repo, task):
    repo = Path(repo)
    status_events, warnings = _load_status(repo, task)
    created = next((r for r in status_events if r["event"] == "created"), None)
    if created is None:
        card = next(iter(sorted((repo / HUB).glob("tasks/" + task + "_*.md"))), None)
        owner, state, header_reviewers = "open", "unknown", []
        if card is not None:
            text = read_text(card)
            match = re.search(r"Owner:\s*([^·\n]+)", text)
            if match:
                owner = match[1].strip().strip("[]").split()[0]
            match = re.search(r"Reviewer:\s*([^·\n]+)", text)
            if match:
                header_reviewers = [x for x in re.findall(r"[a-z]+", match[1]) if x in events.TAGS]
            match = re.search(r"Status:\s*([^·\n]+)", text)
            if match:
                state = match[1].strip().split()[0].lower()
        review_files = sorted(p.relative_to(repo).as_posix() for p in (repo / HUB / "reviews").glob("*_" + task + "_*.md"))
        reviewers = list(header_reviewers)
        for name in review_files:
            match = re.search(r"_([a-z]+)\.md$", name)
            if match and match[1] in events.TAGS and match[1] not in reviewers:
                reviewers.append(match[1])
        pending = [tag for tag in reviewers
                   if not any(re.search(r"_" + task + "_" + tag + r"\.md$", name) for name in review_files)]
        if state in ("review", "changes_requested"):
            next_actor = ",".join(pending) if pending else (owner if owner != "open" else "user")
        elif state == "open":
            next_actor = "any" if owner == "open" else owner
        elif state in ("accepted", "done", "cancelled"):
            next_actor = "none"
        elif state == "claimed" or state not in ("proposed", "awaiting_user", "disputed"):
            next_actor = owner if owner != "open" else "user"
        else:
            next_actor = "user"
        return dict(task=task, state=state, owner=owner, base_owner=owner, reviewers=reviewers,
                    required=None, kind=None, deliverables=[], depends_on=[], initial=None,
                    revision=None, accepted_revision=None, accepted=False, claims=[],
                    next_actor=next_actor, next_via=[],
                    blocked_by=[], open_findings=[], all_findings=[],
                    warnings=warnings + ["legacy task card without a status file"],
                    status_path=status_path(task), events=status_events, review_files=review_files,
                    review_files_all=review_files,
                    submitted_revision=None, latest_accepts={}, stale_claims=[], holder=None,
                    claim_since=None, last_review_file=None, depends_on_met=True)

    fields = created["fields"]
    base_owner, reviewers = fields["owner"], list(fields["reviewers"])
    required, kind = fields["required"], fields["kind"]
    deliverables, depends_on, initial = list(fields["deliverables"]), list(fields["depends_on"]), fields["initial"]
    state = initial
    claims, holder, claim_since = [], None, None
    submit_rev = accepted_rev = None
    latest_accepts = {}
    last_review_file = None
    for record in status_events:
        kind_event, tag, f = record["event"], record["tag"], record["fields"]
        if kind_event == "claim" and holder is None and base_owner in ("open", tag) and tag not in reviewers:
            holder, claim_since = tag, record["time"]
            claims.append(dict(tag=tag, scope=list(f["scope"]), time=record["time"], line=record["line"]))
            state = "claimed"
        elif kind_event == "release" and holder == tag:
            holder, claim_since = None, None
            state = "open"
        elif kind_event == "submit":
            submit_rev = f["rev"]
            state = "review"
        elif kind_event == "request_changes":
            last_review_file = f["file"]
            state = "changes_requested"
            # T055 (user decision 2026-11-03): the reviewer's latest verdict counts, so a later
            # request_changes withdraws its earlier accept (as _accept_closes does for findings).
            latest_accepts.pop(tag, None)
        elif kind_event == "review":
            last_review_file = f["file"]
        elif kind_event == "accept":
            latest_accepts[tag] = f["rev"]
        elif kind_event == "dispute":
            state = "disputed"
        elif kind_event == "user":
            action = f["action"]
            if action == "open":
                state = "open"
                latest_accepts.clear()  # T-hub-028: the user's "not OK" voids earlier accepts
            elif action == "cancel":
                state = "cancelled"
                holder, claim_since = None, None
            elif action == "resolve_dispute":
                state = f["to"]
    owner = _effective_owner(base_owner, set(reviewers), status_events)
    current = revision(repo, deliverables)
    findings = open_findings(repo, status_events)
    open_blocking = [finding for finding in findings if not finding["closed"]]
    accept_revs = {tag: latest_accepts[tag] for tag in reviewers if tag in latest_accepts}
    candidate = None
    if reviewers and not open_blocking:
        values = set(accept_revs.values())
        if required == "all" and len(accept_revs) == len(reviewers) and len(values) == 1:
            candidate = next(iter(values))
        elif required == "any" and accept_revs:
            if current in values:
                candidate = current
            else:
                candidate = accept_revs[sorted(accept_revs)[0]]
    accepted = bool(candidate and candidate == current and not open_blocking)
    if accepted:
        accepted_rev = current
        state = "accepted"
    elif candidate and candidate != current:
        accepted_rev = candidate
        state = "review (deliverables changed)"
    # unmet dependencies: show the dependency's next actor(s), not just its id
    blocked_by, blocked_actors = [], []
    for dependency in depends_on:
        if re.fullmatch(r"T[0-9]{3,}[a-z]?", dependency):
            dep = derive(repo, dependency)
            if not dep["accepted"]:
                blocked_by.append(dependency)
                actors = _actors(dep["next_actor"])
                blocked_actors.extend(actors or ["any"])
        elif re.fullmatch(r"D[0-9]{3,}", dependency):
            if not _decision_recorded(repo, dependency):
                blocked_by.append(dependency)
                blocked_actors.append("user")
    if blocked_by and state not in ("cancelled",):
        state = "blocked"
        accepted = False
    # next actor
    next_via = []
    submit_due = False   # T049 F1/F2: the owner must submit again (set only in the review branch)
    if state == "cancelled":
        next_actor = "none"
    elif blocked_by:
        next_via = list(blocked_by)
        next_actor = ",".join(dict.fromkeys(blocked_actors)) or "none"
    elif state in ("proposed", "awaiting_user", "disputed"):
        next_actor = "user"
    elif state in ("open",):
        next_actor = "any" if owner == "open" else owner
    elif state in ("claimed", "changes_requested"):
        next_actor = owner
    elif state.startswith("review"):
        pending = [tag for tag in reviewers if latest_accepts.get(tag) != current]
        if submit_rev and submit_rev != current and owner and owner != "open":
            # T048: only a submit asks for review.  The deliverables moved on after it, so the
            # owner has to submit again; the reviewers are not asked about unsubmitted bytes.
            # `submit_due` keeps this case apart from "a blocking finding is open" (T049 F1).
            next_actor = owner
            submit_due = True
        elif pending:
            next_actor = ",".join(pending)
        elif open_blocking:
            next_actor = owner if owner and owner != "open" else ",".join(
                dict.fromkeys(review_author(f["file"]) for f in open_blocking if review_author(f["file"]))) or "user"
        else:
            next_actor = "none"
    elif state == "accepted":
        next_actor = "none"
    else:
        next_actor = "user"
    # stale claim warning (plan row 6): quiet owner never loses the claim
    stale = []
    if holder and claim_since:
        try:
            started = datetime.fromisoformat(claim_since.replace("Z", "+00:00"))
            last = _last_tag_commit(repo, holder)
            quiet = (datetime.now(timezone.utc) - (last or started)).total_seconds() / 3600
            if quiet > STALE_HOURS:
                stale.append(dict(tag=holder, hours=round(quiet, 1),
                                  note="claim is stale; only the holder's release or a user ruling ends it"))
        except ValueError:
            pass
    if not reviewers and required:
        warnings.append("no reviewers declared")
    if missing_deliverables(repo, deliverables):
        warnings.append("missing deliverables: " + ",".join(missing_deliverables(repo, deliverables)))
    if submit_rev and submit_rev != current:
        warnings.append("deliverables changed since submit")
    announced = set(_review_files(status_events))
    all_review_files = sorted(p.relative_to(repo).as_posix()
                              for p in (repo / HUB / "reviews").glob("*_" + task + "_*.md"))
    for extra in all_review_files:
        if extra not in announced:
            warnings.append("unannounced review file: " + extra)
    return dict(task=task, state=state, owner=owner, base_owner=base_owner, reviewers=reviewers,
                required=required, kind=kind, deliverables=deliverables, depends_on=depends_on,
                initial=initial, revision=current, accepted_revision=accepted_rev,
                accepted=bool(accepted), claims=claims, next_actor=next_actor, next_via=next_via,
                blocked_by=blocked_by, open_findings=open_blocking, all_findings=findings,
                warnings=warnings, status_path=status_path(task), events=status_events,
                review_files=sorted(announced), review_files_all=all_review_files,
                submitted_revision=submit_rev, submit_due=submit_due,
                latest_accepts=latest_accepts, stale_claims=stale, holder=holder,
                claim_since=claim_since, last_review_file=last_review_file,
                depends_on_met=not blocked_by)


def _last_tag_commit(repo, tag):
    text = _git(repo, "log", "--all", "--format=%ct %s", "-n", "2000").decode(errors="replace")
    for line in text.splitlines():
        stamp, _, subject = line.partition(" ")
        if subject.startswith("[" + tag + "]"):
            return datetime.fromtimestamp(int(stamp), timezone.utc)
    return None


def _decision_recorded(repo, identifier):
    text = read_text(Path(repo) / HUB / "DECISIONS.md")
    return bool(re.search(r"^###\s+" + re.escape(identifier) + r"\b", text, re.M))


def load_tasks(repo):
    return {task: derive(repo, task) for task in task_ids(repo)}


def duplicate_ids(repo):
    """Task ids with more than one task card (status files do not count)."""
    repo = Path(repo)
    counts = {}
    for path in (repo / HUB).glob("tasks/T*_*.md"):
        match = re.match(r"(T[0-9]{3,}[a-z]?)_", path.name)
        if match:
            counts.setdefault(match[1], []).append(path.name)
    return {task: names for task, names in counts.items() if len(names) > 1}


def broken_links(repo):
    """Local Markdown links in the hub that do not resolve."""
    repo = Path(repo)
    from urllib.parse import unquote
    broken = []
    for path in sorted((repo / HUB).rglob("*.md")):
        body = path.read_text(errors="replace")
        body = re.sub(r"```[\s\S]*?```", "", body)
        body = re.sub(r"`[^`\n]+`", "", body)
        for target in re.findall(r"\]\(([^)]+)\)", body):
            if "://" in target or target.startswith("#"):
                continue
            dest = unquote(target.split("#")[0].strip("<>"))
            if dest and not (path.parent / dest).exists():
                broken.append(dict(file=path.relative_to(repo).as_posix(), target=target))
    return broken


# ---------------------------------------------------------------- queue/cursor
def _log_entries(repo, tag):
    text = read_text(Path(repo) / HUB / "logs" / (tag + ".md"))
    parts = re.split(r"^## ", text, flags=re.M)
    return ["## " + part for part in parts if part.strip()]


def log_entries(repo, tag):
    return _log_entries(repo, tag)


def _seen_through(repo, tag):
    entries = _log_entries(repo, tag)
    if not entries:
        return None
    matches = re.findall(r"seen_through=([0-9a-f]{40})", entries[-1])
    return matches[-1] if matches else None


def _is_ancestor(repo, ancestor):
    result = subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", ancestor, "HEAD"],
                            capture_output=True)
    return result.returncode == 0


def new_since(repo, tag):
    """Hub commits after the snapshot this agent actually read (`seen_through`)."""
    repo = Path(repo)
    seen = _seen_through(repo, tag)
    if seen is None or not _is_ancestor(repo, seen):
        commits = _git(repo, "log", "--format=%h %s", "HEAD", "--", HUB).decode(errors="replace").splitlines()
        return dict(tag=tag, seen_through=None, missing=True, commits=commits,
                    note="missing or non-ancestor seen_through: all hub commits count as new")
    commits = _git(repo, "log", "--format=%h %s", seen + "..HEAD", "--", HUB).decode(errors="replace").splitlines()
    return dict(tag=tag, seen_through=seen, missing=False, commits=commits)


def _questions(repo):
    """Parse QUESTIONS.md blocks (heading plus body) and answer lines."""
    text = read_text(Path(repo) / HUB / "QUESTIONS.md")
    pattern = re.compile(r"^###\s+(Q[0-9]{3,})\b([^\n]*)\n((?:(?!^###\s).)*)", re.M | re.S)
    questions = {}
    for match in pattern.finditer(text):
        qid, heading, body = match[1], match[2], match[3]
        asked = re.search(r"asked by \[([a-z]+)\]", heading)
        answers = re.findall(r"-\s+" + qid + r"\s+answered by (D[0-9]{3,})", body)
        questions[qid] = dict(qid=qid, asked_by=asked[1] if asked else None, answered_by=answers,
                              heading=heading.strip(), body=body.strip())
    return questions


def queue(repo, tag):
    """Tasks X must act on, findings awaiting X's response, and answered questions X asked."""
    tasks = load_tasks(repo)
    todo = []
    for task in tasks.values():
        if task["next_actor"] == tag or (tag and tag in task["next_actor"].split(",")):
            todo.append(dict(task=task["task"], state=task["state"], next_actor=task["next_actor"]))
        if task["owner"] == tag:
            for finding in task["open_findings"]:
                if not finding["responded"]:
                    todo.append(dict(task=task["task"], state="finding", finding=finding["finding"]))
    answered = [dict(qid=qid, answered_by=q["answered_by"]) for qid, q in _questions(repo).items()
                if q["asked_by"] == tag and q["answered_by"]]
    return dict(tag=tag, tasks=todo, answered_questions=answered)


def status_view(repo, tag=None):
    repo = Path(repo)
    tasks = load_tasks(repo)
    view = dict(tasks=tasks, duplicates=duplicate_ids(repo), broken_links=broken_links(repo),
                queue=queue(repo, tag) if tag else None,
                new=new_since(repo, tag) if tag else None,
                head=_git(repo, "rev-parse", "HEAD").decode().strip() or None,
                questions=_questions(repo))
    return view
