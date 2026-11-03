"""Tests for the job board (T023 phase 1).  Every test works in a throw-away git
repository; the fake agents spend no tokens.  Run:

    python3 -m unittest discover -s docs/agent_hub/tools -p 'test_job_board.py' -v
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
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
import job  # noqa: E402
import job_adapters as ja  # noqa: E402
import job_dispatcher as jd  # noqa: E402

FAKE = HERE / "fake_agent.py"
FAKE_GROUP = HERE / "fake_group_agent.py"
JOB_PY = HERE / "job.py"
DISPATCHER_PY = HERE / "job_dispatcher.py"
BOARD_PY = HERE / "board.py"


def tok(agent, text):
    m = re.search(r"tokens=(\d+)", text)
    return int(m.group(1)) if m else None


def quiet(fn, *a, **k):
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return fn(*a, **k)


class Base(unittest.TestCase):
    def setUp(self):
        # Hygiene: no __pycache__ in the throw-away repository (hooks and CLIs run there).
        patch_env = mock.patch.dict(os.environ, {"PYTHONDONTWRITEBYTECODE": "1"})
        patch_env.start()
        self.addCleanup(patch_env.stop)

    def make_repo(self) -> Path:
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        repo = Path(td.name)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / "docs" / "agent_hub").mkdir(parents=True)
        return repo

    def fake_cmd(self, tag, *extra):
        return [sys.executable, str(FAKE), "--repo", "{repo}", "--tag", tag,
                "--task", "{task}", "--reason", "{reason}", *extra]

    def configure(self, repo, agents: dict):
        (ja.hub_dir(repo) / "hub_config.json").write_text(json.dumps({"job_agents": agents}))

    def start_job(self, repo, **limits):
        argv = ["start", "--repo", str(repo), "--goal", "test goal", "--quote", "go", "--by", "claude"]
        for k, v in limits.items():
            argv += ["--limit", f"{k}={v}"]
        self.assertEqual(quiet(job.main, argv), 0)

    def set_tasks(self, repo, tasks, agents=()):
        ja.stub_write(repo, {"agents": list(agents), "tasks": tasks})

    def task(self, repo, tid):
        return next(t for t in ja.stub_read(repo)["tasks"] if t["id"] == tid)

    def dispatcher(self, repo, **kw):
        return jd.Dispatcher(repo, poll_s=0.05, err=io.StringIO(), **kw)

    def log_lines(self, repo):
        p = ja.dispatcher_log_path(repo)
        if not p.exists():
            return []
        return [json.loads(x) for x in p.read_text().splitlines()]


def T(tid, **kw):
    d = {"id": tid, "title": f"title {tid}", "posted_by": "claude", "status": "open"}
    d.update(kw)
    return d


class TestClaimRace(Base):
    def test_1_exactly_one_claim(self):
        repo = self.make_repo()
        rounds = 3
        self.set_tasks(repo, [T(f"T00{i}") for i in range(rounds)], agents=["gpt", "deepseek"])
        for i in range(rounds):
            go = repo / f"go{i}"
            procs = [subprocess.Popen(
                [sys.executable, str(FAKE), "--repo", str(repo), "--tag", tag, "--task", f"T00{i}",
                 "--reason", "claim", "--wait-for", str(go)],
                stdout=subprocess.PIPE, text=True) for tag in ("gpt", "deepseek")]
            time.sleep(0.4)  # both are polling for the go file
            go.write_text("go")
            outs = [p.communicate(timeout=30)[0] for p in procs]
            self.assertEqual([p.returncode for p in procs], [0, 0])
            wins = [o for o in outs if "claimed=True" in o]
            self.assertEqual(len(wins), 1, outs)
            t = self.task(repo, f"T00{i}")
            self.assertEqual(t["status"], "review")
            self.assertEqual(t["revision"], 1)
            self.assertIn(t["owner"], ("gpt", "deepseek"))


class TestNoSelfWake(Base):
    def test_2a_same_action_is_not_repeated(self):
        repo = self.make_repo()
        # The agent claims, then crashes: the task returns to open at the same revision,
        # so the identical (agent, reason, task, revision) is offered again but must not wake.
        self.configure(repo, {"gpt": self.fake_cmd("gpt", "--fail")})
        self.set_tasks(repo, [T("T001")])
        self.start_job(repo)
        d = self.dispatcher(repo, parse_tokens=tok)
        for _ in range(3):
            d.run(once=True)
        t = self.task(repo, "T001")
        self.assertEqual(t["status"], "open")
        self.assertIn("exited with code 1", t["note"])
        starts = [x for x in self.log_lines(repo) if x["event"] == "start"]
        self.assertEqual(len(starts), 1)
        self.assertEqual(ja.load_state(repo)["seen"], [["gpt", "claim", "T001", 0]])

    def test_2b_never_offered_own_task(self):
        repo = self.make_repo()
        self.set_tasks(repo, [T("T001", posted_by="gpt")], agents=["claude", "gpt"])
        acts = {(a.agent, a.reason) for a in ja.next_actions(repo)}
        self.assertEqual(acts, {("claude", "claim")})
        self.assertFalse(ja.claim(repo, "gpt", "T001"))
        self.assertEqual(self.task(repo, "T001")["status"], "open")
        self.assertTrue(ja.claim(repo, "claude", "T001"))
        self.assertFalse(ja.claim(repo, "claude", "T001"))  # already claimed

    def test_2c_busy_agent_gets_no_claim_and_review_answer_actions(self):
        repo = self.make_repo()
        self.set_tasks(repo, [
            T("T001", status="claimed", owner="gpt"),
            T("T002"),
            T("T003", status="review", reviewer="gpt", revision=2, reviewed_revisions=[1], owner="claude"),
            T("T004", status="review", reviewer="gpt", revision=2, reviewed_revisions=[2], owner="claude"),
            T("T005", status="claimed", owner="claude", question="q?", question_by="gpt", answer="a"),
        ], agents=["gpt"])
        gpt = {(a.reason, a.task, a.revision) for a in ja.next_actions(repo) if a.agent == "gpt"}
        self.assertEqual(gpt, {("review", "T003", 2), ("answer", "T005", 0)})

    def test_2d_full_cycle_with_two_agents(self):
        repo = self.make_repo()
        self.configure(repo, {"gpt": self.fake_cmd("gpt"), "deepseek": self.fake_cmd("deepseek")})
        self.set_tasks(repo, [T("T001", posted_by="claude", reviewer="deepseek")])
        self.start_job(repo)
        d = self.dispatcher(repo, parse_tokens=tok)
        for _ in range(4):
            d.run(once=True)
        t = self.task(repo, "T001")
        # one of gpt/deepseek claimed; the reviewer deepseek reviewed unless it was the owner
        self.assertIn(t["status"], ("accepted", "review"))
        self.assertNotEqual(t["owner"], "claude")


class TestWakeContent(Base):
    def test_prompt_placeholder_and_answer_wake(self):
        repo = self.make_repo()
        cmd = [sys.executable, "-c", "import sys; print(sys.argv[1]); print('tokens=77')", "{prompt}"]
        self.configure(repo, {"gpt": cmd})
        self.set_tasks(repo, [T("T009", status="claimed", owner="gpt", question="q?",
                                question_by="gpt", answer="the answer")])
        self.start_job(repo)
        self.dispatcher(repo, parse_tokens=tok).run(once=True)
        out = next(ja.dispatcher_runs_dir(repo).glob("*.out")).read_text()
        self.assertIn("[gpt] job-board wake, reason answer, task T009: do this one action, then exit.", out)
        self.assertEqual(ja.load_state(repo)["tokens"], {"gpt": 77})
        lines = self.log_lines(repo)
        self.assertEqual([(x["event"], x["reason"], x["task"], x["revision"]) for x in lines],
                         [("start", "answer", "T009", 0), ("end", "answer", "T009", 0)])
        self.assertEqual(set(lines[1]), {"time", "event", "agent", "reason", "task", "revision", "pid",
                                         "exit_code", "duration_s", "tokens", "killed", "job"})  # T038: the job id

    def test_fake_agent_answer_and_review(self):
        repo = self.make_repo()
        self.configure(repo, {"gpt": self.fake_cmd("gpt")})
        self.set_tasks(repo, [
            T("T001", status="claimed", owner="claude", question="q?", question_by="gpt", answer="a"),
            T("T002", status="review", owner="claude", reviewer="gpt", revision=1)])
        self.start_job(repo)
        d = self.dispatcher(repo, parse_tokens=tok)
        d.run(once=True)
        d.run(once=True)
        self.assertIsNone(self.task(repo, "T001")["question"])
        t2 = self.task(repo, "T002")
        self.assertEqual((t2["status"], t2["reviewed_revisions"]), ("accepted", [1]))
        self.assertIn("T002", ja.board_path(repo).read_text().split("## For the user to check", 1)[1])


class TestLimits(Base):
    def setup_one_agent(self, repo, tasks, **limits):
        self.configure(repo, {"gpt": self.fake_cmd("gpt")})
        self.set_tasks(repo, tasks)
        self.start_job(repo, **limits)

    def paused_text(self, repo):
        text = ja.board_path(repo).read_text()
        self.assertIn("## Paused", text)
        return text.split("## Paused", 1)[1]

    def test_3a_total_wakes_pauses(self):
        repo = self.make_repo()
        self.setup_one_agent(repo, [T("T001"), T("T002")], max_wakes_per_job=1)
        d = self.dispatcher(repo, parse_tokens=tok)
        d.run(once=True)
        self.assertFalse(ja.load_state(repo)["paused"])
        d.run(once=True)
        st = ja.load_state(repo)
        self.assertTrue(st["paused"])
        self.assertEqual(len(st["wakes"]), 1)
        self.assertIn("wakes per job", st["pause_reason"])
        self.assertIn("wakes per job", self.paused_text(repo))
        self.assertEqual(self.task(repo, "T002")["status"], "open")
        d.run(once=True)  # stays paused
        self.assertEqual(len(ja.load_state(repo)["wakes"]), 1)

    def test_3b_tokens_pause(self):
        repo = self.make_repo()
        self.setup_one_agent(repo, [T("T001"), T("T002")], max_tokens_per_agent=1000)
        d = self.dispatcher(repo, parse_tokens=tok)
        d.run(once=True)
        self.assertEqual(ja.load_state(repo)["tokens"], {"gpt": 1234})
        d.run(once=True)
        st = ja.load_state(repo)
        self.assertTrue(st["paused"])
        self.assertIn("tokens for gpt", st["pause_reason"])
        self.assertIn("tokens for gpt", self.paused_text(repo))
        self.assertEqual(len(st["wakes"]), 1)
        lines = [x for x in self.log_lines(repo) if x["event"] == "end"]
        self.assertEqual(lines[0]["tokens"], 1234)

    def test_3c_posted_tasks_pause(self):
        repo = self.make_repo()
        self.setup_one_agent(repo, [T("T001"), T("T002"), T("T003")], max_tasks_posted=2)
        d = self.dispatcher(repo, parse_tokens=tok)
        d.run(once=True)
        st = ja.load_state(repo)
        self.assertTrue(st["paused"])
        self.assertEqual(st["wakes"], [])
        self.assertIn("tasks posted 3", self.paused_text(repo))

    def test_3d_hourly_limit_pauses_until_resume(self):
        repo = self.make_repo()
        self.setup_one_agent(repo, [T("T001")], max_wakes_per_agent_per_hour=1)
        st = ja.load_state(repo)
        st["wakes"].append({"time": ja.iso_now(time.time() - 10), "agent": "gpt", "reason": "claim",
                            "task": "T000", "revision": 0})
        ja.save_state(repo, st)
        d = self.dispatcher(repo, parse_tokens=tok)
        d.run(once=True)
        st = ja.load_state(repo)
        self.assertTrue(st["paused"])                              # J-R5: a limit pauses all wakes
        self.assertIn("gpt", st["pause_reason"])
        self.assertIn("max_wakes_per_agent_per_hour", st["pause_reason"])
        self.assertIn("gpt", self.paused_text(repo))
        self.assertNotIn("Not paused", self.paused_text(repo))
        self.assertEqual(len(st["wakes"]), 1)
        self.assertEqual(self.task(repo, "T001")["status"], "open")
        # an hour later it is still paused: only the user resumes (fake clock; no real waiting)
        later = lambda: time.time() + 3700  # noqa: E731
        d2 = self.dispatcher(repo, parse_tokens=tok, clock=later)
        d2.run(once=True)
        self.assertTrue(ja.load_state(repo)["paused"])
        self.assertEqual(len(ja.load_state(repo)["wakes"]), 1)
        self.assertEqual(self.log_lines(repo), [])
        self.assertEqual(quiet(job.main, ["resume", "--repo", str(repo), "--quote", "ok, go on",
                                          "--by", "claude"]), 0)
        d2.run(once=True)
        self.assertFalse(ja.load_state(repo)["paused"])
        self.assertEqual(len(ja.load_state(repo)["wakes"]), 2)
        self.assertEqual(self.task(repo, "T001")["status"], "review")

    def test_3e_resume_inside_the_hour_pauses_again_unless_the_limit_is_raised(self):
        repo = self.make_repo()
        self.setup_one_agent(repo, [T("T001")], max_wakes_per_agent_per_hour=1)
        st = ja.load_state(repo)
        st["wakes"].append({"time": ja.iso_now(time.time() - 10), "agent": "gpt", "reason": "claim",
                            "task": "T000", "revision": 0})
        ja.save_state(repo, st)
        d = self.dispatcher(repo, parse_tokens=tok)
        d.run(once=True)
        self.assertTrue(ja.load_state(repo)["paused"])
        base = ["resume", "--repo", str(repo), "--quote", "go", "--by", "claude"]
        self.assertEqual(quiet(job.main, base), 0)
        d.run(once=True)                                           # same hour: still at the limit
        self.assertTrue(ja.load_state(repo)["paused"])
        self.assertEqual(len(ja.load_state(repo)["wakes"]), 1)
        self.assertEqual(quiet(job.main, [*base, "--limit", "max_wakes_per_agent_per_hour=3"]), 0)
        d.run(once=True)
        self.assertFalse(ja.load_state(repo)["paused"])
        self.assertEqual(len(ja.load_state(repo)["wakes"]), 2)


class TestEndAndKill(Base):
    def test_4_job_end_during_run(self):
        repo = self.make_repo()
        self.configure(repo, {"gpt": self.fake_cmd("gpt", "--sleep", "1.5")})
        self.set_tasks(repo, [T("T001"), T("T002")])
        self.start_job(repo)
        disp = subprocess.Popen([sys.executable, str(DISPATCHER_PY), "run", "--repo", str(repo),
                                 "--poll-s", "0.2"], stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 15
            while not [x for x in self.log_lines(repo) if x["event"] == "start"]:
                self.assertLess(time.monotonic(), deadline, "no wake started")
                time.sleep(0.05)
            r = subprocess.run([sys.executable, str(JOB_PY), "end", "--repo", str(repo),
                                "--quote", "job end", "--by", "claude"], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            rc = disp.wait(timeout=30)
        finally:
            if disp.poll() is None:
                disp.kill()
        self.assertEqual(rc, 0)
        lines = self.log_lines(repo)
        self.assertEqual([x["event"] for x in lines], ["start", "end"])
        self.assertFalse(lines[1]["killed"])
        self.assertEqual(lines[1]["exit_code"], 0)
        self.assertEqual(self.task(repo, "T001")["status"], "review")   # the running child finished
        self.assertEqual(self.task(repo, "T002")["status"], "open")     # nothing new was woken
        self.assertFalse(ja.load_job(repo)["active"])

    def test_5_overdue_child_is_killed_and_task_reopened(self):
        repo = self.make_repo()
        self.configure(repo, {"gpt": self.fake_cmd("gpt", "--sleep", "30")})
        self.set_tasks(repo, [T("T001")])
        self.start_job(repo, max_wall_s_per_wake=1)
        t0 = time.monotonic()
        self.dispatcher(repo, parse_tokens=tok).run(once=True)
        self.assertLess(time.monotonic() - t0, 15)
        lines = self.log_lines(repo)
        self.assertEqual([x["event"] for x in lines], ["start", "end"])
        self.assertTrue(lines[1]["killed"])
        self.assertLess(lines[1]["duration_s"], 10)
        t = self.task(repo, "T001")
        self.assertEqual(t["status"], "open")
        self.assertIsNone(t["owner"])
        self.assertIn("wall-time", t["note"])
        # the child is really gone
        with self.assertRaises(ProcessLookupError):
            os.kill(lines[0]["pid"], 0)

    def _group_cmd(self, repo, pidfile):
        return [sys.executable, str(FAKE_GROUP), "--repo", "{repo}", "--tag", "gpt", "--task", "{task}",
                "--reason", "{reason}", "--pidfile", str(pidfile)]

    def test_5b_kill_stops_the_whole_group_before_the_task_is_released(self):
        # F2: the leader exits on SIGTERM, a same-group child ignores it.
        repo = self.make_repo()
        pidfile = repo / "pids.json"
        self.addCleanup(gp.cleanup_group, pidfile)
        self.configure(repo, {"gpt": self._group_cmd(repo, pidfile)})
        self.set_tasks(repo, [T("T001")])
        self.start_job(repo, max_wall_s_per_wake=2)
        at_release = []
        real = ja.return_to_open

        def spy(repo_, task, agent, note):
            pids = gp.read_pids(pidfile)
            at_release.append((gp.group_members(pids["leader"]), pids))
            return real(repo_, task, agent, note)
        with mock.patch.object(ja, "return_to_open", spy):
            d = self.dispatcher(repo, parse_tokens=tok)
            d.TERM_GRACE_S = 0.5      # the stubborn child needs the SIGKILL escalation
            d.run(once=True)
        self.assertEqual(len(at_release), 1)
        members, pids = at_release[0]
        self.assertEqual(members, [], "a group member was still alive when the task was released")
        self.assertNotEqual(pids["leader"], pids["child"])
        self.assertEqual(gp.group_members(pids["leader"]), [])
        lines = self.log_lines(repo)
        self.assertEqual([x["event"] for x in lines], ["start", "end"])
        self.assertTrue(lines[1]["killed"])
        self.assertEqual(lines[1]["exit_code"], -15)      # the leader did exit on SIGTERM
        t = self.task(repo, "T001")
        self.assertEqual((t["status"], t["owner"]), ("open", None))
        self.assertIn("wall-time", t["note"])

    def test_5c_group_that_cannot_be_emptied_keeps_the_claim_and_pauses(self):
        repo = self.make_repo()
        pidfile = repo / "pids.json"
        self.addCleanup(gp.cleanup_group, pidfile)
        self.configure(repo, {"gpt": self._group_cmd(repo, pidfile)})
        self.set_tasks(repo, [T("T001")])
        self.start_job(repo, max_wall_s_per_wake=2)
        d = self.dispatcher(repo, parse_tokens=tok)
        d.TERM_GRACE_S = d.KILL_GRACE_S = 0.2
        released = []
        with mock.patch.object(jd.Dispatcher, "_group_alive", staticmethod(lambda pgid: True)), \
                mock.patch.object(ja, "return_to_open", lambda *a: released.append(a)):
            d.run(once=True)
        self.assertEqual(released, [])
        st = ja.load_state(repo)
        self.assertTrue(st["paused"])
        self.assertIn("process group", st["pause_reason"])
        self.assertIn("T001", st["pause_reason"])
        self.assertIn("could not stop", self.paused_board(repo))
        self.assertEqual(self.task(repo, "T001")["status"], "claimed")    # not released
        self.assertEqual([x["event"] for x in self.log_lines(repo)], ["start", "kill_failed"])
        self.assertEqual(d.children, {})

    def paused_board(self, repo):
        return ja.board_path(repo).read_text().split("## Paused", 1)[1]

    def _admission_race(self, repo, interfere, at="reap"):
        # Run one iteration; `interfere` happens after the first JOB read, before admission.
        self.configure(repo, {"gpt": self.fake_cmd("gpt")})
        self.set_tasks(repo, [T("T001")])
        self.start_job(repo)
        d = self.dispatcher(repo, parse_tokens=tok)
        real_reap = d._reap_and_kill
        done = []

        def reap_then_interfere(limits):
            real_reap(limits)
            if not done:
                interfere()
                done.append(1)
        if at == "reap":
            d._reap_and_kill = reap_then_interfere
        launched = []
        real_popen = subprocess.Popen

        def counting_popen(argv, *a, **k):
            if str(FAKE) in [str(x) for x in argv]:
                launched.append(argv)
            return real_popen(argv, *a, **k)
        real_actions = ja.next_actions

        def actions_then_interfere(repo_):
            acts = real_actions(repo_)
            if not done:
                interfere()
                done.append(1)
            return acts
        patches = [mock.patch.object(jd.subprocess, "Popen", counting_popen)]
        if at == "actions":     # the change lands after the actions were chosen
            patches.append(mock.patch.object(ja, "next_actions", actions_then_interfere))
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            self.assertEqual(d.run(once=True), 0)
        self.assertEqual(done, [1])
        self.assertEqual(launched, [])
        self.assertEqual([x for x in self.log_lines(repo) if x["event"] == "start"], [])
        self.assertEqual(list(ja.dispatcher_runs_dir(repo).glob("*.out")), [])
        st = ja.load_state(repo)
        self.assertEqual((st["wakes"], st["seen"]), ([], []))
        self.assertEqual(self.task(repo, "T001")["status"], "open")
        return d

    def test_4b_job_end_after_the_first_read_but_before_admission_starts_nothing(self):
        # F1: iterate read active -> a real `job.py end` completes -> admission runs.
        repo = self.make_repo()

        def real_end():
            r = subprocess.run([sys.executable, str(JOB_PY), "end", "--repo", str(repo), "--quote",
                                "job end", "--by", "claude"], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
        self._admission_race(repo, real_end)
        self.assertFalse(ja.load_job(repo)["active"])

    def test_4c_pause_after_the_first_read_but_before_admission_starts_nothing(self):
        repo = self.make_repo()

        def pause():
            with ja.lock(repo):
                st = ja.load_state(repo)
                st.update(paused=True, pause_reason="by the user")
                ja.save_state(repo, st)
        self._admission_race(repo, pause, at="actions")
        self.assertTrue(ja.load_state(repo)["paused"])

    def test_4d_admission_waits_for_a_job_end_that_holds_the_lock(self):
        # The end command and the admission serialise on one lock: end first, no wake.
        repo = self.make_repo()
        self.configure(repo, {"gpt": self.fake_cmd("gpt")})
        self.set_tasks(repo, [T("T001")])
        self.start_job(repo)
        d = self.dispatcher(repo, parse_tokens=tok)
        action = ja.next_actions(repo)[0]
        import threading
        results = []
        with ja.lock(repo):                       # stands in for job.py end in the middle of its write
            job = ja.load_job(repo)
            job["active"] = False
            ja.write_json_atomic(ja.job_path(repo), job)
            t = threading.Thread(target=lambda: results.append(d._spawn(action, self.fake_cmd("gpt"))))
            t.start()
            time.sleep(0.3)
            self.assertTrue(t.is_alive(), "admission did not wait for the state lock")
        t.join(10)
        self.assertEqual(results, [False])
        self.assertEqual(d.children, {})

    def test_6_nothing_woken_without_active_job(self):
        repo = self.make_repo()
        self.configure(repo, {"gpt": self.fake_cmd("gpt")})
        self.set_tasks(repo, [T("T001")])
        d = self.dispatcher(repo, parse_tokens=tok)
        d.run(once=True)  # JOB.json absent
        self.assertEqual(self.log_lines(repo), [])
        self.assertFalse(ja.board_path(repo).exists())
        self.start_job(repo)
        quiet(job.main, ["end", "--repo", str(repo), "--quote", "stop", "--by", "claude"])
        d.run(once=True)  # active = false
        self.assertEqual(self.log_lines(repo), [])
        self.assertEqual(self.task(repo, "T001")["status"], "open")
        # the loop form exits at once with 0
        self.assertEqual(d.run(), 0)


class TestJobCli(Base):
    def test_7_round_trip(self):
        repo = self.make_repo()
        base = ["--repo", str(repo)]
        self.assertEqual(quiet(job.main, ["status", *base]), 0)
        self.start_job(repo, max_wakes_per_job=7)
        j = ja.load_job(repo)
        self.assertEqual((j["schema"], j["active"], j["goal"], j["ended"]), ("job-1", True, "test goal", None))
        self.assertEqual(j["limits"]["max_wakes_per_job"], 7)
        self.assertEqual(j["limits"]["max_tasks_posted"], 10)
        self.assertRegex(j["started"], r"\d{4}-\d\d-\d\dT.*[+-]\d\d:\d\d$")
        self.assertEqual(j["quotes"][0]["event"], "start")
        self.assertEqual(j["quotes"][0]["quote"], "go")
        # T038: a second job may start while the first is active; it gets its own id
        self.assertEqual(quiet(job.main, ["start", *base, "--goal", "x", "--quote", "q", "--by", "gpt"]), 0)
        self.assertEqual([j["id"] for j in ja.active_jobs(repo)], ["job-1", "job-2"])
        self.assertEqual(quiet(job.main, ["end", *base, "--quote", "which one?", "--by", "gpt"]), 1)  # ambiguous
        self.assertEqual(quiet(job.main, ["end", *base, "--job", "job-2", "--quote", "job end", "--by", "gpt"]), 0)
        self.assertEqual(ja.load_job(repo)["goal"], "test goal")  # the only active job
        # resume clears a pause
        st = ja.load_state(repo)
        st.update(paused=True, pause_reason="because")
        ja.save_state(repo, st)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            job.main(["status", *base, "--json"])
        info = json.loads(buf.getvalue())
        self.assertTrue(info["active"] and info["paused"])
        self.assertEqual(info["pause_reason"], "because")
        self.assertEqual(quiet(job.main, ["resume", *base, "--quote", "ok go on", "--by", "deepseek",
                                          "--limit", "max_wakes_per_job=9"]), 0)
        self.assertFalse(ja.load_state(repo)["paused"])
        self.assertEqual(ja.load_job(repo)["limits"]["max_wakes_per_job"], 9)
        # end, then a second end is refused
        self.assertEqual(quiet(job.main, ["end", *base, "--quote", "job end", "--by", "gpt"]), 0)
        j = ja.load_job(repo)
        self.assertFalse(j["active"])
        self.assertIsNotNone(j["ended"])
        self.assertEqual(quiet(job.main, ["end", *base, "--quote", "again", "--by", "gpt"]), 1)
        self.assertEqual(quiet(job.main, ["resume", *base, "--quote", "q", "--by", "gpt"]), 1)
        self.assertEqual([(q["event"], q["by"], q["quote"]) for q in j["quotes"]],
                         [("start", "claude", "go"), ("resume", "deepseek", "ok go on"), ("end", "gpt", "job end")])
        # a new job may start after the end, with fresh counters
        self.start_job(repo)
        self.assertEqual(ja.load_state(repo), ja.default_state())
        # bad limit
        quiet(job.main, ["end", *base, "--quote", "e", "--by", "gpt"])
        with self.assertRaises(SystemExit):
            quiet(job.main, ["start", *base, "--goal", "g", "--quote", "q", "--by", "x", "--limit", "bogus=1"])
        # no leftovers of the atomic write
        self.assertEqual([p.name for p in ja.hub_dir(repo).glob(".*.tmp.*")], [])

    def test_7b_cli_process(self):
        repo = self.make_repo()
        r = subprocess.run([sys.executable, str(JOB_PY), "start", "--repo", str(repo), "--goal", "g",
                            "--quote", "job start: g", "--by", "claude"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = subprocess.run([sys.executable, str(JOB_PY), "status", "--repo", str(repo)],
                           capture_output=True, text=True)
        self.assertIn("active: g", r.stdout)


class TestBoard(Base):
    def test_8_sections_and_claim_removes_entry(self):
        repo = self.make_repo()
        self.set_tasks(repo, [T("T001"), T("T002", title="second"),
                              T("T003", status="accepted", owner="gpt", revision=1),
                              T("T004", status="claimed", owner="gpt", question="which one?", question_by="gpt")])
        self.start_job(repo)
        text = ja.board_path(repo).read_text()
        self.assertEqual(text.splitlines()[0],
                         "<!-- Generated by docs/agent_hub/tools/board.py; do not edit by hand. -->")
        for s in ("## Open", "## For the user to check", "## Questions for the user", "## Paused"):
            self.assertIn(s, text)
        for needle in ("Goal: test goal", "Started:", "active", "max_wakes_per_job=40",
                       "Tasks posted: 4 of 10", "Wakes: 0 of 40", "Tokens"):
            self.assertIn(needle, text)

        def section(t, name, nxt):
            return t.split(name, 1)[1].split(nxt, 1)[0]
        opens = section(text, "## Open", "## For the user")
        self.assertIn("T001", opens)
        self.assertIn("T002", opens)
        self.assertNotIn("T004", opens)
        self.assertIn("T003", section(text, "## For the user to check", "## Questions"))
        self.assertIn("which one?", section(text, "## Questions for the user", "## Paused"))
        # claim T001: after the next render it is gone from Open
        self.assertTrue(ja.claim(repo, "gpt", "T001"))
        self.assertTrue(board.render(repo))
        opens = section(ja.board_path(repo).read_text(), "## Open", "## For the user")
        self.assertNotIn("T001", opens)
        self.assertIn("T002", opens)
        self.assertFalse(board.render(repo))  # unchanged -> no rewrite

    def test_8b_cli_and_paused_header(self):
        repo = self.make_repo()
        self.set_tasks(repo, [T("T001")])
        self.start_job(repo)
        st = ja.load_state(repo)
        st.update(paused=True, pause_reason="manual test pause")
        ja.save_state(repo, st)
        r = subprocess.run([sys.executable, str(BOARD_PY), "render", "--repo", str(repo)],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        text = ja.board_path(repo).read_text()
        self.assertIn("PAUSED (manual test pause)", text)
        self.assertIn("manual test pause", text.split("## Paused", 1)[1])


class TestConfig(Base):
    def test_9_no_agents_configured_wakes_nobody(self):
        repo = self.make_repo()
        self.set_tasks(repo, [T("T001")], agents=["gpt", "deepseek"])
        self.start_job(repo)
        buf = io.StringIO()
        d = jd.Dispatcher(repo, poll_s=0.05, err=buf)
        d.run(once=True)
        d.run(once=True)
        self.assertIn("warning", buf.getvalue())
        self.assertIn("job_agents", buf.getvalue())
        self.assertEqual(buf.getvalue().count("job_agents"), 1)  # warned once
        self.assertEqual(self.log_lines(repo), [])
        self.assertEqual(ja.load_state(repo)["wakes"], [])
        self.assertEqual(self.task(repo, "T001")["status"], "open")

    def test_9b_backend_selection(self):
        repo = self.make_repo()
        self.assertEqual(ja.get_backend(repo).name, "stub")
        (ja.hub_dir(repo) / "hub_config.json").write_text(json.dumps({"job_backend": "hub"}))
        self.assertEqual(ja.get_backend(repo).name, "hub")
        # phase 2: the hub backend is real; with no job started there are no job tasks
        self.assertEqual(ja.next_actions(repo), [])

    def test_lock_busy(self):
        repo = self.make_repo()
        with ja.lock(repo):
            p = subprocess.run([sys.executable, "-c",
                                "import sys; sys.path.insert(0, %r); import job_adapters as ja\n"
                                "try:\n  ja.lock(%r, timeout=0.3).__enter__()\nexcept ja.Busy: print('busy')"
                                % (str(HERE), str(repo))], capture_output=True, text=True)
            self.assertEqual(p.stdout.strip(), "busy")
        with ja.lock(repo, timeout=0.3):
            pass


if __name__ == "__main__":
    unittest.main()
