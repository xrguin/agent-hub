"""Tests for `job.py start --auto-end` and the dispatcher's auto-end (T052).  Every test works in a
throw-away git repository that holds a copy of docs/agent_hub/tools/ (the HubBase harness of
test_job_board_hub.py); the fake agents spend no tokens.  Run:

    python3 -m unittest discover -s docs/agent_hub/tools -p 'test_auto_end.py' -v
"""
from __future__ import annotations

import re
import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import hub_events as events  # noqa: E402
import hub_state  # noqa: E402
import job_adapters as ja  # noqa: E402
import job_dispatcher as jd  # noqa: E402
from test_job_board_hub import HubBase  # noqa: E402

END_QUOTE = "auto-end: every task accepted or cancelled"


class AutoEndBase(HubBase):
    def setUp(self):
        super().setUp()
        # claude posts, deepseek works, gpt reviews; claude is not woken.
        self.configure({"gpt": self.fake_cmd("gpt"), "deepseek": self.fake_cmd("deepseek")})

    def start(self, goal="auto", auto_end=True):
        argv = ["start", "--goal", goal, "--quote", "job start: " + goal, "--by", "claude"]
        if auto_end:
            argv.append("--auto-end")
        out = self.cli("job.py", *argv).stdout
        return re.match(r"job started: (job-[0-9]+): ", out)[1]

    def post_to(self, job_id, name):
        args = ["new_task.py", "claude", name, "--owner", "open", "--deliverables", f"job/{name}.txt",
                "--reviewers", "gpt", "--job", job_id]
        return self.cli(*args).stdout.split()[0]

    def accept_all(self, d, tasks, rounds=6):
        """Run the dispatcher until every task is accepted (a claim wake, then a review wake)."""
        for _ in range(rounds):
            if all(self.state(t)["accepted"] for t in tasks):
                return
            d.run(once=True)
        self.fail([self.state(t)["state"] for t in tasks])

    def job(self, job_id):
        return ja.get_job(self.repo, job_id)

    def last_quote(self, job_id):
        return self.job(job_id)["quotes"][-1]

    def accepted_job(self, name="one", auto_end=True):
        """An (auto-end) job whose only task is accepted, and a dispatcher that has not seen it yet."""
        jid = self.start(auto_end=auto_end)
        task = self.post_to(jid, name)
        d = self.dispatcher()
        self.accept_all(d, [task])
        self.assertTrue(self.job(jid)["active"])
        return jid, task, d


class TestOption(AutoEndBase):
    def test_1_start_auto_end_sets_the_key(self):
        a = self.start("with", auto_end=True)
        b = self.start("without", auto_end=False)
        self.assertIs(self.job(a)["auto_end"], True)
        self.assertIs(self.job(b)["auto_end"], False)
        self.assertEqual(self.job(a)["quotes"][0]["event"], "start")
        self.assertEqual(self.job(a)["quotes"][0]["quote"], "job start: with")

    def test_2_resume_auto_end_sets_it_and_records_the_quote(self):
        jid = self.start(auto_end=False)
        self.cli("job.py", "resume", "--job", jid, "--auto-end", "--quote", "you can end the job yourself",
                 "--by", "claude")
        job = self.job(jid)
        self.assertIs(job["auto_end"], True)
        self.assertTrue(job["active"])
        self.assertEqual([(q["event"], q["by"], q["quote"]) for q in job["quotes"][-1:]],
                         [("auto_end", "claude", "you can end the job yourself")])
        # without the flag, resume leaves the key alone
        other = self.start("other", auto_end=False)
        self.cli("job.py", "resume", "--job", other, "--quote", "go on", "--by", "claude")
        self.assertIs(self.job(other)["auto_end"], False)
        self.assertEqual(self.last_quote(other)["event"], "resume")

    def test_2b_board_header_shows_auto_end(self):
        a = self.start("with", auto_end=True)
        b = self.start("without", auto_end=False)
        text = ja.board_path(self.repo).read_text()
        blocks = text.split("### Job ")
        mine = {blk.split("\n", 1)[0]: blk for blk in blocks[1:]}
        self.assertIn("- Auto-end: on", mine[a].split("\n- Tasks posted")[0])
        self.assertNotIn("Auto-end", mine[b].split("\n## ")[0])

    def test_2c_old_record_without_the_key_counts_as_false(self):
        jid = self.start(auto_end=False)
        jobs = ja.load_jobs(self.repo)
        for j in jobs:
            j.pop("auto_end", None)
        ja.save_jobs(self.repo, jobs)
        task = self.post_to(jid, "old")
        d = self.dispatcher()
        self.accept_all(d, [task])
        self.assertTrue(d.iterate())
        self.assertTrue(self.job(jid)["active"])


class TestDispatcher(AutoEndBase):
    def test_3_accepted_task_ends_the_job_after_one_iteration(self):
        jid, task, d = self.accepted_job()
        self.assertFalse(d.iterate())            # nothing active any more
        job = self.job(jid)
        self.assertFalse(job["active"])
        self.assertIsNotNone(job["ended"])
        q = self.last_quote(jid)
        self.assertEqual((q["by"], q["event"], q["quote"]), ("dispatcher", "end", END_QUOTE))
        self.assertIn(f"ended {job['ended']}", ja.board_path(self.repo).read_text())
        self.assertEqual(d.run(), 0)             # the loop exits at once
        # the user's check still works on the ended job
        self.cli("job.py", "check", task, "--ok", "--quote", "looks good", "--by", "claude")

    def test_4_without_auto_end_the_job_stays_active(self):
        jid, _, d = self.accepted_job(auto_end=False)
        self.assertTrue(d.iterate())
        self.assertTrue(self.job(jid)["active"])
        self.assertIsNone(self.job(jid)["ended"])

    def test_5_task_in_review_keeps_the_job_active(self):
        jid = self.start()
        task = self.post_to(jid, "waiting")
        d = self.dispatcher()
        d.run(once=True)                          # deepseek claims and submits; gpt has not reviewed
        self.assertEqual(self.state(task)["state"], "review")
        self.assertFalse(self.state(task)["accepted"])
        self.assertTrue(d.iterate())              # starts the review wake; the job goes on
        d._drain()
        self.assertTrue(self.job(jid)["active"])

    def test_6_paused_job_never_auto_ends(self):
        jid, _, d = self.accepted_job()
        st = ja.load_state(self.repo, jid)
        st["paused"], st["pause_reason"] = True, "test"
        ja.save_state(self.repo, st, jid)
        self.assertTrue(d.iterate())
        self.assertTrue(self.job(jid)["active"])
        # after the user resumes it ends
        self.cli("job.py", "resume", "--job", jid, "--quote", "go on", "--by", "claude")
        self.assertFalse(d.iterate())
        self.assertFalse(self.job(jid)["active"])

    def test_7_running_wake_of_the_job_delays_the_end(self):
        jid, task, d = self.accepted_job()
        flag = self.repo / "wake_done"
        out = self.repo / "wake.out"
        proc = subprocess.Popen(["sh", "-c", f"while [ ! -e {flag} ]; do sleep 0.02; done"])
        self.addCleanup(lambda: (flag.touch(), proc.wait()))
        d.children["gpt"] = jd.Child(proc=proc, action=ja.Action("gpt", "review", task, "r1", job=jid),
                                     start=d.clock(), out_path=out, fh=open(out, "w"), job=jid)
        self.assertTrue(d.iterate())              # the wake is still running
        self.assertTrue(self.job(jid)["active"])
        flag.touch()
        proc.wait()
        self.assertFalse(d.iterate())             # reaped, then the job ends in the same pass
        self.assertEqual(d.children, {})
        self.assertFalse(self.job(jid)["active"])
        self.assertEqual(self.last_quote(jid)["by"], "dispatcher")

    def test_8_job_without_tasks_stays_active(self):
        jid = self.start()
        d = self.dispatcher()
        self.assertTrue(d.iterate())
        self.assertTrue(self.job(jid)["active"])

    def test_9_other_jobs_stay_active(self):
        a = self.start("alpha", auto_end=True)
        b = self.start("beta", auto_end=False)
        ta = self.post_to(a, "alpha")
        tb = self.post_to(b, "beta")
        d = self.dispatcher()
        self.accept_all(d, [ta, tb])
        self.assertTrue(d.iterate())              # beta is still active
        self.assertFalse(self.job(a)["active"])
        self.assertTrue(self.job(b)["active"])
        self.assertEqual(self.last_quote(a)["quote"], END_QUOTE)

    def test_10_cancelled_task_counts_as_done(self):
        jid = self.start()
        t1 = self.post_to(jid, "keep")
        t2 = self.post_to(jid, "drop")
        cancel = events.format_event("claude", "user", dict(action="cancel", decision="D001", task=t2), "")
        status = self.repo / hub_state.status_path(t2)
        status.write_text(status.read_text() + cancel + "\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "cancel " + t2)
        self.assertEqual(self.state(t2)["state"], "cancelled")
        d = self.dispatcher()
        self.accept_all(d, [t1])
        self.assertTrue(self.job(jid)["active"])
        self.assertFalse(d.iterate())
        self.assertFalse(self.job(jid)["active"])


if __name__ == "__main__":
    unittest.main()
