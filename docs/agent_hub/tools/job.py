#!/usr/bin/env python3
"""Job switch for the job board: start, end, resume, status, check, set-config, migrate.

Writes docs/agent_hub/JOB.json atomically while holding the hub lock.  Every
call records the user's words (--quote) and who heard them (--by).  `end` and the
dispatcher's wake admission take the same lock, so no wake starts after `end` returns.
`start --auto-end` (or `resume --auto-end`) lets the dispatcher end the job itself once it has
tasks and every one is accepted or cancelled (T052).

Several jobs may be active at once (T038); each has an id (job-1, job-2, ...).  `start` always
adds a job.  `end` and `resume` act on the only active job, or on the one named by `--job ID`;
with none or several active and no --job they refuse and write nothing.  `migrate` rewrites
the legacy single-job JOB.json and .job_state.json in the multi-job form.

`check <task> --ok|--not-ok --quote TEXT --by TAG` records the user's verdict on a job task
(T-hub-028): one D### block in DECISIONS.md through the hub transaction (the quote is the
user's words), and for --not-ok also a `user action=open` event that reopens the task.
It refuses inside a dispatcher wake (the environment variable JOB_TAG is set): the user's words
are recorded only by an agent in the user's own chat.

`set-config <task> --key KEY --value JSON --quote TEXT --by TAG` records the user's decision to change
hub_config.json: one D### block and a `user action=set_config` event on the task.  The commit guard
then lets an agent commit exactly that value.  The same rule applies: only in the user's own chat.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import job_adapters as ja  # noqa: E402


def _parse_limits(items: list[str]) -> dict:
    out = {}
    for item in items or []:
        key, sep, val = item.partition("=")
        if not sep or key not in ja.DEFAULT_LIMITS:
            raise SystemExit(f"error: --limit expects key=value with key in {sorted(ja.DEFAULT_LIMITS)}: {item!r}")
        try:
            num = float(val)
        except ValueError:
            raise SystemExit(f"error: --limit {key}: {val!r} is not a number")
        if num <= 0:
            raise SystemExit(f"error: --limit {key}: must be positive")
        out[key] = int(num) if num == int(num) else num
    return out


def _quote(job: dict, by: str, event: str, quote: str) -> None:
    job["quotes"].append({"time": ja.iso_now(), "by": by, "event": event, "quote": quote})


def _render(repo) -> None:
    try:
        import board
        board.render(repo)
    except Exception as exc:  # the board is derived data; never fail the switch for it
        print(f"warning: could not render BOARD.md: {exc}", file=sys.stderr)


def cmd_start(repo, a) -> int:
    limits = dict(ja.DEFAULT_LIMITS)
    limits.update(_parse_limits(a.limit))
    with ja.lock(repo):
        jobs = ja.load_jobs(repo)
        job = {"schema": "job-1", "id": ja.next_job_id(jobs), "match": "id", "active": True,
               "goal": a.goal, "started": ja.iso_now(), "ended": None, "quotes": [], "limits": limits,
               "auto_end": bool(a.auto_end)}
        _quote(job, a.by, "start", a.quote)
        ja.save_jobs(repo, jobs + [job])
        ja.save_state(repo, ja.default_state(), job["id"])  # a new job starts with fresh counters
    _render(repo)
    print(f"job started: {job['id']}: {a.goal}")
    return 0


def _choose(repo, a):
    """The job `end` / `resume` act on, or None after printing why not (call under the lock)."""
    try:
        job = ja.select_job(repo, a.job)
    except ja.JobChoice as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None
    if not job.get("active"):
        print(f"error: {job['id']} is not active", file=sys.stderr)
        return None
    return job


def _replace(jobs: list[dict], job: dict) -> list[dict]:
    return [job if j["id"] == job["id"] else j for j in jobs]


def cmd_end(repo, a) -> int:
    with ja.lock(repo):
        job = _choose(repo, a)
        if job is None:
            return 1
        job["active"] = False
        job["ended"] = ja.iso_now()
        _quote(job, a.by, "end", a.quote)
        ja.save_jobs(repo, _replace(ja.load_jobs(repo), job))
    _render(repo)
    print(f"job ended: {job['id']}")
    return 0


def cmd_resume(repo, a) -> int:
    with ja.lock(repo):
        job = _choose(repo, a)
        if job is None:
            return 1
        state = ja.load_state(repo, job["id"])
        was = state.get("paused")
        state["paused"] = False
        state["pause_reason"] = None
        job["limits"].update(_parse_limits(a.limit))
        _quote(job, a.by, "resume", a.quote)
        if a.auto_end:  # T052: the user lets the dispatcher end this job once every task is done
            job["auto_end"] = True
            _quote(job, a.by, "auto_end", a.quote)
        ja.save_state(repo, state, job["id"])
        ja.save_jobs(repo, _replace(ja.load_jobs(repo), job))
    _render(repo)
    print(f"job resumed: {job['id']}" if was else f"job {job['id']} was not paused (quote recorded)")
    return 0


def cmd_migrate(repo, a) -> int:
    with ja.lock(repo):
        changed = ja.migrate(repo)
    if changed:
        _render(repo)
    print("migrated JOB.json and .job_state.json to the multi-job form" if changed
          else "already in the multi-job form")
    return 0


class Refused(Exception):
    """The check is not allowed; nothing was written."""


def _check_in_transaction(tx, repo, task, ok, quote):
    """Validate inside the hub transaction (so the state cannot move), then write.

    Returns (decision id, word).  The D### block is committed before the status event is
    appended: hubtx authorizes a `user` event only against the DECISIONS.md at HEAD."""
    import hub_events as events
    import hub_state
    import hubtx
    mine = ja.HubBackend().find_task(repo, task)  # looked up in every job, active or ended
    shown = ja.display_name(repo, task)
    if mine is None:
        raise Refused(f"{shown} is not a task of this job or of any other job (no job id in its created "
                      "event, and not created while a time-window job ran); a check is only recorded "
                      "for job tasks")
    if ok and not mine["accepted"]:
        raise Refused(f"{shown} is {mine['state']}, not accepted; --ok needs an accepted task")
    if not ok and not (mine["state"] == "accepted" or mine["state"].startswith("review")):
        raise Refused(f"{shown} is {mine['state']}; --not-ok needs a task in review or accepted")
    path = hub_state.HUB + "DECISIONS.md"
    current = (hubtx._blob(tx.root, path) or b"").decode("utf-8", errors="replace")
    decision = events.next_decision(current)
    word = "ok" if ok else "not_ok"
    block = "\n".join([
        "",
        f"### {decision} · {hubtx.now()} · recorded by [{tx.tag}] · heard in [{tx.tag}]'s chat",
        "- event time: unknown",
        f'- quote: "{quote}"',
        f"- scope: task={task}; check={word}",
        f"- revision: {mine['revision']}",
        "- supersedes: none",
        "- answers: none",
    ])
    tx.append(path, block)
    tx.commit([path], f"record {decision} (user check {word} {task})")
    if not ok:
        line = events.format_event(tx.tag, "user", dict(decision=decision, action="open", task=task),
                                   note=f"user check not OK ({decision})")
        status = hub_state.status_path(task)
        tx.append(status, line)
        tx.commit([status], f"reopen {task} after user check {decision}")
    return decision, word


def cmd_check(repo, a) -> int:
    import hub_events as events
    import hubtx
    import task_names
    try:
        if os.environ.get("JOB_TAG"):  # the dispatcher sets JOB_TAG in every wake it starts
            raise Refused("job.py check records the user's words and runs only in the user's chat, "
                          "not inside a job-board wake (JOB_TAG is set)")
        if ja.get_backend(repo).name != "hub":
            raise Refused('check needs the hub backend (hub_config.json {"job_backend": "hub"})')
        if not ja.load_jobs(repo):
            raise Refused("no job has been started; there is nothing to check")
        if a.by not in events.TAGS:
            raise Refused(f"--by must be one of {sorted(events.TAGS)}")
        quote = " ".join(str(a.quote).split())  # one line: a quote can never open a new block
        if not quote:
            raise Refused("--quote must hold the user's words")
        try:
            task = task_names.canonical(a.task, repo)
        except ValueError as exc:
            raise Refused(str(exc))
        with contextlib.chdir(repo):  # hubtx works on the repository of the current directory
            with hubtx.transaction(a.by, task) as tx:
                decision, word = _check_in_transaction(tx, repo, task, a.ok, quote)
    except Refused as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except hubtx.HubError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    _render(repo)
    shown = ja.display_name(repo, task)
    print(f"recorded {decision}: check {word} for {shown}" + ("" if a.ok else "; the task is open again"))
    return 0


def _set_config_in_transaction(tx, task, key, text, quote):
    """Record the decision, then the event: hubtx authorizes a `user` event only against the
    DECISIONS.md at HEAD.  Returns the decision id."""
    import hub_events as events
    import hub_state
    import hubtx
    path = hub_state.HUB + "DECISIONS.md"
    current = (hubtx._blob(tx.root, path) or b"").decode("utf-8", errors="replace")
    decision = events.next_decision(current)
    block = "\n".join([
        "",
        f"### {decision} · {hubtx.now()} · recorded by [{tx.tag}] · heard in [{tx.tag}]'s chat",
        "- event time: unknown",
        f'- quote: "{quote}"',
        f"- scope: config={key}; value={text}",
        "- revision: none",
        "- supersedes: none",
        "- answers: none",
    ])
    tx.append(path, block)
    tx.commit([path], f"record {decision} (set {key})")
    line = events.format_event(tx.tag, "user", dict(decision=decision, action="set_config", key=key, value=text),
                               note=f"user set {key} ({decision})")
    status = hub_state.status_path(task)
    tx.append(status, line)
    tx.commit([status], f"record set_config of {key} ({decision})")
    return decision


def cmd_set_config(repo, a) -> int:
    import hub_events as events
    import hub_state
    import hubtx
    import task_names
    try:
        if os.environ.get("JOB_TAG"):  # the dispatcher sets JOB_TAG in every wake it starts
            raise Refused("job.py set-config records the user's words and runs only in the user's chat, "
                          "not inside a job-board wake (JOB_TAG is set)")
        if a.by not in events.TAGS:
            raise Refused(f"--by must be one of {sorted(events.TAGS)}")
        quote = " ".join(str(a.quote).split())  # one line: a quote can never open a new block
        if not quote:
            raise Refused("--quote must hold the user's words")
        if a.key == "_comment" or not a.key.strip():
            raise Refused("--key must name a setting of hub_config.json")
        try:
            value = json.loads(a.value)
        except ValueError:
            raise Refused("--value must be JSON: quote a text as '\"hub\"', and write a Boolean as true")
        try:
            hubtx._validate_config({a.key: value})
            task = task_names.canonical(a.task, repo)
        except (hubtx.BadInput, ValueError) as exc:
            raise Refused(str(exc))
        with contextlib.chdir(repo):  # hubtx works on the repository of the current directory
            with hubtx.transaction(a.by, task) as tx:
                if hubtx._blob(tx.root, hub_state.status_path(task)) is None:
                    raise Refused(f"{task} does not exist; create the task that will change the config first")
                decision = _set_config_in_transaction(tx, task, a.key, json.dumps(value), quote)
    except Refused as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except hubtx.HubError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    print(f"recorded {decision}: {a.key} = {json.dumps(value)} on {ja.display_name(repo, task)}. "
          "The task's owner can now change hub_config.json and submit.")
    return 0


def _info(job: dict, state: dict) -> dict:
    return {
        "id": job["id"],
        "active": bool(job.get("active")),
        "goal": job.get("goal"),
        "started": job.get("started"),
        "ended": job.get("ended"),
        "paused": bool(state.get("paused")),
        "pause_reason": state.get("pause_reason"),
        "limits": ja.job_limits(job),
        "wakes": len(state.get("wakes", [])),
        "tokens": state.get("tokens", {}),
        "quotes": job.get("quotes", []),
    }


def cmd_status(repo, a) -> int:
    visible = [_info(j, ja.load_state(repo, j["id"])) for j in ja.visible_jobs(repo)]
    if a.job:
        job = ja.get_job(repo, a.job)
        if job is None:
            print(f"error: unknown job {a.job}", file=sys.stderr)
            return 1
        chosen = [_info(job, ja.load_state(repo, job["id"]))]
    else:
        job = ja.default_job(repo)
        chosen = [_info(job, ja.load_state(repo, job["id"]))] if job else []
    if a.json:
        top = dict(chosen[0]) if chosen else {
            "id": None, "active": False, "goal": None, "started": None, "ended": None, "paused": False,
            "pause_reason": None, "limits": ja.job_limits(None), "wakes": 0, "tokens": {}, "quotes": []}
        top["jobs"] = visible
        print(json.dumps(top, indent=2, ensure_ascii=False))
    else:
        shown = chosen if a.job else visible
        if not shown:
            print("no job")
        for info in shown:
            print(f"{info['id']} {'active' if info['active'] else 'ended'}: {info['goal']}")
            print(f"started {info['started']}" + (f", ended {info['ended']}" if info["ended"] else ""))
            print(f"paused: {info['pause_reason']}" if info["paused"] else "not paused")
            print(f"wakes: {info['wakes']}  tokens: {info['tokens']}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, quote=True):
        p.add_argument("--repo", help="repository root (default: walk up from cwd to .git)")
        if quote:
            p.add_argument("--quote", required=True, help="the user's words, verbatim")
            p.add_argument("--by", required=True, help="your tag, e.g. claude")

    s = sub.add_parser("start", help="start a job")
    s.add_argument("--goal", required=True)
    s.add_argument("--limit", action="append", default=[], metavar="KEY=VALUE",
                   help=f"override a limit ({', '.join(ja.DEFAULT_LIMITS)}); repeatable")
    s.add_argument("--auto-end", action="store_true",
                   help="the dispatcher ends the job by itself once every task is accepted or cancelled")
    common(s)
    e = sub.add_parser("end", help="end a job (running agents finish)")
    e.add_argument("--job", metavar="ID", help="the job to end (default: the only active job)")
    common(e)
    r = sub.add_parser("resume", help="clear a pause (only the user may ask for this)")
    r.add_argument("--job", metavar="ID", help="the job to resume (default: the only active job)")
    r.add_argument("--limit", action="append", default=[], metavar="KEY=VALUE",
                   help="raise a limit while resuming; repeatable")
    r.add_argument("--auto-end", action="store_true",
                   help="also let the dispatcher end the job once every task is accepted or cancelled")
    common(r)
    c = sub.add_parser("check", help="record the user's check of a job task (ok or not ok)")
    c.add_argument("task", help="task id, T028 or T-hub-028")
    verdict = c.add_mutually_exclusive_group(required=True)
    verdict.add_argument("--ok", dest="ok", action="store_true", help="the user accepts the result")
    verdict.add_argument("--not-ok", dest="ok", action="store_false", help="the user rejects it; the task reopens")
    common(c)
    sc = sub.add_parser("set-config", help="record the user's decision to change a key of hub_config.json")
    sc.add_argument("task", help="the task that will make the change, T028 or T-hub-028")
    sc.add_argument("--key", required=True, help="the setting, for example task_branch")
    sc.add_argument("--value", required=True, help="its new value as JSON, for example '\"hub\"' or true")
    common(sc)
    st = sub.add_parser("status", help="show the jobs on the board")
    st.add_argument("--job", metavar="ID", help="show (and, with --json, select) this job")
    st.add_argument("--json", action="store_true")
    common(st, quote=False)
    m = sub.add_parser("migrate", help="rewrite JOB.json and .job_state.json in the multi-job form")
    common(m, quote=False)

    args = ap.parse_args(argv)
    repo = ja.find_repo(args.repo)
    return {"start": cmd_start, "end": cmd_end, "resume": cmd_resume, "check": cmd_check,
            "set-config": cmd_set_config, "status": cmd_status, "migrate": cmd_migrate}[args.cmd](repo, args)


if __name__ == "__main__":
    sys.exit(main())
