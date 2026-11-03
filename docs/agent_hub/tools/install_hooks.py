#!/usr/bin/env python3
"""Explicit local installation; existing hook configurations are preserved.

The hook goes into the common git directory (all linked worktrees) and runs the main
checkout's commit_msg_hook.py."""
import os
from pathlib import Path
import hubtx


def install(tag="gpt", task="T012"):
    with hubtx.transaction(tag, task) as tx:
        configured = hubtx.git(tx.root, "config", "--get", "core.hooksPath", check=False)
        if configured.returncode == 0:
            raise hubtx.BadInput("core.hooksPath is set; preserve it and manually chain commit_msg_hook.py in that hook setup after review")
        # The common git directory is shared by every linked worktree, so one hook covers them all.
        common = (tx.root/hubtx.git(tx.root,"rev-parse","--git-common-dir").stdout.decode().strip()).resolve()
        directory = common / "hooks"
        hook = directory/"commit-msg"
        if os.path.lexists(hook):
            raise hubtx.BadInput("commit-msg already exists; preserve it and manually chain commit_msg_hook.py after review")
        if not (tx.root/(hubtx.HUB+"tools/commit_msg_hook.py")).is_file():
            raise hubtx.BadInput("Repository tool files are missing; copy/commit them before installing")
        directory.mkdir(exist_ok=True)
        script = ('#!/bin/sh\n'
                  '# Runs the main checkout\'s hook, so a linked worktree never runs an old branch\'s tools.\n'
                  'common="$(cd "$(git rev-parse --git-common-dir)" && pwd)" || exit 0\n'
                  'tool="$common/../docs/agent_hub/tools/commit_msg_hook.py"\n'
                  '[ -f "$tool" ] || exit 0\n'
                  'exec python3 "$tool" "$@"\n')
        fd = os.open(hook, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o755)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(script)
            os.fchmod(f.fileno(),0o755)
        return hook


def main():
    def run():
        parser = hubtx.ArgumentParser(description=__doc__)
        parser.add_argument("--tag", default="gpt")
        parser.add_argument("--task", default="T012")
        args = parser.parse_args()
        print(install(args.tag,args.task))
    return hubtx.cli(run)


if __name__ == "__main__":
    raise SystemExit(main())
