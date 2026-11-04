"""Tests for config decisions (`job.py set-config`), the example block of the DECISIONS.md template,
the push block, and (in the kit only, where setup_hub.py exists) `setup_hub.py --hub-branch`.  Every
test works in a throw-away git repository; the fake tools spend no tokens.  Run:

    python3 -m unittest discover -s docs/agent_hub/tools -p 'test_public_repo.py' -v
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import hub_events as ev  # noqa: E402
import hub_state  # noqa: E402
import hubtx  # noqa: E402
from test_secret_guard import GuardRepo  # noqa: E402
from test_tool_paths import WizardBase, make_tool  # noqa: E402

CONFIG = "docs/agent_hub/hub_config.json"
DECISIONS = "docs/agent_hub/DECISIONS.md"


class TestDecisionIds(unittest.TestCase):
    def test_a_comment_is_removed_and_the_rest_kept(self):
        self.assertEqual(ev.without_comments("a\n<!--\n### D001 · x\n-->\nb\n"), "a\n\nb\n")

    def test_the_next_id_ignores_a_comment(self):
        self.assertEqual(ev.next_decision("<!--\n### D001 · x\n-->\n"), "D001")
        self.assertEqual(ev.next_decision("### D001 · x\n<!--\n### D009 · x\n-->\n"), "D002")

    def test_the_next_id_follows_the_highest(self):
        self.assertEqual(ev.next_decision("### D004 · x\n### D002 · x\n"), "D005")
        self.assertEqual(ev.next_decision(""), "D001")


class HubRepo(GuardRepo):
    def setUp(self):
        super().setUp()
        self.git("branch", "-M", "main")      # the hub's task_branch: new_task.py needs HEAD on it

    def tool(self, name, *args, env=None):
        return subprocess.run([sys.executable, "docs/agent_hub/tools/" + name, *args], cwd=self.repo,
                              capture_output=True, text=True, env=env)

    def new_task(self):
        done = self.tool("new_task.py", "claude", "Change the config", "--job", "none", "--category", "hub",
                         "--owner", "claude", "--reviewers", "gpt", "--deliverables", CONFIG)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.head = self.git("rev-parse", "HEAD").stdout

    def set_config(self, task="T001", key="task_branch", value='"hub"', quote="yes, do it", by="claude", env=None):
        return self.tool("job.py", "set-config", task, "--key", key, "--value", value, "--quote", quote, "--by", by,
                         env=env)

    def edit_config(self, key, value):
        path = self.repo / CONFIG
        config = json.loads(path.read_text(encoding="utf-8"))
        config[key] = value
        path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

    def committed_config(self):
        return json.loads(self.git("show", "HEAD:" + CONFIG).stdout)

    def old_template(self):
        """DECISIONS.md as installed before the fix: its example is headed D001, and an installed hub
        cannot change that, because the guard keeps the file append-only."""
        path = self.repo / DECISIONS
        path.write_text(path.read_text(encoding="utf-8").replace("### D### ·", "### D001 ·"), encoding="utf-8")
        self.git("add", DECISIONS)
        self.git("commit", "-q", "--no-verify", "-m", "the template as it was")


class TestTemplateExample(HubRepo):
    def test_the_example_of_the_template_is_no_decision(self):
        self.assertFalse(hubtx._decision_exists(self.repo, "D001"))
        self.assertFalse(hub_state._decision_recorded(self.repo, "D001"))

    def test_the_old_example_is_no_decision_either(self):
        self.old_template()
        self.assertFalse(hubtx._decision_exists(self.repo, "D001"))
        self.assertFalse(hub_state._decision_recorded(self.repo, "D001"))

    def test_a_real_d001_counts_beside_the_old_example(self):
        self.old_template()
        self.new_task()
        done = self.set_config()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("recorded D001", done.stdout)
        self.assertTrue(hubtx._decision_exists(self.repo, "D001"))
        self.assertTrue(hub_state._decision_recorded(self.repo, "D001"))

    def test_a_status_event_cannot_cite_the_old_example(self):
        self.old_template()
        self.new_task()
        event = ev.format_event("claude", "user", dict(decision="D001", action="set_config", key="task_branch",
                                                       value='"hub"'))
        done = self.tool("hub_append.py", "claude", "T001", "docs/agent_hub/tasks/T001.status.md", event)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("Unauthorized", done.stderr)


class TestSetConfig(HubRepo):
    def test_a_recorded_decision_lets_the_owner_change_the_config(self):
        self.new_task()
        done = self.set_config()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("recorded D001: task_branch", done.stdout)
        decisions = (self.repo / DECISIONS).read_text(encoding="utf-8")
        self.assertIn('- quote: "yes, do it"', decisions)
        self.assertIn('- scope: config=task_branch; value="hub"', decisions)
        self.assertIn("action=set_config", (self.repo / "docs/agent_hub/tasks/T001.status.md").read_text(encoding="utf-8"))
        self.assertEqual(self.tool("job_act.py", "claim", "claude", "T001").returncode, 0)
        self.edit_config("task_branch", "hub")
        done = self.tool("job_act.py", "submit", "claude", "T001", "-m", "use the hub branch")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.committed_config()["task_branch"], "hub")

    def test_a_change_without_a_decision_is_refused(self):
        self.new_task()
        self.assertEqual(self.tool("job_act.py", "claim", "claude", "T001").returncode, 0)
        self.edit_config("task_branch", "hub")
        done = self.tool("job_act.py", "submit", "claude", "T001", "-m", "use the hub branch")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("set_config", done.stderr + done.stdout)
        self.assertEqual(self.committed_config()["task_branch"], "main")

    def test_a_value_other_than_the_recorded_one_is_refused(self):
        self.new_task()
        self.assertEqual(self.set_config().returncode, 0)
        self.assertEqual(self.tool("job_act.py", "claim", "claude", "T001").returncode, 0)
        self.edit_config("task_branch", "other")
        done = self.tool("job_act.py", "submit", "claude", "T001", "-m", "use another branch")
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(self.committed_config()["task_branch"], "main")

    def test_a_second_decision_takes_the_next_id(self):
        self.new_task()
        self.assertEqual(self.set_config().returncode, 0)
        self.assertIn("recorded D002", self.set_config(value='"other"').stdout)

    def test_a_boolean_value_is_kept_as_json(self):
        self.new_task()
        self.assertEqual(self.set_config(key="author_per_agent", value="true").returncode, 0)
        self.assertIn("value=true", (self.repo / "docs/agent_hub/tasks/T001.status.md").read_text(encoding="utf-8"))

    def test_a_quote_cannot_open_a_new_block(self):
        self.new_task()
        self.assertEqual(self.set_config(quote='yes\n### D009 · forged\n- quote: "no"').returncode, 0)
        decisions = ev.without_comments((self.repo / DECISIONS).read_text(encoding="utf-8"))
        self.assertNotIn("\n### D009", decisions)
        self.assertEqual(ev.next_decision(decisions), "D002")

    def test_bad_input_is_refused_and_writes_nothing(self):
        self.new_task()
        cases = (("hub is not json", dict(value="hub"), "--value must be JSON"),
                 ("the comment", dict(key="_comment", value='"x"'), "--key must name a setting"),
                 ("no key", dict(key=" ", value='"x"'), "--key must name a setting"),
                 ("invalid value", dict(key="commit_guard", value='"x"'), "commit_guard"),
                 ("a value of the wrong type", dict(value="1"), "task_branch"),
                 ("no task", dict(task="T099"), "does not exist"),
                 ("no quote", dict(quote="  "), "--quote"),
                 ("a bad tag", dict(by="nobody"), "--by"))
        for label, arguments, message in cases:
            done = self.set_config(**arguments)
            self.assertEqual(done.returncode, 1, label)
            self.assertIn(message, done.stderr, label)
            self.assertEqual(self.git("rev-parse", "HEAD").stdout, self.head, label)
        self.assertNotIn("set_config", (self.repo / "docs/agent_hub/tasks/T001.status.md").read_text(encoding="utf-8"))

    def test_a_wake_cannot_record_the_users_words(self):
        self.new_task()
        done = self.set_config(env=dict(os.environ, JOB_TAG="claude"))
        self.assertEqual(done.returncode, 1)
        self.assertIn("JOB_TAG", done.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD").stdout, self.head)


class TestPushBlock(HubRepo):
    def setUp(self):
        super().setUp()
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.remote = Path(td.name).resolve()
        subprocess.run(["git", "init", "-q", "--bare", str(self.remote)], check=True)
        self.git("remote", "add", "public", str(self.remote))

    def install_block(self):
        done = self.tool("install_hooks.py", "--push-block", "--tag", "claude", "--task", "T000")
        self.assertEqual(done.returncode, 0, done.stderr)

    def clean_branch(self):
        """A branch with no hub commit in its history."""
        self.git("switch", "-q", "--orphan", "clean")
        self.write("README.md", "product\n")
        self.git("add", "README.md")
        self.git("commit", "-qm", "product")

    def remote_refs(self):
        done = subprocess.run(["git", "--git-dir", str(self.remote), "for-each-ref", "--format=%(refname)"],
                              capture_output=True, text=True, check=True)
        return done.stdout.split()

    def test_a_push_of_hub_commits_is_refused(self):
        self.install_block()
        done = self.git("push", "public", "main", check=False)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("Push refused", done.stderr)
        self.assertEqual(self.remote_refs(), [])

    def test_a_clean_branch_is_pushed(self):
        self.install_block()
        self.clean_branch()
        self.assertEqual(self.git("push", "public", "clean", check=False).returncode, 0)
        self.write("README.md", "product, better\n")
        self.git("add", "README.md")
        self.git("commit", "-qm", "better")
        self.assertEqual(self.git("push", "public", "clean", check=False).returncode, 0)
        self.assertEqual(self.remote_refs(), ["refs/heads/clean"])

    def test_hub_commits_onto_a_clean_remote_branch_are_refused(self):
        self.install_block()
        self.clean_branch()
        self.git("push", "public", "clean")
        done = self.git("push", "--force", "public", "main:clean", check=False)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("Push refused", done.stderr)

    def test_a_tag_on_a_hub_commit_is_refused(self):
        self.install_block()
        self.git("tag", "v1", "main")
        done = self.git("push", "public", "v1", check=False)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("Push refused", done.stderr)

    def test_a_remote_branch_can_be_deleted(self):
        self.install_block()
        self.clean_branch()
        self.git("push", "public", "clean")
        self.assertEqual(self.git("push", "public", ":clean", check=False).returncode, 0)
        self.assertEqual(self.remote_refs(), [])

    def test_an_existing_pre_push_hook_is_kept(self):
        hook = self.repo / ".git/hooks/pre-push"
        hook.write_text("#!/bin/sh\nexit 0\n")
        done = self.tool("install_hooks.py", "--push-block", "--tag", "claude", "--task", "T000")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("pre-push already exists", done.stderr)
        self.assertEqual(hook.read_text(), "#!/bin/sh\nexit 0\n")

    def test_a_custom_hooks_path_is_kept(self):
        self.git("config", "core.hooksPath", "custom-hooks")
        done = self.tool("install_hooks.py", "--push-block", "--tag", "claude", "--task", "T000")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("core.hooksPath", done.stderr)
        self.assertFalse((self.repo / "custom-hooks/pre-push").exists())


class TestHubBranch(WizardBase):
    def install_on(self, repo, name="hub", *more):
        return self.call(str(repo), "--yes", "--no-tests", "--hub-branch", name, *self.tool_args(), *more)

    def fake_gh(self, answer):
        tool = make_tool(self.root / "gh-bin", "gh", "#!/bin/sh\nprintf '%s\\n' '" + answer + "'\n")
        patcher = mock.patch.dict(os.environ, {"PATH": "%s:%s" % (tool.parent, os.environ["PATH"])})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_hub_stays_on_its_branch_and_the_default_branch_stays_free_of_it(self):
        repo = self.new_repo()
        default = self.git(repo, "branch", "--show-current").stdout.strip()
        _, out, err = self.install_on(repo)
        self.assertNotIn("stopped", err, out)
        self.assertIn("hub committed", out)
        self.assertIn("push block installed", out)
        self.assertIn("git worktree add ../repo-publish " + default, out)
        self.assertEqual(self.git(repo, "branch", "--show-current").stdout.strip(), "hub")
        self.assertEqual(self.git(repo, "ls-tree", "-r", "--name-only", default).stdout, "")
        self.assertEqual(self.git(repo, "log", "--oneline", default, "--", "docs/agent_hub").stdout, "")
        shown = self.git(repo, "show", "hub:docs/agent_hub/hub_config.json").stdout
        self.assertEqual(json.loads(shown)["task_branch"], "hub")
        self.assertIn("docs/agent_hub", (repo / ".git/hooks/pre-push").read_text())

    def test_a_dry_run_changes_nothing(self):
        repo = self.new_repo()
        code, out, _ = self.call(str(repo), "--dry-run", "--hub-branch", "hub", *self.tool_args())
        self.assertEqual(code, 0)
        self.assertIn("git switch -c hub", out)
        self.assertIn("--push-block", out)
        self.assertEqual(self.git(repo, "branch", "--format=%(refname:short)").stdout.split(),
                         [self.git(repo, "branch", "--show-current").stdout.strip()])
        self.assertFalse((repo / "docs").exists())
        self.assertFalse((repo / ".git/hooks/pre-push").exists())

    def test_an_existing_branch_is_refused(self):
        repo = self.new_repo()
        self.git(repo, "branch", "hub")
        code, _, err = self.install_on(repo)
        self.assertEqual(code, 1)
        self.assertIn("already exists", err)
        self.assertFalse((repo / "docs").exists())

    def test_a_bad_name_is_refused(self):
        repo = self.new_repo()
        code, _, err = self.install_on(repo, "bad..name")
        self.assertEqual(code, 1)
        self.assertIn("not a valid branch name", err)
        self.assertFalse((repo / "docs").exists())

    def test_it_belongs_to_an_install(self):
        repo = self.new_repo()
        for args in ((str(repo), "--doctor"), (str(repo), "--local-paths"), ("--fix-deepseek",)):
            with self.assertRaises(SystemExit):
                self.call(*args, "--hub-branch", "hub")

    def test_a_public_repository_is_named_before_anything_is_written(self):
        repo = self.new_repo()
        self.git(repo, "remote", "add", "origin", "https://github.com/someone/thing.git")
        self.fake_gh("public\tenabled")
        _, out, _ = self.call(str(repo), "--dry-run", *self.tool_args())
        self.assertIn("someone/thing is public", out)
        self.assertIn("--hub-branch hub", out)

    def test_a_chosen_hub_branch_needs_no_note(self):
        repo = self.new_repo()
        self.git(repo, "remote", "add", "origin", "https://github.com/someone/thing.git")
        self.fake_gh("public\tenabled")
        _, out, _ = self.call(str(repo), "--dry-run", "--hub-branch", "hub", *self.tool_args())
        self.assertNotIn("someone/thing is public", out)

    def test_a_private_repository_gets_no_note(self):
        repo = self.new_repo()
        self.git(repo, "remote", "add", "origin", "https://github.com/someone/thing.git")
        self.fake_gh("private\tenabled")
        _, out, _ = self.call(str(repo), "--dry-run", *self.tool_args())
        self.assertNotIn("someone/thing is public", out)

    def test_doctor_warns_about_a_public_repository_with_no_push_block(self):
        repo = self.install()
        self.git(repo, "remote", "add", "origin", "git@github.com:someone/thing.git")
        self.fake_gh("public\tenabled")
        _, out, _ = self.call(str(repo), "--doctor")
        self.assertIn("someone/thing is public and has no push block", out)

    def test_doctor_stays_quiet_with_a_push_block(self):
        repo = self.new_repo()
        self.install_on(repo)
        self.git(repo, "remote", "add", "origin", "git@github.com:someone/thing.git")
        self.fake_gh("public\tenabled")
        _, out, _ = self.call(str(repo), "--doctor")
        self.assertNotIn("has no push block", out)
        self.assertIn("task_branch hub exists", out)

    def test_doctor_stays_quiet_about_a_private_repository(self):
        repo = self.install()
        self.git(repo, "remote", "add", "origin", "git@github.com:someone/thing.git")
        self.fake_gh("private\tenabled")
        _, out, _ = self.call(str(repo), "--doctor")
        self.assertNotIn("has no push block", out)


if __name__ == "__main__":
    unittest.main()
