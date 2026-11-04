"""Tests for a machine's tool paths: hub_config.local.json, and (in the kit only, where setup_hub.py
exists) the wizard's search for tools and its dsh patch.  Every test works in a throw-away folder;
the fake tools spend no tokens.  Run:

    python3 -m unittest discover -s docs/agent_hub/tools -p 'test_tool_paths.py' -v
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
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

import job_adapters as ja  # noqa: E402
from test_job_board import Base, T, tok  # noqa: E402

SETUP = HERE.parents[2] / "setup_hub.py"      # an installed hub has no wizard: those tests are skipped there
LOCAL = "docs/agent_hub/hub_config.local.json"
FAKE_LOGIN = '#!/bin/sh\necho \'{"loggedIn": true}\'\n'


def make_tool(folder, name, body="#!/bin/sh\nexit 0\n", mode=0o755):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(body)
    path.chmod(mode)
    return path


class TestLocalConfig(Base):
    def write_local(self, repo, data):
        ja.local_config_path(repo).write_text(json.dumps(data))

    def test_without_a_local_file_the_commands_stay_bare(self):
        repo = self.make_repo()
        self.configure(repo, {"gpt": ["codex", "exec"]})
        self.assertEqual(ja.load_config(repo)["job_agents"], {"gpt": ["codex", "exec"]})

    def test_the_local_file_replaces_only_the_first_word(self):
        repo = self.make_repo()
        self.configure(repo, {"gpt": ["codex", "exec", "codex"], "claude": ["claude", "-p"], "none": []})
        self.write_local(repo, {"tool_paths": {"codex": "/opt/my tools/codex"}})
        self.assertEqual(ja.load_config(repo)["job_agents"],
                         {"gpt": ["/opt/my tools/codex", "exec", "codex"], "claude": ["claude", "-p"], "none": []})

    def test_an_invalid_local_file_is_refused(self):
        repo = self.make_repo()
        self.configure(repo, {"gpt": ["codex"]})
        for bad in ([], {"tool_paths": ["codex"]}, {"tool_paths": {"codex": 1}}):
            self.write_local(repo, bad)
            with self.assertRaises(ValueError):
                ja.load_config(repo)

    def test_the_dispatcher_wakes_the_path_of_this_machine(self):
        repo = self.make_repo()
        tool = make_tool(repo / "elsewhere", "my-tool", '#!/bin/sh\necho "tokens=1"\ntouch "$1"\n')
        self.configure(repo, {"gpt": ["my-tool", "{repo}/ran"]})
        self.write_local(repo, {"tool_paths": {"my-tool": str(tool)}})
        self.set_tasks(repo, [T("T001")])
        self.start_job(repo)
        self.dispatcher(repo, parse_tokens=tok).run(once=True)
        self.assertTrue((repo / "ran").exists(), self.log_lines(repo))


@unittest.skipUnless(SETUP.is_file(), "setup_hub.py exists only in the kit")
class WizardBase(Base):
    def setUp(self):
        super().setUp()
        spec = importlib.util.spec_from_file_location("setup_hub_under_test", SETUP)
        self.wiz = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.wiz)
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.root = Path(td.name).resolve()
        patch_env = mock.patch.dict(os.environ, {"DSH_HOME": str(self.root / "dsh-home")})
        patch_env.start()
        self.addCleanup(patch_env.stop)

    def call(self, *argv):
        """(exit code, stdout, stderr) of the wizard's main()."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.wiz.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def git(self, repo, *args, check=True):
        done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
        if check and done.returncode != 0:
            self.fail(done.stderr)
        return done

    def new_repo(self):
        repo = self.root / "repo"
        repo.mkdir()
        self.git(repo, "init", "-q")
        for key, value in (("user.name", "test"), ("user.email", "test@example.invalid"), ("commit.gpgsign", "false")):
            self.git(repo, "config", key, value)
        self.git(repo, "commit", "-q", "--allow-empty", "-m", "start")
        return repo

    def tool_args(self):
        folder = self.root / "my tools"
        return [arg for name in ("claude", "codex", "dsh")
                for arg in ("--tool-path", "%s=%s" % (name, make_tool(folder, name, FAKE_LOGIN)))]

    def install(self):
        repo = self.new_repo()
        _, out, err = self.call(str(repo), "--yes", "--no-tests", *self.tool_args())
        self.assertNotIn("stopped", err, out)
        self.assertIn("hub committed", out)
        return repo


class TestFindTool(WizardBase):
    def isolate(self, places):
        """PATH holds only an empty folder, and the search looks only in `places`."""
        empty = self.root / "empty"
        empty.mkdir(exist_ok=True)
        for patcher in (mock.patch.dict(os.environ, {"PATH": str(empty)}),
                        mock.patch.dict(self.wiz.TOOL_PLACES, {"dsh": places}, clear=True)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_tool_path_comes_first_then_path_then_the_usual_places(self):
        place = make_tool(self.root / "place", "dsh")
        on_path = make_tool(self.root / "on-path", "dsh")
        self.isolate((str(place),))
        self.assertEqual(self.wiz.find_tool("dsh"), str(place))
        os.environ["PATH"] = str(on_path.parent)
        self.assertEqual(self.wiz.find_tool("dsh"), str(on_path))
        self.assertEqual(self.wiz.find_tool("dsh", {"dsh": "/chosen/dsh"}), "/chosen/dsh")

    def test_several_versions_give_the_newest_install(self):
        old = make_tool(self.root / "nvm" / "v9" / "bin", "dsh")
        new = make_tool(self.root / "nvm" / "v22" / "bin", "dsh")
        os.utime(old, (1000, 1000))
        os.utime(new, (2000, 2000))
        self.isolate((str(self.root / "nvm" / "*" / "bin" / "dsh"),))
        self.assertEqual(self.wiz.find_tool("dsh"), str(new))

    def test_a_file_that_cannot_run_is_not_found(self):
        place = make_tool(self.root / "place", "dsh", mode=0o644)
        self.isolate((str(place),))
        self.assertIsNone(self.wiz.find_tool("dsh"))

    def test_a_folder_with_spaces_is_found(self):
        place = make_tool(self.root / "Deep Seek.app" / "bin", "dsh")
        self.isolate((str(place),))
        self.assertEqual(self.wiz.find_tool("dsh"), str(place))


class TestDshPatch(WizardBase):
    ENABLED = ["- id: credentials", "  disabled: false", "- id: authorization", "  disabled: false",
               "- id: deepseek-account", "  disabled: false"]

    def test_a_fresh_patch_file_loses_its_empty_list(self):
        lines = ["# a comment", "[]"]
        fixed = self.wiz.dsh_fixed(lines)
        self.assertEqual(lines, ["# a comment", "[]"])
        self.assertNotIn("[]", fixed)
        self.assertEqual(fixed[0], "# a comment")
        self.assertEqual(self.wiz.dsh_missing_or_disabled(fixed), [])
        self.assertIn("[]", [name for name, _ in self.wiz.dsh_plan(lines)])

    def test_a_file_broken_by_the_older_wizard_is_reported_and_repaired(self):
        lines = ["[]", *self.ENABLED]
        path = self.wiz.patch_path()
        path.parent.mkdir(parents=True)
        path.write_text("\n".join(lines) + "\n")
        ok, why = self.wiz.dsh_login_ok()
        self.assertFalse(ok)
        self.assertIn("[]", why)
        self.assertEqual([name for name, _ in self.wiz.dsh_plan(lines)], ["[]"])
        self.assertEqual(self.wiz.dsh_fixed(lines), self.ENABLED)

    def test_a_nested_empty_list_is_kept(self):
        lines = [*self.ENABLED, "- id: other", "  args: []"]
        self.assertFalse(self.wiz.dsh_has_empty_list(lines))
        self.assertEqual(self.wiz.dsh_fixed(lines), lines)


class TestInstall(WizardBase):
    def test_commits_bare_names_and_keeps_this_machines_paths_local(self):
        repo = self.install()
        shown = self.git(repo, "show", "HEAD:docs/agent_hub/hub_config.json").stdout
        self.assertEqual({argv[0] for argv in json.loads(shown)["job_agents"].values()}, {"claude", "codex", "dsh"})
        self.assertEqual(self.git(repo, "grep", "-l", str(self.root), "HEAD", check=False).stdout, "")
        self.assertEqual(self.git(repo, "ls-files", LOCAL).stdout, "")
        self.git(repo, "check-ignore", "-q", LOCAL)
        tools = json.loads((repo / LOCAL).read_text())["tool_paths"]
        self.assertEqual(set(tools), {"claude", "codex", "dsh"})
        self.assertTrue(all("my tools" in path for path in tools.values()))

    def test_the_commit_leaves_the_local_file_out_without_the_ignore_line(self):
        with mock.patch.object(self.wiz, "update_gitignore", lambda repo, dry, assume_yes: True):
            repo = self.install()
        self.assertTrue((repo / LOCAL).is_file())
        self.assertEqual(self.git(repo, "ls-files", LOCAL).stdout, "")

    def test_doctor_uses_the_local_paths(self):
        repo = self.install()
        _, out, _ = self.call(str(repo), "--doctor")
        self.assertIn("my tools/dsh is executable", out)

    def test_doctor_reports_a_tracked_local_file(self):
        repo = self.install()
        self.git(repo, "add", "-f", LOCAL)
        self.git(repo, "commit", "-q", "--no-verify", "-m", "by mistake")
        code, out, _ = self.call(str(repo), "--doctor")
        self.assertEqual(code, 1)
        self.assertIn("tracked by git", out)

    def test_doctor_warns_about_a_full_path_in_the_shared_config(self):
        repo = self.install()
        path = repo / "docs/agent_hub/hub_config.json"
        config = json.loads(path.read_text())
        config["job_agents"]["gpt"][0] = sys.executable
        path.write_text(json.dumps(config))
        _, out, _ = self.call(str(repo), "--doctor")
        self.assertIn("holds a full path", out)

    def test_a_bad_tool_path_is_refused(self):
        repo = self.new_repo()
        for bad in ("nope=/bin/sh", "dsh", "dsh=/does/not/exist"):
            with self.assertRaises(SystemExit):
                self.call(str(repo), "--dry-run", "--tool-path", bad)

    def test_doctor_takes_no_tool_path(self):
        repo = self.new_repo()
        with self.assertRaises(SystemExit):
            self.call(str(repo), "--doctor", "--tool-path", "dsh=/bin/sh")


class TestLocalPaths(WizardBase):
    def test_a_clone_records_its_paths_without_touching_git(self):
        repo = self.install()
        (repo / LOCAL).unlink()
        before = self.git(repo, "status", "--porcelain").stdout
        code, _, _ = self.call(str(repo), "--local-paths", *self.tool_args())
        self.assertEqual(code, 0)
        self.assertIn("my tools", (repo / LOCAL).read_text())
        self.assertEqual(self.git(repo, "status", "--porcelain").stdout, before)

    def test_it_refuses_while_git_does_not_ignore_the_file(self):
        repo = self.install()
        (repo / LOCAL).unlink()
        ignore = repo / ".gitignore"
        ignore.write_text(ignore.read_text().replace(LOCAL + "\n", ""))
        code, _, err = self.call(str(repo), "--local-paths", *self.tool_args())
        self.assertEqual(code, 1)
        self.assertIn("not git-ignored", err)
        self.assertFalse((repo / LOCAL).exists())

    def test_a_dry_run_writes_nothing(self):
        repo = self.install()
        (repo / LOCAL).unlink()
        code, out, _ = self.call(str(repo), "--local-paths", "--dry-run", *self.tool_args())
        self.assertEqual(code, 0)
        self.assertIn("nothing was changed", out)
        self.assertFalse((repo / LOCAL).exists())

    def test_it_needs_an_installed_hub(self):
        repo = self.new_repo()
        code, _, err = self.call(str(repo), "--local-paths")
        self.assertEqual(code, 1)
        self.assertIn("install the hub first", err)

    def test_it_keeps_a_path_recorded_earlier(self):
        repo = self.install()
        data = json.loads((repo / LOCAL).read_text())
        data["tool_paths"]["other"] = "/some/other"
        (repo / LOCAL).write_text(json.dumps(data))
        self.call(str(repo), "--local-paths", *self.tool_args())
        self.assertEqual(json.loads((repo / LOCAL).read_text())["tool_paths"]["other"], "/some/other")


class TestHelloReport(WizardBase):
    NESTED = json.dumps({"type": "error", "status": 400, "error": {"type": "invalid_request_error",
                                                                  "message": "That model needs a newer Codex."}})
    CODEX = "\n".join(json.dumps(event) for event in (      # the shape of a real failed `codex exec --json`
        {"type": "thread.started", "thread_id": "x"},
        {"type": "item.completed", "item": {"id": "item_0", "type": "error", "message": "Model metadata not found."}},
        {"type": "turn.started"},
        {"type": "error", "message": NESTED},
        {"type": "turn.failed", "error": {"message": NESTED}}))
    CLAUDE = json.dumps({"type": "result", "subtype": "success", "is_error": True, "result": "No such model."})
    DSH = "\n".join(json.dumps(event) for event in (
        {"type": "session", "sessionId": "x"}, {"type": "status", "phase": "start"}, {"type": "final", "text": ""}))

    def test_codex_reports_its_real_error_once_under_a_stderr_warning(self):
        report = self.wiz.wake_failure(self.CODEX, "Reading additional input from stdin...\n")
        self.assertEqual(report.count("That model needs a newer Codex."), 1)
        self.assertIn("Model metadata not found.", report)
        self.assertIn("Reading additional input", report)
        self.assertNotIn('"type"', report)

    def test_claude_reports_its_result(self):
        self.assertIn("No such model.", self.wiz.wake_failure(self.CLAUDE, ""))

    def test_dsh_reports_stderr_when_its_events_hold_no_error(self):
        self.assertEqual(self.wiz.wake_failure(self.DSH, "dsh: MISSING_CREDENTIAL\n"), "dsh: MISSING_CREDENTIAL")

    def test_plain_output_and_no_output(self):
        self.assertEqual(self.wiz.wake_failure("something went wrong\n", ""), "something went wrong")
        self.assertEqual(self.wiz.wake_failure("", ""), "no output")

    def test_a_good_result_is_no_error(self):
        self.assertEqual(self.wiz.wake_errors(json.dumps({"type": "result", "is_error": False, "result": "Hi"})), [])

    def test_a_list_or_number_on_a_line_is_ignored(self):
        self.assertEqual(self.wiz.wake_errors("[1, 2]\n3\nnot json\n"), [])

    def test_a_key_in_an_error_is_masked(self):
        key = "Zk4mP9qR2tV7xB1nC6dF" + "8hJ3Lw5Ys0"
        report = self.wiz.wake_failure(json.dumps({"type": "error", "message": "rejected api_key: " + key}), "")
        self.assertIn("rejected", report)
        self.assertNotIn(key, report)

    def test_a_long_error_is_cut(self):
        self.assertLess(len(self.wiz.wake_failure(json.dumps({"type": "error", "message": "x " * 5000}), "")), 400)

    def test_hello_shows_the_real_reason(self):
        repo = self.install()
        make_tool(self.root / "my tools", "codex",
                  "#!/bin/sh\nif [ \"$1\" = login ]; then exit 0; fi\ncat <<'EVENTS'\n%s\nEVENTS\n"
                  "echo 'Reading additional input from stdin...' >&2\nexit 1\n" % self.CODEX)
        _, out, _ = self.call(str(repo), "--doctor", "--hello", "--yes")
        self.assertIn("hello: gpt did not answer READY (exit 1)", out)
        self.assertIn("That model needs a newer Codex.", out)


if __name__ == "__main__":
    unittest.main()
