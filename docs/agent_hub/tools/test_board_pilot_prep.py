"""T028 (T-hub-028) tests: job-board pilot preparation.

  (1)  the board and the dispatcher log show display names (T-<category>-NNN);
  (2a) `job.py check --ok` writes exactly one D### block and the board moves the task;
  (2b) `job.py check --not-ok` writes a D### block plus a `user action=open` event, the state
       becomes open and the owner is woken again (the decision id is in the dedupe key);
  (2c) a check on a non-job or non-accepted task is refused and writes nothing;
  (3a) after a timeout release another agent can claim the task and deliver;
  (3b) the same task is re-woken at most once per job (the second timeout leaves it Open,
       writes a line under Paused and does not pause the job);
  (3c) a task with a named owner keeps that owner after a release.

Every test works in a throw-away git repository (HubBase of test_job_board_hub.py), with fake
agents; nothing touches the real hub.  Run:

    python3 -m unittest discover -s docs/agent_hub/tools -p 'test_board_pilot_prep.py' -v
"""
from __future__ import annotations

import contextlib
import io
import re
import sys
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import board  # noqa: E402
import hub_events as events  # noqa: E402
import hub_state  # noqa: E402
import hubtx  # noqa: E402
import job  # noqa: E402
import job_adapters as ja  # noqa: E402
from test_job_board_hub import HubBase, quiet  # noqa: E402

DECISIONS = "docs/agent_hub/DECISIONS.md"

# A reviewer for the redelivery tests.  fake_hub_agent names its review file after the revision,
# so it cannot review the same bytes twice; this one numbers its files.  It acts only through
# the hub CLIs, like fake_hub_agent.
REREVIEWER = """\
import subprocess, sys
from pathlib import Path
tools, repo, tag, task = sys.argv[1:5]
sys.path.insert(0, tools)
import hub_events as events, hub_state
def cli(script, *args):
    r = subprocess.run([sys.executable, tools + "/" + script, *args], cwd=repo, capture_output=True, text=True)
    if r.returncode:
        print(script, r.returncode, r.stderr)
        raise SystemExit(1)
d = hub_state.derive(Path(repo), task)
rev = d["revision"]
n = sum(1 for e in d["events"] if e["event"] == "review")
name = f"docs/agent_hub/reviews/2026-10-31_{task}_{rev[:8]}r{n}_{tag}.md"
(Path(repo) / name).parent.mkdir(parents=True, exist_ok=True)
(Path(repo) / name).write_text("# Review\\n\\n| ID | Claim | Verdict | Severity | Evidence |\\n"
                               "|---|---|---|---|---|\\n| F1 | ok | CONFIRMED | informational | e |\\n")
cli("hub_commit.py", tag, task, "-m", "review", name)
cli("hub_append.py", tag, task, hub_state.status_path(task), events.format_event(tag, "review", dict(file=name, rev=rev)))
# the note names the review round, so a re-accept of the same revision within the same second is not a duplicate line
cli("hub_append.py", tag, task, hub_state.status_path(task),
    events.format_event(tag, "accept", dict(rev=rev), note=f"review round {n}"))
"""


class PrepBase(HubBase):
    def decisions_text(self):
        return (self.repo / DECISIONS).read_text(encoding="utf-8")

    def decision_ids(self):
        return re.findall(r"^### (D\d{3,})\b", self.decisions_text(), re.M)

    def run_check(self, task, verdict, quote="that looks right", by="claude"):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = job.main(["check", task, verdict, "--quote", quote, "--by", by, "--repo", str(self.repo)])
        return rc, out.getvalue(), err.getvalue()

    def head(self):
        return self.git("rev-parse", "HEAD").strip()

    def sec(self, name, nxt):
        return self.section(name, nxt)

    def checked_section(self):
        return self.section("Checked by the user (this job)", "Questions for the user")

    def check_section_only(self):
        return self.section("For the user to check", "Checked by the user (this job)")

    def full_cycle(self, title="Write the file", deliverable="job/out.txt"):
        """claude posts, deepseek delivers, gpt accepts: the task is accepted."""
        self.configure({"gpt": self.fake_cmd("gpt"), "deepseek": self.fake_cmd("deepseek")})
        self.start_job()
        task = self.post("claude", title, deliverable, reviewers=["gpt"])
        d = self.dispatcher()
        d.run(once=True)
        d.run(once=True)
        self.assertTrue(self.state(task)["accepted"])
        return task, d

    def set_names(self, mapping):
        (self.repo / "docs/agent_hub/TASK_NAMES.tsv").write_text(
            "".join(f"{k}\t{v}\n" for k, v in mapping.items()), encoding="utf-8")
        self.git("add", "docs/agent_hub/TASK_NAMES.tsv")
        self.git("commit", "-qm", "names")


# --------------------------------------------------------------------------
class TestDisplayNames(PrepBase):
    def test_1_board_and_dispatcher_log_show_display_names(self):
        self.configure({"gpt": self.fake_cmd("gpt"), "deepseek": self.fake_cmd("deepseek")})
        self.start_job()
        t1 = self.post("claude", "Write the file", "job/a.txt", reviewers=["gpt"])
        t2 = self.post("claude", "Second thing", "job/b.txt")
        t3 = self.post("claude", "Unmapped thing", "job/c.txt")
        self.set_names({t1: "hub", t2: "model"})           # t3 stays unmapped
        board.render(self.repo)
        text = ja.board_path(self.repo).read_text()
        self.assertIn(f"**T-hub-{t1[1:]}** Write the file", self.open_section())
        self.assertIn(f"**T-model-{t2[1:]}** Second thing", self.open_section())
        self.assertIn(f"**{t3}** Unmapped thing", self.open_section())     # no category: plain id
        self.assertNotIn(f"**{t1}**", text)
        d = self.dispatcher()
        d.run(once=True)
        d.run(once=True)
        d.run(once=True)
        board.render(self.repo)
        accepted = [t for t in (t1, t2, t3) if self.state(t)["accepted"]]
        self.assertIn(t1, accepted)
        self.assertIn(f"**T-hub-{t1[1:]}**", self.check_section())
        # the log keeps the canonical id (state files, dedupe) and adds the display name
        lines = [x for x in self.log_lines() if x["task"] == t1]
        self.assertTrue(lines)
        self.assertTrue(all(x["task_name"] == f"T-hub-{t1[1:]}" for x in lines))
        unmapped = [x for x in self.log_lines() if x["task"] == t3]
        self.assertTrue(all("task_name" not in x for x in unmapped))
        # the canonical id stays in the state files
        self.assertTrue((self.repo / hub_state.status_path(t1)).is_file())
        self.assertNotIn("T-hub", (self.repo / hub_state.status_path(t1)).read_text())


# --------------------------------------------------------------------------
class TestCheckOk(PrepBase):
    def test_2a_check_ok_writes_one_block_and_the_board_moves_the_task(self):
        task, _ = self.full_cycle()
        self.set_names({task: "hub"})
        board.render(self.repo)
        self.assertIn(task, self.check_section_only().replace("T-hub-" + task[1:], task))
        self.assertEqual(self.decision_ids(), [])
        before = self.head()
        rc, out, err = self.run_check(f"T-hub-{task[1:]}", "--ok", quote="Looks good\nto me", by="claude")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.decision_ids(), ["D001"])                    # exactly one block
        text = self.decisions_text()
        self.assertIn("recorded by [claude] · heard in [claude]'s chat", text)
        self.assertIn('- quote: "Looks good to me"', text)                  # one line, user's words
        self.assertIn(f"- scope: task={task}; check=ok", text)
        self.assertRegex(text, r"### D001 · \d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d · recorded by")
        self.assertTrue(hub_state._decision_recorded(self.repo, "D001"))
        self.assertTrue(hubtx._decision_exists(self.repo, "D001"))
        self.assertNotEqual(self.head(), before)
        self.assertEqual(self.git("log", "-1", "--format=%s").strip().split(":")[0], f"[claude] {task}")
        self.assertEqual(self.git("status", "--porcelain", DECISIONS).strip(), "")   # committed
        # ok writes no status event
        self.assertEqual([e["event"] for e in self.state(task)["events"]].count("user"), 0)
        self.assertTrue(self.state(task)["accepted"])
        # the board moved it
        self.assertNotIn(task, self.check_section_only().replace("T-hub-" + task[1:], task))
        self.assertIn("_None._", self.check_section_only())
        checked = self.checked_section()
        self.assertIn(f"**T-hub-{task[1:]}**", checked)
        self.assertIn("D001", checked)
        self.assertNotIn(task, self.open_section())
        # a later job does not show it (the section is per job)
        self.end_job()
        time.sleep(1.1)
        self.start_job()
        self.assertNotIn(f"T-hub-{task[1:]}", self.checked_section())

    def test_2a_next_decision_number_follows_the_existing_headers(self):
        task, _ = self.full_cycle()
        text = self.decisions_text() + "\n### D007 · earlier · recorded by [gpt] · heard in [gpt]'s chat\n" \
            '- quote: "an older decision"\n'
        (self.repo / DECISIONS).write_text(text)
        self.git("add", DECISIONS)
        self.git("commit", "-qm", "older decision")
        rc, _, err = self.run_check(task, "--ok")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.decision_ids(), ["D007", "D008"])
        self.assertTrue(hubtx._decision_exists(self.repo, "D008"))
        self.assertTrue(hubtx._decision_exists(self.repo, "D007"))   # still unambiguous

    def test_2a_an_ok_does_not_cover_a_later_accepted_revision(self):
        task, d = self.full_cycle()
        self.assertEqual(self.run_check(task, "--ok")[0], 0)
        (self.repo / "job" / "out.txt").write_text("changed after the check\n")
        self.cli("hub_commit.py", "deepseek", task, "-m", "tweak", "job/out.txt")
        d.run(once=True)                                  # T048: a commit alone wakes nobody
        self.assertFalse(self.state(task)["accepted"])
        self.cli("job_act.py", "submit", "deepseek", task, "-m", "resubmit")
        d.run(once=True)                                  # gpt reviews the new submit: accepted again
        self.assertTrue(self.state(task)["accepted"])
        board.render(self.repo)
        self.assertIn(task, self.check_section_only())    # needs the user's check again
        self.assertNotIn(task, self.checked_section())


# --------------------------------------------------------------------------
class TestCheckNotOk(PrepBase):
    def test_2b_not_ok_reopens_the_task_and_wakes_the_owner(self):
        task, d = self.full_cycle()
        before = self.head()
        rc, out, err = self.run_check(task, "--not-ok", quote="the colour is wrong")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.decision_ids(), ["D001"])
        self.assertIn(f"- scope: task={task}; check=not_ok", self.decisions_text())
        self.assertTrue(hubtx._decision_exists(self.repo, "D001"))
        last = self.state(task)["events"][-1]
        self.assertEqual((last["tag"], last["event"]), ("claude", "user"))
        self.assertEqual(last["fields"], {"decision": "D001", "action": "open", "task": task})
        subjects = self.git("log", "--format=%s", f"{before}..HEAD").strip().split("\n")
        self.assertEqual(len(subjects), 2)                   # the decision, then the event
        s = self.state(task)
        self.assertEqual((s["state"], s["owner"], s["accepted"]), ("open", "deepseek", False))
        self.assertEqual(s["next_actor"], "deepseek")
        # the board: Open (with the user's words), no longer waiting for the user's check
        board.render(self.repo)
        self.assertIn(task, self.open_section())
        self.assertIn("reopened by the user (D001)", self.open_section())
        self.assertIn("the colour is wrong", self.open_section())
        self.assertNotIn(task, self.check_section_only())
        self.assertNotIn(task, self.checked_section())
        # the owner is woken again, with the decision id in the key
        wakes = [a for a in ja.next_actions(self.repo) if a.task == task and a.agent == "deepseek"
                 and a.revision != s["revision"]]
        self.assertEqual([a.revision for a in wakes], [f"{s['revision']}#r1#D001"])
        d.run(once=True)
        self.assertEqual(self.starts(), [("deepseek", "claim", task), ("gpt", "review", task),
                                         ("deepseek", "claim", task)])
        self.assertEqual(self.state(task)["state"].split()[0], "review")
        d.run(once=True)                                        # gpt reviews the redelivery
        self.assertTrue(self.state(task)["accepted"])
        self.assertEqual(len(self.starts()), 4)
        d.run(once=True)
        self.assertEqual(len(self.starts()), 4)                 # and then it stays quiet
        board.render(self.repo)
        self.assertIn(task, self.check_section_only())          # back with the user for a new check
        # a second not-ok wakes the owner again: D002 is a new key, not swallowed by D001's
        self.assertEqual(self.run_check(task, "--not-ok", quote="still wrong")[0], 0)
        self.assertEqual(self.decision_ids(), ["D001", "D002"])
        d.run(once=True)
        woken = [x for x in self.log_lines() if x["event"] == "start" and x["agent"] == "deepseek"]
        self.assertEqual(len(woken), 3)
        self.assertTrue(woken[-1]["revision"].endswith("#D002"))

    def test_2b_not_ok_is_allowed_for_a_task_in_review(self):
        self.configure({"deepseek": self.fake_cmd("deepseek")})
        self.start_job()
        task = self.post("claude", "In review", "job/r.txt", reviewers=["gpt"])
        self.dispatcher().run(once=True)
        self.assertEqual(self.state(task)["state"], "review")
        rc, _, err = self.run_check(task, "--not-ok", quote="wrong direction")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.state(task)["state"], "open")
        self.assertEqual(self.state(task)["owner"], "deepseek")
        self.assertEqual(self.run_check(task, "--ok")[0], 1)         # --ok needs an accepted task

    def test_2b_user_open_clears_earlier_accepts(self):
        # hub_state: the user's reopen voids the reviewers' accepts, so "accepted" does not win again
        task, _ = self.full_cycle()
        self.assertEqual(self.run_check(task, "--not-ok")[0], 0)
        s = self.state(task)
        self.assertEqual(s["latest_accepts"], {})
        self.assertFalse(s["accepted"])


# --------------------------------------------------------------------------
class TestReopenFreshReview(PrepBase):
    """T028 F1: after a user "not OK" the owner may redeliver byte-identical files; the
    required reviewers must still get exactly one fresh wake per reopen cycle."""

    def resubmit_unchanged(self, task, owner="deepseek"):
        """The owner submits the reopened task again with the very same bytes."""
        d = self.state(task)
        rev = hub_state.revision(self.repo, d["deliverables"])
        if d["holder"] != owner:      # the user's reopen leaves the claim with the owner
            self.cli("claim.py", owner, task, *[x for p in d["deliverables"] for x in ("--scope", p)])
        self.cli("hub_append.py", owner, task, hub_state.status_path(task),
                 events.format_event(owner, "submit", dict(rev=rev),
                                              note=f"unchanged bytes after {self.decision_ids()[-1]}"))
        s = self.state(task)
        self.assertEqual(s["revision"], rev)

    def rereviewer(self, tag):
        script = self.repo / ".rereviewer.py"        # untracked, like hub_config.json
        script.write_text(REREVIEWER, encoding="utf-8")
        return [sys.executable, str(script), str(self.tools), "{repo}", tag, "{task}"]

    def review_starts(self, task):
        return [(x["agent"], x["revision"]) for x in self.log_lines()
                if x["event"] == "start" and x["reason"] == "review" and x["task"] == task]

    def two_reviewer_cycle(self):
        """claude posts, deepseek delivers, gpt and claude review and accept."""
        self.configure({"gpt": self.rereviewer("gpt"), "deepseek": self.fake_cmd("deepseek"),
                        "claude": self.rereviewer("claude")})
        self.start_job()
        task = self.post("claude", "Two reviewers", "job/two.txt", reviewers=["gpt", "claude"])
        d = self.dispatcher()
        for _ in range(3):
            d.run(once=True)
        self.assertTrue(self.state(task)["accepted"])
        self.assertEqual(sorted(a for a, _ in self.review_starts(task)), ["claude", "gpt"])
        return task

    def only_reviewers(self):
        self.configure({"gpt": self.rereviewer("gpt"), "claude": self.rereviewer("claude")})   # not the owner

    def test_f1_unchanged_redelivery_gets_one_fresh_review_per_required_reviewer(self):
        task = self.two_reviewer_cycle()
        first_rev = self.state(task)["revision"]
        self.assertEqual(self.run_check(task, "--not-ok", quote="not what I meant")[0], 0)
        self.only_reviewers()                                    # the owner redelivers by hand
        self.resubmit_unchanged(task)
        s = self.state(task)
        self.assertEqual((s["revision"], s["state"], s["accepted"], s["latest_accepts"]),
                         (first_rev, "review", False, {}))
        self.assertEqual(s["next_actor"], "gpt,claude")
        # while it waits it is on the board (it used to be in no section at all)
        board.render(self.repo)
        waiting = self.section("In review", "Paused")
        self.assertIn(task, waiting)
        self.assertIn("waiting for gpt+claude", waiting)
        self.assertIn("reopened by the user (D001)", waiting)
        self.assertNotIn(task, self.open_section())
        self.assertNotIn(task, self.check_section_only())
        d = self.dispatcher()
        d.run(once=True)
        starts = self.review_starts(task)
        self.assertEqual(len(starts), 4)                         # two before, exactly one each now
        self.assertEqual(sorted(a for a, _ in starts[2:]), ["claude", "gpt"])
        self.assertEqual({r for _, r in starts[2:]}, {f"{first_rev}#D001"})
        self.assertTrue(self.state(task)["accepted"])
        for _ in range(3):                                       # later polls stay quiet
            d.run(once=True)
            self.dispatcher().run(once=True)                     # and so does a restarted dispatcher
        self.assertEqual(len(self.review_starts(task)), 4)
        board.render(self.repo)
        self.assertNotIn(task, self.section("In review", "Paused"))
        self.assertIn(task, self.check_section_only())           # back with the user
        # a later second "not OK" opens another cycle
        self.assertEqual(self.run_check(task, "--not-ok", quote="still not")[0], 0)
        self.resubmit_unchanged(task)
        d.run(once=True)
        starts = self.review_starts(task)
        self.assertEqual(len(starts), 6)
        self.assertEqual({r for _, r in starts[4:]}, {f"{first_rev}#D002"})
        d.run(once=True)
        self.assertEqual(len(self.review_starts(task)), 6)

    def test_f1_a_changed_revision_is_still_one_wake_per_reviewer(self):
        task = self.two_reviewer_cycle()
        self.assertEqual(self.run_check(task, "--not-ok", quote="change it")[0], 0)
        d = self.dispatcher()
        d.run(once=True)                                         # the owner redelivers new bytes
        s = self.state(task)
        self.assertEqual(s["state"], "review")
        self.assertEqual(len(self.review_starts(task)), 2)       # the reviewers have not run yet
        d.run(once=True)
        starts = self.review_starts(task)
        self.assertEqual(len(starts), 4)
        self.assertEqual({r for _, r in starts[2:]}, {f"{s['revision']}#D001"})
        for _ in range(3):
            d.run(once=True)
        self.assertEqual(len(self.review_starts(task)), 4)

    def test_f1_an_unreopened_task_keeps_the_plain_revision_key(self):
        task, _ = self.full_cycle()
        keys = [r for _, r in self.review_starts(task)]
        self.assertEqual(keys, [self.state(task)["revision"]])    # no suffix without a reopen

    def test_f1_the_in_review_section_lists_a_plain_submitted_task(self):
        self.configure({"deepseek": self.fake_cmd("deepseek")})  # nobody reviews
        self.start_job()
        task = self.post("claude", "Waits", "job/w.txt", reviewers=["gpt"])
        self.dispatcher().run(once=True)
        board.render(self.repo)
        self.assertIn(task, self.section("In review", "Paused"))
        self.assertIn("waiting for gpt", self.section("In review", "Paused"))
        self.assertNotIn(task, self.open_section())


# --------------------------------------------------------------------------
class TestCheckRefused(PrepBase):
    def assert_nothing_written(self, head, task):
        self.assertEqual(self.head(), head)
        self.assertEqual(self.decision_ids(), [])
        self.assertEqual([e for e in self.state(task)["events"] if e["event"] == "user"], [])
        self.assertEqual(self.git("status", "--porcelain", DECISIONS).strip(), "")

    def test_2c_refused_for_a_task_that_is_not_accepted(self):
        self.configure({})
        self.start_job()
        task = self.post("claude", "Still open", "job/o.txt", reviewers=["gpt"])
        head = self.head()
        for verdict in ("--ok", "--not-ok"):
            rc, out, err = self.run_check(task, verdict)
            self.assertEqual(rc, 1)
            self.assertIn("open", err)
            self.assertEqual(out, "")
        self.assert_nothing_written(head, task)

    def test_2c_refused_for_a_task_that_is_not_a_job_task(self):
        old = self.post("claude", "Before the job", "job/old.txt", reviewers=["gpt"])
        # make it accepted so only the job membership can refuse it
        self.cli("claim.py", "deepseek", old, "--scope", "job/old.txt")
        (self.repo / "job").mkdir(exist_ok=True)
        (self.repo / "job/old.txt").write_text("x\n")
        self.cli("hub_commit.py", "deepseek", old, "-m", "deliver", "job/old.txt")
        rev = hub_state.revision(self.repo, ["job/old.txt"])
        self.cli("hub_append.py", "deepseek", old, hub_state.status_path(old),
                 events.format_event("deepseek", "submit", dict(rev=rev)))
        self.cli("hub_append.py", "gpt", old, hub_state.status_path(old),
                 events.format_event("gpt", "accept", dict(rev=rev)))
        self.assertTrue(self.state(old)["accepted"])
        time.sleep(1.1)
        self.start_job()
        head = self.head()
        for verdict in ("--ok", "--not-ok"):
            rc, _, err = self.run_check(old, verdict)
            self.assertEqual(rc, 1)
            self.assertIn("not a task of this job", err)
        self.assert_nothing_written(head, old)

    def test_2c_other_refusals_write_nothing(self):
        task, _ = self.full_cycle()
        head = self.head()
        self.assertEqual(self.run_check("T999", "--ok")[0], 1)                # no such task
        self.assertEqual(self.run_check(task, "--ok", quote="  \n ")[0], 1)    # empty quote
        self.assertEqual(self.run_check(task, "--ok", by="nobody")[0], 1)      # unknown tag
        self.assertEqual(self.run_check("T-model-" + task[1:], "--ok")[0], 1)  # wrong category / unmapped
        self.assert_nothing_written(head, task)
        # no verdict, or both, is an argparse error
        for argv in (["check", task, "--quote", "q", "--by", "claude"],
                     ["check", task, "--ok", "--not-ok", "--quote", "q", "--by", "claude"]):
            with self.assertRaises(SystemExit):
                quiet(job.main, argv + ["--repo", str(self.repo)])
        self.assert_nothing_written(head, task)

    def test_2c_a_dirty_decisions_file_refuses_and_leaves_it_alone(self):
        task, _ = self.full_cycle()
        (self.repo / DECISIONS).write_text(self.decisions_text() + "\n- uncommitted edit\n")
        head = self.head()
        rc, _, err = self.run_check(task, "--ok")
        self.assertNotEqual(rc, 0)
        self.assertEqual(self.head(), head)
        self.assertTrue(self.decisions_text().endswith("- uncommitted edit\n"))
        self.assertEqual(self.decision_ids(), [])


# --------------------------------------------------------------------------
class TestReclaimAfterRelease(PrepBase):
    def slow_job(self, **limits):
        self.configure({"deepseek": self.fake_cmd("deepseek", "--sleep", "30")})
        self.start_job(max_wall_s_per_wake=1, **limits)

    def test_3a_after_a_timeout_release_another_agent_can_claim_and_deliver(self):
        self.slow_job()
        task = self.post("claude", "Slow work", "job/slow.txt")
        self.dispatcher().run(once=True)
        s = self.state(task)
        self.assertEqual((s["state"], s["holder"], s["owner"], s["base_owner"]), ("open", None, "open", "open"))
        self.assertEqual(s["next_actor"], "any")
        self.assertEqual(self.claims(), [])
        self.assertTrue(ja.HubBackend().claim(self.repo, "gpt", task))      # another agent claims
        s = self.state(task)
        self.assertEqual((s["state"], s["holder"], s["owner"]), ("claimed", "gpt", "gpt"))
        self.assertEqual([(c["task"], c["tag"]) for c in self.claims()], [(task, "gpt")])
        # and the new owner can deliver: its claim scope is in force for the commit
        self.fake("gpt", task, "claim")
        s = self.state(task)
        self.assertEqual((s["state"], s["owner"], s["holder"]), ("review", "gpt", "gpt"))
        self.assertEqual((self.repo / "job/slow.txt").read_text(), f"gpt delivery 1 of {task}\n")
        # the former holder cannot write into the task any more
        r = self.cli("claim.py", "deepseek", task, "--scope", "job/slow.txt", check=False)
        self.assertEqual(r.returncode, 3)

    def test_3a_a_reviewer_still_cannot_claim_after_a_release(self):
        self.slow_job()
        task = self.post("claude", "Slow work", "job/slow.txt", reviewers=["gpt"])
        self.dispatcher().run(once=True)
        self.assertEqual(self.state(task)["owner"], "open")
        r = self.cli("claim.py", "gpt", task, "--scope", "job/slow.txt", check=False)
        self.assertEqual(r.returncode, 3)
        self.assertFalse(ja.HubBackend().claim(self.repo, "gpt", task))

    def test_3a_effective_owner_unit(self):
        mk = lambda tag, ev, **f: events.parse_text(events.format_event(tag, ev, f, timestamp="2026-10-31T10:00:00-04:00"))[0][0]  # noqa: E731
        claim = lambda tag: mk(tag, "claim", scope=["a/*"])  # noqa: E731
        release = lambda tag: mk(tag, "release")  # noqa: E731
        eo = hub_state._effective_owner
        self.assertEqual(eo("open", set(), []), "open")
        self.assertEqual(eo("open", set(), [claim("gpt")]), "gpt")
        self.assertEqual(eo("open", set(), [claim("gpt"), release("gpt")]), "open")
        self.assertEqual(eo("open", set(), [claim("gpt"), release("gpt"), claim("deepseek")]), "deepseek")
        self.assertEqual(eo("open", {"gpt"}, [claim("gpt")]), "open")                     # reviewer
        self.assertEqual(eo("open", set(), [claim("gpt"), release("claude")]), "gpt")     # not the holder
        self.assertEqual(eo("claude", set(), [claim("claude"), release("claude")]), "claude")

    def test_3b_a_timed_out_task_is_re_woken_at_most_once(self):
        self.slow_job()
        task = self.post("claude", "Slow work", "job/slow.txt")
        d = self.dispatcher()
        d.run(once=True)
        self.assertEqual(self.starts(), [("deepseek", "claim", task)])
        self.assertEqual(ja.load_state(self.repo)["retries"], {task: 1})
        self.assertEqual(ja.load_state(self.repo)["not_retried"], {})
        self.assertEqual(self.state(task)["state"], "open")
        d.run(once=True)                                         # the one retry, the same agent again
        self.assertEqual(self.starts(), [("deepseek", "claim", task)] * 2)
        st = ja.load_state(self.repo)
        self.assertEqual(st["retries"], {task: 2})
        self.assertIn(task, st["not_retried"])
        self.assertFalse(st["paused"])                           # the job goes on
        self.assertEqual(self.state(task)["state"], "open")      # and the task stays Open
        self.assertEqual(self.state(task)["owner"], "open")
        self.assertEqual(len(self.release_events(task)), 2)
        d.run(once=True)
        d.run(once=True)
        self.assertEqual(len(self.starts()), 2)                  # no third wake
        self.set_names({task: "hub"})
        board.render(self.repo)
        text = ja.board_path(self.repo).read_text()
        paused = text.split("## Paused", 1)[1]
        self.assertIn("Not paused", paused)
        self.assertIn(f"T-hub-{task[1:]}: timed out a second time", paused)
        self.assertIn(f"**T-hub-{task[1:]}**", self.open_section())
        # a new job starts with fresh counters
        self.end_job()
        self.start_job()
        self.assertEqual(ja.load_state(self.repo)["retries"], {})
        self.assertEqual(ja.load_state(self.repo)["not_retried"], {})

    def test_3b_the_retry_may_go_to_another_agent(self):
        self.slow_job()
        task = self.post("claude", "Slow work", "job/slow.txt")
        self.dispatcher().run(once=True)
        self.assertEqual(ja.load_state(self.repo)["retries"], {task: 1})
        self.configure({"gpt": self.fake_cmd("gpt")})            # now only gpt is available
        self.dispatcher().run(once=True)
        self.assertEqual(self.starts(), [("deepseek", "claim", task), ("gpt", "claim", task)])
        s = self.state(task)
        self.assertEqual((s["state"], s["owner"]), ("review", "gpt"))
        self.assertEqual(ja.load_state(self.repo)["retries"], {task: 1})

    def retry_starts(self, task):
        return [x for x in self.log_lines() if x["event"] == "start" and x["task"] == task
                and str(x["revision"]).endswith("#t1")]

    def test_f2_two_eligible_agents_and_a_new_dispatcher_admit_one_retry(self):
        self.slow_job()
        task = self.post("claude", "Slow work", "job/slow.txt")
        self.dispatcher().run(once=True)                         # the first wake times out
        self.assertEqual(ja.load_state(self.repo)["retries"], {task: 1})
        self.assertEqual(ja.load_state(self.repo)["retry_reserved"], {})
        self.configure({"gpt": self.fake_cmd("gpt"), "deepseek": self.fake_cmd("deepseek")})
        self.dispatcher().run(once=True)                         # a new process, two eligible agents
        retry = self.retry_starts(task)
        self.assertEqual(len(retry), 1)                          # it used to be two
        st = ja.load_state(self.repo)
        self.assertEqual(len([w for w in st["wakes"] if str(w["revision"]).endswith("#t1")]), 1)
        self.assertEqual(list(st["retry_reserved"]), [task])
        self.assertEqual(st["retry_reserved"][task]["agent"], retry[0]["agent"])
        self.assertIn(retry[0]["agent"], ("gpt", "deepseek"))    # either agent may take it
        self.assertEqual(self.state(task)["state"], "review")
        self.assertEqual(len(self.starts()), 2)
        for _ in range(2):
            self.dispatcher().run(once=True)                     # restarts stay quiet
        self.assertEqual(len(self.starts()), 2)

    def test_f2_the_reservation_survives_a_restart_after_a_failed_retry(self):
        self.slow_job()
        task = self.post("claude", "Slow work", "job/slow.txt")
        self.dispatcher().run(once=True)
        self.configure({"deepseek": self.fake_cmd("deepseek", "--fail")})
        self.dispatcher().run(once=True)                         # the retry claims, then crashes (rc 1)
        self.assertEqual(len(self.retry_starts(task)), 1)
        self.assertEqual(self.state(task)["state"], "open")      # released again, not a timeout
        self.assertEqual(ja.load_state(self.repo)["retries"], {task: 1})
        self.configure({"gpt": self.fake_cmd("gpt")})            # another agent, another process
        self.dispatcher().run(once=True)
        self.assertEqual(len(self.retry_starts(task)), 1)        # the single retry is spent
        self.assertEqual(self.state(task)["state"], "open")

    def test_f2_concurrent_admission_of_the_retry_in_two_processes_admits_one(self):
        import threading
        self.configure({"gpt": self.fake_cmd("gpt", "--sleep", "1"),
                        "deepseek": self.fake_cmd("deepseek", "--sleep", "1")})
        self.start_job()
        task = self.post("claude", "Racy", "job/racy.txt")
        d1, d2 = self.dispatcher(), self.dispatcher()           # two dispatcher "processes"
        gate = threading.Barrier(2)
        results = {}

        def admit(name, d, agent):
            gate.wait()
            results[name] = d._spawn(ja.Action(agent, "claim", task, "rev#t1"),
                                     self.fake_cmd(agent, "--sleep", "1"), reserve_retry=True)
        threads = [threading.Thread(target=admit, args=("a", d1, "gpt")),
                   threading.Thread(target=admit, args=("b", d2, "deepseek"))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
        self.assertEqual(results, {"a": True, "b": True})        # neither stops the loop
        self.assertEqual(len(d1.children) + len(d2.children), 1)
        self.assertEqual(len(ja.load_state(self.repo)["wakes"]), 1)
        self.assertEqual(len(ja.load_state(self.repo)["retry_reserved"]), 1)
        skips = [x for x in self.log_lines() if x["event"] == "skip"]
        self.assertEqual(len(skips), 1)
        self.assertIn("already reserved", skips[0]["why"])
        for d in (d1, d2):
            d._drain()

    def test_f2_an_owner_continuation_after_the_retry_is_not_blocked(self):
        # the reservation belongs to the fresh claim of an Open task only
        self.slow_job()
        task = self.post("claude", "Slow work", "job/slow.txt", reviewers=["gpt"])
        self.dispatcher().run(once=True)
        self.configure({"deepseek": self.fake_cmd("deepseek"), "gpt": self.fake_cmd("gpt", "--outcome", "changes")})
        d = self.dispatcher()
        d.run(once=True)                                         # the retry delivers
        d.run(once=True)                                         # gpt requests changes
        self.assertEqual(self.state(task)["state"], "changes_requested")
        d.run(once=True)                                         # the owner continues
        self.assertEqual(self.state(task)["state"], "review")
        self.assertEqual(len([x for x in self.log_lines() if x["event"] == "skip"]), 0)

    def test_3b_a_failing_wake_that_is_not_a_timeout_does_not_use_the_retry(self):
        self.configure({"gpt": self.fake_cmd("gpt", "--fail")})
        self.start_job()
        task = self.post("claude", "Crashes", "job/crash.txt")
        self.dispatcher().run(once=True)
        self.assertEqual(self.state(task)["state"], "open")
        self.assertEqual(ja.load_state(self.repo)["retries"], {})

    def test_3b_the_existing_limits_still_apply_to_the_retry(self):
        self.slow_job(max_wakes_per_job=1)
        task = self.post("claude", "Slow work", "job/slow.txt")
        d = self.dispatcher()
        d.run(once=True)
        d.run(once=True)                                         # the retry would be wake no. 2
        st = ja.load_state(self.repo)
        self.assertTrue(st["paused"])
        self.assertIn("wakes per job", st["pause_reason"])
        self.assertEqual(len(st["wakes"]), 1)
        self.assertEqual(self.starts(), [("deepseek", "claim", task)])

    def test_3c_a_named_owner_keeps_the_task_after_a_release(self):
        task = self.post("claude", "Named", "job/n.txt", owner="deepseek", reviewers=["gpt"])
        self.cli("claim.py", "deepseek", task, "--scope", "job/n.txt")
        self.cli("claim.py", "deepseek", task, "--release")
        s = self.state(task)
        self.assertEqual((s["state"], s["owner"], s["base_owner"], s["holder"]),
                         ("open", "deepseek", "deepseek", None))
        self.assertEqual(s["next_actor"], "deepseek")
        for other in ("claude", "gpt"):                             # a reviewer, and a non-reviewer
            r = self.cli("claim.py", other, task, "--scope", "job/n.txt", check=False)
            self.assertEqual(r.returncode, 3, other)
        self.assertFalse(ja.HubBackend().claim(self.repo, "claude", task))
        # the owner can claim again (another scope: an identical event line in the same second
        # would be refused as a duplicate)
        self.cli("claim.py", "deepseek", task, "--scope", "job/n2.txt")
        self.assertEqual(self.state(task)["owner"], "deepseek")
        self.assertEqual(self.state(task)["holder"], "deepseek")

    def test_3c_a_named_owner_task_is_only_offered_to_its_owner_after_a_timeout(self):
        self.configure({"deepseek": self.fake_cmd("deepseek", "--sleep", "30"), "gpt": self.fake_cmd("gpt")})
        self.start_job(max_wall_s_per_wake=1)
        task = self.post("claude", "Named", "job/n.txt", owner="deepseek")
        self.dispatcher().run(once=True)
        self.assertEqual({(a.agent, a.reason) for a in ja.next_actions(self.repo) if a.task == task},
                         {("deepseek", "claim")})
        self.assertEqual(self.state(task)["owner"], "deepseek")
        self.assertEqual({a for a, _, _ in self.starts()}, {"deepseek"})


if __name__ == "__main__":
    unittest.main()
