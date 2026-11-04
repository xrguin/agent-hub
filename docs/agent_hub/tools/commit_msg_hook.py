#!/usr/bin/env python3
"""Check secrets and ownership in Git's candidate index; never acquire another hub lock.

The secret check (secret_guard.py) runs first, for every commit in every mode.
Mode from the committed hub_config.json `commit_guard`: "full" (default) or "scoped"."""
from pathlib import Path
import re
import sys
import hubtx
import secret_guard


def _policy_config(root):
    """hub_config.json as committed at the main checkout's HEAD (hubtx.policy_config)."""
    return hubtx.policy_config(root)


def main():
    def run():
        if len(sys.argv) != 2:
            raise hubtx.BadInput("Expected Git's commit message filename")
        text = Path(sys.argv[1]).read_text(encoding="utf-8")
        root = hubtx.repo_root()
        found = secret_guard.commit_findings(root, text)
        if found:
            raise hubtx.BadInput(secret_guard.refusal(found))
        if _policy_config(root)["commit_guard"] == "scoped":
            tag = hubtx.scoped_tag(text)
            if hubtx.git(root, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False).returncode == 0:
                # A merge brings in work already committed on its branch, other agents' events
                # included: whoever merges gets the user-level checks (append-only integrity).
                tag = None
            hubtx.check_scoped(tag, root)
            return
        match = re.match(r"\[(claude|gpt|deepseek)\] (T[0-9]{3,}[a-z]?): \S", text)
        if not match:
            raise hubtx.BadInput("Commit subject must be [tag] T###: message")
        hubtx.check_candidate(match[1])
    return hubtx.cli(run)


if __name__ == "__main__":
    raise SystemExit(main())
