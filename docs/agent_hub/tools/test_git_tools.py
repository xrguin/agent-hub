"""T012 acceptance: real subprocesses/Git indexes in throw-away repositories.

No test command runs in or installs hooks into the real project repository.
"""
from __future__ import annotations
import itertools
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from fnmatch import fnmatchcase

import hub_events as ev
import hubtx as tx
import install_hooks

HERE = Path(__file__).resolve().parent
HUB = "docs/agent_hub/"
STAMP = "2026-10-31T14:30:00-04:00"


def created(owner="gpt", reviewers=("deepseek",), deliverables=("pkg/owned.py",)):
    return ev.format_event("gpt", "created", dict(owner=owner, reviewers=list(reviewers),
        required="all", kind="code", deliverables=list(deliverables), depends_on=[], initial="open"), timestamp=STAMP)


class GitTools(unittest.TestCase):
    def setUp(self):
        self.original_cwd = Path.cwd()
        self.temp = tempfile.TemporaryDirectory(prefix="t012-test-")
        self.root = Path(self.temp.name)
        self.environment = patch.dict(os.environ, {"GIT_CONFIG_NOSYSTEM":"1", "GIT_CONFIG_GLOBAL":os.devnull,
                                                   "HUB_LOCK_TIMEOUT":"0.25",
                                                   "PYTHONDONTWRITEBYTECODE":"1"})
        self.environment.start()
        for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE", "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                    "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "HUB_TX_APPENDS", "HUB_TX_NEW"):
            os.environ.pop(key, None)
        os.chdir(self.root)
        self.git("init", "-q")
        self.git("config", "user.name", "Fixture User")
        self.git("config", "user.email", "fixture@example.test")
        self.git("config", "commit.gpgsign", "false")
        self.write("pkg/owned.py", "old owned\n")
        self.write("pkg/foreign.py", "old foreign\n")
        for tag in ev.TAGS:
            self.write(HUB+"logs/"+tag+".md", "log baseline\n")
            self.write(HUB+"reviews/2026-10-31_T012_"+tag+".md", "review baseline\n")
        self.write(HUB+"DECISIONS.md", '# Decisions\n\n### D001 · time · recorded by gpt\n- quote: "approved fixture"\n- scope: fixture only\n')
        self.write(HUB+"INDEX.md", "# Index\n")
        self.write(HUB+"tasks/T012_git.md", "Owner: [gpt]\n")
        scopes = ["pkg/owned*.py", "work/**", HUB+"tools/*.py", HUB+"hub_config.json"]
        self.write(HUB+"tasks/T012.status.md", created()+"\n"+ev.format_event("gpt","claim",dict(scope=scopes),timestamp=STAMP)+"\n")
        self.write(HUB+"tasks/T013.status.md", created("claude",("gpt",),("pkg/foreign.py",))+"\n"+
                   ev.format_event("claude","claim",dict(scope=["pkg/foreign.py","foreign/**"]),timestamp=STAMP)+"\n")
        for name in ("hubtx.py", "hub_events.py", "hub_append.py", "hub_commit.py", "commit_msg_hook.py", "install_hooks.py"):
            self.write(HUB+"tools/"+name, (HERE/name).read_text())
        self.git("add", "--", *[p.relative_to(self.root).as_posix() for p in self.root.rglob("*") if p.is_file() and ".git" not in p.relative_to(self.root).parts])
        self.git("commit", "-qm", "fixture baseline")

    def tearDown(self):
        os.chdir(self.original_cwd)
        self.environment.stop()
        self.temp.cleanup()

    def write(self, path, text):
        target = self.root/path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def git(self, *args, check=True, env=None):
        environment = os.environ.copy()
        if env:
            environment.update(env)
        result = subprocess.run(["git", "-C", str(self.root), *args], capture_output=True, env=environment)
        if check and result.returncode:
            self.fail(result.stderr.decode(errors="replace"))
        return result

    def cli(self, name, *args, env=None):
        environment = os.environ.copy()
        if env:
            environment.update(env)
        return subprocess.run([sys.executable, str(HERE/name), *args], cwd=self.root,
                              capture_output=True, env=environment, timeout=8)

    def head(self):
        return self.git("rev-parse","HEAD").stdout

    def holder(self):
        code = """import sys,signal,os
sys.path.insert(0,sys.argv[1])
import hubtx
with hubtx.transaction('claude','T013'):
 print('ready',flush=True)
 signal.pause()
"""
        p = subprocess.Popen([sys.executable,"-c",code,str(HERE)], cwd=self.root,
                             stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=os.environ.copy())
        self.addCleanup(lambda: self.stop_holder(p))
        ready,_,_ = select.select([p.stdout],[],[],5)
        self.assertTrue(ready, "Lock holder did not start")
        self.assertEqual(p.stdout.readline(),b"ready\n")
        return p

    @staticmethod
    def stop_holder(p):
        if p.poll() is None:
            p.kill()
        p.communicate(timeout=5)

    def install(self):
        return install_hooks.install()

    def test_live_old_paused_lock_is_not_displaced_and_death_frees_it(self):
        p = self.holder()
        lock = self.root/".git/hub.lock"
        inode = lock.stat().st_ino
        os.utime(lock,(0,0))
        os.kill(p.pid,signal.SIGSTOP)
        with self.assertRaises(tx.Busy):
            with tx.transaction("gpt","T012",timeout=.1):
                self.fail("Displaced a paused live holder")
        self.assertIsNone(p.poll())
        self.assertEqual(lock.stat().st_ino,inode)
        self.stop_holder(p)
        with tx.transaction("gpt","T012",timeout=.5):
            pass
        self.assertEqual(lock.stat().st_ino,inode)

    def test_nested_transaction_is_rejected_without_releasing_outer(self):
        with tx.transaction("gpt","T012") as outer:
            with self.assertRaises(tx.BadInput):
                with tx.transaction("gpt","T012"):
                    pass
            self.assertTrue(outer.active)
        with self.assertRaises(tx.BadInput):
            outer.append(HUB+"INDEX.md","stale transaction")

    def test_commit_preserves_foreign_staging_with_real_hook(self):
        self.install()
        self.write("pkg/owned.py","new owned\n")
        self.write(HUB+"logs/claude.md","foreign staged log\n")
        self.git("add","--",HUB+"logs/claude.md")
        before = self.git("show",":"+HUB+"logs/claude.md").stdout
        result = self.cli("hub_commit.py","gpt","T012","-m","owned only","pkg/owned.py")
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(self.git("show","HEAD:pkg/owned.py").stdout,b"new owned\n")
        self.assertEqual(self.git("show","HEAD:"+HUB+"logs/claude.md").stdout,b"log baseline\n")
        self.assertEqual(self.git("show",":"+HUB+"logs/claude.md").stdout,before)
        self.assertEqual(self.git("diff","--cached","--name-only").stdout.decode().splitlines(),[HUB+"logs/claude.md"])

    def test_new_file_is_added_without_foreign_staging(self):
        self.write("pkg/foreign.py","foreign staged\n")
        self.git("add","--","pkg/foreign.py")
        with tx.transaction("gpt","T012") as t:
            t.write_new("work/new.txt","new\n")
            t.commit(["work/new.txt"],"create")
        self.assertEqual(self.git("show","HEAD:work/new.txt").stdout,b"new\n")
        self.assertEqual(self.git("show","HEAD:pkg/foreign.py").stdout,b"old foreign\n")
        self.assertEqual(self.git("show",":pkg/foreign.py").stdout,b"foreign staged\n")

    def test_foreign_claim_log_review_and_unclaimed_paths_are_refused(self):
        for path in ("pkg/foreign.py", HUB+"logs/claude.md", HUB+"reviews/2026-10-31_T012_deepseek.md", "unclaimed.txt"):
            with self.subTest(path=path), tx.transaction("gpt","T012") as t:
                self.assertFalse(tx.owns("gpt",path))
                with self.assertRaises(tx.Unauthorized):
                    t.commit([path],"forbidden")
        self.assertTrue(tx.owns("gpt",HUB+"logs/gpt.md"))
        self.assertTrue(tx.owns("gpt",HUB+"reviews/2026-10-31_T012_gpt.md"))

    def test_clean_append_is_exact_one_file_and_preserves_staging(self):
        self.install()
        target = HUB+"DECISIONS.md"
        before = (self.root/target).read_bytes()
        self.write("pkg/foreign.py","foreign staged\n")
        self.git("add","--","pkg/foreign.py")
        result = self.cli("hub_append.py","gpt","T012",target,"- exact added line")
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual((self.root/target).read_bytes(),before+b"- exact added line\n")
        self.assertEqual(self.git("diff-tree","--no-commit-id","--name-only","-r","HEAD").stdout.decode().splitlines(),[target])
        self.assertEqual(self.git("diff","--cached","--name-only").stdout,b"pkg/foreign.py\n")

    def test_dirty_worktree_append_refuses_without_overwriting_foreign_line(self):
        path = HUB+"INDEX.md"; self.write(path,"# Index\nforeign\n")
        before, head = (self.root/path).read_bytes(), self.head()
        result = self.cli("hub_append.py","gpt","T012",path,"mine")
        self.assertEqual(result.returncode,4,result.stderr)
        self.assertEqual((self.root/path).read_bytes(),before)
        self.assertEqual(self.head(),head)

    def test_dirty_index_append_refuses_even_if_worktree_matches_head(self):
        path = HUB+"INDEX.md"; self.write(path,"# Index\nstaged foreign\n")
        self.git("add","--",path)
        self.write(path,"# Index\n")
        with tx.transaction("gpt","T012") as t:
            with self.assertRaises(tx.DirtyTarget):
                t.append(path,"mine")
        self.assertEqual(self.git("show",":"+path).stdout,b"# Index\nstaged foreign\n")
        self.assertEqual((self.root/path).read_bytes(),b"# Index\n")

    def test_shared_append_cannot_be_smuggled_through_commit(self):
        path = HUB+"INDEX.md"; self.write(path,"# Index\nunverified\n")
        with tx.transaction("gpt","T012") as t:
            with self.assertRaises(tx.Unauthorized):
                t.commit([path],"raw append")

    def test_append_tamper_after_receipt_is_refused(self):
        for path in (HUB+"INDEX.md",HUB+"logs/gpt.md"):
            with self.subTest(path=path), tx.transaction("gpt","T012") as t:
                t.append(path,"authorized")
                with (self.root/path).open("a") as f:
                    f.write("foreign after receipt\n")
                with self.assertRaises(tx.DirtyTarget):
                    t.commit([path],"must refuse")

    def test_multiline_decision_block_is_one_exact_atomic_append(self):
        path = HUB+"DECISIONS.md"; before = (self.root/path).read_bytes()
        block = '### D002 · time\n- quote: "yes"\n- scope: test'
        with tx.transaction("gpt","T012") as t:
            t.append(path,block); t.commit([path],"decision block")
        self.assertEqual((self.root/path).read_bytes(),before+block.encode()+b"\n")

    def test_two_process_appends_are_serialized_into_separate_exact_commits(self):
        self.install()
        path=HUB+"INDEX.md"
        processes=[]
        for tag,task,line in (("gpt","T012","gpt line"),("claude","T013","claude line")):
            environment=os.environ.copy(); environment["HUB_LOCK_TIMEOUT"]="2"
            p=subprocess.Popen([sys.executable,str(HERE/"hub_append.py"),tag,task,path,line],
                               cwd=self.root,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=environment)
            processes.append(p)
        for p in processes:
            out,err=p.communicate(timeout=8)
            self.assertEqual(p.returncode,0,err)
        self.assertEqual(set((self.root/path).read_text().splitlines()),{"# Index","gpt line","claude line"})
        for sha in self.git("log","-2","--format=%H").stdout.decode().splitlines():
            self.assertEqual(self.git("diff-tree","--no-commit-id","--numstat","-r",sha).stdout.decode(),"1\t0\t"+path+"\n")

    def test_append_rejects_mode_tampering(self):
        path=HUB+"INDEX.md"
        with tx.transaction("gpt","T012") as t:
            t.append(path,"mine")
            (self.root/path).chmod(0o755)
            with self.assertRaises(tx.DirtyTarget):
                t.commit([path],"mode changed")

    def test_write_new_is_exclusive_and_checks_ownership_before_writing(self):
        with tx.transaction("gpt","T012") as t:
            with self.assertRaises(FileExistsError):
                t.write_new("pkg/owned.py","replace")
            with self.assertRaises(tx.Unauthorized):
                t.write_new("foreign/new.txt","foreign")
        self.assertEqual((self.root/"pkg/owned.py").read_text(),"old owned\n")
        self.assertFalse((self.root/"foreign/new.txt").exists())

    def test_path_traversal_symlink_aliases_and_directory_commits_are_refused(self):
        with tempfile.TemporaryDirectory(prefix="t012-outside-") as outside:
            (self.root/"outside").symlink_to(outside,target_is_directory=True)
            (self.root/"alias").symlink_to(self.root/"pkg",target_is_directory=True)
            for path in ("../escape", "pkg/../../escape", str(Path(outside)/"escape"), "C:\\escape", ".git/config", "outside/escape", "alias/owned.py"):
                with self.subTest(path=path), tx.transaction("gpt","T012") as t:
                    with self.assertRaises(tx.BadInput):
                        t.write_new(path,"forbidden")
            self.assertFalse((Path(outside)/"escape").exists())
        with tx.transaction("gpt","T012") as t:
            with self.assertRaises(tx.BadInput):
                t.commit(["pkg"],"directory forbidden")
            with self.assertRaises(tx.BadInput):
                t.commit(iter(()),"empty")

    def test_literal_pathspec_names_do_not_expand(self):
        with tx.transaction("gpt","T012") as t:
            t.write_new("work/a*.txt","star\n")
            t.write_new("work/another.txt","uncommitted\n")
            t.commit(["work/a*.txt"],"literal filename")
        self.assertEqual(self.git("ls-tree","--name-only","-r","HEAD","--","work").stdout,b"work/a*.txt\n")

    def test_foreign_delete_and_rename_both_endpoints_are_refused(self):
        self.install()
        self.git("mv","--","pkg/foreign.py","pkg/owned-renamed.py")
        before = self.git("diff","--cached","--binary").stdout
        with tx.transaction("gpt","T012") as t:
            with self.assertRaises(tx.Unauthorized):
                t.commit(["pkg/foreign.py","pkg/owned-renamed.py"],"foreign rename")
        result = self.git("commit","-m","[gpt] T012: foreign rename",check=False)
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(self.git("diff","--cached","--binary").stdout,before)
        self.git("rm","--",HUB+"logs/claude.md")
        result = self.git("commit","-m","[gpt] T012: foreign delete",check=False)
        self.assertNotEqual(result.returncode,0)

    def test_owned_rename_and_delete_work_with_hook(self):
        self.install()
        self.git("mv","--","pkg/owned.py","pkg/owned-renamed.py")
        with tx.transaction("gpt","T012") as t:
            t.commit(["pkg/owned.py","pkg/owned-renamed.py"],"own rename")
        self.git("rm","--","pkg/owned-renamed.py")
        with tx.transaction("gpt","T012") as t:
            t.commit(["pkg/owned-renamed.py"],"own delete")
        self.assertEqual(self.git("ls-tree","--name-only","HEAD","--","pkg/owned-renamed.py").stdout,b"")

    def test_hook_reads_alternate_candidate_index_not_normal_index(self):
        self.write("pkg/owned.py","good staged\n")
        self.git("add","--","pkg/owned.py")
        self.write(HUB+"logs/claude.md","bad candidate\n")
        alt = str(self.root/".git/candidate.index")
        env = {"GIT_INDEX_FILE":alt}
        self.git("read-tree","HEAD",env=env)
        self.git("add","--",HUB+"logs/claude.md",env=env)
        message = self.root/".git/test-message"; message.write_text("[gpt] T012: candidate\n")
        result = self.cli("commit_msg_hook.py",str(message),env=env)
        self.assertEqual(result.returncode,3,result.stderr)
        self.assertEqual(self.git("diff","--cached","--name-only").stdout,b"pkg/owned.py\n")

    def test_existing_hook_and_hooks_path_are_preserved(self):
        hook = self.root/".git/hooks/commit-msg"
        hook.write_text("existing bytes\n")
        with self.assertRaisesRegex(tx.BadInput,"preserve"):
            self.install()
        self.assertEqual(hook.read_text(),"existing bytes\n")
        self.git("config","core.hooksPath","custom-hooks")
        with self.assertRaisesRegex(tx.BadInput,"hooksPath"):
            self.install()
        self.assertEqual(self.git("config","--get","core.hooksPath").stdout,b"custom-hooks\n")
        self.assertFalse((self.root/"custom-hooks/commit-msg").exists())

    def test_installed_hook_is_executable_even_with_restrictive_umask(self):
        previous=os.umask(0o111)
        try:
            hook=self.install()
        finally:
            os.umask(previous)
        self.assertEqual(hook.stat().st_mode & 0o777,0o755)

    def test_hook_failure_keeps_new_files_and_foreign_index_recoverable(self):
        hook = self.root/".git/hooks/commit-msg"; hook.write_text("#!/bin/sh\nexit 1\n"); hook.chmod(0o755)
        self.write("pkg/foreign.py","foreign staged\n"); self.git("add","--","pkg/foreign.py")
        head = self.head()
        with tx.transaction("gpt","T012") as t:
            t.write_new("work/recover.txt","keep me\n")
            with self.assertRaises(tx.GitFailure):
                t.commit(["work/recover.txt"],"hook fails")
        self.assertEqual(self.head(),head)
        self.assertEqual((self.root/"work/recover.txt").read_bytes(),b"keep me\n")
        self.assertEqual(self.git("show",":pkg/foreign.py").stdout,b"foreign staged\n")
        self.assertEqual(self.git("show",":work/recover.txt").stdout,b"keep me\n")
        with tx.transaction("gpt","T012",timeout=.1):
            pass

    def test_append_hook_failure_leaves_exact_uncommitted_append(self):
        hook=self.root/".git/hooks/commit-msg"; hook.write_text("#!/bin/sh\nexit 1\n"); hook.chmod(0o755)
        path=HUB+"INDEX.md"; before=(self.root/path).read_bytes(); head=self.head()
        result=self.cli("hub_append.py","gpt","T012",path,"recoverable")
        self.assertEqual(result.returncode,5,result.stderr)
        self.assertEqual(self.head(),head)
        self.assertEqual((self.root/path).read_bytes(),before+b"recoverable\n")

    def test_git_index_lock_failure_leaves_files_and_foreign_staging_unchanged(self):
        self.write("pkg/owned.py","owned pending\n")
        self.write("pkg/foreign.py","foreign staged\n"); self.git("add","--","pkg/foreign.py")
        lock=self.root/".git/index.lock"; lock.write_bytes(b"another Git process\n")
        before=self.head()
        with tx.transaction("gpt","T012") as t:
            with self.assertRaises(tx.GitFailure):
                t.commit(["pkg/owned.py"],"git fails")
        self.assertEqual(self.head(),before)
        self.assertEqual((self.root/"pkg/owned.py").read_bytes(),b"owned pending\n")
        self.assertEqual(self.git("show",":pkg/foreign.py").stdout,b"foreign staged\n")
        self.assertEqual(lock.read_bytes(),b"another Git process\n")

    def test_author_is_off_by_default_and_uncommitted_config_cannot_enable_it(self):
        self.write(HUB+"hub_config.json",json.dumps({"author_per_agent":True}))
        self.assertFalse(tx.config()["author_per_agent"])
        self.write("pkg/owned.py","change\n")
        with tx.transaction("gpt","T012") as t:
            t.commit(["pkg/owned.py"],"default identity")
        self.assertEqual(self.git("show","-s","--format=%an <%ae>|%cn <%ce>","HEAD").stdout.strip(),
                         b"Fixture User <fixture@example.test>|Fixture User <fixture@example.test>")

    def test_config_changes_need_user_event_and_author_only_activates_after_commit(self):
        path=HUB+"hub_config.json"; self.write(path,json.dumps({"author_per_agent":True}))
        with tx.transaction("gpt","T012") as t:
            with self.assertRaises(tx.Unauthorized):
                t.commit([path],"unapproved")
        event=ev.format_event("gpt","user",dict(decision="D001",action="set_config",key="author_per_agent",value="true"))
        with tx.transaction("gpt","T012") as t:
            t.append(HUB+"tasks/T012.status.md",event); t.commit([HUB+"tasks/T012.status.md"],"record approval")
        with tx.transaction("gpt","T012") as t:
            t.commit([path],"approved config")
        self.assertEqual(self.git("show","-s","--format=%an","HEAD").stdout.strip(),b"Fixture User")
        self.write("pkg/owned.py","enabled\n")
        with tx.transaction("gpt","T012") as t:
            t.commit(["pkg/owned.py"],"approved identity")
        self.assertEqual(self.git("show","-s","--format=%an <%ae>|%cn <%ce>","HEAD").stdout.strip(),
                         b"gpt-agent <gpt@agents.local>|Fixture User <fixture@example.test>")

    def test_latest_config_approval_uses_commit_order_not_filename_order(self):
        status=HUB+"tasks/T099.status.md"
        with tx.transaction("gpt","T012") as t:
            t.write_new(status,created()+"\n"); t.commit([status],"create config fixture")
        first=ev.format_event("gpt","user",dict(decision="D001",action="set_config",key="author_per_agent",value="true"))
        with tx.transaction("gpt","T012") as t:
            t.append(status,first); t.commit([status],"first approval")
        path=HUB+"hub_config.json"; self.write(path,json.dumps({"author_per_agent":True}))
        with tx.transaction("gpt","T012") as t:
            t.commit([path],"enable in fixture")
        later=ev.format_event("gpt","user",dict(decision="D001",action="set_config",key="author_per_agent",value="false"))
        with tx.transaction("gpt","T012") as t:
            t.append(HUB+"tasks/T012.status.md",later); t.commit([HUB+"tasks/T012.status.md"],"later approval")
        self.write(path,json.dumps({"author_per_agent":False}))
        with tx.transaction("gpt","T012") as t:
            t.commit([path],"disable in fixture")
        self.assertFalse(tx.config()["author_per_agent"])

    def test_created_task_can_be_assigned_to_another_owner_but_not_rewritten(self):
        self.install()
        card=HUB+"tasks/T099_new.md"; status=HUB+"tasks/T099.status.md"
        with tx.transaction("gpt","T012") as t:
            t.write_new(card,"Owner: [deepseek]\n")
            t.write_new(status,created("deepseek",("claude",))+"\n")
            t.commit([card,status],"create assigned task")
        self.assertFalse(tx.owns("gpt",card))
        self.assertTrue(tx.owns("deepseek",card))

    def test_status_actor_checks_and_overlapping_claim(self):
        status=HUB+"tasks/T012.status.md"
        with tx.transaction("gpt","T012") as t:
            for line in (ev.format_event("claude","release"), ev.format_event("gpt","accept",dict(rev="a"*64))):
                with self.assertRaises(tx.Unauthorized):
                    t.append(status,line)
        other=HUB+"tasks/T099.status.md"
        with tx.transaction("gpt","T012") as t:
            t.write_new(other,created()+"\n"); t.commit([other],"create claim fixture")
        with tx.transaction("gpt","T099") as t:
            with self.assertRaises(tx.Unauthorized):
                t.append(other,ev.format_event("gpt","claim",dict(scope=["pkg/f*.py"])))

    def test_tool_does_not_append_a_duplicate_existing_event(self):
        with tx.transaction("gpt","T012") as t:
            with self.assertRaisesRegex(tx.BadInput,"Duplicate"):
                t.append(HUB+"tasks/T012.status.md",created())

    def test_open_task_reviewer_cannot_become_its_owner(self):
        path=HUB+"tasks/T099.status.md"
        with tx.transaction("gpt","T012") as t:
            t.write_new(path,created("open",("gpt",))+"\n"); t.commit([path],"unassigned fixture")
        with tx.transaction("gpt","T099") as t:
            with self.assertRaises(tx.Unauthorized):
                t.append(path,ev.format_event("gpt","claim",dict(scope=["work/new/**"])))

    def test_user_event_requires_a_nonempty_unambiguous_decision_quote(self):
        for identifier in ("D999",):
            event=ev.format_event("gpt","user",dict(decision=identifier,action="open",task="T012"))
            with tx.transaction("gpt","T012") as t:
                with self.assertRaises(tx.Unauthorized):
                    t.append(HUB+"tasks/T012.status.md",event)
        for text in ('### D002 · time\n- quote: ""\n', '### D003 · time\n- quote: "first"\n### D003 · time\n- quote: "second"\n'):
            identifier="D002" if "D002" in text else "D003"
            with tx.transaction("gpt","T012") as t:
                t.append(HUB+"DECISIONS.md",text); t.commit([HUB+"DECISIONS.md"],"decision fixture")
            event=ev.format_event("gpt","user",dict(decision=identifier,action="open",task="T012"))
            with tx.transaction("gpt","T012") as t:
                with self.assertRaises(tx.Unauthorized):
                    t.append(HUB+"tasks/T012.status.md",event)

    def test_cli_exit_codes_and_bad_arguments(self):
        self.assertEqual(self.cli("hub_commit.py","gpt","T012","-m","no","pkg/foreign.py").returncode,3)
        self.assertEqual(self.cli("hub_append.py","gpt","T012","../escape","no").returncode,6)
        self.assertEqual(self.cli("hub_commit.py").returncode,6)
        p=self.holder()
        try:
            self.assertEqual(self.cli("hub_commit.py","gpt","T012","-m","busy","pkg/owned.py",env={"HUB_LOCK_TIMEOUT":"0.1"}).returncode,2)
        finally:
            self.stop_holder(p)


class EventGrammar(unittest.TestCase):
    def test_literal_wire_sample_and_all_event_field_sets(self):
        wire=STAMP+"\tgpt\tclaim\tscope=work/a%2Cb%25%3B%3D.py,work/z.py\tnote"
        records,warnings=ev.parse_text(wire)
        self.assertEqual(warnings,[])
        self.assertEqual(records[0]["fields"]["scope"],["work/a,b%;=.py","work/z.py"])
        rev="a"*64; file=HUB+"reviews/2026-10-31_T012_deepseek.md"
        examples={"release":{},"submit":{"rev":rev},"review":{"file":file,"rev":rev},
                  "request_changes":{"file":file},"accept":{"rev":rev},
                  "response":{"finding":file+"#F1"},"disposition":{"finding":file+"#F1","result":"resolved"},
                  "dispute":{"reproduction":"work/reproduce.py"}}
        for kind,fields in examples.items():
            with self.subTest(kind=kind):
                line=ev.format_event("deepseek",kind,fields,timestamp=STAMP)
                records,warnings=ev.parse_text(line)
                self.assertEqual(warnings,[])
                self.assertEqual(records[0]["fields"],fields)
        for action,extra in {"open":{"task":"T012"},"cancel":{"task":"T012"},
                             "accept_limitation":{"finding":file+"#F1"},
                             "resolve_dispute":{"task":"T012","to":"review"},
                             "approve_batch":{"task":"T012","scope":["work/**"],"limits":"runs=2;seconds=30"},
                             "set_config":{"key":"author_per_agent","value":"false"}}.items():
            fields=dict(decision="D001",action=action,**extra)
            line=ev.format_event("gpt","user",fields,timestamp=STAMP)
            self.assertEqual(ev.parse_text(line)[0][0]["fields"],fields)

    def test_percent_encoding_lists_notes_and_line_order_round_trip(self):
        fields=dict(owner="gpt",reviewers=["claude","deepseek"],required="all",kind="code",
                    deliverables=["work/a,b%;=\t\n.py"],depends_on=["T001","D002"],initial="open")
        line=ev.format_event("gpt","created",fields,"note\tline\nend",timestamp=STAMP)
        self.assertEqual(line.count("\t"),4)
        parsed,warnings=ev.parse_text(line+"\n"+ev.format_event("gpt","claim",dict(scope=["work/**"]),timestamp="2020-01-01T00:00:00+00:00"))
        self.assertEqual(warnings,[])
        self.assertEqual(parsed[0]["fields"],fields)
        self.assertEqual(parsed[0]["note"],"note line end")
        self.assertEqual([x["event"] for x in parsed],["created","claim"])
        scalar=dict(decision="D001",action="set_config",key="example",value="%;=,\t\n")
        encoded=ev.format_event("gpt","user",scalar)
        self.assertEqual(ev.parse_text(encoded)[0][0]["fields"],scalar)

    def test_malformed_unknown_duplicate_and_self_reviewer_are_reported(self):
        line=created()
        _,warnings=ev.parse_text(line+"\n"+line+"\nmalformed\n"+line.replace("\tcreated\t","\tstate\t")+"\n"+line.replace("kind=code","kind=%ZZ"))
        self.assertEqual(len(warnings),4)
        with self.assertRaises(ValueError):
            created("gpt",("gpt",))
        with self.assertRaises(ValueError):
            ev.format_event("gpt","claim",dict(scope=["../escape"]))
        with self.assertRaises(ValueError):
            ev.format_event("gpt","submit",dict(rev="not a hash"))

    def test_glob_intersection_matches_independent_finite_enumeration(self):
        patterns=["a*","b*","*a","*b","?","[ab]","[!a]","a?","[a-c]","a/*"]
        words=["".join(x) for n in range(6) for x in itertools.product("abc/",repeat=n)]
        for a,b in itertools.product(patterns,repeat=2):
            expected=any(fnmatchcase(w,a) and fnmatchcase(w,b) for w in words)
            self.assertEqual(ev.scopes_overlap(a,b),expected,(a,b))
        self.assertTrue(ev.scopes_overlap("pkg/*.py","pkg/test*"))
        self.assertFalse(ev.scopes_overlap("pkg/*.py","pkg/*.md"))
        self.assertTrue(ev.scopes_overlap("work/[α-ω]","work/[β-γ]"))
        self.assertFalse(ev.scopes_overlap("work/[a-c]","work/[x-z]"))

    def test_bad_utf8_line_is_reported_without_losing_valid_events(self):
        with tempfile.TemporaryDirectory(prefix="t012-grammar-") as folder:
            path=Path(folder)/"T001.status.md"
            path.write_bytes(b"bad \xff line\n"+created().encode()+b"\n")
            records,warnings=ev.parse_file(path)
            self.assertEqual(len(warnings),1)
            self.assertEqual(len(records),1)
            self.assertEqual(records[0]["event"],"created")

    def test_unicode_line_separator_is_not_a_posix_wire_newline(self):
        line=ev.format_event("gpt","release",note="unicode\u2028separator",timestamp=STAMP)
        records,warnings=ev.parse_text(line)
        self.assertEqual(warnings,[])
        self.assertEqual(records[0]["note"],"unicode\u2028separator")


if __name__ == "__main__":
    unittest.main()
