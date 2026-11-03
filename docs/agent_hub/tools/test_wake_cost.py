"""Tests for T045 (wake cost, user decision D004): the wake prompt, the Codex sub-agent switch in
hub_config.json, and the hold of the owner's fix wake while a review wake of the task runs.  Every
test works in a throw-away git repository; the fake agents spend no tokens.  Run:

    python3 -m unittest discover -s docs/agent_hub/tools -p 'test_wake_cost.py' -v
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import job_adapters as ja  # noqa: E402
from test_job_board import Base, T, tok  # noqa: E402

HUB = HERE.parent

PRINT_PROMPT = [sys.executable, "-c", "import sys; print(sys.argv[1]); print('tokens=5')", "{prompt}"]
# A wake that keeps running until the flag file exists (bounded, so a failing test cannot hang).
WAIT_FLAG = [sys.executable, "-c",
             "import sys, time, pathlib\n"
             "p = pathlib.Path(sys.argv[1]); t = time.monotonic() + 20\n"
             "while not p.exists() and time.monotonic() < t:\n"
             "    time.sleep(0.01)\n"
             "print('tokens=5')\n"]
QUICK = [sys.executable, "-c", "print('tokens=5')"]


class TestWakePrompt(Base):
    def prompt_of(self, task):
        repo = self.make_repo()
        self.configure(repo, {"gpt": PRINT_PROMPT})
        self.set_tasks(repo, [task])
        self.start_job(repo)
        self.dispatcher(repo, parse_tokens=tok).run(once=True)
        return next(ja.dispatcher_runs_dir(repo).glob("*.out")).read_text()

    def test_claim_wake_prompt_is_lean_and_has_no_rereview_sentence(self):
        out = self.prompt_of(T("T001"))
        self.assertIn("reason claim, task T001", out)
        self.assertIn("do this one action, then exit.", out)
        self.assertIn("do not read STATE.md, DECISIONS.md, INDEX.md", out)
        self.assertIn("Do not start sub-agents or workers", out)
        self.assertNotIn("this is a re-review", out)

    def test_review_wake_prompt_adds_the_rereview_sentence(self):
        out = self.prompt_of(T("T002", status="review", owner="claude", reviewer="gpt", revision=1))
        self.assertIn("reason review, task T002", out)
        self.assertIn("do this one action, then exit.", out)
        self.assertIn("Do not start sub-agents or workers", out)
        self.assertIn("this is a re-review", out)


class TestOwnerFixHold(Base):
    """D004: the owner's continuation wake waits while a review wake of the same task runs."""

    def setUp(self):
        super().setUp()
        self.repo = self.make_repo()
        self.flag = self.repo / "gpt_may_end"
        self.set_tasks(self.repo, [T("T050"), T("T051")])
        self.start_job(self.repo)
        self.actions = []
        patcher = mock.patch.object(ja, "next_actions", lambda *a, **k: list(self.actions))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.d = self.dispatcher(self.repo, parse_tokens=tok)
        # gpt reviews T050 and keeps running until the flag file appears; claude's wake is quick
        self.configure(self.repo, {"gpt": WAIT_FLAG + [str(self.flag)], "claude": QUICK})
        self.addCleanup(self.flag.write_text, "x")  # never leave a sleeping child behind
        self.review = ja.Action("gpt", "review", "T050", "r1", job=None)
        self.actions = [self.review]
        self.d.iterate()  # gpt's review wake starts and keeps running
        self.assertIn("gpt", self.d.children)

    def starts(self, agent=None):
        return [(x["agent"], x["reason"], x["task"]) for x in self.log_lines(self.repo)
                if x["event"] == "start" and agent in (None, x["agent"])]

    def end_gpt(self):
        self.flag.write_text("x")
        self.d._drain()
        self.assertEqual(self.d.children, {})

    def test_continuation_waits_for_the_running_review_then_starts_once(self):
        fix = ja.Action("claude", "claim", "T050", "r1#r1", continuation=True, job=None)
        self.actions = [self.review, fix]
        for _ in range(3):
            self.d.iterate()
        self.assertEqual(self.starts("claude"), [])
        self.assertEqual(list(self.d.children), ["gpt"])
        self.assertNotIn(["claude", "claim", "T050", "r1#r1"], ja.load_state(self.repo)["seen"])
        self.end_gpt()
        self.d.iterate()
        self.d._drain()
        self.d.iterate()  # the key is seen now: no second wake
        self.assertEqual(self.starts("claude"), [("claude", "claim", "T050")])
        self.assertEqual(len(self.starts()), 2)

    def test_continuation_of_another_task_is_not_held(self):
        fix = ja.Action("claude", "claim", "T051", "r1#r1", continuation=True, job=None)
        self.actions = [self.review, fix]
        self.d.iterate()
        self.assertEqual(self.starts("claude"), [("claude", "claim", "T051")])
        self.assertIn("gpt", self.d.children)
        self.end_gpt()

    def test_plain_claim_of_the_same_task_is_not_held(self):
        claim = ja.Action("claude", "claim", "T050", "r1", continuation=False, job=None)
        self.actions = [self.review, claim]
        self.d.iterate()
        self.assertEqual(self.starts("claude"), [("claude", "claim", "T050")])
        self.assertIn("gpt", self.d.children)
        self.end_gpt()


class TestConfigs(unittest.TestCase):
    # T050: an installed starter kit has no starter/ folder, and setup_hub.py drops the agents a
    # machine does not have, so only existing configs and configured agents are checked.
    PATHS = {name: path for name, path in {"hub_config.json": HUB / "hub_config.json",
                                           "starter/hub_config.json": HUB / "starter" / "hub_config.json"}.items()
             if path.exists()}

    def agents(self, path):
        return json.loads(path.read_text())["job_agents"]

    def test_gpt_command_disables_multi_agent_right_after_exec(self):
        for name, path in self.PATHS.items():
            with self.subTest(config=name):
                if "gpt" not in self.agents(path):
                    continue
                cmd = self.agents(path)["gpt"]
                i = cmd.index("exec")
                self.assertEqual(cmd[i + 1:i + 3], ["--disable", "multi_agent"])

    def test_claude_allowed_tools_have_no_agent_tool(self):
        for name, path in self.PATHS.items():
            with self.subTest(config=name):
                if "claude" not in self.agents(path):
                    continue
                cmd = self.agents(path)["claude"]
                value = cmd[cmd.index("--allowedTools") + 1]
                names = {item.split("(")[0].strip() for item in value.split(",")}
                self.assertFalse(names & {"Agent", "Task"}, names)


if __name__ == "__main__":
    unittest.main()
