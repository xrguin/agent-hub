"""Process-group helpers for the job-board tests (independent of the dispatcher's own code)."""
from __future__ import annotations

import json
import os
import signal
import time
from pathlib import Path


def group_members(pgid: int) -> list[int]:
    """Live (non-zombie) processes whose process group is `pgid`, read from /proc."""
    out = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text().rpartition(")")[2].split()
        except OSError:
            continue
        if len(fields) > 2 and fields[2] == str(pgid) and fields[0] not in ("Z", "X"):
            out.append(int(entry.name))
    return sorted(out)


def read_pids(pidfile: Path, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while not pidfile.exists():
        if time.monotonic() > deadline:
            raise AssertionError(f"{pidfile} was never written: the wake died before its child was ready")
        time.sleep(0.01)
    return json.loads(pidfile.read_text())


def cleanup_group(pidfile: Path) -> None:
    """Test cleanup: SIGKILL whatever is left of the fake agent's group."""
    try:
        pids = json.loads(Path(pidfile).read_text())
    except (OSError, ValueError):
        return
    try:
        os.killpg(pids["leader"], signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
