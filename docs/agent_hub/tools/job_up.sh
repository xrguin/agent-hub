#!/usr/bin/env bash
# Start the job-board dispatcher in the background, after `job.py start` ([claude], 2026-11-01).
#
#   bash docs/agent_hub/tools/job_up.sh
#
# Refuses when no job is active. Several jobs may be active at once (T038): one dispatcher serves
# them all, so when a dispatcher already holds docs/agent_hub/.dispatcher.lock this says so and
# exits 0. A second dispatcher would be refused by its own lock anyway.
# The dispatcher exits by itself after the last active job has ended, once running wakes finish.
# Output: docs/agent_hub/logs/dispatcher_runs/dispatcher.out (git-ignored).
set -o pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$REPO" || exit 1
active=$(python3 -B -c 'import sys; sys.path.insert(0, sys.argv[1]); import job_adapters as ja; print("yes" if ja.active_jobs(sys.argv[2]) else "no")' \
         "$REPO/docs/agent_hub/tools" "$REPO" 2>/dev/null || echo "no")
if [[ "$active" != "yes" ]]; then
    echo "[job_up] no active job: run job.py start first" >&2
    exit 1
fi
# Is a dispatcher running? Try the lock it holds for its whole life; release it at once.
if ! python3 -c 'import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o644)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    sys.exit(1)' "$REPO/docs/agent_hub/.dispatcher.lock" 2>/dev/null; then
    echo "[job_up] a dispatcher is already running; it serves every active job"
    exit 0
fi
mkdir -p docs/agent_hub/logs/dispatcher_runs
OUT=docs/agent_hub/logs/dispatcher_runs/dispatcher.out
setsid nohup python3 docs/agent_hub/tools/job_dispatcher.py run --repo "$REPO" >> "$OUT" 2>&1 < /dev/null &
pid=$!
sleep 2
if kill -0 "$pid" 2>/dev/null; then
    echo "[job_up] dispatcher running, pid $pid; log $OUT; board docs/agent_hub/BOARD.md"
else
    echo "[job_up] dispatcher exited at once; last lines of $OUT:" >&2
    tail -5 "$OUT" >&2
    exit 1
fi
