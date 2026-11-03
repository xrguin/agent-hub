"""T013 unit tests for the task-state tools, on throw-away repositories."""
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
from claim import change as change_claim
from hub_status import render
from new_task import create as create_task

TS = "2026-10-31T15:00:00-04:00"


def ev(tag, event, fields=None, note="", timestamp=TS):
    return events.format_event(tag, event, fields or {}, note, timestamp=timestamp)


class Base(unittest.TestCase):
    def setUp(self):
        # Hygiene: no __pycache__ in the throw-away repository (hooks and CLIs run there).
        patch_env = mock.patch.dict(os.environ, {"PYTHONDONTWRITEBYTECODE": "1"})
        patch_env.start()
        self.addCleanup(patch_env.stop)
        self.temp = tempfile.TemporaryDirectory(prefix="t013-test-")
        self.repo = Path(self.temp.name)
        self.old = os.getcwd()
        self.git("init", "-q")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "tester")
        self.write("docs/agent_hub/DECISIONS.md", "# Decisions\n")
        self.write("docs/agent_hub/QUESTIONS.md", "# Questions\n")
        self.write("docs/agent_hub/logs/claude.md", "# claude log\n")
        self.write("docs/agent_hub/logs/gpt.md", "# gpt log\n")
        self.write("docs/agent_hub/logs/deepseek.md", "# deepseek log\n")
        self.write("pkg/file.txt", "data\n")
        self.write("docs/other.txt", "other\n")
        self.commit("fixture")
        os.chdir(self.repo)

    def tearDown(self):
        os.chdir(self.old)
        self.temp.cleanup()

    def write(self, path, text):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def read(self, path):
        return (self.repo / path).read_text()

    def git(self, *args, check=True):
        result = subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True, text=True)
        if check and result.returncode:
            raise RuntimeError(result.stderr)
        return result

    def commit(self, message="fixture"):
        self.git("add", "-A")
        self.git("commit", "-qm", message)
        return self.head()

    def head(self):
        return self.git("rev-parse", "HEAD").stdout.strip()

    def card(self, task, owner="gpt", title="task", status="open"):
        owner_text = "[%s]" % owner if owner in events.TAGS else owner
        return (f"# {task}: {title}\nOwner: {owner_text} · Reviewer: open · Status: {status}\n"
                f"Created 2026-10-31 by [gpt]\n\n## Status history\n")

    def make_task(self, task="T001", owner="gpt", reviewers=("deepseek",), required="all",
                  kind="documentation", deliverables=("pkg/file.txt",), depends_on=(),
                  initial="open", extra=(), title="task", commit=True):
        for path in deliverables:
            if not (self.repo / path).exists():
                self.write(path, "data\n")
        created = ev("gpt", "created", dict(owner=owner, reviewers=list(reviewers), required=required,
                                            kind=kind, deliverables=list(deliverables),
                                            depends_on=list(depends_on), initial=initial))
        lines = [created] + list(extra)
        self.write(hub_state.status_path(task), "\n".join(lines) + "\n")
        self.write("docs/agent_hub/tasks/" + task + "_" + title + ".md", self.card(task, owner, title, initial))
        if commit:
            self.commit("task " + task)
        return hub_state.revision(self.repo, deliverables)

    def append_status(self, task, line):
        path = hub_state.status_path(task)
        text = self.read(path)
        self.write(path, text + line + "\n")
        self.commit("append " + task)

    def review(self, task, tag, rows, when="2026-01-01"):
        path = f"docs/agent_hub/reviews/{when}_{task}_{tag}.md"
        table = "| ID | Claim | Verdict | Severity | Evidence |\n|---|---|---|---|---|\n"
        table += "".join("| %s | claim | %s | %s | evidence |\n" % row for row in rows)
        self.write(path, "# Review\n\n" + table)
        return path, self.commit("review " + task)

    def shim_copy(self):
        source = Path(__file__).resolve().parents[1] / "hub_status.py"
        target = self.repo / "docs/agent_hub/hub_status.py"
        target.write_text(source.read_text())
        return target

    def run_cli(self, name, *args, check_=True):
        env = os.environ.copy()
        env["PYTHONPATH"] = TOOLS
        result = subprocess.run([sys.executable, TOOLS + "/" + name, *args], cwd=str(self.repo),
                                capture_output=True, text=True, env=env)
        if check_ and result.returncode:
            raise RuntimeError(result.stderr)
        return result


class TestTaskTools(Base):
    def test_new_task_id_allocation_and_ownership(self):
        self.make_task("T001", owner="gpt")
        self.make_task("T002", owner="claude")
        info = create_task("claude", "Third task", owner="claude", reviewers=["deepseek"],
                           deliverables=["pkg/file.txt"])
        self.assertEqual(info["task"], "T003")
        state = hub_state.derive(self.repo, "T003")
        self.assertEqual(state["owner"], "claude")
        self.assertEqual(state["state"], "open")
        self.assertEqual(state["next_actor"], "claude")
        self.assertIn("pkg/file.txt", state["deliverables"])
        self.assertIn(info["commit"], self.git("log", "--format=%H").stdout)
        info2 = create_task("gpt", "Open task", owner="open", reviewers=["deepseek"],
                            deliverables=["pkg/file.txt"])
        self.assertEqual(info2["task"], "T004")
        self.assertEqual(hub_state.derive(self.repo, "T004")["next_actor"], "any")
        # A creator may assign a new task to another owner.
        info3 = create_task("claude", "Assigned", owner="gpt", reviewers=["claude"],
                            deliverables=["pkg/file.txt"])
        self.assertEqual(hub_state.derive(self.repo, info3["task"])["owner"], "gpt")

    def test_claim_release_foreign_and_overlap(self):
        self.make_task("T010", owner="open", reviewers=("deepseek",))
        change_claim("claude", "T010", scope=["pkg/*"])
        state = hub_state.derive(self.repo, "T010")
        self.assertEqual((state["state"], state["owner"], state["next_actor"]), ("claimed", "claude", "claude"))
        self.assertEqual(state["claims"][0]["scope"], ["pkg/*"])
        with self.assertRaises(hubtx.Unauthorized):
            change_claim("gpt", "T010", scope=["docs/*"])
        change_claim("claude", "T010", release=True)
        state = hub_state.derive(self.repo, "T010")
        # T-hub-028 changed this: a release used to keep the claimant as owner ("open", "claude",
        # "claude"); a task created with owner "open" now goes back to "open" for anyone.
        self.assertEqual((state["state"], state["owner"], state["next_actor"]), ("open", "open", "any"))
        change_claim("claude", "T010", scope=["pkg/file.txt"])   # claiming again makes the claimant owner
        self.assertEqual(hub_state.derive(self.repo, "T010")["owner"], "claude")
        # A reviewer may not claim an assigned task.
        self.make_task("T011", owner="gpt", reviewers=("deepseek",))
        with self.assertRaises(hubtx.Unauthorized):
            change_claim("deepseek", "T011", scope=["pkg/*"])
        # Overlapping scopes on different open tasks are refused.
        self.make_task("T012", owner="open", reviewers=())
        self.make_task("T013", owner="open", reviewers=())
        change_claim("claude", "T012", scope=["pkg/*"])
        with self.assertRaises(hubtx.Unauthorized):
            change_claim("gpt", "T013", scope=["pkg/sub/*"])

    def test_stale_claim_is_only_a_warning(self):
        old = ev("gpt", "created", dict(owner="open", reviewers=["claude"], required="all",
                                        kind="documentation", deliverables=["pkg/file.txt"],
                                        depends_on=[], initial="open"), timestamp="2026-10-29T00:00:00-04:00")
        claim = ev("gpt", "claim", dict(scope=["pkg/*"]), timestamp="2026-10-29T00:00:01-04:00")
        self.write(hub_state.status_path("T014"), old + "\n" + claim + "\n")
        self.write("docs/agent_hub/tasks/T014_x.md", self.card("T014", "open"))
        self.commit("old fixture")  # not a [gpt] commit
        state = hub_state.derive(self.repo, "T014")
        self.assertEqual(state["owner"], "gpt")
        self.assertTrue(state["stale_claims"])
        self.assertIn("only the holder", state["stale_claims"][0]["note"])
        with self.assertRaises(hubtx.Unauthorized):
            change_claim("claude", "T014", scope=["docs/*"])

    def test_review_parser_new_and_legacy(self):
        self.write("r.md", """# R
| ID | Claim | Verdict | Severity | Evidence |
|---|---|---|---|---|
| F1 | a | **WRONG** | blocking | e |
| F2 | b | CONFIRMED | blocking | e |
| F3 | c | UNVERIFIED |  | e |
""")
        rows = hub_state.parse_review(self.repo / "r.md")
        self.assertEqual([r["id"] for r in rows], ["F1", "F2", "F3"])
        self.assertEqual(rows[0]["verdict"], "WRONG")
        self.assertEqual(rows[0]["severity"], "blocking")
        self.assertEqual(rows[2]["severity"], "informational")  # legacy fallback
        self.write("legacy.md", """# R
| Claim | Verdict | Evidence (command or file) |
|---|---|---|
| a | WRONG | e |
| b | CONFIRMED | e |
""")
        rows = hub_state.parse_review(self.repo / "legacy.md")
        self.assertEqual([r["id"] for r in rows], ["F1", "F2"])
        self.assertEqual(rows[0]["severity"], "blocking")
        self.assertEqual(rows[1]["severity"], "informational")

    def test_findings_responses_and_dispositions(self):
        rev = self.make_task("T020", owner="gpt", reviewers=("claude",))
        path, _ = self.review("T020", "claude", [("F1", "WRONG", "blocking"), ("F2", "UNVERIFIED", "blocking"),
                                                 ("F3", "UNVERIFIABLE", "blocking"), ("F4", "CONFIRMED", "blocking"),
                                                 ("F5", "WRONG", "informational")])
        self.append_status("T020", ev("claude", "review", dict(file=path, rev=rev)))
        state = hub_state.derive(self.repo, "T020")
        self.assertEqual([f["finding"] for f in state["open_findings"]],
                         [path + "#F1", path + "#F2", path + "#F3"])
        self.append_status("T020", ev("gpt", "response", dict(finding=path + "#F1")))
        state = hub_state.derive(self.repo, "T020")
        self.assertTrue(next(f for f in state["open_findings"] if f["id"] == "F1")["responded"])
        self.append_status("T020", ev("claude", "disposition", dict(finding=path + "#F1", result="resolved")))
        self.append_status("T020", ev("claude", "disposition", dict(finding=path + "#F2", result="resolved")))
        state = hub_state.derive(self.repo, "T020")
        self.assertEqual([f["finding"] for f in state["open_findings"]], [path + "#F3"])
        self.write("docs/agent_hub/DECISIONS.md", '# Decisions\n\n### D001 \u00b7 2026-10-31 \u00b7 recorded by [gpt]\n'
                   '- quote: "accept the limitation"\n- scope: test\n')
        self.commit("decision")
        self.append_status("T020", ev("gpt", "user", dict(decision="D001", action="accept_limitation",
                                                          finding=path + "#F3")))
        state = hub_state.derive(self.repo, "T020")
        self.assertEqual(state["open_findings"], [])

    # ---- T054 (user decision 2026-11-03): a reviewer's accept closes its own findings
    def submitted_task(self, task, reviewers=("claude",)):
        """A task with a submit at the current revision; returns the revision."""
        rev = self.make_task(task, owner="gpt", reviewers=reviewers)
        self.append_status(task, ev("gpt", "submit", dict(rev=rev)))
        return rev

    def closed_by(self, task):
        return {f["finding"]: f["closed_by"] for f in hub_state.derive(self.repo, task)["all_findings"]}

    def decision(self, ident="D001"):
        self.write("docs/agent_hub/DECISIONS.md", "# Decisions\n\n### %s \u00b7 2026-11-03 \u00b7 recorded by [gpt]\n"
                   '- quote: "decision"\n- scope: test\n' % ident)
        self.commit("decision")

    def test_t054_legacy_table_accept_closes_the_finding(self):
        # T053 reproduction: README 5 table without ID and Severity; the parser names the WRONG row F3.
        rev = self.submitted_task("T070")
        path = "docs/agent_hub/reviews/2026-11-03_T070_claude.md"
        self.write(path, "# R\n\n| Claim | Verdict | Evidence |\n|---|---|---|\n"
                         "| a | CONFIRMED | e |\n| b | CONFIRMED | e |\n| c | WRONG | e |\n")
        self.commit("legacy review")
        self.append_status("T070", ev("claude", "review", dict(file=path, rev=rev)))
        self.append_status("T070", ev("claude", "request_changes", dict(file=path)))
        state = hub_state.derive(self.repo, "T070")
        self.assertEqual([f["id"] for f in state["all_findings"]], ["F3"])
        self.assertEqual(self.closed_by("T070"), {path + "#F3": None})
        self.assertEqual(len(state["open_findings"]), 1)
        self.append_status("T070", ev("gpt", "submit", dict(rev=rev)))
        self.append_status("T070", ev("claude", "review", dict(file=path, rev=rev)))
        self.append_status("T070", ev("claude", "accept", dict(rev=rev)))
        state = hub_state.derive(self.repo, "T070")
        self.assertEqual(self.closed_by("T070"), {path + "#F3": "accept"})
        self.assertTrue(state["all_findings"][0]["closed"])
        self.assertEqual(state["open_findings"], [])
        self.assertEqual(state["state"], "accepted")
        self.assertEqual(state["next_actor"], "none")

    def test_t054_later_request_changes_reopens(self):
        rev = self.submitted_task("T071")
        path, _ = self.review("T071", "claude", [("F1", "WRONG", "blocking"), ("F2", "UNVERIFIED", "blocking")])
        self.append_status("T071", ev("claude", "review", dict(file=path, rev=rev)))
        self.append_status("T071", ev("claude", "accept", dict(rev=rev)))
        self.assertEqual(self.closed_by("T071"), {path + "#F1": "accept", path + "#F2": "accept"})
        self.append_status("T071", ev("claude", "request_changes", dict(file=path)))
        self.assertEqual(self.closed_by("T071"), {path + "#F1": None, path + "#F2": None})
        self.assertEqual(len(hub_state.derive(self.repo, "T071")["open_findings"]), 2)

    def test_t054_unverifiable_needs_the_user_not_the_accept(self):
        rev = self.submitted_task("T072")
        path, _ = self.review("T072", "claude", [("F1", "UNVERIFIABLE", "blocking")])
        self.append_status("T072", ev("claude", "review", dict(file=path, rev=rev)))
        self.append_status("T072", ev("claude", "accept", dict(rev=rev)))
        state = hub_state.derive(self.repo, "T072")
        self.assertEqual(self.closed_by("T072"), {path + "#F1": None})
        self.assertEqual(state["next_actor"], "gpt")
        self.decision()
        self.append_status("T072", ev("gpt", "user", dict(decision="D001", action="accept_limitation",
                                                          finding=path + "#F1")))
        state = hub_state.derive(self.repo, "T072")
        self.assertEqual(self.closed_by("T072"), {path + "#F1": "limitation"})
        self.assertEqual(state["state"], "accepted")

    def test_t054_another_reviewers_accept_does_not_close_my_finding(self):
        rev = self.submitted_task("T073", reviewers=("claude", "deepseek"))
        path_c, _ = self.review("T073", "claude", [("F1", "WRONG", "blocking")])
        path_d, _ = self.review("T073", "deepseek", [("F1", "WRONG", "blocking")])
        self.append_status("T073", ev("claude", "review", dict(file=path_c, rev=rev)))
        self.append_status("T073", ev("claude", "accept", dict(rev=rev)))
        self.append_status("T073", ev("deepseek", "review", dict(file=path_d, rev=rev)))
        self.append_status("T073", ev("deepseek", "request_changes", dict(file=path_d)))
        self.assertEqual(self.closed_by("T073"), {path_c + "#F1": "accept", path_d + "#F1": None})
        state = hub_state.derive(self.repo, "T073")
        self.assertEqual([f["finding"] for f in state["open_findings"]], [path_d + "#F1"])

    def test_t054_upheld_after_the_accept_reopens_until_a_later_accept(self):
        rev = self.submitted_task("T074")
        path, _ = self.review("T074", "claude", [("F1", "WRONG", "blocking")])
        self.append_status("T074", ev("claude", "review", dict(file=path, rev=rev)))
        self.append_status("T074", ev("claude", "accept", dict(rev=rev)))
        self.assertEqual(self.closed_by("T074"), {path + "#F1": "accept"})
        self.append_status("T074", ev("claude", "disposition", dict(finding=path + "#F1", result="upheld")))
        self.assertEqual(self.closed_by("T074"), {path + "#F1": None})
        self.append_status("T074", ev("claude", "accept", dict(rev=rev), timestamp="2026-10-31T16:00:00-04:00"))
        self.assertEqual(self.closed_by("T074"), {path + "#F1": "accept"})

    def test_t054_user_open_after_the_accept_reopens_until_a_later_accept(self):
        rev = self.submitted_task("T075")
        path, _ = self.review("T075", "claude", [("F1", "WRONG", "blocking")])
        self.append_status("T075", ev("claude", "review", dict(file=path, rev=rev)))
        self.append_status("T075", ev("claude", "accept", dict(rev=rev)))
        self.assertEqual(self.closed_by("T075"), {path + "#F1": "accept"})
        self.decision()
        self.append_status("T075", ev("gpt", "user", dict(decision="D001", action="open", task="T075")))
        self.assertEqual(self.closed_by("T075"), {path + "#F1": None})
        self.append_status("T075", ev("claude", "accept", dict(rev=rev), timestamp="2026-10-31T16:00:00-04:00"))
        self.assertEqual(self.closed_by("T075"), {path + "#F1": "accept"})

    def test_t054_explicit_disposition_stays_closed_after_request_changes(self):
        rev = self.submitted_task("T076")
        path, _ = self.review("T076", "claude", [("F1", "WRONG", "blocking")])
        self.append_status("T076", ev("claude", "review", dict(file=path, rev=rev)))
        self.append_status("T076", ev("claude", "disposition", dict(finding=path + "#F1", result="resolved")))
        self.append_status("T076", ev("claude", "request_changes", dict(file=path)))
        self.assertEqual(self.closed_by("T076"), {path + "#F1": "disposition"})

    # ---- T055 (user decision 2026-11-03): a later request_changes withdraws the reviewer's accept
    def test_t055_request_changes_after_an_accept_withdraws_it(self):
        # deepseek's T054 note: accept revision X, then request_changes on the same X with no blocking row.
        rev = self.submitted_task("T077")
        path, _ = self.review("T077", "claude", [("F1", "CONFIRMED", "informational")])
        self.append_status("T077", ev("claude", "review", dict(file=path, rev=rev)))
        self.append_status("T077", ev("claude", "accept", dict(rev=rev)))
        self.assertTrue(hub_state.derive(self.repo, "T077")["accepted"])
        self.append_status("T077", ev("claude", "request_changes", dict(file=path)))
        state = hub_state.derive(self.repo, "T077")
        self.assertFalse(state["accepted"])
        self.assertEqual(state["state"], "changes_requested")
        self.assertEqual(state["next_actor"], "gpt")
        self.assertNotIn("claude", state["latest_accepts"])
        # The owner submits the same bytes again: the reviewer is pending, nothing is accepted.
        self.append_status("T077", ev("gpt", "submit", dict(rev=rev), timestamp="2026-10-31T16:00:00-04:00"))
        state = hub_state.derive(self.repo, "T077")
        self.assertEqual(state["state"], "review")
        self.assertFalse(state["accepted"])
        self.assertEqual(state["next_actor"], "claude")
        # A new accept from the reviewer accepts the task again.
        self.append_status("T077", ev("claude", "accept", dict(rev=rev), timestamp="2026-10-31T16:01:00-04:00"))
        self.assertTrue(hub_state.derive(self.repo, "T077")["accepted"])

    def test_t055_withdrawn_accept_leaves_the_other_reviewers_accept(self):
        # required=all: one reviewer's withdrawal blocks; required=any: the other accept still counts.
        for task, required, accepted in (("T078", "all", False), ("T079", "any", True)):
            rev = self.make_task(task, owner="gpt", reviewers=("claude", "deepseek"), required=required)
            self.append_status(task, ev("gpt", "submit", dict(rev=rev)))
            path, _ = self.review(task, "deepseek", [("F1", "CONFIRMED", "informational")])
            for tag in ("claude", "deepseek"):
                self.append_status(task, ev(tag, "accept", dict(rev=rev)))
            self.assertTrue(hub_state.derive(self.repo, task)["accepted"], task)
            self.append_status(task, ev("deepseek", "request_changes", dict(file=path)))
            state = hub_state.derive(self.repo, task)
            self.assertEqual(state["accepted"], accepted, task)
            self.assertEqual(state["latest_accepts"], {"claude": rev}, task)

    def test_revision_quorum_and_changed_deliverables(self):
        deliverable = "pkg/file.txt"
        self.make_task("T030", owner="gpt", reviewers=("claude", "deepseek"))
        rev = hub_state.revision(self.repo, ("pkg/file.txt",))
        self.append_status("T030", ev("gpt", "submit", dict(rev=rev)))
        self.append_status("T030", ev("claude", "accept", dict(rev=rev)))
        self.append_status("T030", ev("deepseek", "accept", dict(rev=rev)))
        state = hub_state.derive(self.repo, "T030")
        self.assertTrue(state["accepted"])
        self.assertEqual(state["state"], "accepted")
        self.assertEqual(state["next_actor"], "none")
        self.write(deliverable, "changed\n")
        self.commit("change deliverable")
        state = hub_state.derive(self.repo, "T030")
        self.assertFalse(state["accepted"])
        self.assertEqual(state["state"], "review (deliverables changed)")
        self.assertEqual(state["accepted_revision"], rev)
        self.assertEqual(state["next_actor"], "gpt")  # T048: changed since the submit, so the owner submits again
        # required=any
        self.make_task("T031", owner="gpt", reviewers=("claude", "deepseek"), required="any")
        rev = hub_state.revision(self.repo, ("pkg/file.txt",))
        self.append_status("T031", ev("gpt", "submit", dict(rev=rev)))
        self.append_status("T031", ev("claude", "accept", dict(rev=rev)))
        self.assertTrue(hub_state.derive(self.repo, "T031")["accepted"])

    def test_first_past_cursor_and_missing_log(self):
        self.write("docs/agent_hub/STATE.md", "state\n")
        snap = self.commit("A")
        self.write("docs/agent_hub/STATE.md", "state B\n")
        self.commit("B")
        self.write("docs/agent_hub/logs/claude.md", "# claude log\n\n## 2026-10-31 15:00 [claude]\nseen_through=" + snap + "\n")
        self.commit("C")
        new = hub_state.new_since(self.repo, "claude")
        self.assertFalse(new["missing"])
        self.assertEqual(new["seen_through"], snap)
        self.assertEqual(len(new["commits"]), 2)   # B and the log commit C
        missing = hub_state.new_since(self.repo, "gpt")  # no log entry with a cursor
        self.assertTrue(missing["missing"])
        self.assertEqual(len(missing["commits"]), len(self.git("log", "--format=%h", "--", "docs/agent_hub").stdout.split()))

    def test_queue_and_answered_questions(self):
        self.make_task("T050", owner="gpt", reviewers=("claude",))
        rev = hub_state.revision(self.repo, ("pkg/file.txt",))
        self.append_status("T050", ev("gpt", "submit", dict(rev=rev)))
        queue = hub_state.queue(self.repo, "claude")
        self.assertEqual([item["task"] for item in queue["tasks"]], ["T050"])
        self.write("docs/agent_hub/QUESTIONS.md",
                   "# Questions\n\n### Q001 \u00b7 2026-10-31 \u00b7 asked by [claude]\n"
                   "- question: q?\n- options: a/b\n- blocks: none\n- Q001 answered by D001 (2026-10-31)\n")
        self.commit("question")
        queue = hub_state.queue(self.repo, "claude")
        self.assertEqual(queue["answered_questions"][0]["qid"], "Q001")

    def test_dependencies_and_user_actions(self):
        self.make_task("T040", owner="gpt", reviewers=("claude",))
        rev = hub_state.revision(self.repo, ("pkg/file.txt",))
        self.append_status("T040", ev("gpt", "submit", dict(rev=rev)))
        self.append_status("T040", ev("claude", "accept", dict(rev=rev)))
        self.assertTrue(hub_state.derive(self.repo, "T040")["accepted"])
        self.make_task("T041", owner="gpt", reviewers=("claude",), initial="awaiting_user",
                       depends_on=("T040",))
        state = hub_state.derive(self.repo, "T041")
        self.assertEqual(state["state"], "awaiting_user")
        self.assertEqual(state["blocked_by"], [])
        self.assertEqual(state["next_actor"], "user")
        self.make_task("T042", owner="gpt", reviewers=("claude",), depends_on=("T040", "D001"))
        self.assertFalse(hub_state.derive(self.repo, "T042")["accepted"])
        state = hub_state.derive(self.repo, "T042")
        self.assertEqual(state["blocked_by"], ["D001"])
        self.write("docs/agent_hub/DECISIONS.md", '# Decisions\n\n### D001 \u00b7 2026-10-31 \u00b7 recorded by [gpt]\n'
                   '- quote: "yes"\n- scope: test\n')
        self.commit("decision")
        self.assertEqual(hub_state.derive(self.repo, "T042")["blocked_by"], [])
        self.make_task("T043", owner="gpt", reviewers=("claude",))
        self.append_status("T043", ev("gpt", "user", dict(decision="D001", action="cancel", task="T043")))
        self.assertEqual(hub_state.derive(self.repo, "T043")["state"], "cancelled")
        self.assertEqual(hub_state.derive(self.repo, "T043")["next_actor"], "none")

    def test_legacy_card_status_view_duplicates_and_links(self):
        self.write("docs/agent_hub/tasks/T005_old.md",
                   "# T005: old\nOwner: open · Reviewer: open · Status: review\n")
        self.write("docs/agent_hub/reviews/2026-01-01_T005_gpt.md", "# gpt\n")
        self.write("docs/agent_hub/reviews/2026-01-02_T005_deepseek.md", "# deepseek\n")
        self.write("docs/agent_hub/tasks/T006_a.md", "# T006\nOwner: [gpt]\n")
        self.write("docs/agent_hub/tasks/T006_b.md", "# T006\nOwner: [gpt]\n")
        self.write("docs/agent_hub/tasks/T007_bad.md", "# T007\n[missing](no_such_file.md)\n")
        self.commit("legacy")
        state = hub_state.derive(self.repo, "T005")
        self.assertEqual(state["state"], "review")
        self.assertEqual(state["owner"], "open")
        self.assertNotIn("gpt", state["next_actor"])   # both review files already exist
        self.assertEqual(state["next_actor"], "user")
        self.assertIn("docs/agent_hub/reviews/2026-01-01_T005_gpt.md", state["review_files_all"])
        self.assertIn("T006", hub_state.duplicate_ids(self.repo))
        self.assertTrue(any(item["target"] == "no_such_file.md" for item in hub_state.broken_links(self.repo)))
        view = hub_state.status_view(self.repo, "claude")
        self.assertIn("T005", view["tasks"])
        text = render(self.repo, "claude")
        self.assertIn("seen_through=", text)
        self.assertIn("== Queue [claude]", text)

    def test_status_cli_and_shim(self):
        self.make_task("T050", owner="gpt", reviewers=("claude",))
        p = self.run_cli("hub_status.py", "--tag", "claude")
        self.assertIn("seen_through=", p.stdout)
        self.assertIn("T050", p.stdout)
        self.assertIn("== Queue [claude]", p.stdout)
        env = os.environ.copy()
        shim = self.shim_copy()
        # v1 governs until hub_config.json sets v2_in_force.
        result = subprocess.run([sys.executable, str(shim)], cwd=str(self.repo),
                                capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("== Reviews with WRONG verdicts", result.stdout)
        shutil.copytree(Path(TOOLS), self.repo / "docs/agent_hub/tools",
                        ignore=shutil.ignore_patterns("__pycache__"))
        self.write("docs/agent_hub/hub_config.json", '{"v2_in_force": true}\n')
        self.commit("enable v2")
        result = subprocess.run([sys.executable, str(shim), "--tag", "claude"], cwd=str(self.repo),
                                capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("seen_through=", result.stdout)

    def test_legacy_v1_view_and_v2_fallback(self):
        self.write("docs/agent_hub/tasks/T005_old.md",
                   "# T005: old\nOwner: open · Reviewer: open · Status: review\n")
        self.write("docs/agent_hub/reviews/2026-01-01_T005_gpt.md",
                   "# Review\n\n| Claim | Verdict | Evidence |\n|---|---|---|\n| c | WRONG | e |\n")
        self.commit("legacy")
        shim = self.shim_copy()
        result = subprocess.run([sys.executable, str(shim)], cwd=str(self.repo),
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("reviews: 2026-01-01_T005_gpt.md", result.stdout)
        self.assertIn("2026-01-01_T005_gpt.md: 1 WRONG", result.stdout)
        state = hub_state.derive(self.repo, "T005")
        self.assertEqual(state["state"], "review")
        self.assertNotIn("gpt", state["next_actor"])   # the review file already exists
        self.assertIn("docs/agent_hub/reviews/2026-01-01_T005_gpt.md", state["review_files_all"])

    def test_forged_disposition_by_non_author_is_ignored(self):
        self.make_task("T060", owner="claude", reviewers=("gpt",))
        rev = hub_state.revision(self.repo, ("pkg/file.txt",))
        path, _ = self.review("T060", "gpt", [("F1", "WRONG", "blocking")])
        self.append_status("T060", ev("gpt", "review", dict(file=path, rev=rev)))
        self.append_status("T060", ev("gpt", "accept", dict(rev=rev)))
        # T054: the accept alone closes gpt's own WRONG row; its upheld disposition after the accept reopens it.
        self.append_status("T060", ev("gpt", "disposition", dict(finding=path + "#F1", result="upheld")))
        self.assertFalse(hub_state.derive(self.repo, "T060")["accepted"])
        with open(self.repo / hub_state.status_path("T060"), "a") as fh:
            fh.write(ev("claude", "disposition", dict(finding=path + "#F1", result="resolved")) + "\n")
        self.commit("raw forged disposition")
        state = hub_state.derive(self.repo, "T060")
        self.assertIn("F1", {f["id"] for f in state["open_findings"]})
        self.assertFalse(state["accepted"])
        self.append_status("T060", ev("gpt", "disposition", dict(finding=path + "#F1", result="resolved")))
        self.assertTrue(hub_state.derive(self.repo, "T060")["accepted"])

    def test_blocked_next_actor_and_open_finding_next_actor(self):
        self.make_task("T061", owner="claude", reviewers=("gpt", "deepseek"))
        rev = hub_state.revision(self.repo, ("pkg/file.txt",))
        self.append_status("T061", ev("claude", "submit", dict(rev=rev)))
        self.make_task("T062", owner="gpt", reviewers=("claude",), depends_on=("T061",))
        base = hub_state.derive(self.repo, "T061")
        child = hub_state.derive(self.repo, "T062")
        self.assertEqual(child["next_actor"], base["next_actor"])
        self.assertEqual(child["next_via"], ["T061"])
        # T054: a WRONG row is closed by its author's accept, so the task is accepted and nobody acts.
        self.make_task("T063", owner="gpt", reviewers=("claude",))
        rev = hub_state.revision(self.repo, ("pkg/file.txt",))
        path, _ = self.review("T063", "claude", [("F1", "WRONG", "blocking")])
        self.append_status("T063", ev("gpt", "submit", dict(rev=rev)))
        self.append_status("T063", ev("claude", "review", dict(file=path, rev=rev)))
        self.append_status("T063", ev("claude", "accept", dict(rev=rev)))
        state = hub_state.derive(self.repo, "T063")
        self.assertEqual(state["state"], "accepted")
        self.assertEqual(state["next_actor"], "none")
        # All required accepts in but a blocking finding the accept does not close (UNVERIFIABLE):
        # the owner must respond.
        self.make_task("T066", owner="gpt", reviewers=("claude",))
        rev = hub_state.revision(self.repo, ("pkg/file.txt",))
        path, _ = self.review("T066", "claude", [("F1", "UNVERIFIABLE", "blocking")])
        self.append_status("T066", ev("gpt", "submit", dict(rev=rev)))
        self.append_status("T066", ev("claude", "review", dict(file=path, rev=rev)))
        self.append_status("T066", ev("claude", "accept", dict(rev=rev)))
        state = hub_state.derive(self.repo, "T066")
        self.assertEqual(state["next_actor"], "gpt")
        self.assertIn("finding", str(hub_state.queue(self.repo, "gpt")))
        # Same with an upheld disposition after the accept on a WRONG row.
        self.make_task("T067", owner="gpt", reviewers=("claude",))
        rev = hub_state.revision(self.repo, ("pkg/file.txt",))
        path, _ = self.review("T067", "claude", [("F1", "WRONG", "blocking")])
        self.append_status("T067", ev("gpt", "submit", dict(rev=rev)))
        self.append_status("T067", ev("claude", "review", dict(file=path, rev=rev)))
        self.append_status("T067", ev("claude", "accept", dict(rev=rev)))
        self.append_status("T067", ev("claude", "disposition", dict(finding=path + "#F1", result="upheld")))
        state = hub_state.derive(self.repo, "T067")
        self.assertEqual(state["next_actor"], "gpt")
        self.assertIn("finding", str(hub_state.queue(self.repo, "gpt")))

    def test_unannounced_review_file_warns_and_uncommitted_edit_keeps_revision(self):
        self.make_task("T064", owner="gpt", reviewers=("claude",))
        self.review("T064", "claude", [("F1", "CONFIRMED", "informational")])
        state = hub_state.derive(self.repo, "T064")
        self.assertTrue(any("unannounced review file" in w for w in state["warnings"]))
        # R6: an uncommitted deliverable edit does not change the HEAD revision/acceptance.
        self.make_task("T065", owner="gpt", reviewers=("claude",))
        rev = hub_state.revision(self.repo, ("pkg/file.txt",))
        self.append_status("T065", ev("gpt", "submit", dict(rev=rev)))
        self.append_status("T065", ev("claude", "accept", dict(rev=rev)))
        self.assertTrue(hub_state.derive(self.repo, "T065")["accepted"])
        self.write("pkg/file.txt", "uncommitted edit\n")
        state = hub_state.derive(self.repo, "T065")
        self.assertTrue(state["accepted"])
        self.assertEqual(state["revision"], rev)
        self.commit("commit the edit")
        state = hub_state.derive(self.repo, "T065")
        self.assertFalse(state["accepted"])


if __name__ == "__main__":
    unittest.main()
