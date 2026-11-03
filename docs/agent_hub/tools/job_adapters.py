#!/usr/bin/env python3
"""Adapter layer between the job board (T023 phase 1) and the hub's task state.

Everything the job board needs from the hub (lock, next actions, open entries,
claims) goes through the functions at the bottom of this file.  Two backends sit
behind them: the phase-1 stub (JSON fixtures under docs/agent_hub/.job_fixture/,
the default) and the phase-2 "hub" backend (hub_config.json {"job_backend":
"hub"}), which reads task state with T013's hub_state.py and writes only through
the T012/T013 command-line tools (claim.py, hub_append.py).  The rest of the job
board does not change.

Known limits (reviewed by [gpt] as informational, T023 round 2):
  * F4: the time-window job-membership rule (a job with match "time": a task without a
    `job` field in its `created` event, created between JOB.started and JOB.ended)
    compares whole seconds.  Task `created` events carry second resolution while
    JOB.started has milliseconds, so a task created in the job-start second counts as a
    job task even if it preceded the start.  A job with match "id" (every job started by
    the multi-job job.py, T038) sees only the tasks whose `created` event names it.
  * F5: the no-CLI-under-lock guard (lock_depth) only sees ja.lock() sections of the
    current thread.  An outer hubtx.transaction opened by other code around a backend
    write is not detected; call backend writes outside every hub lock.
Wake admission in job_dispatcher re-reads JOB.json and the pause state under
lock(), the same lock `job.py end` takes, so `job end` and a new wake are serialised.

Several jobs may be active at once (T038).  JOB.json holds {"schema": "jobs-1", "jobs":
[...]} and .job_state.json {"schema": "job-state-1", "jobs": {id: state}}; the legacy
single-record files are still read (see load_jobs, load_state, migrate).

The top half of the file holds small helpers shared by job.py, job_dispatcher.py
and board.py (paths, atomic JSON files, limits, time).  Python standard library
only.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------

DEFAULT_LIMITS = {
    "max_tasks_posted": 10,
    "max_wakes_per_agent_per_hour": 6,
    "max_wakes_per_job": 40,
    "max_wall_s_per_wake": 1800,
    "max_tokens_per_agent": 3000000,
}

LOCK_TIMEOUT_S = 30.0


class Busy(Exception):
    """The hub lock could not be taken within the timeout."""


def find_repo(start: str | os.PathLike | None = None) -> Path:
    """Walk up from `start` (default: cwd) to the directory that holds .git."""
    p = Path(start if start is not None else Path.cwd()).resolve()
    for cand in [p, *p.parents]:
        if (cand / ".git").exists():
            return cand
    raise SystemExit(f"error: no .git found above {p}")


def hub_dir(repo) -> Path:
    return Path(repo) / "docs" / "agent_hub"


def job_path(repo) -> Path:
    return hub_dir(repo) / "JOB.json"


def state_path(repo) -> Path:
    return hub_dir(repo) / ".job_state.json"


def config_path(repo) -> Path:
    return hub_dir(repo) / "hub_config.json"


def board_path(repo) -> Path:
    return hub_dir(repo) / "BOARD.md"


def fixture_dir(repo) -> Path:
    return hub_dir(repo) / ".job_fixture"


def dispatcher_log_path(repo) -> Path:
    return hub_dir(repo) / "logs" / "dispatcher.jsonl"


def dispatcher_runs_dir(repo) -> Path:
    return hub_dir(repo) / "logs" / "dispatcher_runs"


def read_json(path: Path, default=None):
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return default
    return json.loads(text)


def write_json_atomic(path: Path, obj) -> None:
    """Write temp file in the same directory, then os.replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def write_text_atomic(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def iso_now(ts: float | None = None) -> str:
    """ISO-8601 time with UTC offset, millisecond resolution."""
    dt = datetime.fromtimestamp(time.time() if ts is None else ts).astimezone()
    return dt.isoformat(timespec="milliseconds")


def parse_iso(s: str) -> float:
    return datetime.fromisoformat(s).timestamp()


JOBS_SCHEMA = "jobs-1"
STATE_SCHEMA = "job-state-1"
_JOB_ID = re.compile(r"job-([1-9][0-9]*)\Z")


class JobChoice(Exception):
    """No single job could be chosen (none active, several active, or an unknown id)."""


def _normalise(rec: dict) -> dict:
    rec = dict(rec)
    rec.setdefault("id", "job-1")
    rec.setdefault("match", "time")
    return rec


def _raw_jobs(raw) -> list[dict]:
    if isinstance(raw, dict) and isinstance(raw.get("jobs"), list):
        return [j for j in raw["jobs"] if isinstance(j, dict)]
    if isinstance(raw, dict) and "started" in raw:  # legacy single record
        return [raw]
    return []


def load_jobs(repo) -> list[dict]:
    """Every job record in start order, normalised (each has `id` and `match`)."""
    return [_normalise(j) for j in _raw_jobs(read_json(job_path(repo), None))]


def save_jobs(repo, jobs: list[dict]) -> None:
    """Write JOB.json in the container form (the caller holds lock())."""
    write_json_atomic(job_path(repo), {"schema": JOBS_SCHEMA, "jobs": list(jobs)})


def active_jobs(repo) -> list[dict]:
    return [j for j in load_jobs(repo) if j.get("active")]


def get_job(repo, job_id) -> dict | None:
    return next((j for j in load_jobs(repo) if j["id"] == job_id), None)


def next_job_id(jobs: list[dict]) -> str:
    nums = [int(m[1]) for j in jobs if (m := _JOB_ID.match(str(j.get("id", ""))))]
    return f"job-{max(nums, default=0) + 1}"


def _started_ts(job: dict) -> float:
    try:
        return parse_iso(job["started"])
    except (KeyError, TypeError, ValueError):
        return 0.0


def default_job(repo) -> dict | None:
    """The only active job; with several active the newest active one; with none active the
    job that ended last; None when there is no job."""
    jobs = load_jobs(repo)
    active = [j for j in jobs if j.get("active")]
    if active:
        return max(active, key=_started_ts) if len(active) > 1 else active[0]

    def ended(j):
        try:
            return parse_iso(j["ended"])
        except (KeyError, TypeError, ValueError):
            return float("-inf")
    return max(jobs, key=ended) if jobs else None


def _describe(jobs: list[dict]) -> str:
    return ", ".join(f"{j['id']} (goal: {j.get('goal')})" for j in jobs)


def select_job(repo, job_id=None) -> dict:
    """The job a CLI acts on: the one named by --job, else the only active job.

    Raises JobChoice (message for the user) when the id is unknown or zero or several
    jobs are active and no id was given."""
    if job_id:
        job = get_job(repo, job_id)
        if job is None:
            known = ", ".join(j["id"] for j in load_jobs(repo)) or "none"
            raise JobChoice(f"unknown job {job_id} (known: {known})")
        return job
    active = active_jobs(repo)
    if len(active) == 1:
        return active[0]
    if not active:
        raise JobChoice("no active job")
    raise JobChoice(f"{len(active)} jobs are active: {_describe(active)}; pass --job <id>")


def visible_jobs(repo) -> list[dict]:
    """The jobs on the board: every active job, plus each ended job that ended at or after
    the newest job started (an ended job stays until a newer job starts)."""
    jobs = load_jobs(repo)
    if not jobs:
        return []
    newest = max(_started_ts(j) for j in jobs)
    out = []
    for j in jobs:
        if j.get("active"):
            out.append(j)
            continue
        try:
            keep = parse_iso(j["ended"]) >= newest
        except (KeyError, TypeError, ValueError):
            keep = True
        if keep:
            out.append(j)
    return out


def load_job(repo, job_id=None) -> dict | None:
    """The named job, else the default job (see default_job)."""
    return get_job(repo, job_id) if job_id else default_job(repo)


def job_limits(job: dict | None) -> dict:
    lim = dict(DEFAULT_LIMITS)
    if job:
        lim.update(job.get("limits") or {})
    return lim


def default_state() -> dict:
    # T-hub-028: "retries" counts the wall-time releases per task in this job (one re-wake is
    # allowed, the second timeout leaves the task Open); "not_retried" holds the board's
    # explanation for the tasks that used up their retry.  "retry_reserved" holds the single
    # retry wake of a task, written when that wake is admitted (job_dispatcher._spawn).
    # T038 F1: "abandoned" holds {agent: {pgid, task, time}} for a wake whose process group a
    # wall-time kill could not empty; that agent is woken in no job while the group is alive.
    return {"paused": False, "pause_reason": None, "wakes": [], "tokens": {}, "seen": [],
            "retries": {}, "not_retried": {}, "retry_reserved": {}, "abandoned": {}}


def _first_job_id(repo) -> str:
    jobs = load_jobs(repo)
    return jobs[0]["id"] if jobs else "job-1"


def _state_map(repo) -> dict:
    """{job id: stored state}.  A legacy flat file is the state of the first job."""
    raw = read_json(state_path(repo), {}) or {}
    if not isinstance(raw, dict):
        return {}
    if isinstance(raw.get("jobs"), dict):
        return dict(raw["jobs"])
    return {_first_job_id(repo): raw} if raw else {}


def _state_id(repo, job_id) -> str:
    if job_id:
        return job_id
    job = default_job(repo)
    return job["id"] if job else "job-1"


def load_state(repo, job_id=None) -> dict:
    st = default_state()
    st.update(_state_map(repo).get(_state_id(repo, job_id)) or {})
    return st


def save_state(repo, state: dict, job_id=None) -> None:
    states = _state_map(repo)
    states[_state_id(repo, job_id)] = state
    write_json_atomic(state_path(repo), {"schema": STATE_SCHEMA, "jobs": states})


def migrate(repo) -> bool:
    """Rewrite legacy JOB.json and .job_state.json in the container form (the caller holds
    lock()).  Returns whether anything changed; ids, match, counters and quotes stay."""
    changed = False
    raw_job = read_json(job_path(repo), None)
    states = _state_map(repo)  # reads a legacy flat state against the first job
    raw_state = read_json(state_path(repo), None)
    if isinstance(raw_job, dict) and not isinstance(raw_job.get("jobs"), list) and "started" in raw_job:
        save_jobs(repo, load_jobs(repo))
        changed = True
    if isinstance(raw_state, dict) and not isinstance(raw_state.get("jobs"), dict):
        write_json_atomic(state_path(repo), {"schema": STATE_SCHEMA, "jobs": states})
        changed = True
    return changed


def load_config(repo) -> dict:
    cfg = read_json(config_path(repo), {})
    if not isinstance(cfg, dict):
        raise ValueError(f"{config_path(repo)} must hold a JSON object")
    return cfg


# --------------------------------------------------------------------------
# Adapter interface
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Action:
    agent: str
    reason: str  # "claim" | "review" | "answer"
    task: str
    revision: int | str  # stub: int; hub: revision hash (+ round suffix for an owner's continue)
    # True for the wake in which an owner continues its own task (changes requested, a blocking
    # finding, or a user "not OK"); only a fresh claim of an Open task uses up the one retry.
    continuation: bool = False
    job: str | None = None  # id of the job the action belongs to (T038); None = not tied to a job


def display_name(repo, task, mapping=None) -> str:
    """`T024` -> `T-collect-024` (T-hub-028); the id itself when it has no category.

    `mapping` is task_names.load(repo), passed in by callers that format many ids."""
    try:
        if str(Path(__file__).resolve().parent) not in sys.path:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
        import task_names
        return task_names.display(str(task), mapping=task_names.load(repo) if mapping is None else mapping)
    except Exception:  # naming is cosmetic: never break the board or the dispatcher for it
        return str(task)


def name_map(repo) -> dict:
    try:
        if str(Path(__file__).resolve().parent) not in sys.path:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
        import task_names
        return task_names.load(repo)
    except Exception:
        return {}


_DECISION = re.compile(r"^###\s+(D\d{3,})\b[^\n]*\n(.*?)(?=^### |\Z)", re.M | re.S)


def user_checks(repo) -> list[dict]:
    """The user's check decisions in DECISIONS.md, in file order (T-hub-028).

    A block counts when its scope line reads `task=T0xx; check=ok|not_ok`.  Each result has
    id, task, check ("ok" or "not_ok"), quote and revision (None when the block has none)."""
    try:
        text = (hub_dir(repo) / "DECISIONS.md").read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    out = []
    for m in _DECISION.finditer(text):
        body = m[2]
        scope = re.search(r"^- scope:\s*task=(T[0-9]{3,}[a-z]?);\s*check=(ok|not_ok)\s*$", body, re.M)
        if not scope:
            continue
        quote = re.search(r'^- quote:\s*"(.+)"\s*$', body, re.M)
        rev = re.search(r"^- revision:\s*([0-9a-f]{64})\s*$", body, re.M)
        out.append({"id": m[1], "task": scope[1], "check": scope[2],
                    "quote": quote[1] if quote else "", "revision": rev[1] if rev else None})
    return out


_lock_state = threading.local()


def lock_depth() -> int:
    """How many ja.lock() sections the current thread is inside (0 or 1)."""
    return getattr(_lock_state, "depth", 0)


def git_dir(repo) -> Path:
    """The repository's git directory (handles a .git file as in a worktree)."""
    g = Path(repo) / ".git"
    if g.is_dir():
        return g
    r = subprocess.run(["git", "-C", str(repo), "rev-parse", "--absolute-git-dir"],
                       capture_output=True, text=True)
    return Path(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else g


@contextlib.contextmanager
def lock(repo, timeout: float | None = None):
    """Exclusive flock on <git dir>/hub.lock; retry up to `timeout` s, else Busy.

    This is the same file hubtx.transaction locks, so the dispatcher's short state
    writes (JOB.json, .job_state.json) are serialised with hub transactions.  Two
    rules keep that from deadlocking:
      * never nest: a second lock() in the same thread raises Busy at once;
      * never call a hub CLI (claim.py, hub_append.py, ...) while holding it; the
        hub backend checks lock_depth() and refuses.
    The kernel frees the lock if the holder dies.
    """
    if lock_depth() > 0:
        raise Busy("hub lock is already held by this thread (not re-entrant)")
    timeout = LOCK_TIMEOUT_S if timeout is None else timeout
    path = git_dir(repo) / "hub.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise Busy(f"could not take {path} within {timeout:g} s")
                time.sleep(0.02)
        _lock_state.depth = 1
        try:
            yield
        finally:
            _lock_state.depth = 0
    finally:
        os.close(fd)  # closing the descriptor releases the flock


# --------------------------------------------------------------------------
# Stub backend: JSON fixtures in docs/agent_hub/.job_fixture/tasks.json
#
# {"agents": ["gpt", "claude"],            # optional; extra agents to consider
#  "tasks": [{"id", "title", "posted_by", "owner", "status", "reviewer",
#             "revision", "reviewed_revisions", "question", "question_by",
#             "answer", "note", "posted_in_job"}]}
# status in {open, claimed, review, accepted, user_ok}
# --------------------------------------------------------------------------

TASK_DEFAULTS = {
    "title": "",
    "posted_by": None,
    "owner": None,
    "status": "open",
    "reviewer": None,
    "revision": 0,
    "reviewed_revisions": [],
    "question": None,
    "question_by": None,
    "answer": None,
    "note": None,
    "posted_in_job": True,
}


def _tasks_file(repo) -> Path:
    return fixture_dir(repo) / "tasks.json"


def _stub_read(repo) -> dict:
    data = read_json(_tasks_file(repo), None) or {}
    if isinstance(data, list):
        data = {"tasks": data}
    data.setdefault("agents", [])
    tasks = []
    for t in data.get("tasks", []):
        full = {k: (list(v) if isinstance(v, list) else v) for k, v in TASK_DEFAULTS.items()}
        full.update(t)
        tasks.append(full)
    data["tasks"] = tasks
    return data


def _stub_write(repo, data: dict) -> None:
    write_json_atomic(_tasks_file(repo), data)


def stub_write(repo, data: dict) -> None:
    """Replace the whole fixture (test setup); takes the lock."""
    with lock(repo):
        _stub_write(repo, data)


def stub_read(repo) -> dict:
    return _stub_read(repo)


def stub_update_task(repo, task_id: str, fn) -> dict:
    """Apply fn(task_dict) to one task under the lock; returns the updated task."""
    with lock(repo):
        data = _stub_read(repo)
        for t in data["tasks"]:
            if t["id"] == task_id:
                fn(t)
                _stub_write(repo, data)
                return t
    raise KeyError(task_id)


class StubBackend:
    name = "stub"

    @staticmethod
    def _job_id(repo, job):
        """Id of `job` (a record, an id, or None for no filter)."""
        return job.get("id") if isinstance(job, dict) else job

    def _tasks(self, repo, job) -> list[dict]:
        """Fixture tasks of one job: a task may carry "job"; one without belongs to every job.
        job=None means no filter."""
        jid = self._job_id(repo, job)
        tasks = _stub_read(repo)["tasks"]
        if jid is None:
            return tasks
        return [t for t in tasks if t.get("job") in (None, jid)]

    def _tag(self, repo, job, t) -> str | None:
        return self._job_id(repo, job) or t.get("job")

    def next_actions(self, repo, job=None) -> list[Action]:
        data = _stub_read(repo)
        tasks = self._tasks(repo, job)
        agents = set(data["agents"])
        try:
            agents |= set((load_config(repo).get("job_agents") or {}).keys())
        except ValueError:
            pass
        for t in tasks:
            for k in ("posted_by", "owner", "reviewer", "question_by"):
                if t.get(k):
                    agents.add(t[k])
        out: list[Action] = []
        for agent in sorted(agents):
            busy = any(t["status"] == "claimed" and t["owner"] == agent for t in tasks)
            for t in tasks:
                rev = t["revision"]
                jid = self._tag(repo, job, t)
                if t["status"] == "open" and not busy and t["posted_by"] != agent:
                    out.append(Action(agent, "claim", t["id"], rev, job=jid))
                if (t["status"] == "review" and t["reviewer"] == agent
                        and rev not in t["reviewed_revisions"]):
                    out.append(Action(agent, "review", t["id"], rev, job=jid))
                if t["question"] and t["answer"] and t["question_by"] == agent:
                    out.append(Action(agent, "answer", t["id"], rev, job=jid))
        return out

    def posted_count(self, repo, job=None) -> int:
        return sum(1 for t in self._tasks(repo, job) if t["posted_in_job"])

    def _entry(self, t: dict, extra: str = "", job=None, repo=None) -> dict:
        line = f"{t['id']} · {t['title']} · posted by {t['posted_by']}"
        if extra:
            line += " · " + extra
        return {"task": t["id"], "title": t["title"], "line": line,
                "job": self._job_id(repo, job) or t.get("job")}

    def open_entries(self, repo, job=None) -> list[dict]:
        return [self._entry(t, job=job) for t in self._tasks(repo, job) if t["status"] == "open"]

    def in_review_entries(self, repo, job=None) -> list[dict]:
        return [self._entry(t, f"owner {t['owner']}, waiting for {t['reviewer']}, revision {t['revision']}", job)
                for t in self._tasks(repo, job) if t["status"] == "review"]

    def user_check_entries(self, repo, job=None) -> list[dict]:
        return [self._entry(t, f"owner {t['owner']}, revision {t['revision']}", job)
                for t in self._tasks(repo, job) if t["status"] == "accepted"]

    def checked_entries(self, repo, job=None) -> list[dict]:
        return [self._entry(t, f"owner {t['owner']}, revision {t['revision']}", job)
                for t in self._tasks(repo, job) if t["status"] == "user_ok"]

    def questions(self, repo, job=None) -> list[dict]:
        out = []
        for t in self._tasks(repo, job):
            if t["question"] and not t["answer"]:
                e = self._entry(t, f"asked by {t['question_by']}: {t['question']}", job)
                e["question"] = t["question"]
                out.append(e)
        return out

    def claim(self, repo, agent: str, task: str) -> bool:
        with lock(repo):
            data = _stub_read(repo)
            for t in data["tasks"]:
                if t["id"] != task:
                    continue
                if t["status"] != "open" or t["posted_by"] == agent:
                    return False
                t["status"] = "claimed"
                t["owner"] = agent
                _stub_write(repo, data)
                return True
        return False

    def return_to_open(self, repo, task: str, agent: str, note: str) -> bool:
        """Reopen a task the agent holds (status claimed); otherwise do nothing."""
        with lock(repo):
            data = _stub_read(repo)
            for t in data["tasks"]:
                if t["id"] == task and t["status"] == "claimed" and t["owner"] == agent:
                    t["status"] = "open"
                    t["owner"] = None
                    t["note"] = note
                    _stub_write(repo, data)
                    return True
        return False


# --------------------------------------------------------------------------
# Hub backend (phase 2): task state from T013's hub_state, writes through the
# T012/T013 command-line tools.  Selected by hub_config.json {"job_backend": "hub"}.
#
# Only "job tasks" count (T038).  The task's `created` event decides: `job=<id>` puts it in
# that job only, `job=none` in no job; with no job field it belongs to each job whose
# `match` is "time" and that was running when the task was created (created at or after
# `started` and, for an ended job, at or before `ended`; compared at whole-second
# resolution, because event times have no fractions).  Other tasks and legacy cards without
# a status file are invisible to the dispatcher and to the board.
# --------------------------------------------------------------------------

TOOLS_DIR = Path(__file__).resolve().parent
CLI_TIMEOUT_S = 90.0
_reported: set = set()


def _hub_modules():
    """hub_events and hub_state, imported lazily from this directory."""
    if str(TOOLS_DIR) not in sys.path:
        sys.path.insert(0, str(TOOLS_DIR))
    import hub_events
    import hub_state
    return hub_events, hub_state


def _parse_event_time(text: str) -> float:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def _card_title(repo, task: str) -> str:
    """First heading of the task card, without its leading 'T###:'."""
    import re
    for card in sorted((hub_dir(repo) / "tasks").glob(task + "_*.md")):
        try:
            for line in card.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("# "):
                    return re.sub(r"^T[0-9]{3,}[a-z]?\s*:\s*", "", line[2:].strip()) or task
        except OSError:
            pass
    return task


class HubBackend:
    name = "hub"

    # ---- reading --------------------------------------------------------
    @staticmethod
    def _member(job: dict, created: dict) -> bool:
        """Does the task with this `created` record belong to `job`? (the card's rule)"""
        field = created["fields"].get("job")
        if field == "none":
            return False
        if field:
            return field == job["id"]
        if job.get("match", "time") != "time" or not job.get("started"):
            return False
        when = _parse_event_time(created["time"])
        if when < int(parse_iso(job["started"])):
            return False
        return not (job.get("ended") and when > int(parse_iso(job["ended"])))

    def _collect(self, repo, jobs: list[dict], only: str | None = None) -> list[dict]:
        """derive() output of every task that belongs to at least one of `jobs`, plus posted_by,
        title, round, `job` (the first job it belongs to) and `jobs` (all of them)."""
        if not jobs:
            return []
        ev, hs = _hub_modules()
        out = []
        for task in hs.task_ids(repo):
            if only is not None and task != only:
                continue
            try:
                records, _ = ev.parse_file(Path(repo) / hs.status_path(task))
                created = next((r for r in records if r["event"] == "created"), None)
                if created is None:
                    continue
                members = [j["id"] for j in jobs if self._member(j, created)]
                if not members:
                    continue
                d = hs.derive(repo, task)
            except Exception as exc:  # one bad record must not blind the board
                key = (task, str(exc))
                if key not in _reported:
                    _reported.add(key)
                    print(f"warning: job board skips {task}: {exc}", file=sys.stderr, flush=True)
                continue
            d["posted_by"] = created["tag"]
            d["title"] = _card_title(repo, task)
            d["round"] = sum(1 for r in d["events"] if r["event"] in ("review", "request_changes"))
            d["job"], d["jobs"] = members[0], members
            out.append(d)
        return out

    def job_tasks(self, repo, job) -> list[dict]:
        """The tasks of one job (a record or an id).  An unknown id has none."""
        if not isinstance(job, dict):
            job = get_job(repo, job)
        return self._collect(repo, [job] if job else [])

    def _job_tasks(self, repo, job=None) -> list[dict]:
        """The tasks of `job`, or with job=None the union over the jobs on the board (each task
        once, tagged with the first job it belongs to)."""
        if job is None:
            return self._collect(repo, visible_jobs(repo))
        return self.job_tasks(repo, job)

    def find_task(self, repo, task: str):
        """The task as derive() output (with `job`/`jobs`) when it belongs to any job, active or
        ended; None otherwise."""
        found = self._collect(repo, load_jobs(repo), only=task)
        return found[0] if found else None

    def posted_count(self, repo, job=None) -> int:
        return len(self._job_tasks(repo, job))

    def next_actions(self, repo, job=None) -> list[Action]:
        if job is None:  # every job on the board, each computed on its own (busy is per job)
            return [a for j in visible_jobs(repo) for a in self.next_actions(repo, j)]
        ev, _ = _hub_modules()
        tasks = self._job_tasks(repo, job)
        out: list[Action] = []
        for agent in sorted(ev.TAGS):
            # The agent's own unfinished work: claimed, or sent back by a reviewer.
            busy = any(t["owner"] == agent and t["state"] in ("claimed", "changes_requested")
                       for t in tasks)
            for t in tasks:
                state, rev = t["state"], t["revision"]
                reopen = self._pending_reopen(t)
                # T-hub-028: a task the user reopened is continued by its owner (below), not
                # offered as a fresh claim: a changed revision would make that a second wake.
                if (state == "open" and not busy and t["owner"] in ("open", agent)
                        and t["posted_by"] != agent and agent not in t["reviewers"]
                        and not (reopen and t["owner"] == agent)):
                    out.append(Action(agent, "claim", t["task"], rev, job=t["job"]))
                # T048: only a submit asks for review.  The wake needs a submit whose bytes are
                # still the deliverables at HEAD; a later commit wakes nobody until the owner
                # submits again (hub_state.derive then names the owner as next actor).
                sub = t["submitted_revision"]
                if (state.startswith("review") and agent in t["reviewers"]
                        and sub is not None and rev == sub
                        and t["latest_accepts"].get(agent) != sub):
                    # T-hub-028 F1: after a user "not OK" the same bytes need a fresh review, so the
                    # latest reopen decision is part of the key (unchanged for a never-reopened task).
                    cycle = self._last_reopen(t)
                    out.append(Action(agent, "review", t["task"], f"{sub}#{cycle}" if cycle else sub,
                                      job=t["job"]))
                # The owner continues its own task: changes requested, or named as
                # next because a blocking finding is open.  The posted-by rule
                # does not apply to a task the agent already owns.
                if t["owner"] == agent and (
                        state == "changes_requested"
                        or (state.startswith("review") and t["open_findings"]
                            # T049 F1: "a submit is due" also names the owner; it is not a
                            # finding to fix, so a bare commit after a resubmit wakes nobody.
                            and not t.get("submit_due")
                            and agent in [x for x in t["next_actor"].split(",")])):
                    out.append(Action(agent, "claim", t["task"], f"{sub or rev}#r{t['round']}",
                                      continuation=True, job=t["job"]))
                # T-hub-028: the user said "not OK" (user action=open) and no submit has followed:
                # the owner continues.  The decision id is part of the key so that a second
                # reopen of the same revision is not swallowed by the dedupe of the first.
                if t["owner"] == agent and state == "open" and reopen:
                    out.append(Action(agent, "claim", t["task"], f"{sub or rev}#r{t['round']}#{reopen}",
                                      continuation=True, job=t["job"]))
        return out  # no "answer" wakes in phase 2: the user answers in chat

    @staticmethod
    def _pending_reopen(t: dict):
        """Decision id of the user's latest `user action=open` that no later submit answered."""
        found = None
        for r in t["events"]:
            if r["event"] == "user" and r["fields"]["action"] == "open":
                found = r["fields"]["decision"]
            elif r["event"] == "submit":
                found = None
        return found

    @staticmethod
    def _last_reopen(t: dict):
        """Decision id of the user's latest `user action=open`, answered by a submit or not
        (None when the task was never reopened).  It is the review wake's reopen cycle."""
        found = None
        for r in t["events"]:
            if r["event"] == "user" and r["fields"]["action"] == "open":
                found = r["fields"]["decision"]
        return found

    def _entry(self, t: dict, line: str) -> dict:
        return {"task": t["task"], "title": t["title"], "line": line, "summary": line, "job": t["job"]}

    def open_entries(self, repo, job=None) -> list[dict]:
        out = []
        checks = {c["id"]: c for c in user_checks(repo)}
        for t in self._job_tasks(repo, job):
            if t["state"] != "open":
                continue
            line = f"posted by {t['posted_by']}, owner {t['owner']}"
            if t["reviewers"]:
                line += ", reviewers " + "+".join(t["reviewers"])
            last = t["events"][-1] if t["events"] else None
            if last and last["event"] == "release":
                line += f"; released by {last['tag']}: {last['note'] or 'no note'}"
            reopen = self._pending_reopen(t)
            if reopen:  # T-hub-028: the user's check said "not OK"
                said = checks.get(reopen, {}).get("quote", "")
                line += f"; reopened by the user ({reopen}): \"{said}\"" if said else f"; reopened by the user ({reopen})"
            out.append(self._entry(t, line))
        return out

    @staticmethod
    def _ok_decision(t: dict, checks: list[dict]):
        """The `check=ok` decision that still covers this task, else None.

        The latest check decision for the task decides, and it must name the revision that is
        accepted now (a decision without a revision line covers any revision)."""
        mine = [c for c in checks if c["task"] == t["task"]]
        if not mine or mine[-1]["check"] != "ok" or not t["accepted"]:
            return None
        rev = mine[-1]["revision"]
        return mine[-1] if rev is None or rev == t["accepted_revision"] else None

    def in_review_entries(self, repo, job=None) -> list[dict]:
        """Tasks submitted and waiting for a reviewer's accept (T-hub-028 F1)."""
        out = []
        for t in self._job_tasks(repo, job):
            if t["accepted"] or not t["state"].startswith("review"):
                continue
            if t.get("submit_due"):
                # T049 F2: no reviewer is woken for unsubmitted bytes; the owner acts next.
                line = (f"owner {t['owner']}, deliverables changed since the submit at "
                        f"{str(t['submitted_revision'])[:8]}; waiting for {t['owner']} to submit again "
                        f"(HEAD {str(t['revision'])[:8]})")
            else:
                waiting = [r for r in t["reviewers"] if t["latest_accepts"].get(r) != t["revision"]]
                line = f"owner {t['owner']}, waiting for {'+'.join(waiting) or 'a reviewer'} at {str(t['revision'])[:8]}"
            if self._last_reopen(t):
                line += f"; reopened by the user ({self._last_reopen(t)})"
            out.append(self._entry(t, line))
        return out

    def user_check_entries(self, repo, job=None) -> list[dict]:
        checks = user_checks(repo)
        return [self._entry(t, f"owner {t['owner']}, accepted by {'+'.join(t['reviewers'])} "
                               f"at {str(t['accepted_revision'])[:8]}")
                for t in self._job_tasks(repo, job) if t["accepted"] and not self._ok_decision(t, checks)]

    def checked_entries(self, repo, job=None) -> list[dict]:
        checks = user_checks(repo)
        out = []
        for t in self._job_tasks(repo, job):
            c = self._ok_decision(t, checks)
            if c:
                out.append(self._entry(t, f"owner {t['owner']}, accepted by {'+'.join(t['reviewers'])}; "
                                          f"checked OK by the user in {c['id']}"))
        return out

    def questions(self, repo, job=None) -> list[dict]:
        return [self._entry(t, f"{t['state']}, next: {t['next_actor']}")
                for t in self._job_tasks(repo, job) if t["state"] in ("awaiting_user", "disputed")]

    # ---- writing: only through the CLIs ---------------------------------
    def _cli(self, repo, script: str, *args: str) -> int:
        if lock_depth() > 0:
            raise RuntimeError(f"{script} takes the hub lock; do not call it while holding "
                               "job_adapters.lock() (would deadlock)")
        try:
            r = subprocess.run([sys.executable, str(TOOLS_DIR / script), *args], cwd=str(repo),
                               capture_output=True, text=True, timeout=CLI_TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"warning: {script} failed: {exc}", file=sys.stderr, flush=True)
            return 99
        if r.returncode not in (0, 3):  # 3 = not permitted, the normal loss of a race
            print(f"warning: {script} exit {r.returncode}: {r.stderr.strip()}", file=sys.stderr, flush=True)
        return r.returncode

    def claim(self, repo, agent: str, task: str) -> bool:
        ev, hs = _hub_modules()
        if agent not in ev.TAGS:
            return False
        records, _ = ev.parse_file(Path(repo) / hs.status_path(task))
        created = next((r for r in records if r["event"] == "created"), None)
        if created is None or created["tag"] == agent:  # J-R3: never claim what you posted
            return False
        d = hs.derive(repo, task)
        if d["state"] != "open" or agent in d["reviewers"] or d["owner"] not in ("open", agent):
            return False
        scope = list(d["deliverables"]) or [f"docs/agent_hub/tasks/{task}_*"]
        args = [agent, task]
        for item in scope:
            args += ["--scope", item]
        return self._cli(Path(repo), "claim.py", *args) == 0

    def return_to_open(self, repo, task: str, agent: str, note: str) -> bool:
        """Release the claim the agent holds on a task that is still `claimed`."""
        ev, hs = _hub_modules()
        if agent not in ev.TAGS:
            return False
        d = hs.derive(repo, task)
        if d["holder"] != agent or d["state"] != "claimed":
            return False
        line = ev.format_event(agent, "release", {}, note)
        return self._cli(Path(repo), "hub_append.py", agent, task, hs.status_path(task), line) == 0


def get_backend(repo):
    """The stub, unless hub_config.json has {"job_backend": "hub"}."""
    try:
        backend = load_config(repo).get("job_backend")
    except ValueError:
        backend = None
    return HubBackend() if backend == "hub" else StubBackend()


def next_actions(repo, job=None) -> list[Action]:
    return get_backend(repo).next_actions(Path(repo), job)


def posted_count(repo, job=None) -> int:
    return get_backend(repo).posted_count(Path(repo), job)


def open_entries(repo, job=None) -> list[dict]:
    return get_backend(repo).open_entries(Path(repo), job)


def in_review_entries(repo, job=None) -> list[dict]:
    return get_backend(repo).in_review_entries(Path(repo), job)


def user_check_entries(repo, job=None) -> list[dict]:
    return get_backend(repo).user_check_entries(Path(repo), job)


def checked_entries(repo, job=None) -> list[dict]:
    return get_backend(repo).checked_entries(Path(repo), job)


def questions(repo, job=None) -> list[dict]:
    return get_backend(repo).questions(Path(repo), job)


def claim(repo, agent: str, task: str) -> bool:
    return get_backend(repo).claim(Path(repo), agent, task)


def return_to_open(repo, task: str, agent: str, note: str):
    return get_backend(repo).return_to_open(Path(repo), task, agent, note)
