#!/usr/bin/env python3
"""Fake agent for test_job_board_hub.py: acts only through the real hub CLIs
(claim.py, hub_commit.py, hub_append.py), never through job_adapters.  Spends no tokens.

reason=claim:  claim the task with its deliverables as scope (if it does not already
               hold the claim), wait --sleep seconds, write the deliverables, commit
               them and append `submit`.  A lost claim race ends cleanly with exit 0.
               With the claim already held (changes requested) it just redelivers.
reason=review: write a review file, commit it, append `review`, then `accept`
               (--outcome accept) or `request_changes` (--outcome changes).
--fail exits 1 after the claim (claim) or before any change (review).
--wait-for PATH waits until PATH exists (lets a test start two agents together).
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hub_events as events  # noqa: E402
import hub_state  # noqa: E402


def cli(repo: Path, script: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(HERE / script), *args], cwd=str(repo),
                          capture_output=True, text=True)


def must(r: subprocess.CompletedProcess, what: str) -> None:
    if r.returncode:
        print(f"{what} failed ({r.returncode}): {r.stderr.strip()}", flush=True)
        raise SystemExit(1)


def append(repo, tag, task, event, fields, note=""):
    line = events.format_event(tag, event, fields, note)
    must(cli(repo, "hub_append.py", tag, task, hub_state.status_path(task), line), "append " + event)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--task", required=True)
    ap.add_argument("--reason", required=True, choices=["claim", "review", "answer"])
    ap.add_argument("--sleep", type=float, default=0.0)
    ap.add_argument("--fail", action="store_true")
    ap.add_argument("--outcome", choices=["accept", "changes"], default="accept")
    ap.add_argument("--wait-for")
    a = ap.parse_args(argv)
    repo, tag, task = Path(a.repo), a.tag, a.task

    if a.wait_for:
        deadline = time.monotonic() + 20
        while not Path(a.wait_for).exists() and time.monotonic() < deadline:
            time.sleep(0.005)

    d = hub_state.derive(repo, task)
    if a.reason == "claim":
        if d["holder"] != tag:
            r = cli(repo, "claim.py", tag, task, *[x for p in d["deliverables"] for x in ("--scope", p)])
            print(f"claimed={r.returncode == 0}", flush=True)
            if r.returncode == 3:
                return 0           # lost the race: nothing to do
            must(r, "claim")
        time.sleep(a.sleep)
        if a.fail:
            return 1
        n = 1 + sum(1 for e in d["events"] if e["event"] == "submit")
        for path in d["deliverables"]:
            target = repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"{tag} delivery {n} of {task}\n")
        must(cli(repo, "hub_commit.py", tag, task, "-m", f"deliver {n}", *d["deliverables"]), "deliver")
        rev = hub_state.revision(repo, d["deliverables"])
        append(repo, tag, task, "submit", dict(rev=rev))
        return 0

    if a.reason == "review":
        time.sleep(a.sleep)
        if a.fail:
            return 1
        rev = d["revision"]
        name = f"docs/agent_hub/reviews/2026-10-31_{task}_{rev[:8]}_{tag}.md"
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text("# Review\n\n| ID | Claim | Verdict | Severity | Evidence |\n"
                                 "|---|---|---|---|---|\n| F1 | ok | CONFIRMED | informational | e |\n")
        must(cli(repo, "hub_commit.py", tag, task, "-m", "review", name), "review file")
        append(repo, tag, task, "review", dict(file=name, rev=rev))
        if a.outcome == "accept":
            append(repo, tag, task, "accept", dict(rev=rev))
        else:
            append(repo, tag, task, "request_changes", dict(file=name))
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
