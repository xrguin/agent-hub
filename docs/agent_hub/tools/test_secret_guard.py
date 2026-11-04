"""Tests for the secret check of the commit guard, and (in the kit only) the wizard's secret checks.
Every test works in a throw-away git repository, with the real hook installed.  The fake keys are
built by joining pieces, so that this file itself holds no key-like string.  Run:

    python3 -m unittest discover -s docs/agent_hub/tools -p 'test_secret_guard.py' -v
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import secret_guard as sg  # noqa: E402
from test_tool_paths import WizardBase, make_tool  # noqa: E402

SK = "sk-" + "a1b2c3d4" * 4
GITHUB = "gh" + "p_" + "aB3dE5gH7j" * 4
ANTHROPIC = "sk-" + "ant-api03-" + "Ab1Cd2Ef3Gh4" * 3
JWT = ".".join(("ey" + "J" + "hbGciOiJIUzI1NiJ9abc", "ey" + "J" + "zdWIiOiIxMjM0NTY3ODkwIn0", "dozjgNryP4J3jVmNHl0w5N"))
PRIVATE = "-----BEGIN " + "RSA PRIVATE KEY-----"
AWS = "AK" + "IA" + "Z7Q2M4K8P1R6T3W9"
GOOGLE = "AI" + "za" + "Sy" + "Ab1Cd2Ef3Gh4Ij5Kl6Mn7Op8Qr9St0Uv1W"
SLACK = "xo" + "xb-" + "123456789012-abcDEF123456"
ASSIGNED = "api_key" + ' = "' + "Zk4mP9qR2tV7xB1nC6dF8hJ3" + '"'
HUB_FILES = shutil.ignore_patterns("__pycache__", "hub_config.local.json")


class TestScanLine(unittest.TestCase):
    def test_key_formats_are_found(self):
        for line, kind in ((SK, "API key (sk-)"), ("token: " + ANTHROPIC, "API key (sk-)"), (GITHUB, "GitHub token"),
                           (JWT, "JSON web token"), (PRIVATE, "private key"), (AWS, "AWS access key"),
                           (GOOGLE, "Google API key"), (SLACK, "Slack token"), (ASSIGNED, "assigned secret")):
            self.assertEqual(sg.scan_line(line), kind, line[:12])

    def test_ordinary_text_is_left_alone(self):
        for line in ("the disk-usage-report-for-all-of-the-machines is ready",
                     "task-force-working-group-minutes-2026",
                     '"secret_scanning_push_protection": {"status": "enabled"}',
                     "password = get_password_from_environment_variable()",
                     'api_key: "${{ secrets.DEEPSEEK_API_KEY }}"',
                     "sha256 e32ca4477095e3b259915d3db0b2acd1091e9d142706a54ba3eb2dcc54184c41",
                     "id 3f2504e0-4f89-11d3-9a0c-0305e82c3301",
                     "api_key = your_api_key_goes_here_1234567",
                     "AK" + "IA" + "IOSFODNN7EXAMPLE",
                     "sk-" + "abcdefghijklmnopqrstuvwxyz"):
            self.assertIsNone(sg.scan_line(line), line)

    def test_the_allow_comment_skips_a_line(self):
        self.assertIsNone(sg.scan_line(SK + "  # " + sg.ALLOW))

    def test_the_second_match_on_a_line_counts(self):
        self.assertEqual(sg.scan_line("AK" + "IA" + "IOSFODNN7EXAMPLE " + AWS), "AWS access key")


class TestSecretFile(unittest.TestCase):
    def test_secret_files_are_named(self):
        for path in (".env", "config/.env.local", "server.pem", "keys/id_rsa", "id_ed25519", ".credentials.yaml",
                     ".codex/auth.json", ".netrc", "docs/agent_hub/hub_config.local.json"):
            self.assertTrue(sg.secret_file(path), path)

    def test_other_files_pass(self):
        for path in (".env.example", "x/.env.sample", "id_rsa.pub", "environment.yml", "auth.json", "docs/secrets.md"):
            self.assertIsNone(sg.secret_file(path), path)


class TestAddedLines(unittest.TestCase):
    def test_numbers_and_an_added_line_that_looks_like_a_header(self):
        diff = ("diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1,2 @@\n-old\n+new\n+++ not a header\n"
                "@@ -9,0 +11 @@\n+later\n\\ No newline at end of file\n")
        self.assertEqual(list(sg.added_lines(diff)), [("x", 1, "new"), ("x", 2, "++ not a header"), ("x", 11, "later")])


class GuardRepo(unittest.TestCase):
    """A repository with the kit's hub, its commit guard installed."""

    def setUp(self):
        patch_env = mock.patch.dict(os.environ, {"PYTHONDONTWRITEBYTECODE": "1"})
        patch_env.start()
        self.addCleanup(patch_env.stop)
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.repo = Path(td.name).resolve()
        self.git("init", "-q")
        for key, value in (("user.name", "test"), ("user.email", "test@example.invalid"), ("commit.gpgsign", "false")):
            self.git("config", key, value)
        shutil.copytree(HERE.parent, self.repo / "docs/agent_hub", ignore=HUB_FILES)
        self.git("add", ".")
        self.git("commit", "-qm", "baseline")
        subprocess.run([sys.executable, "docs/agent_hub/tools/install_hooks.py", "--tag", "claude", "--task", "T000"],
                       cwd=self.repo, check=True, capture_output=True)
        self.head = self.git("rev-parse", "HEAD").stdout

    def git(self, *args, check=True):
        done = subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True, text=True)
        if check and done.returncode:
            self.fail(done.stderr)
        return done

    def write(self, name, text):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def commit(self, *args, message="work"):
        return self.git("commit", "-m", message, *args, check=False)

    def assertRefused(self, done, value=None):
        self.assertNotEqual(done.returncode, 0, done.stdout)
        self.assertIn("looks like a secret", done.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD").stdout, self.head)
        if value:
            self.assertNotIn(value, done.stderr + done.stdout)


class TestCommits(GuardRepo):
    def test_a_user_commit_with_a_key_is_refused_without_showing_it(self):
        self.write("notes.txt", "one\ntwo\nkey " + SK + "\n")
        self.git("add", "notes.txt")
        done = self.commit()
        self.assertRefused(done, SK)
        self.assertIn("notes.txt:3: API key (sk-)", done.stderr)

    def test_an_agent_log_with_a_key_is_refused(self):
        self.write("docs/agent_hub/logs/gpt.md", "ran with " + GITHUB + "\n")
        self.git("add", ".")
        self.assertRefused(self.commit(message="[gpt] T001: log"), GITHUB)

    def test_a_key_in_the_commit_message_is_refused(self):
        self.write("notes.txt", "fine\n")
        self.git("add", "notes.txt")
        done = self.commit(message="work\n\nused " + SK)
        self.assertRefused(done, SK)
        self.assertIn("commit message:3", done.stderr)

    def test_a_secret_file_is_refused(self):
        self.write(".env", "NAME=value\n")
        self.git("add", ".env")
        self.assertRefused(self.commit())

    def test_the_local_paths_file_is_refused(self):
        self.write("docs/agent_hub/hub_config.local.json", "{}\n")
        self.git("add", "-f", "docs/agent_hub/hub_config.local.json")
        self.assertRefused(self.commit())

    def test_git_commit_only_is_checked(self):
        self.write("notes.txt", "fine\n")
        self.git("add", "notes.txt")
        self.git("commit", "-qm", "notes")
        self.head = self.git("rev-parse", "HEAD").stdout
        self.write("notes.txt", "fine\n" + SK + "\n")
        self.assertRefused(self.commit("--only", "--", "notes.txt"), SK)

    def test_a_clean_commit_passes(self):
        self.write("notes.txt", "fine\n")
        self.git("add", "notes.txt")
        self.assertEqual(self.commit().returncode, 0)

    def test_the_allow_comment_lets_a_line_through(self):
        self.write("fixture.txt", SK + " # " + sg.ALLOW + "\n")
        self.git("add", "fixture.txt")
        self.assertEqual(self.commit().returncode, 0)

    def test_an_unstaged_key_does_not_block_another_file(self):
        self.write("clean.txt", "fine\n")
        self.write("draft.txt", SK + "\n")
        self.git("add", "clean.txt")
        self.assertEqual(self.commit().returncode, 0)

    def test_taking_a_key_out_passes(self):
        self.write("notes.txt", SK + "\nfine\n")
        self.git("add", "notes.txt")
        self.git("commit", "-q", "--no-verify", "-m", "by mistake")
        self.write("notes.txt", "fine\n")
        self.git("add", "notes.txt")
        self.assertEqual(self.commit().returncode, 0)

    def test_all_reports_a_tracked_key_without_showing_it(self):
        self.write("notes.txt", SK + "\n")
        self.git("add", "notes.txt")
        self.git("commit", "-q", "--no-verify", "-m", "by mistake")
        done = subprocess.run([sys.executable, "docs/agent_hub/tools/secret_guard.py", "--all"], cwd=self.repo,
                              capture_output=True, text=True)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("notes.txt:1", done.stderr)
        self.assertNotIn(SK, done.stderr + done.stdout)

    def test_the_hub_itself_passes_a_full_check(self):
        done = subprocess.run([sys.executable, "docs/agent_hub/tools/secret_guard.py", "--all"], cwd=self.repo,
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)


class TestDoctor(WizardBase):
    def fake_gh(self, answer):
        tool = make_tool(self.root / "gh-bin", "gh", "#!/bin/sh\nprintf '%s\\n' '" + answer + "'\n")
        patcher = mock.patch.dict(os.environ, {"PATH": "%s:%s" % (tool.parent, os.environ["PATH"])})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_it_reports_a_tracked_key_without_showing_it(self):
        repo = self.install()
        (repo / "notes.txt").write_text(SK + "\n")
        self.git(repo, "add", "notes.txt")
        self.git(repo, "commit", "-q", "--no-verify", "-m", "by mistake")
        code, out, _ = self.call(str(repo), "--doctor")
        self.assertEqual(code, 1)
        self.assertIn("tracked files may hold a secret", out)
        self.assertIn("notes.txt:1", out)
        self.assertNotIn(SK, out)

    def test_a_clean_repository_passes(self):
        repo = self.install()
        _, out, _ = self.call(str(repo), "--doctor")
        self.assertIn("no key-like strings in tracked files", out)

    def test_it_warns_about_a_public_repository_without_push_protection(self):
        repo = self.install()
        self.git(repo, "remote", "add", "origin", "git@github.com:someone/thing.git")
        self.fake_gh("public\tdisabled")
        _, out, _ = self.call(str(repo), "--doctor")
        self.assertIn("someone/thing is public and GitHub push protection is off", out)

    def test_it_stays_quiet_when_push_protection_is_on_or_the_repository_is_private(self):
        repo = self.install()
        self.git(repo, "remote", "add", "origin", "https://github.com/someone/thing.git")
        for answer in ("public\tenabled", "private\tdisabled", "public\t"):
            self.fake_gh(answer)
            _, out, _ = self.call(str(repo), "--doctor")
            self.assertNotIn("push protection is off", out)

    def test_it_stays_quiet_without_a_github_remote(self):
        repo = self.install()
        self.git(repo, "remote", "add", "origin", "https://example.invalid/someone/thing.git")
        self.fake_gh("public\tdisabled")
        _, out, _ = self.call(str(repo), "--doctor")
        self.assertNotIn("push protection is off", out)


if __name__ == "__main__":
    unittest.main()
