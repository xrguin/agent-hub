"""T013 integration test: real hub CLIs, real commit-msg hook, throw-away repo.

Runs after T012's tools are accepted. It never touches the real hub state: the
repository root is a TemporaryDirectory, the tools are copied into it for the
hook, and every commit goes through git in that directory.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TOOLS = str(Path(__file__).resolve().parent)
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import hub_events as events
import hub_state
import hubtx
from install_hooks import install


class Integration(unittest.TestCase):
    def setUp(self):
        # Hygiene: no __pycache__ in the throw-away repository (hooks and CLIs run there).
        patch_env = mock.patch.dict(os.environ, {"PYTHONDONTWRITEBYTECODE": "1"})
        patch_env.start()
        self.addCleanup(patch_env.stop)
        self.temp = tempfile.TemporaryDirectory(prefix="t013-integration-")
        self.repo = Path(self.temp.name)
        self.old = os.getcwd()
        self.git("init", "-q")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "tester")
        self.write("docs/agent_hub/DECISIONS.md", "# Decisions\n")
        self.write("docs/agent_hub/QUESTIONS.md", "# Questions\n")
        for tag in ("claude", "gpt", "deepseek"):
            self.write(f"docs/agent_hub/logs/{tag}.md", f"# {tag} log\n")
        (self.repo / "docs/agent_hub/reviews").mkdir(parents=True)
        self.write("pkg/file.txt", "data\n")
        self.write("docs/other.txt", "other\n")
        shutil.copytree(Path(TOOLS), self.repo / "docs/agent_hub/tools",
                        ignore=shutil.ignore_patterns("__pycache__"))
        self.commit("fixture")
        os.chdir(self.repo)
        self.hook = install("gpt", "T012")
        self.assertTrue(self.hook.is_file())

    def tearDown(self):
        os.chdir(self.old)
        self.temp.cleanup()

    def write(self, path, text):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def git(self, *args, check=True):
        result = subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True, text=True)
        if check and result.returncode:
            raise RuntimeError(result.stderr)
        return result

    def commit(self, message="fixture"):
        self.git("add", "-A")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD").stdout.strip()

    def cli(self, name, *args, check_=True):
        env = os.environ.copy()
        env["PYTHONPATH"] = TOOLS
        result = subprocess.run([sys.executable, TOOLS + "/" + name, *args], cwd=str(self.repo),
                                capture_output=True, text=True, env=env)
        if check_ and result.returncode:
            raise RuntimeError(name + ": " + result.returncode.__str__() + "\n" + result.stdout + result.stderr)
        return result

    def append(self, tag, task, path, event, check_=True):
        return self.cli("hub_append.py", tag, task, path, events.format_event(
            tag, event[0], event[1], timestamp=hubtx.now()), check_=check_)

    def test_new_task_claim_review_accept_and_staging(self):
        created = self.cli("new_task.py", "gpt", "Integration task", "--owner", "gpt",
                           "--reviewers", "deepseek", "--deliverables", "pkg/file.txt")
        task = created.stdout.split()[0]
        commit = created.stdout.split()[1]
        self.assertIn(commit, self.git("log", "--format=%H").stdout)
        state = hub_state.derive(self.repo, task)
        self.assertEqual((state["owner"], state["reviewers"]), ("gpt", ["deepseek"]))
        self.cli("claim.py", "gpt", task, "--scope", "pkg/*")
        state = hub_state.derive(self.repo, task)
        self.assertEqual(state["state"], "claimed")
        rev = hub_state.revision(self.repo, ["pkg/file.txt"])
        self.append("gpt", task, hub_state.status_path(task), ("submit", dict(rev=rev)))
        review_path = f"docs/agent_hub/reviews/2026-10-31_{task}_deepseek.md"
        self.write(review_path, "# Review\n\n| ID | Claim | Verdict | Severity | Evidence |\n"
                                 "|---|---|---|---|---|\n| F1 | ok | CONFIRMED | informational | e |\n")
        self.cli("hub_commit.py", "deepseek", task, "-m", "review", review_path)
        self.append("deepseek", task, hub_state.status_path(task), ("review", dict(file=review_path, rev=rev)))
        self.append("deepseek", task, hub_state.status_path(task), ("accept", dict(rev=rev)))
        state = hub_state.derive(self.repo, task)
        self.assertTrue(state["accepted"])
        out = self.cli("hub_status.py", "--tag", "deepseek").stdout
        self.assertIn("seen_through=", out)
        self.assertIn(task, out)
        self.assertIn("accepted=yes", out)
        # A path-limited CLI commit preserves unrelated staged content.
        self.write("docs/other.txt", "staged\n")
        self.git("add", "docs/other.txt")
        self.cli("hub_append.py", "gpt", task, "docs/agent_hub/DECISIONS.md", "- integration append")
        staged = self.git("diff", "--cached", "--name-only").stdout.split()
        self.assertEqual(staged, ["docs/other.txt"])
        self.assertIn("docs/agent_hub/DECISIONS.md", self.git("show", "--name-only", "--format=", "HEAD").stdout)

    def test_hook_rejects_foreign_commit_and_dirty_append(self):
        created = self.cli("new_task.py", "gpt", "Hook task", "--owner", "gpt",
                           "--reviewers", "deepseek", "--deliverables", "pkg/file.txt")
        task = created.stdout.split()[0]
        before = self.git("rev-parse", "HEAD").stdout.strip()
        self.write("docs/agent_hub/logs/gpt.md", "# gpt log\nchanged by deepseek\n")
        self.git("add", "docs/agent_hub/logs/gpt.md")
        bad = subprocess.run(["git", "-C", str(self.repo), "commit", "-m",
                              f"[deepseek] {task}: foreign"], capture_output=True, text=True)
        self.assertNotEqual(bad.returncode, 0)
        self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), before)
        self.assertIn("docs/agent_hub/logs/gpt.md", self.git("diff", "--cached", "--name-only").stdout)
        # Dirty append target is refused with exit 4 and left untouched.
        self.git("reset", "-q", "HEAD", "--", "docs/agent_hub/logs/gpt.md")
        self.git("checkout", "--", "docs/agent_hub/logs/gpt.md")
        self.write(hub_state.status_path(task), self.repo.joinpath(hub_state.status_path(task)).read_text() + "dirty\n")
        result = self.append("gpt", task, hub_state.status_path(task), ("release", {}), check_=False)
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertNotIn("release", self.git("show", "HEAD:docs/agent_hub/tasks/" + task + ".status.md").stdout)

    def test_claim_conflict_and_queue(self):
        a = self.cli("new_task.py", "gpt", "A", "--owner", "gpt", "--reviewers", "deepseek",
                     "--deliverables", "pkg/file.txt").stdout.split()[0]
        self.cli("claim.py", "gpt", a, "--scope", "pkg/*")
        result = self.cli("claim.py", "deepseek", a, "--scope", "pkg/*", check_=False)
        self.assertEqual(result.returncode, 3, result.stderr)
        queue = self.cli("hub_status.py", "--tag", "gpt").stdout
        self.assertIn("Queue [gpt]", queue)
        self.assertIn(a, queue)


if __name__ == "__main__":
    unittest.main()
