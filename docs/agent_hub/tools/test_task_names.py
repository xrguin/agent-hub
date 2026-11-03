"""T026 tests: task display names, canonical ids at the CLI boundary, new_task --category.

Everything runs on throw-away repositories (tools copied in, real commit-msg hook).
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

TOOLS = str(Path(__file__).resolve().parent)
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import hub_events as events
import hub_state
import hubtx
import new_task
import task_names
from hub_status import render
from install_hooks import install

MAPPING = "docs/agent_hub/TASK_NAMES.tsv"
SEED = "T001\thub\nT002\tloc\nT005\tcheck\nT024\tcollect\n"


class Base(unittest.TestCase):
    seed = SEED
    cards = ()  # extra task card file names committed in the fixture

    def setUp(self):
        # Hygiene: no __pycache__ in the throw-away repository (hooks and CLIs run there).
        patch_env = mock.patch.dict(os.environ, {"PYTHONDONTWRITEBYTECODE": "1"})
        patch_env.start()
        self.addCleanup(patch_env.stop)
        self.temp = tempfile.TemporaryDirectory(prefix="t026-test-")
        self.repo = Path(self.temp.name)
        self.old = os.getcwd()
        self.git("init", "-q")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "tester")
        self.write("docs/agent_hub/DECISIONS.md", "# Decisions\n")
        self.write("docs/agent_hub/QUESTIONS.md", "# Questions\n")
        for tag in ("claude", "gpt", "deepseek"):
            self.write(f"docs/agent_hub/logs/{tag}.md", f"# {tag} log\n")
        (self.repo / "docs/agent_hub/reviews").mkdir(parents=True)
        self.write("pkg/file.txt", "data\n")
        if self.seed is not None:
            self.write(MAPPING, self.seed)
        for name in self.cards:
            self.write("docs/agent_hub/tasks/" + name, "# card\n")
        shutil.copytree(Path(TOOLS), self.repo / "docs/agent_hub/tools",
                        ignore=shutil.ignore_patterns("__pycache__"))
        self.git("add", "-A")
        self.git("commit", "-qm", "fixture")
        os.chdir(self.repo)
        install("gpt", "T012")

    def tearDown(self):
        os.chdir(self.old)
        self.temp.cleanup()

    def write(self, path, text):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def git(self, *args, check=True):
        result = subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True, text=True)
        if check and result.returncode:
            raise RuntimeError(result.stderr)
        return result

    def cli(self, name, *args, check_=True):
        env = os.environ.copy()
        env["PYTHONPATH"] = TOOLS
        result = subprocess.run([sys.executable, TOOLS + "/" + name, *args], cwd=str(self.repo),
                                capture_output=True, text=True, env=env)
        if check_ and result.returncode:
            raise RuntimeError(f"{name}: {result.returncode}\n{result.stdout}{result.stderr}")
        return result

    def new(self, tag, title, *extra, check_=True):
        return self.cli("new_task.py", tag, title, "--owner", tag, "--reviewers", "deepseek",
                        "--deliverables", "pkg/file.txt", *extra, check_=check_)

    def head_files(self):
        return set(self.git("show", "--name-only", "--format=", "HEAD").stdout.split())

    def head_subject(self):
        return self.git("log", "-1", "--format=%s").stdout.strip()


class Library(Base):
    def test_display_and_canonical_round_trip(self):
        self.assertEqual(task_names.display("T024"), "T-collect-024")
        self.assertEqual(task_names.display("T001", self.repo), "T-hub-001")
        for form in ("T024", "T-collect-024", "t-COLLECT-024", "T-Collect-024"):
            self.assertEqual(task_names.canonical(form), "T024")
        for task in ("T001", "T002", "T005", "T024"):
            self.assertEqual(task_names.canonical(task_names.display(task)), task)
        self.assertEqual(task_names.category("T002"), "loc")
        self.assertEqual(task_names.category("T999"), None)

    def test_unmapped_id_is_unchanged_and_flagged(self):
        warnings = []
        self.assertEqual(task_names.display("T777", warnings=warnings), "T777")
        self.assertEqual(len(warnings), 1)
        self.assertIn("T777", warnings[0])
        self.assertEqual(task_names.canonical("T777"), "T777")  # plain ids always pass
        with self.assertRaises(ValueError):
            task_names.canonical("T-hub-777")

    def test_legacy_suffix_ids(self):
        self.assertEqual(task_names.canonical("T005a"), "T005a")
        self.assertEqual(task_names.display("T005a"), "T-check-005a")
        self.assertEqual(task_names.canonical("T-check-005a"), "T005a")
        with self.assertRaises(ValueError):
            task_names.canonical("T-hub-005a")

    def test_uppercase_legacy_suffix_alias(self):
        # F4: the suffix is normalised to lower case in the canonical id.
        for form in ("T-check-005a", "T-CHECK-005A", "t-Check-005A"):
            self.assertEqual(task_names.canonical(form), "T005a", form)
        with self.assertRaises(ValueError):
            task_names.canonical("T-hub-005A")

    def test_mismatch_refused(self):
        for bad in ("T-hub-024", "T-collect-001", "T-HUB-024"):
            with self.assertRaises(ValueError, msg=bad):
                task_names.canonical(bad)

    def test_unknown_category_and_malformed_refused(self):
        for bad in ("T-bogus-024", "T-024", "T--024", "T-collect-24", "collect-024", "T-collect-024x1",
                    "T024 ", "", "t-collect-024-1"):
            if bad == "T024 ":
                self.assertEqual(task_names.canonical(bad), "T024")  # surrounding blanks are trimmed
                continue
            with self.assertRaises(ValueError, msg=repr(bad)):
                task_names.canonical(bad)
        with self.assertRaises(ValueError):
            task_names.canonical(24)

    def test_malformed_mapping_refused(self):
        self.write(MAPPING, SEED + "T030 bogus\n")
        with self.assertRaises(ValueError):
            task_names.load(self.repo)
        self.write(MAPPING, SEED + "T001\tmodel\n")
        with self.assertRaises(ValueError):
            task_names.load(self.repo)

    def test_mapping_file_is_appendable_by_any_tag(self):
        # hubtx.owns() is True for append-only paths inside a transaction (allow_append);
        # outside one it is False for them, as for DECISIONS.md. Check the underlying rule.
        self.assertTrue(hubtx.append_only(MAPPING))
        for tag in ("claude", "gpt", "deepseek"):
            self.assertTrue(hubtx._owns(self.repo, tag, MAPPING, allow_append=True), tag)
            with hubtx.transaction(tag, "T001") as tx:
                tx.append(MAPPING, "T%03d\thub" % (900 + len(tag)))
                self.assertTrue(hubtx.owns(tag, MAPPING))
                self.git("checkout", "--", MAPPING)


class Cli(Base):
    def test_new_task_category_commits_mapping_with_created_event(self):
        for tag in ("gpt", "claude"):
            out = self.new(tag, "Cat task " + tag, "--category", "model").stdout.split()
            task = out[0]
            self.assertEqual(task_names.category(task, self.repo), "model")
            self.assertEqual(self.head_files(), {MAPPING, hub_state.status_path(task),
                                                 "docs/agent_hub/tasks/%s_cat_task_%s.md" % (task, tag)})
            self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), out[1])
            self.assertEqual(self.git("status", "--short").stdout.strip(), "")
        self.assertEqual(self.git("show", "HEAD:" + MAPPING).stdout, SEED + "T025\tmodel\nT026\tmodel\n")
        self.assertEqual(hub_state.derive(self.repo, "T026")["owner"], "claude")

    def test_new_task_requires_category_when_mapping_exists(self):
        before = self.git("rev-parse", "HEAD").stdout
        result = self.new("gpt", "No category", check_=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--category", result.stderr)
        result = self.new("gpt", "Bad category", "--category", "bogus", check_=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.git("rev-parse", "HEAD").stdout, before)
        self.assertEqual(self.git("status", "--short").stdout.strip(), "")

    def test_new_task_depends_on_accepts_display_names(self):
        out = self.new("gpt", "Dependent", "--category", "ctrl",
                       "--depends-on", "T-hub-001,t-COLLECT-024", "--depends-on", "D004").stdout.split()
        text = self.git("show", "HEAD:" + hub_state.status_path(out[0])).stdout
        self.assertIn("T001", text)
        self.assertIn("T024", text)
        self.assertIn("D004", text)
        self.assertNotIn("T-hub", text)
        result = self.new("gpt", "Bad dep", "--category", "ctrl", "--depends-on", "T-hub-024", check_=False)
        self.assertNotEqual(result.returncode, 0)

    def test_claim_and_append_accept_display_names(self):
        task = self.new("gpt", "Claim me", "--category", "hub").stdout.split()[0]
        display = task_names.display(task, self.repo)
        self.assertEqual(display, "T-hub-" + task[1:])
        self.cli("claim.py", "gpt", display, "--scope", "pkg/*")
        self.assertEqual(hub_state.derive(self.repo, task)["state"], "claimed")
        self.assertIn(hub_state.status_path(task), self.head_files())
        self.assertIn("claim " + task, self.head_subject())
        self.assertNotIn("T-hub", self.head_subject())
        # mismatched category and unknown category are refused, nothing is committed
        before = self.git("rev-parse", "HEAD").stdout
        for bad in ("T-collect-" + task[1:], "T-nope-" + task[1:]):
            result = self.cli("claim.py", "gpt", bad, "--release", check_=False)
            self.assertEqual(result.returncode, 6, bad)
        self.assertEqual(self.git("rev-parse", "HEAD").stdout, before)
        self.cli("claim.py", "gpt", display.upper(), "--release")
        self.assertEqual(hub_state.derive(self.repo, task)["state"], "open")

    def test_hub_append_and_commit_store_canonical_id(self):
        task = self.new("gpt", "Append me", "--category", "hub").stdout.split()[0]
        display = task_names.display(task, self.repo)
        self.cli("hub_append.py", "gpt", display, "docs/agent_hub/DECISIONS.md", "- appended")
        self.assertEqual(self.head_subject(), "[gpt] %s: append docs/agent_hub/DECISIONS.md" % task)
        event = events.format_event("gpt", "claim", dict(scope=["pkg/*"]), timestamp=hubtx.now())
        self.cli("hub_append.py", "gpt", display, hub_state.status_path(task), event)
        self.assertIn("claim", self.git("show", "HEAD:" + hub_state.status_path(task)).stdout)
        self.assertEqual(self.head_subject().split()[1], task + ":")
        self.write("pkg/file.txt", "changed\n")
        self.write("docs/agent_hub/logs/gpt.md", "# gpt log\nmore\n")
        self.cli("hub_commit.py", "gpt", display, "-m", "log", "docs/agent_hub/logs/gpt.md")
        self.assertEqual(self.head_subject(), "[gpt] %s: log" % task)
        result = self.cli("hub_append.py", "gpt", "T-collect-" + task[1:], "docs/agent_hub/DECISIONS.md",
                          "- x", check_=False)
        self.assertEqual(result.returncode, 6)
        result = self.cli("hub_commit.py", "gpt", "T-nope-" + task[1:], "-m", "x", "pkg/file.txt", check_=False)
        self.assertEqual(result.returncode, 6)

    def test_status_view_prints_display_names(self):
        task = self.new("gpt", "Shown", "--category", "ctrl").stdout.split()[0]
        text = render(self.repo)
        self.assertIn("T-ctrl-" + task[1:], text)
        self.assertNotIn("== Tasks without a category", text)
        # a task without a category is flagged, not hidden
        self.write("docs/agent_hub/tasks/T090.status.md", self.git("show", "HEAD:" + hub_state.status_path(task)).stdout)
        text = render(self.repo)
        self.assertIn("T090", text)
        self.assertIn("== Tasks without a category", text)


def snapshot(test):
    """Everything an invalid call must leave untouched: HEAD, index, working tree, mapping bytes."""
    return dict(head=test.git("rev-parse", "HEAD").stdout,
                status=test.git("status", "--porcelain", "--untracked-files=all").stdout,
                cached=test.git("diff", "--cached").stdout,
                mapping=(test.repo / MAPPING).read_bytes() if (test.repo / MAPPING).exists() else None)


GOOD = dict(owner="gpt", reviewers=["deepseek"], required="all", kind="code",
            deliverables=["pkg/file.txt"], depends_on=[], initial="open", category="model")


class Validation(Base):
    """F1: all creation input is validated before any write."""

    def test_invalid_kind_cli_leaves_everything_unchanged_then_valid_create_succeeds(self):
        before = snapshot(self)
        result = self.new("gpt", "Bad kind", "--category", "model", "--kind", "not-a-kind", check_=False)
        self.assertEqual(result.returncode, 6)
        self.assertIn("Unknown task kind", result.stderr)
        self.assertEqual(snapshot(self), before)
        self.assertEqual(before["status"], "")
        out = self.new("gpt", "Good kind", "--category", "model", "--kind", "code").stdout.split()
        self.assertEqual(out[0], "T025")
        self.assertEqual(task_names.category("T025", self.repo), "model")
        self.assertEqual(snapshot(self)["status"], "")

    def test_every_invalid_argument_is_refused_before_any_write(self):
        cases = {
            "kind": dict(kind="nope"),
            "owner": dict(owner="nobody", reviewers=[]),
            "unknown reviewer": dict(reviewers=["nobody"]),
            "self review": dict(reviewers=["gpt"]),
            "duplicate reviewer": dict(reviewers=["deepseek", "deepseek"]),
            "required": dict(required="most"),
            "initial": dict(initial="done"),
            "no deliverables": dict(deliverables=[]),
            "unnormalised deliverable": dict(deliverables=["./pkg/../pkg/file.txt"]),
            "dependency": dict(depends_on=["X1"]),
            "category": dict(category="bogus"),
            "empty title": dict(title="  "),
            "newline title": dict(title="two\nlines"),
            "tag": dict(tag="mallory"),
        }
        before = snapshot(self)
        for name, change in cases.items():
            args = dict(GOOD, title="Invalid " + name, tag="gpt")
            args.update(change)
            tag, title = args.pop("tag"), args.pop("title")
            with self.assertRaises(hubtx.BadInput, msg=name):
                new_task.create(tag, title, **args)
            self.assertEqual(snapshot(self), before, name)
        info = new_task.create("gpt", "Valid afterwards", **GOOD)
        self.assertEqual(info["task"], "T025")

    def test_category_outside_the_list_never_reaches_the_mapping(self):
        before = snapshot(self)
        for bad in ("bogus", "Model", "hub\tmodel", ""):
            with self.assertRaises(hubtx.BadInput, msg=repr(bad)):
                new_task.create("gpt", "Cat", **dict(GOOD, category=bad))
            self.assertEqual(snapshot(self), before)
        # task_names.load still rejects a hand-written malformed mapping.
        self.write(MAPPING, SEED + "T030\tbogus\n")
        with self.assertRaises(ValueError):
            task_names.load(self.repo)


class Allocation(Base):
    """F2: the maximum uses the complete number (SEED holds up to T024)."""

    def create(self, **extra):
        return new_task.create("gpt", "Alloc", **dict(GOOD, **extra))["task"]


class AllocationSmall(Allocation):
    def test_small_numbers_keep_three_digits(self):
        self.assertEqual(self.create(), "T025")
        self.assertEqual(self.create(), "T026")


class AllocationSuffix(Allocation):
    cards = ("T040a_legacy.md",)

    def test_suffix_ids_count_by_their_base_number(self):
        self.assertEqual(self.create(), "T041")


class AllocationFileAhead(Allocation):
    cards = ("T030_file.md",)

    def test_file_ahead_of_mapping(self):
        self.assertEqual(self.create(), "T031")


class AllocationFourDigitFile(Allocation):
    cards = ("T1000_file.md",)

    def test_four_digit_file_only(self):
        self.assertEqual(new_task._next_id(self.repo), "T1001")
        info = new_task.create("gpt", "Alloc", **GOOD)
        self.assertEqual(info["task"], "T1001")
        self.assertEqual(self.git("show", "HEAD:" + MAPPING).stdout, SEED + "T1001\tmodel\n")


class AllocationRollover(Allocation):
    cards = ("T999_file.md",)

    def test_nine_nine_nine_rolls_over(self):
        self.assertEqual(self.create(), "T1000")
        self.assertEqual(self.create(), "T1001")


class AllocationMappingOnly(Allocation):
    seed = "T1000\thub\n"

    def test_four_digit_mapping_only(self):
        self.assertEqual(new_task._next_id(self.repo), "T1001")
        info = new_task.create("gpt", "Alloc", **GOOD)
        self.assertEqual(info["task"], "T1001")
        self.assertEqual(self.git("show", "HEAD:" + MAPPING).stdout, "T1000\thub\nT1001\tmodel\n")
        self.assertEqual(self.head_files(), {MAPPING, info["status"], info["card"]})


class AllocationMixed(Allocation):
    seed = "T999\thub\nT1002\tloc\n"
    cards = ("T998_file.md",)

    def test_mapping_four_digit_ahead_of_files(self):
        self.assertEqual(self.create(), "T1003")


class Racing:
    def committed_mapping_append_while_waiting(self, line):
        """Patch new_task so that `line` is appended and committed by another agent after the
        creator has chosen its candidate id and before it holds the lock."""
        real = hubtx.transaction
        done = []

        def racing(tag, task, **kw):
            if not done:
                done.append(task)
                with real("claude", "T010") as other:
                    other.append(MAPPING, line)
                    other.commit([MAPPING], "append mapping")
            return real(tag, task, **kw)

        return mock.patch.object(new_task.hubtx, "transaction", racing), done


class LockedAllocation(Racing, Base):
    """F3: the id is recomputed under the hub lock."""

    def test_mapping_line_committed_while_waiting_is_honoured(self):
        patch, done = self.committed_mapping_append_while_waiting("T050\tcollect")
        with patch:
            info = new_task.create("gpt", "Race", **GOOD)
        self.assertEqual(done, ["T025"])  # the stale candidate
        self.assertEqual(info["task"], "T051")
        self.assertEqual(self.git("show", "HEAD:" + MAPPING).stdout, SEED + "T050\tcollect\nT051\tmodel\n")
        self.assertEqual(self.git("status", "--porcelain").stdout.strip(), "")


class LockedAllocationGpt(Racing, Base):
    seed = "T005\tcheck\n"

    def test_gpt_race_t010_gives_t011(self):
        patch, done = self.committed_mapping_append_while_waiting("T010\tcollect")
        with patch:
            info = new_task.create("gpt", "Race", **GOOD)
        self.assertEqual(done[0], "T006")
        self.assertEqual(info["task"], "T011")
        lines = (self.repo / MAPPING).read_text().splitlines()
        self.assertEqual(lines, ["T005\tcheck", "T010\tcollect", "T011\tmodel"])
        self.assertEqual(self.git("status", "--porcelain").stdout.strip(), "")


class LockedCategoryCheck(Racing, Base):
    seed = None  # no mapping at first

    def test_category_required_is_rechecked_under_the_lock(self):
        # A mapping appears (committed) while the creator waits.
        patch, done = self.committed_mapping_append_while_waiting("T900\thub")
        with patch:
            with self.assertRaises(hubtx.BadInput):
                new_task.create("gpt", "No cat", **dict(GOOD, category=None))
        self.assertEqual(self.git("status", "--porcelain").stdout.strip(), "")
        self.assertEqual(self.git("show", "HEAD:" + MAPPING).stdout, "T900\thub\n")


class LegacyRepo(Base):
    seed = None  # a repository with no mapping file keeps the old behaviour

    def test_plain_ids_work_and_category_is_optional(self):
        self.assertEqual(task_names.canonical("T001"), "T001")
        self.assertEqual(task_names.display("T001"), "T001")
        out = self.new("gpt", "Legacy").stdout.split()
        self.assertNotIn(MAPPING, self.head_files())
        self.cli("claim.py", "gpt", out[0], "--scope", "pkg/*")
        # giving --category creates the mapping file
        out = self.new("gpt", "First mapped", "--category", "hub").stdout.split()
        self.assertIn(MAPPING, self.head_files())
        self.assertEqual(task_names.category(out[0], self.repo), "hub")
        self.assertNotEqual(self.new("gpt", "Now required", check_=False).returncode, 0)


if __name__ == "__main__":
    unittest.main()
