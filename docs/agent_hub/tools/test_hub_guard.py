"""T042 tests: `task_branch` (tasks are created only on one branch) and the scoped commit guard.

Everything runs on throw-away git repositories (tools copied in, the real installed commit-msg
hook). Nothing here touches or installs a hook in the real repository.
"""
from __future__ import annotations

import json
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

import hub_events as ev
import hubtx
from install_hooks import install

HUB = "docs/agent_hub/"
STAMP = "2026-11-02T09:00:00-05:00"
LATER = "2026-11-02T10:00:00-05:00"
SCOPED = {"task_branch": "main", "commit_guard": "scoped"}


def created(creator, owner, reviewers=("deepseek",)):
    return ev.format_event(creator, "created", dict(owner=owner, reviewers=list(reviewers), required="all",
                           kind="code", deliverables=["pkg/free.py"], depends_on=[], initial="open"),
                           timestamp=STAMP)


def claim(tag, scope):
    return ev.format_event(tag, "claim", dict(scope=scope), timestamp=STAMP)


class Base(unittest.TestCase):
    config = SCOPED  # committed hub_config.json; None = no file

    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"PYTHONDONTWRITEBYTECODE": "1", "GIT_CONFIG_NOSYSTEM": "1",
                                               "GIT_CONFIG_GLOBAL": os.devnull})
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE", "HUB_TX_APPENDS", "HUB_TX_NEW"):
            os.environ.pop(key, None)
        self.temp = tempfile.TemporaryDirectory(prefix="t042-test-")
        self.base = Path(self.temp.name).resolve()
        self.repo = self.base / "main-checkout"
        self.repo.mkdir()
        self.old = os.getcwd()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "tester")
        self.git("config", "commit.gpgsign", "false")
        self.write(HUB+"DECISIONS.md", "# Decisions\n- first line\n- second line\n")
        self.write(HUB+"QUESTIONS.md", "# Questions\n")
        for tag in ("claude", "gpt", "deepseek"):
            self.write(HUB+f"logs/{tag}.md", f"# {tag} log\n")
        self.write(HUB+"reviews/2026-11-01_T001_gpt.md", "review\n")
        # T001: posted by claude, owned by gpt, active claim pkg/gpt/**.
        self.write(HUB+"tasks/T001_work.md", "# T001\nOwner: [gpt]\n")
        self.write(HUB+"tasks/T001.status.md", created("claude", "gpt")+"\n"+claim("gpt", ["pkg/gpt/**"])+"\n")
        # T003: owned by claude, active claim pkg/claude/**.
        self.write(HUB+"tasks/T003_other.md", "# T003\nOwner: [claude]\n")
        self.write(HUB+"tasks/T003.status.md", created("claude", "claude")+"\n"+claim("claude", ["pkg/claude/**"])+"\n")
        self.write("pkg/gpt/a.py", "a\n")
        self.write("pkg/claude/b.py", "b\n")
        self.write("pkg/free.py", "free\n")
        if self.config is not None:
            self.write(HUB+"hub_config.json", json.dumps(self.config)+"\n")
        shutil.copytree(Path(TOOLS), self.repo/(HUB+"tools"), ignore=shutil.ignore_patterns("__pycache__"))
        self.git("add", "-A")
        self.git("commit", "-qm", "fixture")
        os.chdir(self.repo)
        self.addCleanup(os.chdir, self.old)
        self.hook = install("gpt", "T012")
        self.addCleanup(self.temp.cleanup)

    def write(self, path, text, root=None):
        target = (root or self.repo)/path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def read(self, path, root=None):
        return ((root or self.repo)/path).read_text()

    def append(self, path, text, root=None):
        self.write(path, self.read(path, root)+text, root)

    def git(self, *args, root=None, check=True):
        result = subprocess.run(["git", "-C", str(root or self.repo), *args], capture_output=True, text=True)
        if check and result.returncode:
            raise RuntimeError(result.stderr)
        return result

    def head(self, root=None):
        return self.git("rev-parse", "HEAD", root=root).stdout.strip()

    def try_commit(self, files, message, root=None):
        """Stage `files` and commit; returns the git result (the hook runs for real)."""
        self.git("add", "--", *files, root=root)
        return self.git("commit", "-q", "-m", message, root=root, check=False)

    def assertAccepted(self, files, message, root=None):
        before = self.head(root)
        result = self.try_commit(files, message, root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(self.head(root), before)

    def assertRefused(self, files, message, text=None, root=None):
        before = self.head(root)
        result = self.try_commit(files, message, root)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.head(root), before)
        if text:
            self.assertIn(text, result.stderr + result.stdout)
        self.git("reset", "-q", "--", *files, root=root)  # unstage for later steps

    def new_task(self, tag="claude", root=None, check_=True):
        env = os.environ.copy()
        env["PYTHONPATH"] = TOOLS
        result = subprocess.run([sys.executable, TOOLS+"/new_task.py", tag, "A new task", "--owner", tag,
                                 "--reviewers", "deepseek" if tag != "deepseek" else "gpt",
                                 "--deliverables", "pkg/free.py"],
                                cwd=str(root or self.repo), capture_output=True, text=True, env=env)
        if check_ and result.returncode:
            raise RuntimeError(result.stdout + result.stderr)
        return result

    def status_files(self, root=None):
        return sorted(p.name for p in ((root or self.repo)/(HUB+"tasks")).glob("*.status.md"))


class TaskBranch(Base):
    def test_on_main_creates(self):
        result = self.new_task()
        self.assertEqual(result.stdout.split()[0], "T004")
        self.assertIn("T004.status.md", self.status_files())

    def test_other_branch_refused_without_writes(self):
        self.git("checkout", "-q", "-b", "feature")
        head, status = self.head(), self.git("status", "--porcelain").stdout
        result = self.new_task(check_=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("main", result.stderr)
        self.assertEqual(self.head(), head)
        self.assertEqual(self.git("status", "--porcelain").stdout, status)
        self.assertNotIn("T004.status.md", self.status_files())

    def test_detached_head_refused(self):
        self.git("checkout", "-q", "--detach")
        head = self.head()
        result = self.new_task(check_=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("detached", result.stderr)
        self.assertEqual(self.head(), head)
        self.assertNotIn("T004.status.md", self.status_files())

    def test_linked_worktree_on_other_branch_refused_and_names_main_checkout(self):
        wt = self.base/"wt"
        self.git("worktree", "add", "-q", "-b", "claude/side", str(wt))
        head = self.head(wt)
        result = self.new_task(root=wt, check_=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(str(self.repo), result.stderr)
        self.assertEqual(self.head(wt), head)
        self.assertNotIn("T004.status.md", self.status_files(wt))
        self.assertNotIn("T004.status.md", self.status_files())
        # the main checkout still works
        self.assertEqual(self.new_task().returncode, 0)

    def test_branch_from_before_the_key_is_refused(self):
        # T046 F1: a worktree whose own committed config has no task_branch key still follows main.
        old = self.base/"old"
        self.git("worktree", "add", "-q", "-b", "old", str(old))
        self.write(HUB+"hub_config.json", json.dumps({"commit_guard": "scoped"})+"\n", old)
        self.git("add", HUB+"hub_config.json", root=old)
        self.git("commit", "-q", "--no-verify", "-m", "a branch from before task_branch", root=old)
        head, status = self.head(old), self.git("status", "--porcelain", root=old).stdout
        result = self.new_task(root=old, check_=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("'main'", result.stderr)
        self.assertEqual(self.head(old), head)
        self.assertEqual(self.git("status", "--porcelain", root=old).stdout, status)
        self.assertNotIn("T004.status.md", self.status_files(old))
        # main allocates normally, and only once
        self.assertEqual(self.new_task().stdout.split()[0], "T004")


class NoTaskBranchKey(Base):
    config = {"commit_guard": "scoped"}

    def test_branch_allowed_without_key(self):
        self.git("checkout", "-q", "-b", "feature")
        result = self.new_task()
        self.assertEqual(result.stdout.split()[0], "T004")


class Validation(Base):
    def test_validate_config(self):
        for good in ({"task_branch": "main"}, {"commit_guard": "full"}, {"commit_guard": "scoped"}, {}):
            hubtx._validate_config(good)
        for bad in ({"task_branch": ""}, {"task_branch": "  "}, {"task_branch": 1}, {"task_branch": None},
                    {"commit_guard": "other"}, {"commit_guard": True}, {"commit_guard": ""}):
            with self.assertRaises(hubtx.BadInput, msg=str(bad)):
                hubtx._validate_config(bad)

    def test_defaults_and_scoped_tag(self):
        self.assertEqual(hubtx.DEFAULTS["commit_guard"], "full")
        self.assertIsNone(hubtx.DEFAULTS["task_branch"])
        self.assertEqual(hubtx.scoped_tag("[gpt] T001/T002: x\n"), "gpt")
        self.assertEqual(hubtx.scoped_tag("# comment\n[claude] DECISIONS: x\n"), "claude")
        self.assertIsNone(hubtx.scoped_tag("claude: x\n"))
        self.assertIsNone(hubtx.scoped_tag(" [claude] x\n"))
        self.assertIsNone(hubtx.scoped_tag(""))


class ScopedGuard(Base):
    def test_user_commit_of_code_is_allowed(self):
        self.append("pkg/free.py", "more\n")
        self.assertAccepted(["pkg/free.py"], "tweak the code")

    def test_user_commit_inside_a_claim_is_allowed(self):
        self.append("pkg/gpt/a.py", "more\n")
        self.assertAccepted(["pkg/gpt/a.py"], "tweak gpt's file")

    def test_agent_commit_in_foreign_claim_refused(self):
        self.append("pkg/gpt/a.py", "more\n")
        self.assertRefused(["pkg/gpt/a.py"], "[claude] T003: edit gpt's file", "claim")

    def test_agent_commit_unclaimed_and_own_claim_allowed(self):
        self.append("pkg/free.py", "more\n")
        self.assertAccepted(["pkg/free.py"], "[claude] T003: unclaimed")
        self.append("pkg/claude/b.py", "more\n")
        self.assertAccepted(["pkg/claude/b.py"], "[claude] T003: own claim")

    def test_multi_task_subject_is_an_agent_commit(self):
        self.append("pkg/gpt/a.py", "more\n")
        self.assertRefused(["pkg/gpt/a.py"], "[claude] T001/T002: x")  # agent rules apply
        self.append("pkg/free.py", "more\n")
        self.assertAccepted(["pkg/free.py"], "[claude] T001/T002: x")
        self.append("pkg/free.py", "again\n")
        self.assertAccepted(["pkg/free.py"], "[deepseek] DECISIONS: no task id needed")

    def test_rewriting_decisions_refused_for_everyone(self):
        path = HUB+"DECISIONS.md"
        original = self.read(path)
        for message in ("rewrite a line", "[claude] DECISIONS: rewrite a line"):
            self.write(path, original.replace("first line", "FIRST line"))
            self.assertRefused([path], message, "Append-only")
        self.write(path, "# Decisions\n- first line\n")  # deleting a line
        self.assertRefused([path], "shorten")
        self.write(path, original[:-1])  # dropping the final newline
        self.assertRefused([path], "[gpt] DECISIONS: drop newline")

    def test_appending_decisions_allowed(self):
        path = HUB+"DECISIONS.md"
        self.append(path, "- third line\n")
        self.assertAccepted([path], "user adds a line")
        self.append(path, "- fourth line\n")
        self.assertAccepted([path], "[claude] DECISIONS: add a line")

    def test_agent_edits_of_logs(self):
        self.append(HUB+"logs/gpt.md", "x\n")
        self.assertRefused([HUB+"logs/gpt.md"], "[claude] T003: foreign log", "gpt.md")
        self.assertAccepted([HUB+"logs/gpt.md"], "user fixes a log")
        self.append(HUB+"logs/claude.md", "x\n")
        self.assertAccepted([HUB+"logs/claude.md"], "[claude] T003: own log")

    def test_agent_edits_of_reviews(self):
        path = HUB+"reviews/2026-11-01_T001_gpt.md"
        self.append(path, "more\n")
        self.assertRefused([path], "[claude] T001: foreign review")
        self.assertAccepted([path], "[gpt] T001: own review")

    def test_task_card_owner_or_creator_only(self):
        card = HUB+"tasks/T001_work.md"  # owner gpt, creator claude
        self.append(card, "poster note\n")
        self.assertAccepted([card], "[claude] T001: poster fills the card")
        self.append(card, "owner note\n")
        self.assertAccepted([card], "[gpt] T001: owner edits")
        self.append(card, "third\n")
        self.assertRefused([card], "[deepseek] T001: third agent", "owner or the creator")
        self.assertAccepted([card], "user edit")

    def test_legacy_card_owner_holds_against_a_new_status_file(self):
        # T046 F5: a card with no status file in HEAD keeps its Owner: line as the authority.
        card, status = HUB+"tasks/T005_legacy.md", HUB+"tasks/T005.status.md"
        self.write(card, "# T005\nOwner: [gpt]\n")
        self.assertAccepted([card], "user adds a legacy card")
        self.write(status, created("claude", "claude")+"\n")
        self.append(card, "rewritten by claude\n")
        self.assertRefused([card, status], "[claude] T005: take over a legacy card", "owner")
        self.git("reset", "-q", "--", card, status)
        (self.repo/status).unlink()
        self.assertAccepted([card], "[gpt] T005: the legacy owner edits")

    def test_legacy_card_owner_holds_across_two_commits(self):
        # T046 round 3 (F7): a status file committed first, on its own, cannot take over the card later.
        card, status = HUB+"tasks/T005_legacy.md", HUB+"tasks/T005.status.md"
        self.write(card, "# T005\nOwner: [gpt]\n")
        self.assertAccepted([card], "user adds a legacy card")
        self.write(status, created("claude", "claude")+"\n")
        self.assertAccepted([status], "[claude] T005: status file first")
        self.append(card, "rewritten by claude\n")
        self.assertRefused([card], "[claude] T005: then the card", "Only the owner may change")
        self.assertAccepted([card], "[gpt] T005: the legacy owner edits")
        # a card committed together with its status file (new_task.py) is not legacy: its creator may edit it
        self.assertEqual(self.new_task("claude").returncode, 0)
        new_card = next(p for p in (self.repo/(HUB+"tasks")).glob("T*_*.md")
                        if p.name.split("_")[0] not in ("T001", "T003", "T005"))
        self.write(new_card.relative_to(self.repo).as_posix(), new_card.read_text()+"owner note\n")
        self.assertAccepted([new_card.relative_to(self.repo).as_posix()], "[claude] T006: owner edits its card")

    def test_status_event_authorization(self):
        path = HUB+"tasks/T001.status.md"
        # gpt (the claim holder) may release its own claim
        self.append(path, ev.format_event("gpt", "release", {}, timestamp=LATER)+"\n")
        self.assertAccepted([path], "[gpt] T001: release")

    def test_status_event_with_another_tag_refused_for_agents(self):
        path = HUB+"tasks/T001.status.md"
        self.append(path, ev.format_event("gpt", "release", {}, timestamp=LATER)+"\n")
        self.assertRefused([path], "[claude] T001: forged release", "another agent")
        self.assertRefused([path], "[deepseek] T003: forged release")
        # an unauthorized event of the agent's own tag is refused too
        self.write(path, self.read(path).replace("\tgpt\trelease", "\tclaude\trelease"))
        self.assertRefused([path], "[claude] T001: not the holder", "Unauthorized")
        # a user commit is not checked for authorization
        self.assertAccepted([path], "user note")

    def test_malformed_status_line_refused_for_everyone(self):
        path = HUB+"tasks/T001.status.md"
        self.append(path, "this is not an event\n")
        self.assertRefused([path], "user note")
        self.assertRefused([path], "[gpt] T001: bad line")

    def test_new_status_file_must_start_with_created(self):
        path = HUB+"tasks/T004.status.md"
        self.write(path, claim("claude", ["pkg/x/**"])+"\n")
        self.assertRefused([path], "user adds status")
        self.write(path, created("claude", "claude")+"\n")
        self.assertAccepted([path], "[claude] T004: create")

    def test_hub_config_needs_user_decision_for_agents(self):
        path = HUB+"hub_config.json"
        self.write(path, json.dumps({**SCOPED, "memory_export": True})+"\n")
        self.assertRefused([path], "[gpt] T001: flip config", "set_config")
        self.assertAccepted([path], "the user edits the config")

    def test_state_and_other_hub_files_are_allowed(self):
        self.write(HUB+"STATE.md", "state\n")
        self.assertAccepted([HUB+"STATE.md"], "[deepseek] T001: STATE")

    def test_transaction_proofs_are_still_checked(self):
        path = HUB+"INDEX.md"
        self.write(path, "# Index\n")
        self.git("add", "--", path)
        self.git("commit", "-q", "-m", "add index")
        self.append(path, "line\n")
        self.git("add", "--", path)
        message = self.repo/".git/msg"
        message.write_text("[gpt] T001: tx\n")
        env = os.environ.copy()
        env["HUB_TX_APPENDS"] = json.dumps({path: dict(before="0"*64, after="1"*64)})
        result = subprocess.run([sys.executable, TOOLS+"/commit_msg_hook.py", str(message)], cwd=str(self.repo),
                                capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, hubtx.DirtyTarget.exit_code, result.stderr)
        del env["HUB_TX_APPENDS"]
        result = subprocess.run([sys.executable, TOOLS+"/commit_msg_hook.py", str(message)], cwd=str(self.repo),
                                capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_hub_tools_still_commit_under_the_scoped_guard(self):
        env = os.environ.copy()
        env["PYTHONPATH"] = TOOLS
        line = ev.format_event("gpt", "release", {}, timestamp=hubtx.now())
        result = subprocess.run([sys.executable, TOOLS+"/hub_append.py", "gpt", "T001", HUB+"tasks/T001.status.md",
                                 line], cwd=str(self.repo), capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.new_task().returncode, 0)


class MergeCommits(Base):
    def side_with_gpt_event(self):
        self.git("checkout", "-q", "-b", "side")
        path = HUB+"tasks/T001.status.md"
        self.append(path, ev.format_event("gpt", "release", {}, timestamp=LATER)+"\n")
        self.assertAccepted([path], "[gpt] T001: release")
        self.git("checkout", "-q", "main")
        self.write("pkg/free.py", "free on main\n")
        self.assertAccepted(["pkg/free.py"], "user change on main")       # so the merge is a real merge

    def test_merge_with_an_agent_subject_gets_user_level_checks(self):
        self.side_with_gpt_event()
        before = self.head()
        result = self.git("merge", "--no-ff", "-q", "side", "-m", "[claude] T003: merge side", check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(self.head(), before)
        self.assertIn("\tgpt\trelease", self.read(HUB+"tasks/T001.status.md"))

    def test_merge_that_rewrites_an_append_only_file_is_refused(self):
        self.git("checkout", "-q", "-b", "side")
        self.write(HUB+"DECISIONS.md", "# Decisions\n- first line, rewritten\n- second line\n")
        self.git("add", HUB+"DECISIONS.md")
        self.git("commit", "-q", "--no-verify", "-m", "rewrite (bypassing the hook)")
        self.git("checkout", "-q", "main")
        self.write("pkg/free.py", "free on main\n")
        self.assertAccepted(["pkg/free.py"], "user change on main")
        before = self.head()
        result = self.git("merge", "--no-ff", "-q", "side", "-m", "[claude] T003: merge side", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.head(), before)
        self.git("merge", "--abort")


class FinishedTaskClaims(Base):
    def cli(self, *args):
        env = os.environ.copy()
        env["PYTHONPATH"] = TOOLS
        r = subprocess.run([sys.executable, *args], cwd=str(self.repo), capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout

    def test_claim_of_an_accepted_task_no_longer_guards_its_files(self):
        tools = HUB + "tools/"
        task = self.cli(tools + "new_task.py", "claude", "Done task", "--owner", "gpt", "--reviewers", "deepseek",
                        "--deliverables", "pkg/done.py").split()[0]
        self.cli(tools + "job_act.py", "claim", "gpt", task)
        self.write("pkg/done.py", "done\n")
        self.write("pkg/done_free.py", "x\n")
        self.cli(tools + "job_act.py", "submit", "gpt", task, "-m", "deliver")
        # while the task is in review, its claim still guards its files
        self.write("pkg/done.py", "edited by claude\n")
        self.assertRefused(["pkg/done.py"], "[claude] T003: edit", "active claim of gpt")
        self.git("checkout", "-q", "--", "pkg/done.py")
        review = HUB + f"reviews/2026-11-02_{task}_deepseek.md"
        self.write(review, "# Review\n\n| ID | Claim | Verdict | Severity | Evidence |\n|---|---|---|---|---|\n"
                           "| F1 | ok | CONFIRMED | informational | e |\n")
        self.cli(tools + "job_act.py", "review", "deepseek", task, "--file", review, "--accept")
        # accepted: the claim no longer blocks another agent
        self.write("pkg/done.py", "edited by claude\n")
        self.assertAccepted(["pkg/done.py"], "[claude] T003: edit after acceptance")

    def test_unstaged_terminal_event_does_not_release_a_claim(self):
        # T046 F2: the guard trusts only staged/committed status records.
        status = HUB+"tasks/T001.status.md"
        cancel = ev.format_event("claude", "user", dict(action="cancel", decision="D999", task="T001"),
                                 note="unstaged only", timestamp=LATER)+"\n"
        committed = self.read(status)
        self.append(status, cancel)                       # on disk, never staged
        self.write("pkg/gpt/a.py", "edited by claude\n")
        self.assertRefused(["pkg/gpt/a.py"], "[claude] T003: foreign edit", "active claim of gpt")
        self.assertEqual(self.git("show", "HEAD:"+status).stdout, committed)
        # a committed cancellation (here by the user) does release the claim
        self.assertAccepted([status], "user cancels T001")
        self.assertAccepted(["pkg/gpt/a.py"], "[claude] T003: edit after the cancellation")

    def test_unstaged_review_edit_does_not_release_a_claim(self):
        # T046 F2: review files are acceptance evidence; an unstaged edit that closes a blocking
        # finding must not make the task look accepted to the guard.
        tools = HUB + "tools/"
        task = self.cli(tools + "new_task.py", "claude", "Reviewed task", "--owner", "gpt", "--reviewers",
                        "deepseek", "--deliverables", "pkg/r.py").split()[0]
        self.cli(tools + "job_act.py", "claim", "gpt", task)
        self.write("pkg/r.py", "r\n")
        self.cli(tools + "job_act.py", "submit", "gpt", task, "-m", "deliver")
        review = HUB + f"reviews/2026-11-02_{task}_deepseek.md"
        self.write(review, "# Review\n\n| ID | Claim | Verdict | Severity | Evidence |\n|---|---|---|---|---|\n"
                           "| F1 | x | WRONG | blocking | e |\n")
        self.cli(tools + "job_act.py", "review", "deepseek", task, "--file", review, "--accept")
        self.write(review, self.read(review).replace("WRONG | blocking", "CONFIRMED | informational"))
        self.write("pkg/r.py", "edited by claude\n")
        self.assertRefused(["pkg/r.py"], "[claude] T003: edit", "active claim of gpt")
        # committed, the same review closes the finding and the task is accepted: the claim is released
        self.assertAccepted([review], "user commits the review edit")
        self.assertAccepted(["pkg/r.py"], "[claude] T003: edit after acceptance")

    def accepted_task(self, task, deliverable, depends_on, reviewer_accepts=True):
        """Commit task `task`: posted by claude, owned and claimed by gpt, submitted at the committed
        revision of `deliverable`, and (optionally) accepted by deepseek; it waits on `depends_on`."""
        import hub_state
        self.write(deliverable, task + "\n")
        self.assertAccepted([deliverable], "user adds " + deliverable)
        rev = hub_state.revision(self.repo, [deliverable])
        lines = [ev.format_event("claude", "created", dict(owner="gpt", reviewers=["deepseek"], required="all",
                                 kind="code", deliverables=[deliverable], depends_on=depends_on, initial="open"),
                                 timestamp=STAMP),
                 ev.format_event("gpt", "claim", dict(scope=[deliverable]), timestamp=STAMP),
                 ev.format_event("gpt", "submit", dict(rev=rev), timestamp=LATER)]
        if reviewer_accepts:
            lines.append(ev.format_event("deepseek", "accept", dict(rev=rev), timestamp=LATER))
        status = HUB + f"tasks/{task}.status.md"
        self.write(status, "".join(line + "\n" for line in lines))
        self.assertAccepted([status], f"user records {task}")
        return status, rev

    def test_unstaged_decision_does_not_release_a_claim(self):
        # T046 round 3 (F8): DECISIONS.md is an input of the derivation (D### prerequisites), so an
        # unstaged decision heading must not turn a blocked task into an accepted one for the guard.
        import hub_state
        self.accepted_task("T007", "pkg/dep.py", ["D001"])
        self.assertEqual(hub_state.derive(self.repo, "T007")["state"], "blocked")
        self.write("pkg/dep.py", "edited by claude\n")
        self.assertRefused(["pkg/dep.py"], "[claude] T003: foreign edit", "active claim of gpt")
        decisions = HUB + "DECISIONS.md"
        self.append(decisions, "### D001: fixture decision\n")       # on disk, never staged
        self.assertTrue(hub_state.derive(self.repo, "T007")["accepted"])
        self.assertRefused(["pkg/dep.py"], "[claude] T003: foreign edit", "active claim of gpt")
        self.assertNotIn("D001", self.git("show", "HEAD:" + decisions).stdout)
        # committed, the decision meets the prerequisite: the task is accepted and its claim released
        self.assertAccepted([decisions], "user records D001")
        self.assertAccepted(["pkg/dep.py"], "[claude] T003: edit after the decision")

    def test_unstaged_dependency_status_does_not_release_a_claim(self):
        # T046 round 3 (F8): a dependency task's status file is an input too.
        import hub_state
        dep_status, dep_rev = self.accepted_task("T007", "pkg/first.py", [], reviewer_accepts=False)
        self.accepted_task("T008", "pkg/second.py", ["T007"])
        self.assertEqual(hub_state.derive(self.repo, "T008")["state"], "blocked")
        self.write("pkg/second.py", "edited by claude\n")
        self.assertRefused(["pkg/second.py"], "[claude] T003: foreign edit", "active claim of gpt")
        self.append(dep_status, ev.format_event("deepseek", "accept", dict(rev=dep_rev), timestamp=LATER) + "\n")
        self.assertTrue(hub_state.derive(self.repo, "T008")["accepted"])   # on disk only
        self.assertRefused(["pkg/second.py"], "[claude] T003: foreign edit", "active claim of gpt")
        self.assertAccepted([dep_status], "user records the dependency's accept")
        self.assertAccepted(["pkg/second.py"], "[claude] T003: edit after the dependency is accepted")


class FullMode(Base):
    config = {"task_branch": "main"}  # no commit_guard key

    def test_untagged_commit_refused_as_before(self):
        self.append("pkg/free.py", "more\n")
        self.assertRefused(["pkg/free.py"], "no tag at all", "Commit subject must be")
        self.assertRefused(["pkg/free.py"], "[claude] no task id", "Commit subject must be")

    def test_ownership_still_required(self):
        self.append("pkg/free.py", "more\n")
        self.assertRefused(["pkg/free.py"], "[claude] T003: unowned", "unowned path")
        self.append("pkg/claude/b.py", "more\n")
        self.assertAccepted(["pkg/claude/b.py"], "[claude] T003: own claim")

    def test_explicit_full_is_the_same(self):
        self.write(HUB+"hub_config.json", json.dumps({"commit_guard": "full"})+"\n")
        self.git("add", "-A")
        self.git("commit", "-q", "--no-verify", "-m", "config")
        self.append("pkg/free.py", "more\n")
        self.assertRefused(["pkg/free.py"], "no tag at all", "Commit subject must be")


class NoConfigFile(Base):
    config = None

    def test_missing_config_is_full_mode(self):
        self.append("pkg/free.py", "more\n")
        self.assertRefused(["pkg/free.py"], "no tag", "Commit subject must be")
        self.assertEqual(self.new_task().returncode, 0)  # no task_branch: no restriction


class LinkedWorktreeHook(Base):
    def setUp(self):
        super().setUp()
        self.wt = self.base/"wt"
        self.git("worktree", "add", "-q", "-b", "side", str(self.wt))
        self.hook_path = Path(HUB+"tools/commit_msg_hook.py")

    def test_hook_lives_in_the_common_git_dir(self):
        self.assertEqual(self.hook, (self.repo/".git/hooks/commit-msg").resolve())
        text = self.hook.read_text()
        self.assertIn("--git-common-dir", text)
        self.assertNotIn("--show-toplevel", text)
        self.assertEqual(self.hook.stat().st_mode & 0o777, 0o755)

    def test_worktree_commit_is_guarded(self):
        self.append("pkg/gpt/a.py", "more\n", self.wt)
        self.assertRefused(["pkg/gpt/a.py"], "[claude] T003: foreign claim", "claim", root=self.wt)
        self.assertAccepted(["pkg/gpt/a.py"], "user commit", root=self.wt)

    def test_worktree_runs_the_main_checkouts_hook(self):
        # An uncommitted edit makes the worktree's copy refuse every commit; it must not be used.
        self.write(str(self.hook_path), "raise SystemExit(1)\n", self.wt)
        self.append("pkg/free.py", "more\n", self.wt)
        self.assertAccepted(["pkg/free.py"], "passes through the main checkout's hook", root=self.wt)
        # The main checkout's copy is the one that runs: break it and the worktree commit is refused.
        self.write(str(self.hook_path), "raise SystemExit(1)\n")
        self.append("pkg/free.py", "again\n", self.wt)
        self.assertRefused(["pkg/free.py"], "now refused", root=self.wt)

    def test_missing_hook_file_in_main_checkout_allows_the_commit(self):
        (self.repo/self.hook_path).rename(self.repo/(str(self.hook_path)+".gone"))
        self.append("pkg/gpt/a.py", "more\n", self.wt)
        self.assertAccepted(["pkg/gpt/a.py"], "[claude] T003: nothing checks this", root=self.wt)
        self.append("pkg/gpt/a.py", "more\n")
        self.assertAccepted(["pkg/gpt/a.py"], "[claude] T003: nor in main")

    def test_worktree_on_a_branch_without_the_key_follows_main(self):
        # A branch from before the guard has no commit_guard key; the mode comes from main.
        old = self.base/"old"
        self.git("worktree", "add", "-q", "-b", "old", str(old))
        self.write(HUB+"hub_config.json", "{}\n", old)
        self.git("add", HUB+"hub_config.json", root=old)
        self.git("commit", "-q", "--no-verify", "-m", "a branch from before the guard", root=old)
        self.append("pkg/free.py", "more\n", old)
        self.assertAccepted(["pkg/free.py"], "user commit on an old branch", root=old)

    def test_existing_hook_is_preserved(self):
        with self.assertRaisesRegex(hubtx.BadInput, "preserve"):
            install("gpt", "T012")
        self.git("config", "core.hooksPath", "custom-hooks")
        self.hook.unlink()
        with self.assertRaisesRegex(hubtx.BadInput, "hooksPath"):
            install("gpt", "T012")


if __name__ == "__main__":
    unittest.main()
