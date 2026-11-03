#!/usr/bin/env python3
"""Fake agent with a stubborn same-group child; used only by the job-board tests.

It optionally claims its task (--claim stub|hub), starts a child in its own process
group that ignores SIGTERM, writes {"leader": pid, "child": pid} to --pidfile once the
child is ready, then sleeps with the default SIGTERM action.  A wall-time kill that only
signals or waits for the leader therefore leaves the child alive after the leader dies.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import job_adapters as ja  # noqa: E402

CHILD = """
import os, signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
open(sys.argv[1], "w").write(str(os.getpid()))
time.sleep(120)
"""


def hub_claim(repo: Path, tag: str, task: str) -> bool:
    import hub_state
    d = hub_state.derive(repo, task)
    args = [x for p in d["deliverables"] for x in ("--scope", p)]
    r = subprocess.run([sys.executable, str(HERE / "claim.py"), tag, task, *args], cwd=str(repo),
                       capture_output=True, text=True)
    return r.returncode == 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--task", required=True)
    ap.add_argument("--reason", required=True)
    ap.add_argument("--pidfile", required=True)
    ap.add_argument("--claim", choices=["stub", "hub"], default="stub")
    a = ap.parse_args(argv)
    repo = Path(a.repo)
    if a.reason == "claim":
        ok = ja.claim(repo, a.tag, a.task) if a.claim == "stub" else hub_claim(repo, a.tag, a.task)
        if not ok:
            return 0
    pidfile = Path(a.pidfile)
    child_file = pidfile.with_suffix(".child")
    child = subprocess.Popen([sys.executable, "-c", CHILD, str(child_file)])  # same process group
    deadline = time.monotonic() + 20
    while not child_file.exists() and time.monotonic() < deadline:
        time.sleep(0.005)
    while True:  # wait until the child has written its whole pid
        try:
            cpid = int(child_file.read_text())
            break
        except ValueError:
            time.sleep(0.005)
    tmp = pidfile.with_name(pidfile.name + ".tmp")
    tmp.write_text(json.dumps({"leader": os.getpid(), "child": cpid}))
    tmp.replace(pidfile)
    time.sleep(120)
    child.wait()
    return 0


if __name__ == "__main__":
    sys.exit(main())
