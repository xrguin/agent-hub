"""Integration tests for the job board's hub backend (T023 phase 2).

Every test works in a throw-away git repository that holds a copy of docs/agent_hub/tools/.
Fake agents (fake_hub_agent.py) act only through the real CLIs new_task.py, claim.py,
hub_append.py and hub_commit.py, so no test spends a token or touches the real repository.
Run:

    python3 -m unittest discover -s docs/agent_hub/tools -p 'test_job_board_hub.py' -v
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import board  # noqa: E402
import group_probe as gp  # noqa: E402
import hub_events as events  # noqa: E402
import hub_state  # noqa: E402
import job  # noqa: E402
import job_adapters as ja  # noqa: E402
import job_dispatcher as jd  # noqa: E402


def tok(agent, text):
    m = re.search(r"tokens=(\d+)", text)
    return int(m.group(1)) if m else None


def quiet(fn, *a, **k):
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return fn(*a, **k)


class HubBase(unittest.TestCase):
    def setUp(self):
        # Hygiene: no __pycache__ in the throw-away repository (hooks and CLIs run there).
        patch_env = mock.patch.dict(os.environ, {"PYTHONDONTWRITEBYTECODE": "1"})
        patch_env.start()
        self.addCleanup(patch_env.stop)
        td = tempfile.TemporaryDirectory(prefix="t023-hub-")
        self.addCleanup(td.cleanup)
        self.repo = Path(td.name)
        self.tools = self.repo / "docs" / "agent_hub" / "tools"
        self.git("init", "-q")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "tester")
        (self.repo / "docs" / "agent_hub").mkdir(parents=True, exist_ok=True)
        shutil.copytree(HERE, self.tools, ignore=shutil.ignore_patterns("__pycache__"))
        for name in ("DECISIONS.md", "QUESTIONS.md"):
            (self.repo / "docs" / "agent_hub" / name).write_text("# " + name + "\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "fixture")
        self.configure()

    # ---- plumbing -------------------------------------------------------
    def git(self, *args):
        r = subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def cli(self, script, *args, check=True):
        r = subprocess.run([sys.executable, str(self.tools / script), *args], cwd=str(self.repo),
                           capture_output=True, text=True)
        if check:
            self.assertEqual(r.returncode, 0, f"{script} {args}: {r.stdout}{r.stderr}")
        return r

    def fake_cmd(self, tag, *extra):
        return [sys.executable, str(self.tools / "fake_hub_agent.py"), "--repo", "{repo}", "--tag", tag,
                "--task", "{task}", "--reason", "{reason}", *extra]

    def configure(self, agents: dict | None = None):
        """hub_config.json stays untracked, so hubtx (which reads the committed one) ignores it."""
        cfg = {"job_backend": "hub", "job_agents": agents or {}}
        (ja.hub_dir(self.repo) / "hub_config.json").write_text(json.dumps(cfg))

    def start_job(self, **limits):
        argv = ["start", "--repo", str(self.repo), "--goal", "hub test", "--quote", "job start", "--by", "claude"]
        for k, v in limits.items():
            argv += ["--limit", f"{k}={v}"]
        self.assertEqual(quiet(job.main, argv), 0)

    def end_job(self):
        self.assertEqual(quiet(job.main, ["end", "--repo", str(self.repo), "--quote", "job end",
                                          "--by", "claude"]), 0)

    def post(self, poster, title, deliverable, reviewers=(), owner="open", initial=None):
        args = ["new_task.py", poster, title, "--owner", owner, "--deliverables", deliverable]
        for r in reviewers:
            args += ["--reviewers", r]
        if initial:
            args += ["--initial", initial]
        return self.cli(*args).stdout.split()[0]

    def fake(self, tag, task, reason, *extra):
        r = subprocess.run([sys.executable, str(self.tools / "fake_hub_agent.py"), "--repo", str(self.repo),
                            "--tag", tag, "--task", task, "--reason", reason, *extra],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout

    def state(self, task):
        return hub_state.derive(self.repo, task)

    def dispatcher(self, **kw):
        kw.setdefault("parse_tokens", tok)
        return jd.Dispatcher(self.repo, poll_s=0.05, err=io.StringIO(), **kw)

    def log_lines(self):
        p = ja.dispatcher_log_path(self.repo)
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def starts(self):
        return [(x["agent"], x["reason"], x["task"]) for x in self.log_lines() if x["event"] == "start"]

    def section(self, name, nxt=None):
        text = ja.board_path(self.repo).read_text()
        part = text.split("## " + name, 1)[1]
        return part.split("## " + nxt, 1)[0] if nxt else part

    def open_section(self):
        return self.section("Open", "For the user to check")

    def check_section(self):
        return self.section("For the user to check", "Questions for the user")

    def claims(self):
        return events.active_claims(self.repo)

    def release_events(self, task):
        return [e for e in self.state(task)["events"] if e["event"] == "release"]


# --------------------------------------------------------------------------
class TestRace(HubBase):
    def test_1_two_agents_race_for_one_task_exactly_one_claim(self):
        for i in range(3):
            task = self.post("claude", f"race {i}", f"job/race{i}.txt")
            go = self.repo / f"go{i}"
            procs = [subprocess.Popen(
                [sys.executable, str(self.tools / "fake_hub_agent.py"), "--repo", str(self.repo), "--tag", tag,
                 "--task", task, "--reason", "claim", "--wait-for", str(go)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for tag in ("gpt", "deepseek")]
            time.sleep(0.4)  # both poll for the go file
            go.write_text("go")
            outs = [p.communicate(timeout=60) for p in procs]
            self.assertEqual([p.returncode for p in procs], [0, 0], outs)  # the loser ends cleanly
            self.assertEqual(sum("claimed=True" in o[0] for o in outs), 1, outs)
            self.assertEqual(sum("claimed=False" in o[0] for o in outs), 1, outs)
            mine = [c for c in self.claims() if c["task"] == task]
            self.assertEqual(len(mine), 1)
            d = self.state(task)
            self.assertEqual(d["holder"], mine[0]["tag"])
            self.assertEqual(d["state"], "review")          # the winner delivered and submitted
            claim_commits = self.git("log", "--format=%s", f"--grep=claim {task}$").split("\n")
            self.assertEqual(len([c for c in claim_commits if c]), 1)

    def test_1b_dispatcher_wakes_both_and_one_claim_stays_in_force(self):
        self.configure({"gpt": self.fake_cmd("gpt", "--sleep", "0.3"),
                        "deepseek": self.fake_cmd("deepseek", "--sleep", "0.3")})
        self.start_job()
        task = self.post("claude", "both want it", "job/both.txt")
        self.dispatcher().run(once=True)
        self.assertEqual(sorted(a for a, _, _ in self.starts()), ["deepseek", "gpt"])
        ends = [x for x in self.log_lines() if x["event"] == "end"]
        self.assertEqual([(x["exit_code"], x["killed"]) for x in ends], [(0, False), (0, False)])
        self.assertEqual(len([c for c in self.claims() if c["task"] == task]), 1)
        self.assertEqual(self.state(task)["state"], "review")
        self.assertEqual(self.release_events(task), [])


# --------------------------------------------------------------------------
class TestCycle(HubBase):
    def setUp(self):
        super().setUp()
        # A = claude posts, B = gpt reviews, C = deepseek works.  claude is not woken.
        self.configure({"gpt": self.fake_cmd("gpt"), "deepseek": self.fake_cmd("deepseek")})

    def full_cycle(self):
        self.start_job()
        task = self.post("claude", "Write the file", "job/out.txt", reviewers=["gpt"])
        d = self.dispatcher()
        board.render(self.repo)
        self.assertIn(task, self.open_section())
        self.assertIn("Write the file", self.open_section())      # title from the card's heading
        d.run(once=True)
        self.assertEqual(self.starts(), [("deepseek", "claim", task)])   # C, not the poster A or reviewer B
        self.assertEqual(self.state(task)["state"], "review")
        self.assertNotIn(task, self.open_section())                       # claimed: left Open
        d.run(once=True)
        self.assertEqual(self.starts(), [("deepseek", "claim", task), ("gpt", "review", task)])
        return task, d

    def test_2_full_cycle(self):
        task, d = self.full_cycle()
        s = self.state(task)
        self.assertTrue(s["accepted"])
        self.assertEqual(s["accepted_revision"], hub_state.revision(self.repo, ["job/out.txt"]))
        self.assertEqual(s["latest_accepts"], {"gpt": s["revision"]})
        text = ja.board_path(self.repo).read_text()
        self.assertIn(task, self.check_section())
        self.assertNotIn(task, self.open_section())
        self.assertIn("Tasks posted: 1 of 10", text)
        self.assertIn("Wakes: 2 of 40", text)
        d.run(once=True)
        d.run(once=True)
        self.assertEqual(len(self.starts()), 2)           # nothing left to do, no repeat wake
        # every agent action is a normal hub commit by the right tag
        subjects = self.git("log", "--format=%s").split("\n")
        self.assertTrue(any(s.startswith(f"[deepseek] {task}: claim") for s in subjects))
        self.assertTrue(any(s.startswith(f"[gpt] {task}: append") for s in subjects))

    def commit_deliverable(self, task, text):
        """The owner (still holding the claim) edits the deliverable and commits it, with no submit."""
        (self.repo / "job" / "out.txt").write_text(text)
        self.cli("hub_commit.py", "deepseek", task, "-m", "tweak", "job/out.txt")

    def test_4_deliverable_commit_after_acceptance_wakes_nobody_until_the_next_submit(self):
        """T048 (a): only a submit asks for review."""
        task, d = self.full_cycle()
        first_rev = self.state(task)["revision"]
        self.assertTrue(self.state(task)["accepted"])
        self.commit_deliverable(task, "changed after acceptance\n")
        s = self.state(task)
        self.assertEqual(s["state"], "review (deliverables changed)")
        self.assertFalse(s["accepted"])
        self.assertNotEqual(s["revision"], first_rev)
        self.assertEqual(s["submitted_revision"], first_rev)
        self.assertEqual(s["next_actor"], "deepseek")                 # the owner, not the reviewer
        self.assertIn("deliverables changed since submit", s["warnings"])
        board.render(self.repo)
        self.assertNotIn(task, self.check_section())
        self.assertEqual(ja.next_actions(self.repo), [])
        d.run(once=True)
        d.run(once=True)
        self.assertEqual(len(self.starts()), 2)                       # claim + the first review only
        # the owner submits again: the reviewer is woken once, for the new submitted revision
        self.fake("deepseek", task, "claim")
        s = self.state(task)
        self.assertNotEqual(s["submitted_revision"], first_rev)
        self.assertEqual(s["submitted_revision"], s["revision"])
        self.assertEqual(s["next_actor"], "gpt")
        d.run(once=True)
        starts = [x for x in self.log_lines() if x["event"] == "start"]
        self.assertEqual([(x["agent"], x["reason"]) for x in starts],
                         [("deepseek", "claim"), ("gpt", "review"), ("gpt", "review")])
        self.assertEqual(starts[2]["revision"], s["revision"])
        self.assertNotEqual(starts[1]["revision"], starts[2]["revision"])
        s2 = self.state(task)
        self.assertTrue(s2["accepted"])
        self.assertEqual(s2["accepted_revision"], s["revision"])
        self.assertIn(task, self.check_section())
        d.run(once=True)
        self.assertEqual(len(self.starts()), 3)                      # accepted and quiet

    def test_4b_deliverable_commit_between_submit_and_review_wakes_nobody(self):
        """T048 (b)."""
        self.start_job()
        task = self.post("claude", "Write the file", "job/out.txt", reviewers=["gpt"])
        self.fake("deepseek", task, "claim")                          # claim + deliver + submit
        sub = self.state(task)["submitted_revision"]
        self.assertEqual({(a.agent, a.reason) for a in ja.next_actions(self.repo)}, {("gpt", "review")})
        self.commit_deliverable(task, "edited before the review started\n")
        s = self.state(task)
        self.assertNotEqual(s["revision"], sub)
        self.assertEqual(s["state"], "review")
        self.assertIn("deliverables changed since submit", s["warnings"])
        self.assertEqual(s["next_actor"], "deepseek")
        self.assertEqual(ja.next_actions(self.repo), [])
        d = self.dispatcher()
        d.run(once=True)
        self.assertEqual(self.starts(), [])
        self.fake("deepseek", task, "claim")                          # submits again
        s = self.state(task)
        self.assertEqual(s["submitted_revision"], s["revision"])
        self.assertNotEqual(s["submitted_revision"], sub)
        acts = ja.next_actions(self.repo)
        self.assertEqual([(a.agent, a.reason, a.revision) for a in acts],
                         [("gpt", "review", s["revision"])])

    def test_4c_deliverable_commit_while_changes_requested_starts_no_second_fix_wake(self):
        """T048 (c)."""
        self.start_job()
        task = self.post("claude", "Write the file", "job/out.txt", reviewers=["gpt"])
        self.fake("deepseek", task, "claim")
        self.fake("gpt", task, "review", "--outcome", "changes")
        self.assertEqual(self.state(task)["state"], "changes_requested")
        before = ja.next_actions(self.repo)
        self.assertEqual([(a.agent, a.reason, a.continuation) for a in before],
                         [("deepseek", "claim", True)])
        self.commit_deliverable(task, "partial fix, not submitted\n")
        self.assertNotEqual(self.state(task)["revision"], self.state(task)["submitted_revision"])
        after = ja.next_actions(self.repo)
        self.assertEqual([(a.agent, a.reason, a.revision) for a in after],
                         [(a.agent, a.reason, a.revision) for a in before])     # same dedupe key
        # the dispatcher runs that one fix wake only; the commit made no second one
        d = self.dispatcher()
        d.run(once=True)
        d.run(once=True)
        self.assertEqual([x for x in self.starts() if x[1] == "claim" and x[0] == "deepseek"],
                         [("deepseek", "claim", task)])

    def request_changes_blocking(self, task, tag="gpt"):
        """A review with one WRONG | blocking row, which stays open after the owner resubmits."""
        name = f"docs/agent_hub/reviews/2026-11-03_{task}_{tag}.md"
        (self.repo / name).parent.mkdir(parents=True, exist_ok=True)
        (self.repo / name).write_text("# Review\n\n| ID | Claim | Verdict | Severity | Evidence |\n"
                                      "|---|---|---|---|---|\n| F1 | fix it | WRONG | blocking | e |\n")
        self.cli("hub_commit.py", tag, task, "-m", "review", name)
        rev = self.state(task)["revision"]
        for kind, fields in (("review", dict(file=name, rev=rev)), ("request_changes", dict(file=name))):
            self.cli("hub_append.py", tag, task, hub_state.status_path(task),
                     events.format_event(tag, kind, fields))

    def in_review_row(self, task):
        board.render(self.repo)
        rows = [x for x in self.section("In review").splitlines() if f"**{task}**" in x]
        self.assertEqual(len(rows), 1, rows)
        return rows[0]

    def test_4d_commit_after_a_resubmit_with_an_open_finding_starts_no_owner_wake(self):
        """T049 F1 (GPT and DeepSeek): changes requested, the owner resubmits while the finding is
        still open, then commits without a submit.  No owner fix wake and no review wake follow."""
        self.start_job()
        task = self.post("claude", "Write the file", "job/out.txt", reviewers=["gpt"])
        self.fake("deepseek", task, "claim")
        self.request_changes_blocking(task)
        self.assertEqual(self.state(task)["state"], "changes_requested")
        first_fix = [(a.agent, a.reason, a.revision) for a in ja.next_actions(self.repo)]
        self.assertEqual([x[:2] for x in first_fix], [("deepseek", "claim")])
        d = self.dispatcher()
        d.run(once=True)                                  # the one fix wake: redeliver + submit
        s = self.state(task)
        self.assertEqual(s["state"], "review")
        self.assertTrue(s["open_findings"])               # open until the reviewer disposes of it
        self.assertEqual(s["submitted_revision"], s["revision"])
        self.assertFalse(s["submit_due"])
        self.assertEqual([(a.agent, a.reason) for a in ja.next_actions(self.repo)], [("gpt", "review")])
        self.commit_deliverable(task, "follow-up, not submitted\n")
        s = self.state(task)
        self.assertTrue(s["submit_due"])
        self.assertEqual(s["next_actor"], "deepseek")
        self.assertEqual(ja.next_actions(self.repo), [])  # neither a review nor a second fix wake
        before = len(self.starts())
        d.run(once=True)
        d.run(once=True)
        self.assertEqual(len(self.starts()), before)
        self.assertEqual([x for x in self.starts() if x[0] == "deepseek"], [("deepseek", "claim", task)])
        # the owner submits again: the reviewer, and only the reviewer, is woken for it
        self.fake("deepseek", task, "claim")
        s = self.state(task)
        self.assertFalse(s["submit_due"])
        self.assertEqual([(a.agent, a.reason, a.revision) for a in ja.next_actions(self.repo)],
                         [("gpt", "review", s["revision"])])

    def test_4f_accept_without_a_disposition_closes_a_legacy_table_finding(self):
        """T054 (T053 stall): the reviewer's README 5 table has no ID and no Severity column, its WRONG
        row is a blocking finding.  The owner resubmits, the reviewer accepts and writes no disposition:
        the task is accepted, nothing is due, and the dispatcher starts no wake."""
        self.start_job()
        task = self.post("claude", "Write the file", "job/out.txt", reviewers=["gpt"])
        self.fake("deepseek", task, "claim")
        name = f"docs/agent_hub/reviews/2026-11-03_{task}_gpt.md"
        (self.repo / name).parent.mkdir(parents=True, exist_ok=True)
        (self.repo / name).write_text("# Review\n\n| Claim | Verdict | Evidence |\n|---|---|---|\n"
                                      "| the file is there | CONFIRMED | e |\n| the content is right | WRONG | e |\n")
        self.cli("hub_commit.py", "gpt", task, "-m", "review", name)
        rev = self.state(task)["revision"]
        for kind, fields in (("review", dict(file=name, rev=rev)), ("request_changes", dict(file=name))):
            self.cli("hub_append.py", "gpt", task, hub_state.status_path(task), events.format_event("gpt", kind, fields))
        self.assertEqual(self.state(task)["state"], "changes_requested")
        d = self.dispatcher()
        d.run(once=True)                                  # the one fix wake: redeliver + submit
        s = self.state(task)
        self.assertEqual(s["state"], "review")
        self.assertEqual([f["id"] for f in s["open_findings"]], ["F2"])
        rev = s["revision"]
        for kind, fields in (("review", dict(file=name, rev=rev)), ("accept", dict(rev=rev))):
            self.cli("hub_append.py", "gpt", task, hub_state.status_path(task), events.format_event("gpt", kind, fields))
        s = self.state(task)
        self.assertEqual(s["state"], "accepted")
        self.assertEqual(s["open_findings"], [])
        self.assertEqual([f["closed_by"] for f in s["all_findings"]], ["accept"])
        self.assertEqual([a for a in ja.next_actions(self.repo) if a.task == task], [])
        before = len(self.starts())
        d.run(once=True)
        self.assertEqual(len(self.starts()), before)

    def test_4e_in_review_row_names_the_owner_when_a_submit_is_due(self):
        """T049 F2: the board row does not tell the user to wait for a reviewer nobody wakes."""
        self.start_job()
        task = self.post("claude", "Write the file", "job/out.txt", reviewers=["gpt"])
        self.fake("deepseek", task, "claim")
        sub = self.state(task)["submitted_revision"]
        self.assertIn("waiting for gpt at " + sub[:8], self.in_review_row(task))
        self.commit_deliverable(task, "edited before the review started\n")
        head = self.state(task)["revision"]
        row = self.in_review_row(task)
        self.assertNotIn("waiting for gpt", row)
        self.assertIn(f"deliverables changed since the submit at {sub[:8]}", row)
        self.assertIn(f"waiting for deepseek to submit again (HEAD {head[:8]})", row)
        # after an acceptance too (state "review (deliverables changed)")
        self.fake("deepseek", task, "claim")
        self.fake("gpt", task, "review")
        self.assertTrue(self.state(task)["accepted"])
        self.commit_deliverable(task, "changed after acceptance\n")
        self.assertEqual(self.state(task)["state"], "review (deliverables changed)")
        row = self.in_review_row(task)
        self.assertNotIn("waiting for gpt", row)
        self.assertIn("waiting for deepseek to submit again", row)


# --------------------------------------------------------------------------
class TestJobTasksOnly(HubBase):
    def test_3_tasks_created_before_job_start_are_never_woken_or_shown(self):
        self.configure({"gpt": self.fake_cmd("gpt"), "deepseek": self.fake_cmd("deepseek")})
        old = self.post("claude", "Old hub task", "job/old.txt")
        legacy = self.repo / "docs/agent_hub/tasks/T099_legacy_card.md"
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text("# T099: Legacy card\nOwner: open · Reviewer: [gpt] · Status: open\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "legacy card")
        time.sleep(1.1)                       # event times have whole-second resolution
        self.start_job()
        new = self.post("claude", "Fresh job task", "job/new.txt")
        self.assertEqual(old, "T001")
        self.assertEqual(new, "T100")         # new_task takes 1 + the highest id, legacy included
        self.assertEqual(ja.posted_count(self.repo), 1)
        self.assertEqual({a.task for a in ja.next_actions(self.repo)}, {new})
        board.render(self.repo)
        self.assertIn(new, self.open_section())
        self.assertNotIn(old, ja.board_path(self.repo).read_text())
        d = self.dispatcher()
        d.run(once=True)
        d.run(once=True)
        self.assertTrue(self.starts())
        self.assertEqual({t for _, _, t in self.starts()}, {new})
        text = ja.board_path(self.repo).read_text()
        self.assertNotIn(old, text)
        self.assertNotIn("T099", text)
        self.assertNotIn("Old hub task", text)
        self.assertIn("Tasks posted: 1 of 10", text)
        self.assertEqual(self.state(old)["state"], "open")      # untouched

    def test_3b_no_job_means_no_job_tasks(self):
        self.post("claude", "Some task", "job/x.txt")
        self.assertEqual(ja.next_actions(self.repo), [])
        self.assertEqual(ja.posted_count(self.repo), 0)
        self.assertEqual(ja.open_entries(self.repo), [])

    def test_3c_a_later_job_does_not_inherit_the_earlier_jobs_tasks(self):
        self.start_job()
        first = self.post("claude", "Job one task", "job/one.txt")
        self.assertEqual(ja.posted_count(self.repo), 1)
        self.end_job()
        self.assertEqual(ja.posted_count(self.repo), 1)          # still shown after the end
        time.sleep(1.1)
        self.start_job()
        self.assertEqual(ja.posted_count(self.repo), 0)
        self.assertNotIn(first, [a.task for a in ja.next_actions(self.repo)])


# --------------------------------------------------------------------------
class TestKillAndEnd(HubBase):
    def test_5_wall_time_kill_releases_the_claim(self):
        self.configure({"deepseek": self.fake_cmd("deepseek", "--sleep", "30")})
        self.start_job(max_wall_s_per_wake=1)
        task = self.post("claude", "Slow work", "job/slow.txt", reviewers=["gpt"])
        t0 = time.monotonic()
        self.dispatcher().run(once=True)
        self.assertLess(time.monotonic() - t0, 20)
        lines = self.log_lines()
        self.assertEqual([x["event"] for x in lines], ["start", "end"])
        self.assertTrue(lines[1]["killed"])
        with self.assertRaises(ProcessLookupError):
            os.kill(lines[0]["pid"], 0)
        s = self.state(task)
        self.assertEqual(s["state"], "open")
        self.assertIsNone(s["holder"])
        self.assertEqual(self.claims(), [])
        rel = self.release_events(task)
        self.assertEqual([(e["tag"], "wall-time" in e["note"]) for e in rel], [("deepseek", True)])
        text = ja.board_path(self.repo).read_text()
        self.assertIn(task, self.open_section())
        self.assertIn("released by deepseek", self.open_section())
        # T-hub-028 changed this test.  It used to pin the stall: a release kept the claimant as
        # owner and the dispatcher never repeated the wake for the same (agent, reason, task,
        # revision), so the task stayed Open for good.  Now the release returns an owner-"open"
        # task to "open" (anyone but the poster and the reviewer may claim it) and the dispatcher
        # re-wakes it once per job (the claim key gets a "#t1" suffix).  The second timeout
        # leaves it Open without a further wake (details in test_board_pilot_prep.py).
        self.assertEqual(s["owner"], "open")
        offered = {(a.agent, a.reason) for a in ja.next_actions(self.repo) if a.task == task}
        self.assertEqual(offered, {("deepseek", "claim")})   # claude posted it, gpt reviews it
        self.dispatcher().run(once=True)
        self.assertEqual(len(self.starts()), 2)              # the one retry
        self.dispatcher().run(once=True)
        self.assertEqual(len(self.starts()), 2)              # and no further wake
        self.assertEqual(self.state(task)["state"], "open")
        self.assertFalse(ja.load_state(self.repo)["paused"])
        self.assertIn("Tasks posted: 1 of 10", text)

    def test_5b_failed_wake_before_submit_also_returns_task_to_open(self):
        self.configure({"gpt": self.fake_cmd("gpt", "--fail")})
        self.start_job()
        task = self.post("claude", "Crashes", "job/crash.txt")
        self.dispatcher().run(once=True)
        self.assertEqual(self.state(task)["state"], "open")
        rel = self.release_events(task)
        self.assertEqual(len(rel), 1)
        self.assertIn("exited with code 1", rel[0]["note"])

    def test_5c_return_to_open_does_nothing_unless_the_agent_holds_the_claim(self):
        task = self.post("claude", "Held", "job/held.txt", reviewers=["gpt"])
        self.start_job()
        be = ja.HubBackend()
        self.assertFalse(be.return_to_open(self.repo, task, "deepseek", "not mine"))     # nobody holds it
        self.assertTrue(be.claim(self.repo, "deepseek", task))
        n = len(self.state(task)["events"])
        self.assertFalse(be.return_to_open(self.repo, task, "gpt", "not mine"))          # someone else holds it
        self.assertEqual(len(self.state(task)["events"]), n)
        self.fake("deepseek", task, "claim")                                              # delivers, submits
        self.assertEqual(self.state(task)["state"], "review")
        self.assertFalse(be.return_to_open(self.repo, task, "deepseek", "late"))         # no longer `claimed`
        self.assertEqual(self.release_events(task), [])
        self.assertEqual(self.state(task)["state"], "review")

    def test_5d_kill_stops_the_whole_group_before_the_claim_is_released(self):
        # F2 on the real hub: the leader exits on SIGTERM, a same-group child ignores it.
        pidfile = self.repo / "pids.json"
        self.addCleanup(gp.cleanup_group, pidfile)
        self.configure({"deepseek": [sys.executable, str(self.tools / "fake_group_agent.py"), "--repo",
                                     "{repo}", "--tag", "deepseek", "--task", "{task}", "--reason",
                                     "{reason}", "--pidfile", str(pidfile), "--claim", "hub"]})
        self.start_job(max_wall_s_per_wake=2)
        task = self.post("claude", "Stubborn", "job/stubborn.txt", reviewers=["gpt"])
        at_release = []
        real = ja.return_to_open

        def spy(repo, task_, agent, note):
            pids = gp.read_pids(pidfile)
            at_release.append(gp.group_members(pids["leader"]))
            return real(repo, task_, agent, note)
        with mock.patch.object(ja, "return_to_open", spy):
            d = self.dispatcher()
            d.TERM_GRACE_S = 0.5
            d.run(once=True)
        self.assertEqual(at_release, [[]])
        lines = self.log_lines()
        self.assertEqual([x["event"] for x in lines], ["start", "end"])
        self.assertTrue(lines[1]["killed"])
        self.assertEqual(lines[1]["exit_code"], -15)
        s = self.state(task)
        self.assertEqual((s["state"], s["holder"]), ("open", None))
        self.assertEqual(self.claims(), [])
        self.assertEqual([(e["tag"], "wall-time" in e["note"]) for e in self.release_events(task)],
                         [("deepseek", True)])

    def test_5e_job_end_between_the_first_read_and_admission_starts_nothing(self):
        # F1 on the real hub: iterate read active -> a real `job.py end` completes -> admission.
        self.configure({"gpt": self.fake_cmd("gpt")})
        self.start_job()
        task = self.post("claude", "Late", "job/late.txt")
        d = self.dispatcher()
        real_reap = d._reap_and_kill
        done = []

        def reap_then_end(limits):
            real_reap(limits)
            if not done:
                self.end_job()           # the real job.py end, in-process
                done.append(1)
        d._reap_and_kill = reap_then_end
        launched = []
        real_popen = subprocess.Popen

        def counting_popen(argv, *a, **k):
            if "fake_hub_agent.py" in " ".join(str(x) for x in argv):
                launched.append(argv)
            return real_popen(argv, *a, **k)
        with mock.patch.object(jd.subprocess, "Popen", counting_popen):
            self.assertEqual(d.run(once=True), 0)
        self.assertEqual((done, launched, self.starts()), ([1], [], []))
        self.assertEqual(list(ja.dispatcher_runs_dir(self.repo).glob("*.out")), [])
        st = ja.load_state(self.repo)
        self.assertEqual((st["wakes"], st["seen"]), ([], []))
        self.assertEqual(self.state(task)["state"], "open")
        self.assertFalse(ja.load_job(self.repo)["active"])

    def test_6a_job_end_stops_new_wakes(self):
        self.configure({"gpt": self.fake_cmd("gpt"), "deepseek": self.fake_cmd("deepseek")})
        self.start_job()
        task = self.post("claude", "Never woken", "job/n.txt")
        self.end_job()
        d = self.dispatcher()
        d.run(once=True)
        self.assertEqual(self.log_lines(), [])
        self.assertEqual(d.run(), 0)                      # the loop form exits at once
        self.assertEqual(self.state(task)["state"], "open")
        self.assertIn(task, self.open_section())          # still on the board for the user

    def test_6b_job_end_during_a_run_lets_it_finish_and_wakes_nobody_else(self):
        self.configure({"gpt": self.fake_cmd("gpt", "--sleep", "1.5")})
        self.start_job()
        t1 = self.post("claude", "First", "job/a.txt")
        t2 = self.post("claude", "Second", "job/b.txt")
        disp = subprocess.Popen([sys.executable, str(self.tools / "job_dispatcher.py"), "run", "--repo",
                                 str(self.repo), "--poll-s", "0.2"], stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 30
            while not self.starts():
                self.assertLess(time.monotonic(), deadline, "no wake started")
                time.sleep(0.05)
            r = subprocess.run([sys.executable, str(self.tools / "job.py"), "end", "--repo", str(self.repo),
                                "--quote", "job end", "--by", "claude"], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(disp.wait(timeout=60), 0)
        finally:
            if disp.poll() is None:
                disp.kill()
        lines = self.log_lines()
        self.assertEqual([x["event"] for x in lines], ["start", "end"])
        self.assertEqual((lines[1]["exit_code"], lines[1]["killed"]), (0, False))
        woken = lines[0]["task"]
        other = t2 if woken == t1 else t1
        self.assertEqual(self.state(woken)["state"], "review")    # the running child finished
        self.assertEqual(self.state(other)["state"], "open")      # and nothing new was woken
        self.assertIn(other, self.open_section())


# --------------------------------------------------------------------------
class TestLimits(HubBase):
    def setUp(self):
        super().setUp()
        self.configure({"gpt": self.fake_cmd("gpt")})

    def paused_text(self):
        text = ja.board_path(self.repo).read_text()
        self.assertIn("## Paused", text)
        return text.split("## Paused", 1)[1]

    def test_7a_wakes_per_job_pauses(self):
        self.start_job(max_wakes_per_job=1)
        self.post("claude", "One", "job/1.txt")
        t2 = self.post("claude", "Two", "job/2.txt")
        d = self.dispatcher()
        d.run(once=True)
        self.assertFalse(ja.load_state(self.repo)["paused"])
        d.run(once=True)
        st = ja.load_state(self.repo)
        self.assertTrue(st["paused"])
        self.assertIn("wakes per job", st["pause_reason"])
        self.assertIn("wakes per job", self.paused_text())
        self.assertEqual(len(st["wakes"]), 1)
        self.assertEqual(self.state(t2)["state"], "open")
        d.run(once=True)
        self.assertEqual(len(ja.load_state(self.repo)["wakes"]), 1)

    def test_7b_hourly_limit_pauses_until_resume(self):
        self.start_job(max_wakes_per_agent_per_hour=1)
        task = self.post("claude", "Hourly", "job/h.txt")
        st = ja.load_state(self.repo)
        st["wakes"].append({"time": ja.iso_now(time.time() - 10), "agent": "gpt", "reason": "claim",
                            "task": "T000", "revision": "x"})
        ja.save_state(self.repo, st)
        self.dispatcher().run(once=True)
        st = ja.load_state(self.repo)
        self.assertTrue(st["paused"])                                  # J-R5
        self.assertIn("gpt", st["pause_reason"])
        self.assertIn("max_wakes_per_agent_per_hour", st["pause_reason"])
        self.assertEqual(len(st["wakes"]), 1)
        self.assertEqual(self.state(task)["state"], "open")
        self.assertIn("gpt", self.paused_text())
        self.assertNotIn("Not paused", self.paused_text())
        later = lambda: time.time() + 3700  # noqa: E731
        self.dispatcher(clock=later).run(once=True)                    # an hour later: still paused
        self.assertEqual(len(ja.load_state(self.repo)["wakes"]), 1)
        self.assertEqual(self.starts(), [])
        self.assertEqual(quiet(job.main, ["resume", "--repo", str(self.repo), "--quote", "go on",
                                          "--by", "claude"]), 0)
        self.dispatcher(clock=later).run(once=True)
        self.assertFalse(ja.load_state(self.repo)["paused"])
        self.assertEqual(len(ja.load_state(self.repo)["wakes"]), 2)
        self.assertEqual(self.state(task)["state"], "review")

    def test_7c_too_many_posted_tasks_pause_before_any_wake(self):
        self.start_job(max_tasks_posted=2)
        for i in range(3):
            self.post("claude", f"Task {i}", f"job/{i}.txt")
        self.dispatcher().run(once=True)
        st = ja.load_state(self.repo)
        self.assertTrue(st["paused"])
        self.assertEqual(st["wakes"], [])
        self.assertIn("tasks posted 3", self.paused_text())
        self.assertIn("Tasks posted: 3 of 2", ja.board_path(self.repo).read_text())

    def test_7d_tokens_limit(self):
        self.configure({"gpt": self.fake_cmd("gpt")})
        self.start_job(max_tokens_per_agent=1000)
        self.post("claude", "A", "job/a.txt")
        self.post("claude", "B", "job/b.txt")
        d = self.dispatcher(parse_tokens=lambda agent, text: 1234)
        d.run(once=True)
        d.run(once=True)
        st = ja.load_state(self.repo)
        self.assertTrue(st["paused"])
        self.assertIn("tokens for gpt", st["pause_reason"])
        self.assertEqual(len(st["wakes"]), 1)


# --------------------------------------------------------------------------
class TestBackendRules(HubBase):
    def acts(self, agent=None):
        return {(a.agent, a.reason, a.task) for a in ja.next_actions(self.repo)
                if agent is None or a.agent == agent}

    def test_8_claim_rules(self):
        t1 = self.post("claude", "Mine", "job/m.txt", reviewers=["gpt"])
        t2 = self.post("claude", "Assigned", "job/a.txt", owner="gpt")
        be = ja.HubBackend()
        time.sleep(1.1)                       # event times have whole-second resolution
        self.start_job()
        t3 = self.post("claude", "Fresh", "job/f.txt", reviewers=["gpt"])
        t4 = self.post("gpt", "Gpt posted", "job/g.txt")
        self.assertEqual(ja.posted_count(self.repo), 2)             # t1, t2 predate the job
        self.assertFalse(be.claim(self.repo, "claude", t3))          # posted it
        self.assertFalse(be.claim(self.repo, "gpt", t3))             # reviewer
        self.assertFalse(be.claim(self.repo, "gpt", t4))             # posted it
        self.assertFalse(be.claim(self.repo, "nobody", t3))
        self.assertFalse(be.claim(self.repo, "deepseek", "T777"))    # unknown task
        self.assertEqual(self.claims(), [])
        self.assertEqual(self.acts(), {("deepseek", "claim", t3), ("claude", "claim", t4),
                                       ("deepseek", "claim", t4)})
        self.assertTrue(be.claim(self.repo, "deepseek", t3))
        self.assertFalse(be.claim(self.repo, "claude", t3))          # already claimed
        self.assertEqual([c["tag"] for c in self.claims()], ["deepseek"])
        self.assertEqual(self.claims()[0]["scope"], ["job/f.txt"])    # scope = the deliverables
        # deepseek is busy now, so it is offered nothing to claim; claude still is
        self.assertEqual(self.acts("deepseek"), set())
        self.assertEqual(self.acts("claude"), {("claude", "claim", t4)})
        self.assertNotIn(t1, {t for _, _, t in self.acts()})

    def test_8b_assigned_task_goes_only_to_its_owner(self):
        self.start_job()
        t = self.post("claude", "Assigned", "job/a.txt", owner="gpt")
        self.assertEqual(self.acts(), {("gpt", "claim", t)})

    def test_9_review_and_owner_continue_cycle(self):
        self.start_job()
        t1 = self.post("claude", "First", "job/1.txt", reviewers=["gpt"])
        t2 = self.post("claude", "Second", "job/2.txt", reviewers=["gpt"])
        self.assertEqual(self.acts(), {("deepseek", "claim", t1), ("deepseek", "claim", t2)})
        self.fake("deepseek", t1, "claim")                           # claim + deliver + submit
        rev1 = self.state(t1)["revision"]
        self.assertEqual(self.acts("gpt"), {("gpt", "review", t1)})
        self.assertEqual(self.acts("deepseek"), {("deepseek", "claim", t2)})   # free again
        self.fake("gpt", t1, "review", "--outcome", "changes")
        s = self.state(t1)
        self.assertEqual(s["state"], "changes_requested")
        a = [x for x in ja.next_actions(self.repo) if x.agent == "deepseek"]
        self.assertEqual([(x.reason, x.task) for x in a], [("claim", t1)])   # busy: no new claim, only its own
        self.assertTrue(a[0].revision.startswith(rev1 + "#r"))
        self.assertEqual(self.acts("gpt"), set())                    # t2 is still open and gpt reviews it
        self.fake("deepseek", t1, "claim")                           # continues: redelivers, submits again
        rev2 = self.state(t1)["revision"]
        self.assertNotEqual(rev1, rev2)
        self.assertEqual(self.acts("gpt"), {("gpt", "review", t1)})
        self.fake("gpt", t1, "review")
        self.assertTrue(self.state(t1)["accepted"])
        self.assertEqual(self.acts("gpt"), set())
        self.assertEqual({x.revision for x in ja.next_actions(self.repo) if x.reason == "review"}, set())

    def test_9b_no_answer_wakes_and_questions_section(self):
        self.start_job()
        q = self.post("claude", "Which start heading?", "job/q.txt", initial="awaiting_user")
        board.render(self.repo)
        self.assertEqual(self.acts(), set())
        self.assertIn(q, self.section("Questions for the user", "Paused"))
        self.assertIn("awaiting_user", self.section("Questions for the user", "Paused"))
        self.assertNotIn(q, self.open_section())
        self.assertEqual({a.reason for a in ja.next_actions(self.repo)}, set())

    def test_9c_card_title_and_summary_lines(self):
        self.start_job()
        t = self.post("claude", "A title with: colon", "job/t.txt", reviewers=["gpt"])
        entry = ja.open_entries(self.repo)[0]
        self.assertEqual((entry["task"], entry["title"]), (t, "A title with: colon"))
        self.assertIn("posted by claude", entry["line"])
        self.assertIn("reviewers gpt", entry["line"])


# --------------------------------------------------------------------------
class TestLock(HubBase):
    def test_10_same_lock_file_as_hubtx_and_no_nesting(self):
        # a live hubtx transaction in another process blocks ja.lock (same inode) ...
        holder = subprocess.Popen(
            [sys.executable, "-c",
             "import sys, time; sys.path.insert(0, %r); import hubtx\n"
             "with hubtx.transaction('gpt', 'T001'):\n    print('held', flush=True); time.sleep(2.0)\n"
             % str(self.tools)], cwd=str(self.repo), stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "held")
            with self.assertRaises(ja.Busy):
                with ja.lock(self.repo, timeout=0.3):
                    self.fail("took the lock while a hub transaction held it")
        finally:
            holder.communicate(timeout=30)
        # ... and ja.lock held by the dispatcher blocks a hub CLI, which then gives up cleanly
        with ja.lock(self.repo):
            env = dict(os.environ, HUB_LOCK_TIMEOUT="0.3")
            r = subprocess.run([sys.executable, str(self.tools / "hub_commit.py"), "gpt", "T001", "-m", "x",
                                "docs/agent_hub/DECISIONS.md"], cwd=str(self.repo), env=env,
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 2, r.stderr)        # Busy
            with self.assertRaises(ja.Busy):                    # not re-entrant: fails at once
                with ja.lock(self.repo, timeout=5):
                    pass
        with ja.lock(self.repo, timeout=0.3):
            pass

    def test_10b_backend_refuses_to_call_a_cli_while_holding_the_lock(self):
        self.start_job()
        t = self.post("claude", "Lock test", "job/l.txt")
        be = ja.HubBackend()
        t0 = time.monotonic()
        with ja.lock(self.repo):
            with self.assertRaises(RuntimeError):
                be.claim(self.repo, "gpt", t)
        self.assertLess(time.monotonic() - t0, 5)              # refused at once, no 30 s wait
        self.assertTrue(be.claim(self.repo, "gpt", t))         # fine once the lock is free
        with ja.lock(self.repo):
            with self.assertRaises(RuntimeError):
                be.return_to_open(self.repo, t, "gpt", "x")
        self.assertTrue(be.return_to_open(self.repo, t, "gpt", "x"))

    def test_10c_dispatcher_state_writes_run_while_agents_use_the_hub(self):
        # The dispatcher never calls a hub CLI under its lock: a wake (state write, spawn,
        # kill/release) completes with real agents committing at the same time.
        self.configure({"gpt": self.fake_cmd("gpt", "--sleep", "0.2"),
                        "deepseek": self.fake_cmd("deepseek", "--sleep", "0.2")})
        self.start_job()
        for i in range(3):
            self.post("claude", f"Busy {i}", f"job/b{i}.txt")
        d = self.dispatcher()
        t0 = time.monotonic()
        for _ in range(3):
            d.run(once=True)
        self.assertLess(time.monotonic() - t0, 60)
        ends = [x for x in self.log_lines() if x["event"] == "end"]
        self.assertGreaterEqual(len(ends), 3)
        self.assertTrue(all(x["exit_code"] == 0 and not x["killed"] for x in ends))
        self.assertEqual(len(ja.load_state(self.repo)["wakes"]), len(ends))
        tasks = [c["task"] for c in self.claims()]
        self.assertEqual(len(tasks), len(set(tasks)))          # never two claims on one task


class TestRuntimeFiles(HubBase):
    def test_11_runtime_files_and_real_repo_untouched(self):
        self.configure({"gpt": self.fake_cmd("gpt")})
        self.start_job()
        self.post("claude", "Files", "job/f.txt")
        self.dispatcher().run(once=True)
        hub = ja.hub_dir(self.repo)
        for name in ("JOB.json", ".job_state.json", "BOARD.md", ".dispatcher.lock",
                     "logs/dispatcher.jsonl"):
            self.assertTrue((hub / name).exists(), name)
        self.assertTrue(any((hub / "logs" / "dispatcher_runs").glob("*.out")))
        # none of them was committed by an agent action: they are untracked runtime files
        tracked = self.git("ls-files").split("\n")
        for name in ("JOB.json", ".job_state.json", "BOARD.md", ".dispatcher.lock", "dispatcher.jsonl"):
            self.assertFalse(any(t.endswith(name) for t in tracked), name)
        # no hook was installed in the throw-away repository either
        self.assertEqual(sorted(p.name for p in (self.repo / ".git" / "hooks").glob("*") if not p.name.endswith(".sample")), [])


if __name__ == "__main__":
    unittest.main()
