#!/usr/bin/env python3
"""Fake agent process, used only by test_job_board.py (spends no tokens).

reason=claim:  claim the task; on success hold it for --sleep seconds, then set
               it to review with the fixture's reviewer and revision + 1.
reason=review: mark the current revision reviewed and set status accepted.
reason=answer: clear the question and its answer.
--fail exits 1 after the claim (claim) or before any change (others).
--wait-for PATH waits until PATH exists (lets tests start two agents together).
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import job_adapters as ja  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--task", required=True)
    ap.add_argument("--reason", required=True, choices=["claim", "review", "answer"])
    ap.add_argument("--sleep", type=float, default=0.0)
    ap.add_argument("--fail", action="store_true")
    ap.add_argument("--wait-for")
    a = ap.parse_args(argv)
    repo = Path(a.repo)

    def done(code: int) -> int:
        print("tokens=1234", flush=True)
        return code

    if a.wait_for:
        deadline = time.monotonic() + 20
        while not Path(a.wait_for).exists() and time.monotonic() < deadline:
            time.sleep(0.005)

    if a.reason == "claim":
        ok = ja.claim(repo, a.tag, a.task)
        print(f"claimed={ok}", flush=True)
        if not ok:
            return done(0)
        time.sleep(a.sleep)
        if a.fail:
            return done(1)

        def f(t):
            t["status"] = "review"
            t["reviewer"] = t.get("reviewer") or t.get("posted_by")
            t["revision"] += 1
        ja.stub_update_task(repo, a.task, f)
        return done(0)

    time.sleep(a.sleep)
    if a.fail:
        return done(1)
    if a.reason == "review":
        def f(t):
            t["reviewed_revisions"].append(t["revision"])
            t["status"] = "accepted"
    else:
        def f(t):
            t["question"] = t["answer"] = t["question_by"] = None
    ja.stub_update_task(repo, a.task, f)
    return done(0)


if __name__ == "__main__":
    sys.exit(main())
