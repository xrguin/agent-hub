#!/usr/bin/env python3
"""Job-board dispatcher: a model-free loop that wakes agents when there is work.

One dispatcher serves every active job (T038).  Each iteration it reads JOB.json, reaps
finished wakes, stops overdue ones (by the limits of the job the wake belongs to), and then
serves each active job in turn: it asks the adapter for that job's next actions and starts
at most one process per agent, within that job's limits, counters and pause.  The job whose
last wake is oldest goes first, so a busy job cannot starve another.  It never wakes an agent
twice for the same (agent, reason, task, revision) in one job.  Across jobs an agent has at
most one wake at a time (the agents share one working tree); the hourly per-agent limit counts
the wakes of the job being served, so two jobs can wake one agent up to twice the hourly limit.
Real agent commands are opt-in: they come from
docs/agent_hub/hub_config.json, key "job_agents":

    {"job_agents": {"gpt": ["codex", "exec", "-C", "{repo}", "-s", "workspace-write", "{prompt}"]}}

Placeholders in argv items: {repo} {prompt} (and also {tag} {reason} {task}
{revision} {job}).  A wake also gets the env var JOB_ID, and every dispatcher.jsonl line has a
`job` field.  With no "job_agents" the dispatcher wakes nobody.

Guarantees (T023 round 2, per job since T038):
  * Wake admission (_spawn) re-reads that job's active flag and pause state under the same
    state lock that `job.py end` takes, so no process starts after a completed
    `job end` of its job or while it is paused.  Popen runs under that lock (it returns at once;
    the child takes the hub lock later).  Running children finish normally.
  * A wall-time kill stops the whole process group of the wake (SIGTERM, grace,
    then SIGKILL of the group even if the leader already exited), verifies that no
    live member is left, reaps the leader, and only then releases the task.  If the
    group cannot be emptied the task is NOT released and the job is paused.  The stuck group
    is also recorded in that job's state ("abandoned": {agent: {pgid, task, time}}): while
    it is alive that agent is woken in no job (also after a dispatcher restart), because the
    agents share one working tree.  The entry is removed, with an "abandoned_gone" log line,
    at the start of the first iteration that finds the group gone; the old job stays paused
    and its task claimed until the user resumes.
  * Retry after a timeout (T-hub-028): the release that follows a wall-time kill makes a job
    task claimable by anyone again (a task created with owner "open").  The dispatcher
    re-wakes it at most once per job: .job_state.json keeps "retries" per task and job.  The first
    timeout adds "#t1" to the claim key so the dedupe does not swallow the retry; a second
    timeout leaves the task Open, writes a line under Paused on the board ("not_retried")
    and wakes nobody for it again.  It does not pause the job.
  * The single retry is reserved when its wake is admitted (F2): .job_state.json keeps
    "retry_reserved" per task, written under the same lock as the admission, so a second
    eligible agent, or a dispatcher restarted meanwhile, is not woken for the same retry.
  * Every limit pauses all wakes of its job (J-R5), the hourly per-agent limit included, and
    only that job; only `job.py resume --job ID` clears it.  Wakes of the last hour still count after a resume,
    so resume with a higher --limit (or wait) if the hourly limit was the cause.

Known limits (reviewed, accepted as informational):
  * The time-window job-membership rule in job_adapters (jobs with match "time", tasks
    without a job field) compares whole seconds (task created events carry second
    resolution, JOB.started has milliseconds), so a task created in the job-start second
    may count as a job task (F4).
  * The no-CLI-under-lock guard only sees ja.lock() sections of this thread.  An
    outer hubtx.transaction held by some other code around a backend write is not
    detected; call backend writes outside every hub lock (F5).
  * Group cleanup covers the wake's own process group.  A descendant that moved
    to another session or group (setsid) is outside it.  A wake that exits by
    itself with descendants still running is not checked.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import subprocess
import sys
import time
import fcntl
from dataclasses import dataclass, replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import job_adapters as ja  # noqa: E402
import board  # noqa: E402

REASON_ORDER = {"answer": 0, "review": 1, "claim": 2}
PROMPT = ("[{tag}] job-board wake, reason {reason}, task {task}: do this one action, then exit. "
          "Follow docs/agent_hub/README.md, section 9 'When the dispatcher wakes you' and the sections "
          "it points to. Read only what this action needs: the task card, its status file, its "
          "deliverables, and this task's reviews and responses. Skip the session-start reading list "
          "(README section 1): do not read STATE.md, DECISIONS.md, INDEX.md, other tasks or other "
          "agents' logs, and do not run hub_status.py, unless the task card names them; for user "
          "decisions about this task, grep DECISIONS.md for the task id. Do not start sub-agents or "
          "workers; do the work yourself.")
REREVIEW = (" If you reviewed this task before, this is a re-review: start from your last review of it "
            "and the owner's responses, look only at what changed in the deliverables since that review "
            "(git log -p on them), and recheck your open findings and the changed parts; do not redo "
            "checks of unchanged material.")


def parse_tokens(agent: str, stdout_text: str):
    """Pluggable: return the tokens a run used, or None when unknown."""
    return None


@dataclass
class Child:
    proc: subprocess.Popen
    action: ja.Action
    start: float
    out_path: Path
    fh: object
    job: str | None = None


class Dispatcher:
    TERM_GRACE_S = 3.0   # SIGTERM to the group, then wait this long for it to empty
    KILL_GRACE_S = 10.0  # SIGKILL to the group, then wait this long

    def __init__(self, repo, clock=time.time, sleep=time.sleep, parse_tokens=parse_tokens,
                 poll_s: float = 10.0, err=None):
        self.repo = Path(repo)
        self.clock = clock
        self.sleep = sleep
        self.parse_tokens = parse_tokens
        self.poll_s = poll_s
        self.err = err or sys.stderr
        self.children: dict[str, Child] = {}
        self._n = 0
        self._warned: set[str] = set()
        self._held: dict[str, dict] = {}  # agent -> live abandoned group (all jobs), see iterate()

    # ---- small helpers --------------------------------------------------
    def warn(self, key: str, msg: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            print(f"warning: {msg}", file=self.err, flush=True)

    def _mutate(self, fn, job_id) -> dict:
        """Read-modify-write one job's state under the hub lock."""
        with ja.lock(self.repo):
            st = ja.load_state(self.repo, job_id)
            fn(st)
            ja.save_state(self.repo, st, job_id)
        return st

    def _limits_map(self) -> dict:
        """{job id: limits} for every job in JOB.json."""
        return {j["id"]: ja.job_limits(j) for j in ja.load_jobs(self.repo)}

    def _log(self, **fields) -> None:
        path = ja.dispatcher_log_path(self.repo)
        path.parent.mkdir(parents=True, exist_ok=True)
        if fields.get("task"):  # T-hub-028: show T-<category>-NNN next to the canonical id
            shown = ja.display_name(self.repo, fields["task"])
            if shown != fields["task"]:
                fields["task_name"] = shown
        line = json.dumps({"time": ja.iso_now(self.clock()), **fields}, ensure_ascii=False) + "\n"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)

    def _pause(self, job_id: str, reason: str) -> None:
        """Pause one job (the others go on)."""
        def f(st):
            if not st["paused"]:
                st["paused"] = True
                st["pause_reason"] = reason
        self._mutate(f, job_id)
        print(f"paused {job_id}: {reason}", file=self.err, flush=True)

    # ---- children -------------------------------------------------------
    def _finish(self, agent: str, rc: int, killed: bool) -> None:
        ch = self.children.pop(agent)
        ch.fh.close()
        text = ch.out_path.read_text(encoding="utf-8", errors="replace")
        try:
            tokens = self.parse_tokens(agent, text)
        except Exception as exc:
            self.warn(f"tok-{agent}", f"parse_tokens failed for {agent}: {exc}")
            tokens = None
        a = ch.action
        self._log(event="end", job=ch.job, agent=agent, reason=a.reason, task=a.task, revision=a.revision,
                  pid=ch.proc.pid, exit_code=rc, duration_s=round(self.clock() - ch.start, 3),
                  tokens=tokens, killed=killed)
        if tokens is not None:
            def f(st):
                st["tokens"][agent] = st["tokens"].get(agent, 0) + int(tokens)
            self._mutate(f, ch.job)
        if killed or rc != 0:
            note = (f"{agent} wake killed after the wall-time limit" if killed
                    else f"{agent} wake exited with code {rc}")
            released = False
            try:
                released = bool(ja.return_to_open(self.repo, a.task, agent, note))
            except Exception as exc:
                self.warn(f"rto-{agent}", f"return_to_open failed for {ja.display_name(self.repo, a.task)}: {exc}")
            if killed and released:
                self._count_timeout(ch.job, agent, a.task)

    def _count_timeout(self, job_id: str, agent: str, task: str) -> None:
        """T-hub-028: count a wall-time release; the second one for a task stops its retries."""
        count = []

        def f(st):
            n = st["retries"].get(task, 0) + 1
            st["retries"][task] = n
            count.append(n)
            if n >= 2:
                st["not_retried"][task] = (f"timed out a second time (last: {agent}); left Open, "
                                           "not woken again (one retry per job)")
        self._mutate(f, job_id)
        print(f"task {ja.display_name(self.repo, task)}: wall-time release no. {count[0]} ({agent})",
              file=self.err, flush=True)

    def _reap_and_kill(self, limits_by_job: dict) -> None:
        """Reap finished wakes and stop overdue ones, each by the limits of its own job."""
        for agent, ch in list(self.children.items()):
            rc = ch.proc.poll()
            limits = limits_by_job.get(ch.job) or ja.job_limits(None)
            if rc is not None:
                self._finish(agent, rc, killed=False)
            elif self.clock() - ch.start > limits["max_wall_s_per_wake"]:
                if self._kill(ch):
                    self._finish(agent, ch.proc.returncode, killed=True)
                else:
                    self._abandon(agent, ch)

    def _release_gone(self) -> None:
        """Start of an iteration: drop the abandoned-group entries whose group is gone (under the
        hub lock, logged as abandoned_gone) and remember the ones still alive in self._held.
        Every job's persisted state is read, so a restarted dispatcher keeps the exclusion."""
        held: dict[str, dict] = {}
        for job in ja.load_jobs(self.repo):
            for agent, ent in sorted((ja.load_state(self.repo, job["id"]).get("abandoned") or {}).items()):
                if self._group_alive(int(ent["pgid"])):
                    held.setdefault(agent, dict(ent, job=job["id"]))
                    continue
                gone = []

                def f(st, agent=agent, pgid=ent["pgid"]):
                    cur = st["abandoned"].get(agent)
                    if cur and cur.get("pgid") == pgid:
                        del st["abandoned"][agent]
                        gone.append(cur)
                self._mutate(f, job["id"])
                if gone:
                    self._log(event="abandoned_gone", job=job["id"], agent=agent, task=ent.get("task"),
                              pgid=ent["pgid"])
        self._held = held

    @staticmethod
    def _group_alive(pgid: int) -> bool:
        """True while a non-zombie process is left in process group `pgid`."""
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        proc = Path("/proc")
        if not (proc / "self" / "stat").exists():
            return True  # cannot look closer; the group answered signal 0
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                fields = (entry / "stat").read_text().rpartition(")")[2].split()
            except OSError:
                continue  # exited while we looked
            # fields: state ppid pgrp ...
            if len(fields) > 2 and fields[2] == str(pgid) and fields[0] not in ("Z", "X"):
                return True
        return False

    def _wait_group_gone(self, ch: Child, grace: float) -> bool:
        """Reap the leader and wait up to `grace` s for the whole group to disappear."""
        deadline = time.monotonic() + grace
        while True:
            ch.proc.poll()  # reaps the leader as soon as it has exited
            if ch.proc.returncode is not None and not self._group_alive(ch.proc.pid):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)

    def _kill(self, ch: Child) -> bool:
        """Stop the wake's whole process group (it was started with start_new_session,
        so pgid == leader pid).  True only when no live member is left and the leader
        is reaped.  The SIGKILL goes to the group even if the leader already exited."""
        pgid = ch.proc.pid
        for sig, grace in ((signal.SIGTERM, self.TERM_GRACE_S), (signal.SIGKILL, self.KILL_GRACE_S)):
            try:
                os.killpg(pgid, sig)
            except ProcessLookupError:
                pass
            if self._wait_group_gone(ch, grace):
                return True
        return False

    def _abandon(self, agent: str, ch: Child) -> None:
        """The group could not be emptied: keep the task claimed and pause the wake's job."""
        self.children.pop(agent, None)
        ch.fh.close()
        a = ch.action
        # The agent must stay excluded in every job while this group lives: record it first
        # (persisted, so a restarted dispatcher keeps the exclusion), then pause the job.
        entry = {"pgid": ch.proc.pid, "task": a.task, "time": ja.iso_now(self.clock())}

        def f(st):
            st["abandoned"][agent] = entry
        self._mutate(f, ch.job)
        self._held[agent] = dict(entry, job=ch.job)
        self._log(event="kill_failed", job=ch.job, agent=agent, reason=a.reason, task=a.task, revision=a.revision,
                  pid=ch.proc.pid, exit_code=ch.proc.returncode,
                  duration_s=round(self.clock() - ch.start, 3), tokens=None, killed=False)
        shown = ja.display_name(self.repo, a.task)
        self._pause(ch.job, f"could not stop the process group of the {agent} wake on {shown} "
                    f"(pgid {ch.proc.pid}); task {shown} was not released")

    def _spawn(self, a: ja.Action, argv_tpl: list[str], reserve_retry: bool = False) -> bool:
        """Admit and start one wake.  Returns False when the action's job is no longer active or
        is paused: that is re-read here, under the state lock `job.py end` takes, so a
        `job end` that completed after this iteration's first read still stops the wake.

        The job is `a.job` (the default job when the action names none).  The wake is recorded
        in that job's state only.

        `reserve_retry` marks the one retry wake of a task (T-hub-028 F2): under the same lock
        the reservation is checked and written into .job_state.json, so a second eligible agent
        or a restarted dispatcher is not admitted for it.  Such a wake that loses the
        reservation is skipped and True is returned (the caller goes on with other actions)."""
        job_id = a.job
        if job_id is None:
            default = ja.default_job(self.repo)
            job_id = default["id"] if default else "job-1"
        subs = {"{repo}": str(self.repo), "{tag}": a.agent, "{reason}": a.reason,
                "{task}": str(a.task), "{revision}": str(a.revision), "{job}": job_id}
        prompt = PROMPT.format(tag=a.agent, reason=a.reason, task=a.task) + (REREVIEW if a.reason == "review" else "")
        subs["{prompt}"] = prompt

        def fill(item: str) -> str:
            for k, v in subs.items():
                item = item.replace(k, v)
            return item
        argv = [fill(str(x)) for x in argv_tpl]
        runs = ja.dispatcher_runs_dir(self.repo)
        runs.mkdir(parents=True, exist_ok=True)
        self._n += 1
        out_path = runs / f"{int(self.clock())}_{self._n:04d}_{a.agent}_{a.task}_{a.reason}.out"
        fh = open(out_path, "w", encoding="utf-8")
        env = dict(os.environ, JOB_TAG=a.agent, JOB_REASON=a.reason, JOB_TASK=str(a.task),
                   JOB_REVISION=str(a.revision), JOB_ID=job_id)

        err = None
        proc = None
        taken = False
        with ja.lock(self.repo):  # same lock as job.py end / resume
            job = ja.get_job(self.repo, job_id)
            st = ja.load_state(self.repo, job_id)
            admitted = bool(job and job.get("active")) and not st["paused"]
            if admitted and reserve_retry and a.task in st["retry_reserved"]:
                admitted, taken = False, True
            if admitted:
                if reserve_retry:
                    st["retry_reserved"][a.task] = {"agent": a.agent, "revision": a.revision,
                                                    "time": ja.iso_now(self.clock())}
                st["wakes"].append({"time": ja.iso_now(self.clock()), "agent": a.agent, "reason": a.reason,
                                    "task": a.task, "revision": a.revision})
                st["seen"].append([a.agent, a.reason, a.task, a.revision])
                ja.save_state(self.repo, st, job_id)
                try:
                    # Popen returns at once; the child takes the hub lock later.
                    proc = subprocess.Popen(argv, cwd=self.repo, stdout=fh, stderr=subprocess.STDOUT,
                                            stdin=subprocess.DEVNULL, start_new_session=True, env=env)
                except OSError as exc:
                    err = exc
        if not admitted:
            fh.close()
            with contextlib.suppress(OSError):
                out_path.unlink()
            self._log(event="skip", job=job_id, agent=a.agent, reason=a.reason, task=a.task,
                      revision=a.revision, pid=None, exit_code=None, duration_s=None, tokens=None,
                      killed=False,
                      why=("the single retry of this task is already reserved" if taken
                           else "job ended or paused before the wake was admitted"))
            return taken
        if err is not None:
            fh.close()
            print(f"error: cannot start {a.agent}: {err}", file=self.err, flush=True)
            self._log(event="start", job=job_id, agent=a.agent, reason=a.reason, task=a.task,
                      revision=a.revision, pid=None, exit_code=None, duration_s=None, tokens=None,
                      killed=False)
            self._log(event="end", job=job_id, agent=a.agent, reason=a.reason, task=a.task,
                      revision=a.revision, pid=None, exit_code=None, duration_s=0.0, tokens=None,
                      killed=False)
            return True
        self.children[a.agent] = Child(proc, a, self.clock(), out_path, fh, job_id)
        self._log(event="start", job=job_id, agent=a.agent, reason=a.reason, task=a.task,
                  revision=a.revision, pid=proc.pid, exit_code=None, duration_s=None, tokens=None,
                  killed=False)
        return True

    # ---- one iteration --------------------------------------------------
    def _actions(self, job: dict) -> list[ja.Action]:
        """The next actions of one job, each tagged with the job's id.  ja.next_actions(repo)
        covers every job on the board; an action with no job (a stub fixture task without one)
        belongs to every job."""
        return [a if a.job else replace(a, job=job["id"])
                for a in ja.next_actions(self.repo) if a.job in (None, job["id"])]

    def _maybe_wake(self, job: dict) -> None:
        """Serve one active job: its limits, counters, pause and tasks only."""
        job_id = job["id"]
        limits = ja.job_limits(job)
        state = ja.load_state(self.repo, job_id)
        if state["paused"]:
            return
        try:
            agents = ja.load_config(self.repo).get("job_agents") or {}
        except ValueError as exc:
            self.warn("cfg", f"hub_config.json unreadable ({exc}); waking nobody")
            return
        if not agents:
            self.warn("noagents", 'no "job_agents" in hub_config.json; waking nobody')
            return
        posted = ja.posted_count(self.repo, job)
        if posted > limits["max_tasks_posted"]:
            self._pause(job_id, f"tasks posted {posted} exceed max_tasks_posted {limits['max_tasks_posted']}")
            return
        actions = sorted(self._actions(job),
                         key=lambda a: (a.agent, REASON_ORDER.get(a.reason, 9), str(a.task)))
        seen = {tuple(s) for s in state["seen"]}
        for a in actions:
            if a.agent in self.children or a.agent in self._held or a.agent not in agents:
                continue  # one wake per agent at a time, across all jobs (a stuck group counts)
            if a.continuation and any(ch.action.task == a.task and ch.action.reason == "review"
                                      for ch in self.children.values()):
                continue  # D004: the owner fixes once per review round, after the running reviews end
            tries = state.get("retries", {}).get(a.task, 0)
            if a.reason == "claim" and tries >= 2:
                continue  # T-hub-028: timed out twice; stays Open, the board says so
            reserve = False
            if a.reason == "claim" and tries == 1:
                # The one retry: a new key, so the wake that timed out does not swallow it.
                a = replace(a, revision=f"{a.revision}#t{tries}")
                if not a.continuation:
                    # F2: the retry is reserved when it is admitted (persisted, under the state
                    # lock), so a second eligible agent or a new dispatcher process gets none.
                    if a.task in state["retry_reserved"]:
                        continue
                    reserve = True
            if (a.agent, a.reason, a.task, a.revision) in seen:
                continue  # no repeat, and an agent's own change never wakes it again
            now = self.clock()
            if len(state["wakes"]) >= limits["max_wakes_per_job"]:
                self._pause(job_id, f"wakes per job reached {limits['max_wakes_per_job']}")
                return
            if state["tokens"].get(a.agent, 0) >= limits["max_tokens_per_agent"]:
                self._pause(job_id, f"tokens for {a.agent} reached {limits['max_tokens_per_agent']}")
                return
            recent = sum(1 for w in state["wakes"]
                         if w["agent"] == a.agent and now - ja.parse_iso(w["time"]) < 3600)
            if recent >= limits["max_wakes_per_agent_per_hour"]:
                self._pause(job_id, f"wakes per hour for {a.agent} reached "
                            f"{limits['max_wakes_per_agent_per_hour']} (max_wakes_per_agent_per_hour)")
                return
            if not self._spawn(a, agents[a.agent], reserve_retry=reserve):
                return  # the job ended or was paused meanwhile: start nothing else for it
            state = ja.load_state(self.repo, job_id)
            seen.add((a.agent, a.reason, a.task, a.revision))

    def _render(self) -> None:
        if not (ja.job_path(self.repo).exists() or ja.board_path(self.repo).exists()):
            return
        try:
            board.render(self.repo)
        except Exception as exc:
            self.warn("board", f"could not render BOARD.md: {exc}")

    def _last_wake(self, job: dict) -> float:
        times = [ja.parse_iso(w["time"]) for w in ja.load_state(self.repo, job["id"]).get("wakes", [])]
        return max(times) if times else float("-inf")

    def _auto_end_due(self, job: dict) -> bool:
        """T052: an auto_end job with tasks, all accepted or cancelled, not paused, no wake running."""
        if not job.get("auto_end"):
            return False
        job_id = job["id"]
        if any((ch.job or ch.action.job) == job_id for ch in self.children.values()):
            return False
        if ja.load_state(self.repo, job_id).get("paused"):
            return False
        list_tasks = getattr(ja.get_backend(self.repo), "job_tasks", None)
        if list_tasks is None:  # the stub backend has no job-task listing: never auto-end
            return False
        tasks = list_tasks(self.repo, job)
        return bool(tasks) and all(t["state"] == "cancelled" or t["accepted"] for t in tasks)

    def _auto_end(self, job: dict) -> bool:
        """End `job` like `job.py end` when _auto_end_due; True if it was ended."""
        if not self._auto_end_due(job):
            return False
        with ja.lock(self.repo):
            jobs = ja.load_jobs(self.repo)
            cur = next((j for j in jobs if j["id"] == job["id"]), None)
            if cur is None or not cur.get("active"):
                return False
            cur["active"] = False
            cur["ended"] = ja.iso_now(self.clock())
            cur.setdefault("quotes", []).append(
                {"time": ja.iso_now(self.clock()), "by": "dispatcher", "event": "end",
                 "quote": "auto-end: every task accepted or cancelled"})
            ja.save_jobs(self.repo, jobs)
        self._log(event="auto_end", job=job["id"])
        print(f"auto-ended {job['id']}: every task accepted or cancelled", file=self.err, flush=True)
        return True

    def iterate(self) -> bool:
        """One pass; returns True when any job is active."""
        jobs = ja.load_jobs(self.repo)
        self._release_gone()
        self._reap_and_kill({j["id"]: ja.job_limits(j) for j in jobs})
        active = [j for j in jobs if j.get("active") and not self._auto_end(j)]
        # The job whose last wake is oldest goes first (no wakes first; ties by start order).
        for job in sorted(active, key=self._last_wake):
            self._maybe_wake(job)
        self._render()
        return bool(active)

    def _drain(self) -> None:
        while self.children:
            self._reap_and_kill(self._limits_map())
            if self.children:
                self.sleep(0.05)
        self._render()

    def run(self, once: bool = False) -> int:
        lock_path = ja.hub_dir(self.repo) / ".dispatcher.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lfd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(lfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(lfd)
            print("error: another dispatcher is running for this repository", file=self.err)
            return 1
        try:
            if once:
                self.iterate()
                self._drain()
                return 0
            while True:
                active = self.iterate()
                if not active and not self.children:
                    return 0
                self.sleep(min(self.poll_s, 1.0) if self.children else self.poll_s)
        except KeyboardInterrupt:
            print(f"interrupted; {len(self.children)} running child(ren) left alone", file=self.err)
            return 130
        finally:
            os.close(lfd)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run the dispatcher loop")
    r.add_argument("--repo", help="repository root (default: walk up from cwd to .git)")
    r.add_argument("--poll-s", type=float, default=10.0, help="seconds between iterations (default 10)")
    r.add_argument("--once", action="store_true",
                   help="one iteration, then wait for the spawned children and exit")
    args = ap.parse_args(argv)
    d = Dispatcher(ja.find_repo(args.repo), poll_s=args.poll_s)
    return d.run(once=args.once)


if __name__ == "__main__":
    sys.exit(main())
