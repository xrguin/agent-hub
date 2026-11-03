#!/usr/bin/env python3
"""One command per job-board step for a woken agent ([claude], 2026-11-01).

The mechanics are those of fake_hub_agent.py (tested in test_job_board_hub.py), with real
content written by the agent between the steps:

  job_act.py show   TAG TASK                     state, holder, deliverables, revision, reviewers
  job_act.py claim  TAG TASK                     claim with the deliverables as write scope
                                                 (exit 3: another agent holds it; stop)
  job_act.py submit TAG TASK -m MESSAGE          commit the deliverables, record `submit` at their revision
  job_act.py review TAG TASK --file F (--accept | --changes)
                                                 commit the review file F, record `review`, then
                                                 `accept` or `request_changes`
  job_act.py disposition TAG TASK --finding <review file>#<ID> (--resolved | --upheld) [-m NOTE]
                                                 record a `disposition` on one of the task's blocking
                                                 findings; only the review file's author (the file ends
                                                 `_<TAG>.md`); T054

Writes go only through claim.py, hub_commit.py and hub_append.py (the hub lock and its checks).
Run from the repository root.
"""
from __future__ import annotations
import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hub_events as events  # noqa: E402
import hub_state  # noqa: E402


def _root(start: Path) -> Path:
    for p in (start, *start.parents):
        if (p / ".git").exists():
            return p
    raise SystemExit("error: not inside a git repository")


def cli(repo: Path, script: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(HERE / script), *args], cwd=str(repo),
                          capture_output=True, text=True)


def must(r: subprocess.CompletedProcess, what: str) -> None:
    if r.returncode:
        print(f"error: {what} failed ({r.returncode}): {(r.stderr or r.stdout).strip()}", file=sys.stderr)
        raise SystemExit(r.returncode)


def append(repo: Path, tag: str, task: str, event: str, fields: dict, note: str = "") -> None:
    line = events.format_event(tag, event, fields, note)
    must(cli(repo, "hub_append.py", tag, task, hub_state.status_path(task), line), "append " + event)


def _differ_from_head(repo: Path, paths) -> list:
    """The paths whose working tree differs from HEAD: in bytes (a path missing from HEAD differs),
    or, T046 round 3 (F9), in what git tracks besides the bytes (the executable bit, a symlink),
    which `git diff HEAD` reports under the repository's own core.fileMode."""
    out = []
    for p in paths:
        head = subprocess.run(["git", "show", "HEAD:" + p], cwd=str(repo), capture_output=True)
        if head.returncode or head.stdout != (repo / p).read_bytes():
            out.append(p)
            continue
        diff = subprocess.run(["git", "diff", "--quiet", "HEAD", "--", p], cwd=str(repo), capture_output=True)
        if diff.returncode:
            out.append(p)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("show", "claim", "submit", "review", "disposition"):
        p = sub.add_parser(name)
        p.add_argument("tag")
        p.add_argument("task")
        if name == "submit":
            p.add_argument("-m", "--message", required=True)
        if name == "disposition":
            p.add_argument("--finding", required=True, metavar="FILE#ID")
            p.add_argument("-m", "--message", default="")
            g = p.add_mutually_exclusive_group(required=True)
            g.add_argument("--resolved", action="store_true")
            g.add_argument("--upheld", action="store_true")
        if name == "review":
            p.add_argument("--file", required=True)
            g = p.add_mutually_exclusive_group(required=True)
            g.add_argument("--accept", action="store_true")
            g.add_argument("--changes", action="store_true")
    a = ap.parse_args(argv)
    repo = _root(Path.cwd())
    tag, task = a.tag, a.task
    if tag not in events.TAGS:
        raise SystemExit(f"error: unknown tag {tag!r}; expected one of {sorted(events.TAGS)}")
    d = hub_state.derive(repo, task)

    if a.cmd == "show":
        for key in ("state", "holder", "owner", "reviewers", "deliverables", "revision", "accepted"):
            print(f"{key}: {d.get(key)}")
        print(f"status file: {hub_state.status_path(task)}")
        return 0

    if a.cmd == "claim":
        if d["holder"] == tag:
            print(f"{task}: already held by {tag}")
            return 0
        r = cli(repo, "claim.py", tag, task, *[x for p in d["deliverables"] for x in ("--scope", p)])
        if r.returncode == 3:
            print(f"{task}: claimed by another agent; stop", file=sys.stderr)
            return 3
        must(r, "claim")
        print(f"{task}: claimed by {tag}; write scope {d['deliverables']}")
        return 0

    if a.cmd == "submit":
        if d["holder"] != tag:
            raise SystemExit(f"error: {tag} does not hold {task} (holder {d['holder']})")
        missing = [p for p in d["deliverables"] if not (repo / p).is_file()]
        if missing:
            raise SystemExit(f"error: deliverables missing: {missing}")
        # T044: work already done counts too (README §9).  Deliverables already committed as they are
        # have nothing to commit; the submit is recorded at their committed revision.
        # T046 F3: "committed as they are" is checked byte by byte against HEAD, not with `git status`,
        # which hides ignored files and, with status.showUntrackedFiles=no, untracked ones.
        if _differ_from_head(repo, d["deliverables"]):
            must(cli(repo, "hub_commit.py", tag, task, "-m", a.message, *d["deliverables"]), "commit deliverables")
        else:
            print(f"{task}: deliverables unchanged since HEAD; nothing to commit")
        left = _differ_from_head(repo, d["deliverables"])
        if left:  # e.g. an ignored file the commit did not take: record no submit
            raise SystemExit(f"error: deliverables not committed as they are on disk: {left}; no submit recorded")
        rev = hub_state.revision(repo, d["deliverables"])
        append(repo, tag, task, "submit", dict(rev=rev))
        print(f"{task}: submitted at revision {rev[:12]}")
        return 0

    if a.cmd == "disposition":
        key = a.finding
        file_part = key.split("#", 1)[0]
        if not file_part.endswith(f"_{tag}.md"):
            raise SystemExit(f"error: only the review file's author may dispose of a finding: "
                             f"the file in --finding must end with _{tag}.md, got {file_part!r}")
        valid = d["all_findings"]
        if key not in [f["finding"] for f in valid]:
            lines = [f"  {f['finding']}  {f['verdict']}  {f['claim'][:60]}" for f in valid] or ["  (none)"]
            raise SystemExit(f"error: {key!r} is not a blocking finding of {task}; valid keys:\n" + "\n".join(lines))
        if a.resolved and any(f["verdict"] == "UNVERIFIABLE" for f in valid if f["finding"] == key):
            # T054 round 2 (deepseek F7), round 3 (gpt F7, deepseek F8: a re-review may reuse the ID):
            # a disposition cannot close an UNVERIFIABLE row, so do not report "resolved".
            raise SystemExit(f"error: {key} names an UNVERIFIABLE row: only the user's accept_limitation "
                             "closes it; --resolved would not")
        result = "resolved" if a.resolved else "upheld"
        append(repo, tag, task, "disposition", dict(finding=key, result=result), a.message)
        print(f"{task}: {key} {result}")
        return 0

    # review
    if tag == d.get("owner") or tag == d["holder"]:
        raise SystemExit(f"error: {tag} may not review its own task {task}")
    name = Path(a.file).as_posix()
    if not (name.startswith("docs/agent_hub/reviews/") and name.endswith(f"_{tag}.md")):
        raise SystemExit(f"error: the review file must be docs/agent_hub/reviews/<date>_<task>_{tag}.md")
    if not (repo / name).is_file():
        raise SystemExit(f"error: {name} does not exist")
    rev = d["revision"]
    must(cli(repo, "hub_commit.py", tag, task, "-m", "review", name), "commit review file")
    append(repo, tag, task, "review", dict(file=name, rev=rev))
    if a.accept:
        append(repo, tag, task, "accept", dict(rev=rev))
    else:
        append(repo, tag, task, "request_changes", dict(file=name))
    print(f"{task}: review recorded ({'accept' if a.accept else 'changes requested'}) at revision {rev[:12]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
