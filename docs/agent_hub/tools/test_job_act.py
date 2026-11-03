#!/usr/bin/env python3
"""Tests for job_act.py in a throw-away repository ([claude], 2026-11-01).

    python3 -m unittest discover -s docs/agent_hub/tools -p 'test_job_act.py'
"""
from __future__ import annotations
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hub_state  # noqa: E402
from test_job_board_hub import HubBase  # noqa: E402


class TestJobAct(HubBase):
    def act(self, *args, code=0):
        r = subprocess.run([sys.executable, str(self.tools / "job_act.py"), *args], cwd=str(self.repo),
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, code, r.stdout + r.stderr)
        return r

    def test_claim_submit_review_accept(self):
        self.start_job()
        task = self.post("claude", "Write the file", "job/out.txt", reviewers=["gpt"])
        self.act("claim", "deepseek", task)
        self.act("claim", "gpt", task, code=3)                       # already held: the loser stops
        self.act("claim", "deepseek", task)                          # re-claim by the holder is a no-op
        (self.repo / "job").mkdir()
        (self.repo / "job" / "out.txt").write_text("result\n")
        self.act("submit", "deepseek", task, "-m", "deliver")
        self.assertEqual(self.state(task)["state"], "review")
        self.act("review", "deepseek", task, "--file", f"docs/agent_hub/reviews/x_{task}_deepseek.md",
                 "--accept", code=1)                                 # the holder may not review its own work
        review = self.repo / "docs" / "agent_hub" / "reviews" / f"2026-11-01_{task}_gpt.md"
        review.parent.mkdir(parents=True, exist_ok=True)
        review.write_text("# Review\n\n| ID | Claim | Verdict | Severity | Evidence |\n|---|---|---|---|---|\n"
                          "| F1 | ok | CONFIRMED | informational | e |\n")
        self.act("review", "gpt", task, "--file", review.relative_to(self.repo).as_posix(), "--accept")
        s = self.state(task)
        self.assertTrue(s["accepted"])
        self.assertEqual(s["accepted_revision"], hub_state.revision(self.repo, ["job/out.txt"]))
        subjects = self.git("log", "--format=%s")
        self.assertIn(f"[deepseek] {task}: claim", subjects)
        self.assertIn(f"[gpt] {task}: append", subjects)

    # ---- T054: the disposition subcommand
    def reviewed_with_wrong_row(self, extra_rows=""):
        """A task whose reviewer gpt requested changes with one WRONG blocking row (F1) and any
        `extra_rows`; returns task, file."""
        self.start_job()
        task = self.post("claude", "Write the file", "job/out.txt", reviewers=["gpt"])
        self.act("claim", "deepseek", task)
        (self.repo / "job").mkdir()
        (self.repo / "job" / "out.txt").write_text("result\n")
        self.act("submit", "deepseek", task, "-m", "deliver")
        review = self.repo / "docs" / "agent_hub" / "reviews" / f"2026-11-03_{task}_gpt.md"
        review.parent.mkdir(parents=True, exist_ok=True)
        review.write_text("# Review\n\n| ID | Claim | Verdict | Severity | Evidence |\n|---|---|---|---|---|\n"
                          "| F1 | the output is wrong | WRONG | blocking | e |\n"
                          "| F2 | fine | CONFIRMED | informational | e |\n" + extra_rows)
        name = review.relative_to(self.repo).as_posix()
        self.act("review", "gpt", task, "--file", name, "--changes")
        return task, name

    def status_lines(self, task):
        return (self.repo / hub_state.status_path(task)).read_text().splitlines()

    def test_disposition_resolved_writes_the_event_and_closes_the_finding(self):
        task, name = self.reviewed_with_wrong_row()
        self.assertEqual([f["finding"] for f in self.state(task)["open_findings"]], [name + "#F1"])
        r = self.act("disposition", "gpt", task, "--finding", name + "#F1", "--resolved", "-m", "fixed")
        self.assertEqual(r.stdout.strip(), f"{task}: {name}#F1 resolved")
        last = self.status_lines(task)[-1].split("\t")
        self.assertEqual((last[1], last[2]), ("gpt", "disposition"))
        self.assertIn("result=resolved", last[3])
        self.assertEqual(last[4], "fixed")
        finding = self.state(task)["all_findings"][0]
        self.assertTrue(finding["closed"])
        self.assertEqual(finding["closed_by"], "disposition")
        self.assertEqual(self.state(task)["open_findings"], [])

    def test_disposition_upheld_writes_result_upheld(self):
        task, name = self.reviewed_with_wrong_row()
        self.act("disposition", "gpt", task, "--finding", name + "#F1", "--upheld")
        last = self.status_lines(task)[-1].split("\t")
        self.assertEqual((last[1], last[2]), ("gpt", "disposition"))
        self.assertIn("result=upheld", last[3])
        self.assertFalse(self.state(task)["all_findings"][0]["closed"])

    def test_disposition_by_a_non_author_is_refused(self):
        task, name = self.reviewed_with_wrong_row()
        before = self.status_lines(task)
        r = self.act("disposition", "deepseek", task, "--finding", name + "#F1", "--resolved", code=1)
        self.assertIn("_deepseek.md", r.stderr)
        self.assertEqual(self.status_lines(task), before)

    def test_disposition_of_an_unknown_finding_lists_the_valid_keys(self):
        task, name = self.reviewed_with_wrong_row()
        before = self.status_lines(task)
        r = self.act("disposition", "gpt", task, "--finding", name + "#F9", "--resolved", code=1)
        self.assertIn(name + "#F1", r.stderr)                 # the valid key, with its verdict
        self.assertIn("WRONG", r.stderr)
        self.assertNotIn("F2", r.stderr)                      # the informational row is no blocking finding
        self.assertEqual(self.status_lines(task), before)

    def test_disposition_resolved_of_an_unverifiable_row_is_refused(self):
        # T054 round 2 (deepseek F7): only the user's accept_limitation closes an UNVERIFIABLE row.
        task, name = self.reviewed_with_wrong_row("| F3 | not checkable here | UNVERIFIABLE | blocking | e |\n")
        before = self.status_lines(task)
        r = self.act("disposition", "gpt", task, "--finding", name + "#F3", "--resolved", code=1)
        self.assertIn("UNVERIFIABLE", r.stderr)
        self.assertIn("accept_limitation", r.stderr)
        self.assertEqual(self.status_lines(task), before)
        self.act("disposition", "gpt", task, "--finding", name + "#F3", "--upheld")   # upheld is still recorded
        self.assertIn("result=upheld", self.status_lines(task)[-1])

    def test_disposition_resolved_is_refused_when_a_reused_id_is_unverifiable(self):
        # T054 round 3 (gpt F7, deepseek F8): a re-review table reuses F1 with verdict UNVERIFIABLE.
        task, name = self.reviewed_with_wrong_row()
        review = self.repo / name
        review.write_text(review.read_text() + "\n## Re-review\n\n| ID | Claim | Verdict | Severity | Evidence |\n"
                          "|---|---|---|---|---|\n| F1 | cannot be checked here | UNVERIFIABLE | blocking | e |\n")
        self.cli("hub_commit.py", "gpt", task, "-m", "re-review", name)
        self.assertEqual([f["verdict"] for f in self.state(task)["all_findings"]], ["WRONG", "UNVERIFIABLE"])
        before = self.status_lines(task)
        r = self.act("disposition", "gpt", task, "--finding", name + "#F1", "--resolved", code=1)
        self.assertIn("UNVERIFIABLE", r.stderr)
        self.assertEqual(self.status_lines(task), before)

    def test_submit_of_work_already_committed(self):
        # T044: deliverables committed before the claim are submitted without a deliverables commit.
        (self.repo / "job").mkdir()
        (self.repo / "job" / "done.txt").write_text("already done\n")
        self.git("add", "job/done.txt")
        self.git("commit", "-qm", "work done before the task")
        self.start_job()
        task = self.post("claude", "Work already done", "job/done.txt", owner="claude", reviewers=["gpt"])
        self.act("claim", "claude", task)
        head = self.git("rev-parse", "HEAD").strip()
        r = self.act("submit", "claude", task, "-m", "deliver")
        self.assertIn("nothing to commit", r.stdout)
        self.assertEqual(self.state(task)["state"], "review")
        self.assertEqual(self.state(task)["submitted_revision"], hub_state.revision(self.repo, ["job/done.txt"]))
        self.assertEqual(self.git("log", "--format=%s", f"{head}..HEAD").strip().splitlines(),
                         [f"[claude] {task}: append docs/agent_hub/tasks/{task}.status.md"])

    def test_submit_of_an_ignored_deliverable_is_refused(self):
        # T046 F3: `git status` hides ignored files; the unchanged path must not be taken for them.
        (self.repo / ".gitignore").write_text((self.repo / ".gitignore").read_text() + "job/out.txt\n"
                                              if (self.repo / ".gitignore").exists() else "job/out.txt\n")
        self.git("add", ".gitignore")
        self.git("commit", "-qm", "ignore the deliverable")
        self.start_job()
        task = self.post("claude", "Ignored file", "job/out.txt", owner="claude", reviewers=["gpt"])
        self.act("claim", "claude", task)
        (self.repo / "job").mkdir()
        (self.repo / "job" / "out.txt").write_text("payload\n")
        r = subprocess.run([sys.executable, str(self.tools / "job_act.py"), "submit", "claude", task, "-m", "x"],
                           cwd=str(self.repo), capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("nothing to commit", r.stdout)
        self.assertEqual(self.state(task)["state"], "claimed")
        self.assertEqual(self.state(task)["submitted_revision"], None)

    def test_submit_of_a_hidden_untracked_deliverable_commits_it(self):
        # T046 F3: with status.showUntrackedFiles=no, an untracked deliverable is still committed.
        self.git("config", "status.showUntrackedFiles", "no")
        self.start_job()
        task = self.post("claude", "Hidden file", "job/out.txt", owner="claude", reviewers=["gpt"])
        self.act("claim", "claude", task)
        (self.repo / "job").mkdir()
        (self.repo / "job" / "out.txt").write_text("payload\n")
        r = self.act("submit", "claude", task, "-m", "deliver")
        self.assertNotIn("nothing to commit", r.stdout)
        self.assertEqual(self.git("show", "HEAD:job/out.txt"), "payload\n")
        self.assertEqual(self.state(task)["state"], "review")

    def test_submit_of_a_mode_only_change_commits_the_mode(self):
        # T046 round 3 (F9): making a committed script executable leaves its bytes unchanged; the
        # submit must still commit the new mode, not take the "unchanged" path.
        self.git("config", "core.fileMode", "true")
        (self.repo / "job").mkdir()
        (self.repo / "job" / "run.sh").write_text("#!/bin/sh\nexit 0\n")
        (self.repo / "job" / "run.sh").chmod(0o644)
        self.git("add", "job/run.sh")
        self.git("commit", "-qm", "script, not executable")
        self.start_job()
        task = self.post("claude", "Make the script executable", "job/run.sh", owner="claude", reviewers=["gpt"])
        self.act("claim", "claude", task)
        (self.repo / "job" / "run.sh").chmod(0o755)
        r = self.act("submit", "claude", task, "-m", "make it executable")
        self.assertNotIn("nothing to commit", r.stdout)
        self.assertEqual(self.git("ls-tree", "HEAD", "job/run.sh").split()[0], "100755")
        self.assertEqual(self.git("diff", "--name-only", "HEAD", "--", "job/run.sh").strip(), "")
        self.assertEqual(self.state(task)["state"], "review")

    def test_changes_requested_then_redeliver(self):
        self.start_job()
        task = self.post("claude", "Write the file", "job/out.txt", reviewers=["gpt"])
        self.act("claim", "deepseek", task)
        (self.repo / "job").mkdir()
        (self.repo / "job" / "out.txt").write_text("first\n")
        self.act("submit", "deepseek", task, "-m", "deliver 1")
        review = f"docs/agent_hub/reviews/2026-11-01_{task}_gpt.md"
        (self.repo / review).parent.mkdir(parents=True, exist_ok=True)
        (self.repo / review).write_text("# Review\n\n| ID | Claim | Verdict | Severity | Evidence |\n|---|---|---|---|---|\n"
                                        "| F1 | x | WRONG | blocking | e |\n")
        self.act("review", "gpt", task, "--file", review, "--changes")
        self.assertFalse(self.state(task)["accepted"])
        (self.repo / "job" / "out.txt").write_text("second\n")
        self.act("submit", "deepseek", task, "-m", "deliver 2")
        self.assertEqual(self.state(task)["state"], "review")

    def test_submit_requires_the_claim_and_review_file_naming(self):
        self.start_job()
        task = self.post("claude", "Write the file", "job/out.txt", reviewers=["gpt"])
        self.act("submit", "gpt", task, "-m", "x", code=1)
        self.act("claim", "deepseek", task)
        self.act("review", "gpt", task, "--file", "elsewhere/review_gpt.md", "--accept", code=1)
        self.act("show", "gpt", task)


if __name__ == "__main__":
    unittest.main()
