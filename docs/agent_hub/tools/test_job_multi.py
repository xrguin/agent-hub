"""Tests for several active jobs at once (T038).  Every test works in a throw-away git repository
that holds a copy of docs/agent_hub/tools/ (the HubBase harness of test_job_board_hub.py); the
fake agents spend no tokens.  Run:

    python3 -m unittest discover -s docs/agent_hub/tools -p 'test_job_multi.py' -v
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import board  # noqa: E402
import group_probe as gp  # noqa: E402
import hub_state  # noqa: E402
import job  # noqa: E402
import job_adapters as ja  # noqa: E402
import job_dispatcher as jd  # noqa: E402
from test_job_board_hub import HubBase  # noqa: E402


class MultiBase(HubBase):
    def start(self, goal, **limits):
        argv = ["start", "--goal", goal, "--quote", "job start: " + goal, "--by", "claude"]
        for k, v in limits.items():
            argv += ["--limit", f"{k}={v}"]
        out = self.cli("job.py", *argv).stdout
        return re.match(r"job started: (job-[0-9]+): ", out)[1]

    def post(self, poster, title, deliverable, reviewers=(), owner="open", initial=None, job=None):
        args = ["new_task.py", poster, title, "--owner", owner, "--deliverables", deliverable]
        for r in reviewers:
            args += ["--reviewers", r]
        if initial:
            args += ["--initial", initial]
        if job:
            args += ["--job", job]
        return self.cli(*args).stdout.split()[0]

    def head(self):
        return self.git("rev-parse", "HEAD").strip()

    def tasks_of(self, job):
        return {a.task for a in ja.next_actions(self.repo, job)}

    def job_log(self, event="start"):
        return [(x["job"], x["task"]) for x in self.log_lines() if x["event"] == event]


class TestTwoJobs(MultiBase):
    def test_1_two_jobs_start_without_an_end(self):
        self.configure({"gpt": self.fake_cmd("gpt")})
        self.assertEqual(self.start("alpha"), "job-1")
        self.assertEqual(self.start("beta"), "job-2")
        self.assertEqual([(j["id"], j["goal"], j["match"]) for j in ja.active_jobs(self.repo)],
                         [("job-1", "alpha", "id"), ("job-2", "beta", "id")])
        t1 = self.post("claude", "One", "job/one.txt", job="job-1")
        t2 = self.post("claude", "Two", "job/two.txt", job="job-2")
        # end without --job is ambiguous: refused, nothing ended
        r = self.cli("job.py", "end", "--quote", "which?", "--by", "claude", check=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("job-1", r.stderr)
        self.assertIn("job-2", r.stderr)
        self.assertIn("--job", r.stderr)
        self.assertEqual(len(ja.active_jobs(self.repo)), 2)
        # end --job job-1 ends only job-1
        self.cli("job.py", "end", "--job", "job-1", "--quote", "stop alpha", "--by", "claude")
        self.assertEqual([j["id"] for j in ja.active_jobs(self.repo)], ["job-2"])
        self.assertFalse(ja.get_job(self.repo, "job-1")["active"])
        self.assertIsNotNone(ja.get_job(self.repo, "job-1")["ended"])
        # the dispatcher still has work: job-2's task is woken, job-1's is not
        d = self.dispatcher()
        self.assertTrue(d.iterate())
        d._drain()
        self.assertEqual(self.starts(), [("gpt", "claim", t2)])
        self.assertEqual(self.state(t1)["state"], "open")
        self.assertEqual(self.state(t2)["state"], "review")

    def test_2_tasks_are_separated_by_job_id(self):
        self.start("alpha")
        self.start("beta")
        t1 = self.post("claude", "One", "job/one.txt", job="job-1")
        t2 = self.post("claude", "Two", "job/two.txt", job="job-2")
        t3 = self.post("claude", "Three", "job/three.txt", job="job-2")
        t4 = self.post("claude", "Nobody's", "job/four.txt", job="none")
        self.assertEqual((ja.posted_count(self.repo, "job-1"), ja.posted_count(self.repo, "job-2")), (1, 2))
        self.assertEqual(self.tasks_of("job-1"), {t1})
        self.assertEqual(self.tasks_of("job-2"), {t2, t3})
        self.assertEqual(self.tasks_of(None), {t1, t2, t3})          # the union; t4 is in no job
        self.assertEqual(ja.posted_count(self.repo), 3)
        self.assertTrue(all(a.job == "job-2" for a in ja.next_actions(self.repo, "job-2")))
        status = (self.repo / hub_state.status_path(t1)).read_text()
        self.assertIn("job=job-1", status)
        self.assertIn("job=none", (self.repo / hub_state.status_path(t4)).read_text())
        self.assertNotIn(t4, [e["task"] for e in ja.open_entries(self.repo)])
        # without --job, two active jobs: refused, nothing is written
        head = self.head()
        r = self.cli("new_task.py", "claude", "Ambiguous", "--deliverables", "job/x.txt", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("job-1", r.stderr + r.stdout)
        self.assertIn("job-2", r.stderr + r.stdout)
        self.assertEqual(self.head(), head)
        # an unknown job id is refused as well
        r = self.cli("new_task.py", "claude", "Unknown", "--deliverables", "job/y.txt", "--job", "job-9",
                     check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.head(), head)
        r = self.cli("new_task.py", "claude", "Malformed", "--deliverables", "job/z.txt", "--job", "bogus",
                     check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.head(), head)
        self.assertEqual(ja.posted_count(self.repo), 3)

    def test_2b_one_active_job_is_the_default_and_none_means_no_field(self):
        self.assertEqual(self.post("claude", "Before any job", "job/a.txt"), "T001")
        self.assertNotIn("job=", (self.repo / hub_state.status_path("T001")).read_text())
        self.start("alpha")
        t2 = self.post("claude", "Default", "job/b.txt")
        self.assertIn("job=job-1", (self.repo / hub_state.status_path(t2)).read_text())
        self.assertEqual(ja.posted_count(self.repo, "job-1"), 1)

    def test_3_limits_are_separate(self):
        self.configure({"gpt": self.fake_cmd("gpt")})
        self.start("alpha", max_tasks_posted=1)
        self.start("beta", max_wakes_per_job=1)
        a1 = self.post("claude", "A1", "job/a1.txt", job="job-1")
        a2 = self.post("claude", "A2", "job/a2.txt", job="job-1")
        b1 = self.post("claude", "B1", "job/b1.txt", job="job-2")
        d = self.dispatcher()
        d.run(once=True)
        st1, st2 = ja.load_state(self.repo, "job-1"), ja.load_state(self.repo, "job-2")
        self.assertTrue(st1["paused"])
        self.assertIn("tasks posted 2", st1["pause_reason"])
        self.assertEqual(st1["wakes"], [])
        self.assertFalse(st2["paused"])
        self.assertEqual(self.job_log(), [("job-2", b1)])
        self.assertEqual(len(st2["wakes"]), 1)
        # job-1 is resumed with a higher limit: its wakes do not count against job-2's limit of one
        self.cli("job.py", "resume", "--job", "job-1", "--limit", "max_tasks_posted=5", "--quote", "go on",
                 "--by", "claude")
        d.run(once=True)
        self.assertEqual(self.job_log(), [("job-2", b1), ("job-1", a1)])
        self.assertFalse(ja.load_state(self.repo, "job-2")["paused"])
        self.assertEqual(len(ja.load_state(self.repo, "job-1")["wakes"]), 1)
        self.assertEqual(len(ja.load_state(self.repo, "job-2")["wakes"]), 1)
        # job-2 reaches its own limit; job-1 is not paused by it
        b2 = self.post("claude", "B2", "job/b2.txt", job="job-2")
        d.run(once=True)
        st1, st2 = ja.load_state(self.repo, "job-1"), ja.load_state(self.repo, "job-2")
        self.assertTrue(st2["paused"])
        self.assertIn("wakes per job", st2["pause_reason"])
        self.assertFalse(st1["paused"])
        self.assertEqual(self.job_log(), [("job-2", b1), ("job-1", a1), ("job-1", a2)])
        self.assertEqual(self.state(b2)["state"], "open")

    def test_4_one_pause_does_not_pause_the_other(self):
        self.configure({"gpt": self.fake_cmd("gpt")})
        self.start("alpha")
        self.start("beta")
        t1 = self.post("claude", "One", "job/one.txt", job="job-1")
        t2 = self.post("claude", "Two", "job/two.txt", job="job-2")
        st = ja.load_state(self.repo, "job-1")
        st.update(paused=True, pause_reason="by the test")
        ja.save_state(self.repo, st, "job-1")
        d = self.dispatcher()
        d.run(once=True)
        self.assertEqual(self.starts(), [("gpt", "claim", t2)])
        self.assertEqual(self.state(t1)["state"], "open")
        # resume needs the job id while two jobs are active
        r = self.cli("job.py", "resume", "--quote", "go on", "--by", "claude", check=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("--job", r.stderr)
        self.assertTrue(ja.load_state(self.repo, "job-1")["paused"])
        self.cli("job.py", "resume", "--job", "job-1", "--quote", "go on", "--by", "claude")
        self.assertFalse(ja.load_state(self.repo, "job-1")["paused"])
        d.run(once=True)
        self.assertEqual(self.starts(), [("gpt", "claim", t2), ("gpt", "claim", t1)])
        self.assertEqual(self.state(t1)["state"], "review")
        # only job-1's resume quote was recorded
        self.assertEqual([q["event"] for q in ja.get_job(self.repo, "job-1")["quotes"]], ["start", "resume"])
        self.assertEqual([q["event"] for q in ja.get_job(self.repo, "job-2")["quotes"]], ["start"])


class TestLegacyJob(MultiBase):
    def write_legacy(self, t0):
        """The single-record JOB.json and flat .job_state.json written before T038."""
        ja.write_json_atomic(ja.job_path(self.repo), {
            "schema": "job-1", "active": True, "goal": "legacy goal", "started": ja.iso_now(t0), "ended": None,
            "quotes": [{"time": ja.iso_now(t0), "by": "claude", "event": "start", "quote": "job start: legacy"}],
            "limits": dict(ja.DEFAULT_LIMITS)})
        st = ja.default_state()
        st["wakes"] = [{"time": ja.iso_now(t0 + 1), "agent": "gpt", "reason": "claim", "task": "T000",
                        "revision": "x"}]
        ja.write_json_atomic(ja.state_path(self.repo), st)

    def test_5_legacy_time_window_job_keeps_its_tasks(self):
        pre = self.post("claude", "Before the job", "job/pre.txt")      # no JOB.json: no job field
        time.sleep(1.1)                    # event times have whole-second resolution
        t0 = time.time()
        time.sleep(1.1)
        a = self.post("claude", "Legacy A", "job/a.txt")
        b = self.post("claude", "Legacy B", "job/b.txt")
        self.assertNotIn("job=", (self.repo / hub_state.status_path(a)).read_text())
        self.write_legacy(t0)
        jobs = ja.load_jobs(self.repo)
        self.assertEqual([(j["id"], j["match"]) for j in jobs], [("job-1", "time")])
        self.assertEqual(ja.posted_count(self.repo, "job-1"), 2)
        acts = {a_.task for a_ in ja.next_actions(self.repo)}
        self.assertEqual(acts, {a, b})
        self.assertNotIn(pre, acts)
        self.assertEqual(len(ja.load_state(self.repo)["wakes"]), 1)
        # a second job starts from the legacy files: job-2 sees nothing, job-1 keeps its tasks and state
        self.assertEqual(self.start("second"), "job-2")
        self.assertEqual((ja.posted_count(self.repo, "job-1"), ja.posted_count(self.repo, "job-2")), (2, 0))
        self.assertEqual(len(ja.load_state(self.repo, "job-1")["wakes"]), 1)
        self.assertEqual(ja.load_state(self.repo, "job-2"), ja.default_state())
        self.assertEqual(self.tasks_of("job-1"), {a, b})
        self.assertEqual(self.tasks_of("job-2"), set())
        # the same legacy files, now migrated
        self.write_legacy(t0)
        out = self.cli("job.py", "migrate").stdout
        self.assertIn("migrated JOB.json and .job_state.json to the multi-job form", out)
        raw = json.loads(ja.job_path(self.repo).read_text())
        self.assertEqual(raw["schema"], "jobs-1")
        self.assertEqual([(j["id"], j["match"], j["goal"]) for j in raw["jobs"]],
                         [("job-1", "time", "legacy goal")])
        self.assertEqual(len(raw["jobs"][0]["quotes"]), 1)
        raw_state = json.loads(ja.state_path(self.repo).read_text())
        self.assertEqual(raw_state["schema"], "job-state-1")
        self.assertEqual(len(raw_state["jobs"]["job-1"]["wakes"]), 1)
        self.assertEqual(ja.posted_count(self.repo, "job-1"), 2)
        self.assertEqual(self.tasks_of("job-1"), {a, b})
        before = (ja.job_path(self.repo).read_text(), ja.state_path(self.repo).read_text())
        self.assertIn("already in the multi-job form", self.cli("job.py", "migrate").stdout)
        self.assertEqual(before, (ja.job_path(self.repo).read_text(), ja.state_path(self.repo).read_text()))
        # new tasks join job-1 only by id
        c = self.post("claude", "By id", "job/c.txt", job="job-1")
        self.post("claude", "Nobody's", "job/d.txt", job="none")
        self.assertEqual(ja.posted_count(self.repo, "job-1"), 3)
        self.assertEqual(self.tasks_of("job-1"), {a, b, c})
        e = self.post("claude", "Default", "job/e.txt")            # one active job: its id is written
        self.assertIn("job=job-1", (self.repo / hub_state.status_path(e)).read_text())
        self.assertEqual(ja.posted_count(self.repo, "job-1"), 4)

    def test_5b_status_lists_the_jobs_on_the_board(self):
        self.start("alpha")
        self.start("beta")
        info = json.loads(self.cli("job.py", "status", "--json").stdout)
        self.assertEqual(info["id"], "job-2")                          # the newest active job
        self.assertEqual([j["id"] for j in info["jobs"]], ["job-1", "job-2"])
        info1 = json.loads(self.cli("job.py", "status", "--json", "--job", "job-1").stdout)
        self.assertEqual((info1["id"], info1["goal"]), ("job-1", "alpha"))
        text = self.cli("job.py", "status").stdout
        self.assertIn("job-1 active: alpha", text)
        self.assertIn("job-2 active: beta", text)
        self.assertEqual(ja.select_job(self.repo, "job-1")["goal"], "alpha")
        with self.assertRaises(ja.JobChoice) as cm:
            ja.select_job(self.repo)
        self.assertIn("2 jobs are active: job-1 (goal: alpha), job-2 (goal: beta); pass --job <id>", str(cm.exception))


class TestBoardAndDispatcher(MultiBase):
    def test_6_board_shows_both_jobs(self):
        self.start("alpha goal")
        self.start("beta goal")
        t1 = self.post("claude", "One", "job/one.txt", job="job-1")
        t2 = self.post("claude", "Two", "job/two.txt", job="job-2")
        board.render(self.repo)
        text = ja.board_path(self.repo).read_text()
        self.assertIn("### Job job-1", text)
        self.assertIn("### Job job-2", text)
        self.assertIn("Goal: alpha goal", text)
        self.assertIn("Goal: beta goal", text)
        self.assertEqual(text.count("- Tasks posted: 1 of 10"), 2)
        opens = self.open_section()
        line1 = next(x for x in opens.splitlines() if f"**{t1}**" in x)
        line2 = next(x for x in opens.splitlines() if f"**{t2}**" in x)
        self.assertTrue(line1.endswith("; job-1)"), line1)
        self.assertTrue(line2.endswith("; job-2)"), line2)
        self.assertIn("_Not paused._", self.section("Paused"))
        st = ja.load_state(self.repo, "job-1")
        st.update(paused=True, pause_reason="by the test")
        ja.save_state(self.repo, st, "job-1")
        board.render(self.repo)
        paused = self.section("Paused")
        self.assertIn("- job-1 paused: by the test. Only the user can resume (`job.py resume --job job-1`).",
                      paused)
        self.assertNotIn("job-2 paused", paused)
        self.assertNotIn("_Not paused._", paused)
        self.assertIn("active, PAUSED (by the test)", ja.board_path(self.repo).read_text())

    def test_7_one_dispatcher_one_wake_per_agent_across_jobs(self):
        self.configure({"gpt": self.fake_cmd("gpt")})
        self.start("alpha")
        self.start("beta")
        ta = self.post("claude", "A", "job/a.txt", job="job-1")
        tb = self.post("claude", "B", "job/b.txt", job="job-2")
        d = self.dispatcher()
        d.run(once=True)
        self.assertEqual(self.starts(), [("gpt", "claim", ta)])         # one agent, one wake
        self.assertEqual(self.state(tb)["state"], "open")
        d.run(once=True)
        self.assertEqual(self.starts(), [("gpt", "claim", ta), ("gpt", "claim", tb)])
        self.assertEqual(self.job_log(), [("job-1", ta), ("job-2", tb)])
        self.assertEqual([x["job"] for x in self.log_lines()], ["job-1", "job-1", "job-2", "job-2"])
        self.assertEqual((len(ja.load_state(self.repo, "job-1")["wakes"]),
                          len(ja.load_state(self.repo, "job-2")["wakes"])), (1, 1))
        d.run(once=True)
        self.assertEqual(len(self.starts()), 2)                          # nothing left, no repeat


class TestAbandonedGroup(MultiBase):
    """T038 F1: a wake whose process group cannot be emptied keeps its agent out of every job."""

    def stuck(self):
        """job-1 has a gpt wake that exceeds its wall time and whose group cleanup fails; job-2 has an
        eligible gpt claim.  Returns (dispatcher, job-1 task, job-2 task, pgid)."""
        pidfile = self.repo / "pids.json"
        self.addCleanup(gp.cleanup_group, pidfile)
        self.configure({"gpt": [sys.executable, str(self.tools / "fake_group_agent.py"), "--repo", "{repo}",
                                "--tag", "gpt", "--task", "{task}", "--reason", "{reason}",
                                "--pidfile", str(pidfile), "--claim", "hub"]})
        self.assertEqual(self.start("alpha", max_wall_s_per_wake=1), "job-1")
        self.assertEqual(self.start("beta"), "job-2")
        t1 = self.post("claude", "One", "job/one.txt", job="job-1")
        t2 = self.post("claude", "Two", "job/two.txt", job="job-2")
        d = self.dispatcher()
        d.TERM_GRACE_S = d.KILL_GRACE_S = 0.2
        d.iterate()                                    # gpt is woken on job-1's task
        deadline = time.monotonic() + 20
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(pidfile.exists())              # the wake holds its claim
        time.sleep(1.2)                                # past max_wall_s_per_wake
        with mock.patch.object(jd.Dispatcher, "_group_alive", staticmethod(lambda pgid: True)):
            d.iterate()                                # the kill "fails": the group is abandoned
        self.assertEqual(self.starts(), [("gpt", "claim", t1)])
        pgid = next(x["pid"] for x in self.log_lines() if x["event"] == "start")
        return d, t1, t2, pgid

    def alive(self, value):
        return mock.patch.object(jd.Dispatcher, "_group_alive", staticmethod(lambda pgid: value))

    def test_8_failed_cleanup_keeps_the_agent_out_of_the_other_job(self):
        d, t1, t2, pgid = self.stuck()
        self.assertEqual([x["event"] for x in self.log_lines()], ["start", "kill_failed"])
        self.assertEqual(d.children, {})
        # no second gpt process, nothing recorded for job-2
        st1, st2 = ja.load_state(self.repo, "job-1"), ja.load_state(self.repo, "job-2")
        self.assertEqual(st2["wakes"], [])
        self.assertEqual(st2["seen"], [])
        self.assertFalse(st2["paused"])
        self.assertTrue(st1["paused"])
        self.assertEqual(st1["abandoned"]["gpt"]["pgid"], pgid)
        self.assertEqual(st1["abandoned"]["gpt"]["task"], t1)
        self.assertEqual(st2["abandoned"], {})
        # the job-1 task stays claimed
        self.assertEqual(self.state(t1)["holder"], "gpt")
        self.assertEqual(self.release_events(t1), [])
        self.assertEqual([c["task"] for c in self.claims()], [t1])
        self.assertEqual(self.state(t2)["state"], "open")
        # another iteration, the group still alive: still nothing
        with self.alive(True):
            d.iterate()
        self.assertEqual(self.starts(), [("gpt", "claim", t1)])
        # the board names the held agent
        paused = self.section("Paused")
        self.assertIn(f"- gpt is held: the process group {pgid} of its wake on {t1} (job-1) is still running; "
                      "no new gpt wake starts in any job.", paused)
        self.assertIn("- job-1 paused:", paused)
        self.assertNotIn("job-2 paused", paused)

    def test_8b_a_restarted_dispatcher_keeps_the_exclusion(self):
        _, t1, t2, pgid = self.stuck()
        d2 = self.dispatcher()
        with self.alive(True):
            d2.iterate()
        self.assertEqual(self.starts(), [("gpt", "claim", t1)])
        self.assertEqual(d2.children, {})
        self.assertEqual(ja.load_state(self.repo, "job-2")["wakes"], [])
        self.assertEqual(ja.load_state(self.repo, "job-1")["abandoned"]["gpt"]["pgid"], pgid)

    def test_8c_once_the_group_is_gone_the_agent_is_eligible_again(self):
        _, t1, t2, pgid = self.stuck()
        self.configure({"gpt": self.fake_cmd("gpt")})              # a well-behaved agent from now on
        d2 = self.dispatcher()
        with self.alive(False):
            d2.iterate()
        d2._drain()
        self.assertEqual(ja.load_state(self.repo, "job-1")["abandoned"], {})
        gone = [x for x in self.log_lines() if x["event"] == "abandoned_gone"]
        self.assertEqual([(x["job"], x["agent"], x["task"], x["pgid"]) for x in gone],
                         [("job-1", "gpt", t1, pgid)])
        self.assertEqual(self.starts(), [("gpt", "claim", t1), ("gpt", "claim", t2)])
        self.assertEqual(self.job_log(), [("job-1", t1), ("job-2", t2)])
        self.assertEqual(len(ja.load_state(self.repo, "job-2")["wakes"]), 1)
        # the old job stays paused and its task stays claimed until the user resumes
        self.assertTrue(ja.load_state(self.repo, "job-1")["paused"])
        self.assertEqual(self.state(t1)["holder"], "gpt")
        self.assertNotIn("is held", self.section("Paused"))
        self.assertEqual(len(ja.load_state(self.repo, "job-1")["wakes"]), 1)


class TestCheckInsideAWake(MultiBase):
    """job.py check records the user's words; it refuses inside a dispatcher wake (JOB_TAG set)."""

    def run_check(self, task):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = job.main(["check", task, "--ok", "--quote", "that looks right", "--by", "claude",
                           "--repo", str(self.repo)])
        return rc, out.getvalue(), err.getvalue()

    def test_9_check_is_refused_with_job_tag_and_works_without(self):
        self.configure({"gpt": self.fake_cmd("gpt"), "deepseek": self.fake_cmd("deepseek")})
        self.start("alpha")
        task = self.post("claude", "Write the file", "job/out.txt", reviewers=["gpt"])
        d = self.dispatcher()
        d.run(once=True)
        d.run(once=True)
        self.assertTrue(self.state(task)["accepted"])
        decisions = self.repo / "docs/agent_hub/DECISIONS.md"
        before, head = decisions.read_text(), self.head()
        with mock.patch.dict(os.environ, {"JOB_TAG": "deepseek", "JOB_TASK": task}):
            rc, out, err = self.run_check(task)
        self.assertEqual(rc, 1)
        self.assertIn("job.py check records the user's words and runs only in the user's chat, "
                      "not inside a job-board wake (JOB_TAG is set)", err)
        self.assertEqual(out, "")
        self.assertEqual(decisions.read_text(), before)
        self.assertEqual(self.head(), head)
        self.assertEqual(self.git("status", "--porcelain", "--untracked-files=no"), "")
        # an empty JOB_TAG does not count; without the variable the normal check works
        env = {k: v for k, v in os.environ.items() if k not in ("JOB_TAG", "JOB_TASK")}
        with mock.patch.dict(os.environ, env, clear=True):
            rc, out, err = self.run_check(task)
        self.assertEqual(rc, 0, err)
        self.assertIn("recorded D001", out)
        self.assertEqual(len(re.findall(r"^### D\d{3,}\b", decisions.read_text(), re.M)), 1)


if __name__ == "__main__":
    unittest.main()
