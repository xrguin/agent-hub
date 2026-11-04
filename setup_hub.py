#!/usr/bin/env python3
"""Set up the agent hub from the starter kit in your repository (T050).

    python3 setup_hub.py REPO [--yes] [--dry-run] [--no-tests] [--hello]   install the hub
                              [--tool-path TOOL=PATH ...]
    python3 setup_hub.py REPO --local-paths [--dry-run] [--tool-path ...]  record this machine's tool paths
                                                       (for a clone that already has the hub)
    python3 setup_hub.py REPO --doctor [--tests] [--hello]                 check an installed hub
    python3 setup_hub.py --fix-deepseek [--yes]                            enable dsh's stored credentials
                                                       (alone; without --yes and a terminal: preview only)

The kit folder is the folder of this script.  Python 3.11+, standard library only.  The wizard never
asks for, reads or stores a password or an API key, and it never pushes.  It writes only inside REPO
(it refuses symlinked docs, .gitignore, CLAUDE.md, AGENTS.md); --fix-deepseek alone edits one dsh file.
A tool not on PATH is looked for in the usual install places.  Full paths go only in the git-ignored
hub_config.local.json; the committed hub_config.json keeps the bare names, so git shares no machine's
paths.  The output of a failing command is shown with emails and key-like strings masked; that is a
best effort, not a guarantee.
"""
import argparse
import glob
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

KIT = Path(__file__).resolve().parent
HUB = "docs/agent_hub"
LOCAL_CONFIG = HUB + "/hub_config.local.json"     # this machine's tool paths; git-ignored, never committed
AGENTS = (("claude", "claude"), ("gpt", "codex"), ("deepseek", "dsh"))  # (tag, command)
DSH_APP = "DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh"
TOOL_PLACES = {      # searched in this order when PATH has no such tool; anywhere else needs --tool-path
    "claude": ("~/.claude/local/claude", "~/.local/bin/claude", "~/.npm-global/bin/claude",
               "~/.nvm/versions/node/*/bin/claude", "/opt/homebrew/bin/claude", "/usr/local/bin/claude"),
    "codex": ("~/.local/bin/codex", "~/.npm-global/bin/codex", "~/.nvm/versions/node/*/bin/codex",
              "/opt/homebrew/bin/codex", "/usr/local/bin/codex"),
    "dsh": ("/Applications/" + DSH_APP, "~/Applications/" + DSH_APP, "~/.local/bin/dsh",
            "/opt/homebrew/bin/dsh", "/usr/local/bin/dsh"),
}
DSH_ENTRIES = ("credentials", "authorization", "deepseek-account")
DSH_COMMENT = ('# Restore stored authentication (dsh-headless README, "Credentials"), '
               'so saved keys apply to headless runs.')
HELLO_PROMPT = "Reply with the single word READY."
JOB_ENV = ("JOB_TAG", "JOB_REASON", "JOB_TASK", "JOB_REVISION", "JOB_ID")
GITIGNORE_HEADER = "# agent hub (setup_hub.py)"
FIX_LOGIN = {
    "claude": "claude auth login",
    "gpt": "codex login",
    "deepseek": "save your DeepSeek API key in dsh (dsh-headless README, 'Credentials'), "
                "then run setup_hub.py --fix-deepseek",
}
GUARD_FIX = "python3 docs/agent_hub/tools/install_hooks.py --tag claude --task T000"
WRITTEN = ("docs", HUB, ".gitignore", "CLAUDE.md", "AGENTS.md")   # every path the install writes to or under
N_STEPS = 9


class Stop(Exception):
    """A step failed in a way that ends the install."""

    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


class Checks:
    """The ✓/✗/– checklist printed at the end."""

    def __init__(self):
        self.items = []

    def ok(self, label):
        self.items.append(("ok", label, ""))

    def fail(self, label, fix=""):
        self.items.append(("fail", label, fix))

    def skip(self, label):
        self.items.append(("skip", label, ""))

    def failed(self):
        return any(kind == "fail" for kind, _, _ in self.items)

    def show(self):
        print("\nChecklist")
        for kind, label, fix in self.items:
            print("  %s %s" % ({"ok": "✓", "fail": "✗", "skip": "–"}[kind], label))
            for n, line in enumerate(fix.splitlines()):
                print("      %s%s" % ("fix: " if n == 0 else "     ", line))


# ---------------------------------------------------------------- small helpers

def run(cmd, cwd=None, timeout=60, env=None):
    """Run a command, capture everything.  Returns (returncode, stdout, stderr); 127 when it cannot start."""
    try:
        done = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env,
                              stdin=subprocess.DEVNULL, errors="replace")
    except subprocess.TimeoutExpired as exc:
        return 124, "", "timed out after %ds" % exc.timeout
    except OSError as exc:
        return 127, "", str(exc)
    return done.returncode, done.stdout, done.stderr


def git(repo, *args):
    return run(["git", "--no-optional-locks", "-C", str(repo), *args])


EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
KEY_VALUE = re.compile(r"(?i)\b(api[_-]?key|token|secret|password|passwd)\b(\s*[:=]\s*)(\S+)"
                       r"|\b(bearer)(\s+)(\S+)")
LONG_RUN = re.compile(r"[A-Za-z0-9_+=-]{20,}")


def masked_run(match):
    """A run of 20+ key-like characters is masked when it looks like a key: it has a digit and a letter,
    and an upper-case letter or 8+ digits.  Lower-case names such as test_6_adds_missing_rows stay."""
    word = match.group(0)
    digits = sum(c.isdigit() for c in word)
    looks = digits and any(c.isalpha() for c in word) and (any(c.isupper() for c in word) or digits >= 8)
    return "<masked>" if looks else word


def redact(text):
    """Mask emails, `token: ...`-style values and key-like strings (T051 F4).  A best effort, not a proof."""
    text = EMAIL.sub("<email>", text)
    text = KEY_VALUE.sub(lambda m: (m.group(1) or m.group(4)) + (m.group(2) or m.group(5)) + "<masked>", text)
    return LONG_RUN.sub(masked_run, text)


def tail(text, n=5):
    """The last n non-blank lines of a command's output, masked by redact()."""
    lines = [line for line in (text or "").splitlines() if line.strip()]
    return redact("\n".join(lines[-n:]))


def ask(question, assume_yes):
    if assume_yes:
        print("%s [y/N] y (--yes)" % question)
        return True
    try:
        answer = input("%s [y/N] " % question)
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


def step(n, title):
    print("\n[%d/%d] %s" % (n, N_STEPS, title))


def find_tool(command, overrides=None):
    """Full path of a tool: --tool-path first, then PATH, then TOOL_PLACES.  None when it is not found."""
    if overrides and command in overrides:
        return overrides[command]
    path = shutil.which(command)
    if path:
        return str(Path(path).absolute())
    for pattern in TOOL_PLACES.get(command, ()):
        hits = [p for p in glob.glob(os.path.expanduser(pattern)) if os.path.isfile(p) and os.access(p, os.X_OK)]
        if hits:
            return max(hits, key=os.path.getmtime)      # several nvm versions: the newest install
    return None


def local_tool_paths(repo):
    """{command: full path} from the git-ignored local file, {} without one.  ValueError when it is invalid."""
    path = repo / LOCAL_CONFIG
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    tools = data.get("tool_paths", {}) if isinstance(data, dict) else None
    if not isinstance(tools, dict) or not all(isinstance(value, str) for value in tools.values()):
        raise ValueError("%s needs a tool_paths object of strings" % LOCAL_CONFIG)
    return tools


def write_local_config(repo, tools):
    """Merge {command: full path} into the local file.  It holds paths only, never a credential."""
    data = {"_comment": "This machine's tool paths. Git-ignored: do not commit this file. Each key is a command "
                        "name in hub_config.json job_agents; its value replaces that first word.",
            "tool_paths": {**local_tool_paths(repo), **tools}}
    path = repo / LOCAL_CONFIG
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o644)


def dsh_home():
    return Path(os.environ.get("DSH_HOME") or (Path.home() / ".dsh"))


def patch_path():
    return dsh_home() / "profiles" / "headless" / "cordis.patch.yml"


# ---------------------------------------------------------------- DeepSeek patch file (line based)

ID_LINE = re.compile(r"^(\s*)-\s+id:\s*['\"]?([\w.-]+)['\"]?\s*(#.*)?$")
DISABLED_LINE = re.compile(r"^(\s+disabled:[ \t]*)([^#]*?)([ \t]*(?:#.*)?)$")   # any value, comment kept
EMPTY_LIST_LINE = re.compile(r"^\[\s*\]\s*(#.*)?$")   # the body of a fresh dsh patch file


def dsh_has_empty_list(lines):
    """True when a top-level `[]` is present.  YAML rejects it in front of `- id:` entries, so dsh would
    stop loading the profile; it has to go before any entry is added."""
    return any(EMPTY_LIST_LINE.match(line) for line in lines)


def dsh_scan(lines):
    """{entry id: (index of its disabled line or None, its value text or None, indent for a new key,
    index of its id line)}.

    Only the literal value `false` counts as enabled (T051 F3): an entry without a disabled line,
    or with any other value, is not enabled.
    """
    found = {}
    for i, line in enumerate(lines):
        match = ID_LINE.match(line)
        if not match:
            continue
        indent = len(match.group(1))
        at, value, key_indent = None, None, None
        for j in range(i + 1, len(lines)):
            nxt = lines[j]
            if not nxt.strip() or nxt.lstrip().startswith("#"):
                continue
            lead = len(nxt) - len(nxt.lstrip())
            if lead <= indent:
                break
            if key_indent is None:
                key_indent = lead
            hit = DISABLED_LINE.match(nxt)
            if hit:
                at, value = j, hit.group(2).strip()
                break
        found[match.group(2)] = (at, value, indent + 2 if key_indent is None else key_indent, i)
    return found


def dsh_enabled(entry):
    return entry is not None and entry[1] == "false"


def dsh_missing_or_disabled(lines):
    """Entries of DSH_ENTRIES that are absent or lack an explicit `disabled: false`."""
    found = dsh_scan(lines)
    return [name for name in DSH_ENTRIES if not dsh_enabled(found.get(name))]


def dsh_plan(lines):
    """[(entry, what changes)] for the entries that are not enabled."""
    found = dsh_scan(lines)
    plan = []
    for name in dsh_missing_or_disabled(lines):
        if name not in found:
            plan.append((name, "add the entry with disabled: false"))
        elif found[name][0] is None:
            plan.append((name, "add disabled: false to the entry"))
        else:
            plan.append((name, "disabled: %s -> disabled: false" % (found[name][1] or "(empty)")))
    if dsh_has_empty_list(lines):
        plan.append(("[]", "remove the empty list, which cannot stand in front of entries"))
    return plan


def dsh_fixed(lines):
    """New lines with the three entries enabled (a copy; the input is untouched)."""
    out = list(lines)
    found = dsh_scan(out)
    inserts = []
    for name in DSH_ENTRIES:
        entry = found.get(name)
        if entry is None or dsh_enabled(entry):
            continue
        at, _, key_indent, id_at = entry
        if at is None:
            inserts.append((id_at + 1, " " * key_indent + "disabled: false"))
        else:
            hit = DISABLED_LINE.match(out[at])
            head = hit.group(1) if hit.group(1).endswith((" ", "\t")) else hit.group(1) + " "
            out[at] = head + "false" + hit.group(3)
    for index, line in sorted(inserts, reverse=True):
        out.insert(index, line)
    out = [line for line in out if not EMPTY_LIST_LINE.match(line)]
    missing = [name for name in DSH_ENTRIES if name not in found]
    if missing:
        out.append(DSH_COMMENT)
        for name in missing:
            out += ["- id: %s" % name, "  disabled: false"]
    return out


def read_lines(path):
    """(lines, newline): the file's lines and its line ending, so a CRLF file stays CRLF (T051 N1)."""
    text = path.read_bytes().decode("utf-8")
    return text.splitlines(), ("\r\n" if "\r\n" in text else "\n")


def write_lines(path, lines, newline="\n"):
    path.write_bytes((newline.join(lines) + newline).encode("utf-8"))


def dsh_login_ok():
    path = patch_path()
    if not path.is_file():
        return False, "no %s" % path
    lines = read_lines(path)[0]
    bad = dsh_missing_or_disabled(lines)
    if not bad and dsh_has_empty_list(lines):
        return False, "a leftover `[]` in %s stops dsh from loading the profile" % path
    return (not bad), ("missing or not `disabled: false` in the headless profile: %s" % ", ".join(bad) if bad else "")


def fix_deepseek(assume_yes):
    path = patch_path()
    if not path.is_file():
        print("Cannot find %s\nRun `%s headless --help` once so dsh creates the profile, then try again."
              % (path, shlex.quote(find_tool("dsh") or "dsh")))
        return 1
    lines, newline = read_lines(path)
    plan = dsh_plan(lines)
    if not plan:
        print("%s already enables credentials, authorization and deepseek-account. Nothing to change." % path)
        return 0
    print("Planned change in %s (the only change this wizard makes outside your repository):" % path)
    for name, change in plan:
        print("  - %s: %s" % (name, change))
    print("A copy is saved first as %s.bak" % path.name)
    if not assume_yes and not sys.stdin.isatty():      # the preview: show the plan, change nothing
        print("Nothing changed. Rerun with --yes (or in a terminal) to apply.")
        return 2
    if not ask("Apply?", assume_yes):
        print("Nothing changed.")
        return 1
    shutil.copy2(path, str(path) + ".bak")
    write_lines(path, dsh_fixed(lines), newline)
    print("Done. Backup: %s.bak" % path)
    return 0


# ---------------------------------------------------------------- login checks (free, print no secret)

def login_status(tag, command):
    """(logged in, reason).  Only the exit code or a boolean field is read; nothing else is shown."""
    if tag == "claude":
        code, out, _ = run([command, "auth", "status", "--json"], timeout=30)
        if code != 0 and not out.strip():
            return False, "could not check the login"
        try:
            return bool(json.loads(out).get("loggedIn")), "not logged in"
        except (ValueError, AttributeError):
            return False, "could not check the login"
    if tag == "gpt":
        code, _, _ = run([command, "login", "status"], timeout=30)
        return code == 0, "not logged in"
    ok, why = dsh_login_ok()
    return ok, why


def check_login(checks, tag, command):
    ok, why = login_status(tag, command)
    if ok:
        checks.ok("%s: logged in" % tag)
    else:
        checks.fail("%s: %s" % (tag, why or "not logged in"), FIX_LOGIN[tag])


# ---------------------------------------------------------------- template readers

def fenced(text, heading_prefix):
    """The first fenced block under the section whose heading starts with heading_prefix."""
    start = text.find(heading_prefix)
    if start < 0:
        return ""
    rest = text[start:]
    nxt = re.search(r"^## ", rest[len(heading_prefix):], re.M)
    section = rest if not nxt else rest[:len(heading_prefix) + nxt.start()]
    match = re.search(r"^```[^\n]*\n(.*?)^```", section, re.M | re.S)
    return match.group(1) if match else ""


def agents_block():
    text = (KIT / "AGENTS_BLOCK.md").read_text(encoding="utf-8")
    match = re.search(r"^```markdown\n(.*?)^```", text, re.M | re.S)
    if not match:
        raise Stop("AGENTS_BLOCK.md of the kit has no markdown block")
    return match.group(1).rstrip("\n") + "\n"


def snippet_lines():
    return [line.rstrip() for line in (KIT / "gitignore_snippet.txt").read_text(encoding="utf-8").splitlines()
            if line.strip()]


# ---------------------------------------------------------------- install steps

def preflight(repo):
    step(1, "Preflight")
    if sys.version_info < (3, 11):
        raise Stop("Python 3.11 or newer is needed (this is %d.%d)" % sys.version_info[:2])
    if not shutil.which("git"):
        raise Stop("git is not on PATH")
    if not repo.is_dir():
        raise Stop("%s is not a folder" % repo)
    code, out, _ = git(repo, "rev-parse", "--show-toplevel")
    if code != 0 or Path(out.strip()).resolve() != repo:
        raise Stop("%s is not the top level of a git work tree" % repo)
    if git(repo, "diff", "--cached", "--quiet")[0] != 0:
        raise Stop("something is staged in %s; commit or unstage it first" % repo)
    if (repo / HUB).exists() or (repo / HUB).is_symlink():
        raise Stop("already installed (%s exists); run --doctor" % (repo / HUB))
    for name in WRITTEN:       # T051 F2: a symlink here would let the install write outside REPO
        path = repo / name
        if path.is_symlink() or (path.exists() and repo not in path.resolve().parents):
            raise Stop("%s is a symbolic link; the wizard writes only inside %s. Replace the link with a "
                       "real file or folder, or install by hand (README_FIRST.md, section 4)" % (path, repo))
    code, out, _ = git(repo, "symbolic-ref", "--short", "HEAD")
    if code != 0 or not out.strip():
        raise Stop("HEAD is detached; check out your default branch first")
    branch = out.strip()
    print("  ok: Python %d.%d, git, work tree %s, nothing staged, branch %s" % (*sys.version_info[:2], repo, branch))
    dirty = bool(git(repo, "status", "--porcelain", "--", ".gitignore")[1].strip())
    if dirty:
        print("  note: .gitignore has changes of yours that are not committed; the hub commit will leave "
              ".gitignore out")
    return branch, dirty


def find_agents(checks, overrides=None):
    step(2, "Agents")
    found = {}
    for tag, command in AGENTS:
        path = find_tool(command, overrides)
        if not path:
            checks.skip("%s: `%s` not found (optional; if it is installed elsewhere, rerun with --tool-path %s=/full/path)"
                        % (tag, command, command))
            print("  – %s: not found on PATH or in the usual places (optional)" % tag)
            continue
        found[tag] = path
        check_login(checks, tag, found[tag])
        print("  %s %s: %s" % ("✓" if checks.items[-1][0] == "ok" else "✗", tag, found[tag]))
    return found


def make_config(found, branch):
    step(3, "hub_config.json")
    config = json.loads((KIT / HUB / "hub_config.json").read_text(encoding="utf-8"))
    config["job_agents"] = {tag: argv for tag, argv in config["job_agents"].items() if tag in found}
    config["task_branch"] = branch
    print("  agents: %s; task_branch: %s" % (", ".join(config["job_agents"]) or "none", branch))
    return config


def copy_tree(repo, config, found, dry, assume_yes):
    step(4, "Copy the hub")
    src, dst = KIT / HUB, repo / HUB
    files = [p for p in sorted(src.rglob("*"))
             if p.is_file() and "__pycache__" not in p.parts and p.name != Path(LOCAL_CONFIG).name]
    print("  will create %d files under %s (existing files are never overwritten)" % (len(files), dst))
    if found:
        print("  will record this machine's tool paths in %s (git-ignored, never committed)" % (repo / LOCAL_CONFIG))
    if dry:
        return
    if not ask("Copy the hub into your repository?", assume_yes):
        raise Stop("stopped by you before the copy")
    for path in files:
        rel = path.relative_to(src)
        target = dst / rel
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if rel.as_posix() == "hub_config.json":
            target.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
            target.chmod(0o644)
        else:
            shutil.copy2(path, target)
    if found:
        commands = dict(AGENTS)
        write_local_config(repo, {commands[tag]: path for tag, path in found.items()})


def update_gitignore(repo, dry, assume_yes):
    step(5, ".gitignore")
    path = repo / ".gitignore"
    have = {line.strip() for line in path.read_text(encoding="utf-8").splitlines()} if path.is_file() else set()
    add = [line for line in snippet_lines() if line not in have]
    if not add:
        print("  already complete; nothing to add")
        return True
    print("  will append %d lines to %s under %r" % (len(add), path, GITIGNORE_HEADER))
    if dry:
        return True
    if not ask("Append to .gitignore?", assume_yes):
        print("  skipped")
        return False
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    if text and not text.endswith("\n"):
        text += "\n"
    text += ("\n" if text else "") + GITIGNORE_HEADER + "\n" + "\n".join(add) + "\n"
    path.write_text(text, encoding="utf-8")
    return True


def update_config_files(repo, dry, assume_yes):
    step(6, "CLAUDE.md and AGENTS.md")
    block = agents_block()
    first = block.splitlines()[0]
    todo = []
    for name in ("CLAUDE.md", "AGENTS.md"):
        path = repo / name
        text = path.read_text(encoding="utf-8") if path.is_file() else None
        if text is not None and first in text:
            print("  %s already has the block" % name)
        else:
            todo.append((path, text))
            print("  will %s %s" % ("append the block to" if text is not None else "create", path))
    if dry or not todo:
        return
    if not ask("Write the agent block?", assume_yes):
        print("  skipped")
        return
    for path, text in todo:
        new = block if text is None else text + ("" if text.endswith("\n") or not text else "\n") + "\n" + block
        path.write_text(new, encoding="utf-8")
    print("  These two files are not added to git (they may be git-ignored). Commit them if you want.")


def commit(repo, dry, assume_yes, checks, gitignore_dirty=False):
    step(7, "Commit")
    has_ignore = dry or (repo / ".gitignore").is_file()      # the .gitignore step can be declined
    paths = ["docs/agent_hub"] + ([".gitignore"] if has_ignore and not gitignore_dirty else [])
    print("  will run: git add %s && git commit -m 'Add agent hub (setup_hub.py)' (never pushed)" % " ".join(paths))
    if gitignore_dirty:      # T051 F5: never fold the user's own uncommitted edits into the hub commit
        print("  .gitignore is left out: it had uncommitted changes of yours before the install")
    if dry:
        return
    if not ask("Commit the hub?", assume_yes):
        checks.fail("hub commit skipped", "git add %s && git commit -m 'Add agent hub'" % " ".join(paths))
        return
    code, _, err = git(repo, "add", *paths)
    if code == 0:      # also when the .gitignore step was declined: machine paths never enter the commit
        code, _, err = git(repo, "rm", "-q", "--cached", "--ignore-unmatch", "--", LOCAL_CONFIG)
    if code == 0:
        code, _, err = git(repo, "commit", "-q", "-m", "Add agent hub (setup_hub.py)")
    if code != 0:
        checks.fail("hub commit failed (exit %d)" % code,
                    "%s\nthen commit by hand: git add %s && git commit -m 'Add agent hub'"
                    % (tail(err, 5) or "no message", " ".join(paths)))
        return
    checks.ok("hub committed (not pushed)")
    if gitignore_dirty:
        checks.skip(".gitignore not committed: it had uncommitted changes of yours; review and commit it yourself")


def install_guard(repo, dry, assume_yes, checks):
    step(8, "Commit guard")
    print("  will run: %s" % GUARD_FIX)
    if dry:
        return
    if not ask("Install the commit guard?", assume_yes):
        checks.fail("commit guard not installed", GUARD_FIX)
        return
    code, out, err = run([sys.executable, "docs/agent_hub/tools/install_hooks.py", "--tag", "claude", "--task", "T000"],
                         cwd=repo)
    if code == 0:
        checks.ok("commit guard installed")
    else:
        checks.fail("commit guard not installed: %s" % (tail(err or out, 3).replace("\n", " ") or "failed"),
                    "resolve the message above, then run: " + GUARD_FIX)


def self_test(repo, checks):
    tools = repo / HUB / "tools"
    modules = sorted(p.stem for p in tools.glob("test_*.py") if p.stem != "test_watch_wakes")
    print("  running %d test modules (fake agents only, no tokens) ..." % len(modules))
    env = {k: v for k, v in os.environ.items() if k not in JOB_ENV}
    code, out, err = run([sys.executable, "-B", "-m", "unittest", *modules], cwd=tools, timeout=900, env=env)
    if code == 0:
        checks.ok("self-test passes")
    else:
        checks.fail("self-test failed", tail(err or out, 8))


def secret_scan(repo, checks):
    """The guard only sees new commits, so check what is already tracked.  The report holds no value."""
    tool = repo / HUB / "tools" / "secret_guard.py"
    if not tool.is_file():
        checks.fail("the secret guard is missing", "copy docs/agent_hub/tools/secret_guard.py from the kit")
        return
    code, out, err = run([sys.executable, "-B", str(tool), "--all"], cwd=repo, timeout=120)
    if code == 0:
        checks.ok("no key-like strings in tracked files")
    else:
        checks.fail("tracked files may hold a secret", (tail(err or out, 12) or "no message") +
                    "\nremove it, and replace the key if it was ever pushed")


def github_protection(repo, checks):
    """A warning for a public GitHub repository without push protection.  Silent without `gh` or an answer."""
    match = re.search(r"github\.com[:/]([^/\s]+/[^/\s]+?)(?:\.git)?$", git(repo, "remote", "get-url", "origin")[1].strip())
    if not match or not shutil.which("gh"):
        return
    code, out, _ = run(["gh", "api", "repos/%s" % match.group(1), "--jq",
                        '[.visibility, .security_and_analysis.secret_scanning_push_protection.status // ""] | @tsv'],
                       timeout=20)
    if code == 0 and out.strip().split("\t") == ["public", "disabled"]:
        checks.skip("%s is public and GitHub push protection is off (warning); turn it on in the repository's "
                    "security settings" % match.group(1))


def kickoff_prompts(repo, found):
    path = repo / HUB / "KICKOFF_PROMPT.md"
    template = fenced(path.read_text(encoding="utf-8"), "## 1.") if path.is_file() else ""
    if not template:
        print("  (no kickoff prompt found)")
        return
    print("\nKickoff prompts. Paste each into that agent's chat once:")
    for tag in found:
        print("\n----- %s -----" % tag)
        if tag == "claude":
            print("(Claude Code reads CLAUDE.md, so it needs this prompt only once.)")
        print(template.replace("<REPO_PATH>", str(repo)).replace("<TAG>", tag).rstrip())
    print("-----")


def install(repo, args):
    checks = Checks()
    branch, gitignore_dirty = preflight(repo)
    found = find_agents(checks, args.tools)
    config = make_config(found, branch)
    copy_tree(repo, config, found, args.dry_run, args.yes)
    update_gitignore(repo, args.dry_run, args.yes)
    update_config_files(repo, args.dry_run, args.yes)
    commit(repo, args.dry_run, args.yes, checks, gitignore_dirty)
    install_guard(repo, args.dry_run, args.yes, checks)
    step(9, "Self-test and kickoff prompts")
    if args.dry_run:
        print("  would run the self-test%s and print the kickoff prompt for: %s"
              % (" (skipped: --no-tests)" if args.no_tests else "", ", ".join(found) or "no agent"))
        print("\nDry run: nothing was changed.")
        return 0
    if args.no_tests:
        checks.skip("self-test not run (--no-tests)")
    else:
        self_test(repo, checks)
    secret_scan(repo, checks)
    github_protection(repo, checks)
    if args.hello:
        hello(repo, checks, args.yes)
    kickoff_prompts(repo, found)
    checks.show()
    return 1 if checks.failed() else 0


# ---------------------------------------------------------------- doctor

def read_config(repo, checks):
    path = repo / HUB / "hub_config.json"
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(config, dict):      # T051 F6: [] or 42 is valid JSON but no config
            raise ValueError("the top level is not a JSON object")
        agents = config["job_agents"]
        if not isinstance(agents, dict):
            raise ValueError("job_agents is not an object")
        if not all(isinstance(argv, list) and all(isinstance(a, str) for a in argv) for argv in agents.values()):
            raise ValueError("every job_agents entry must be a list of strings")
        if not isinstance(config.get("task_branch", "main"), str):
            raise ValueError("task_branch is not a string")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        checks.fail("hub_config.json is not valid: %s" % exc, "fix %s" % path)
        return None
    checks.ok("hub_config.json is valid JSON")
    return config


def doctor(repo, args):
    checks = Checks()
    print("Doctor for %s (read-only)" % repo)
    before = git(repo, "status", "--porcelain")[1]
    if sys.version_info >= (3, 11):
        checks.ok("Python %d.%d" % sys.version_info[:2])
    else:
        checks.fail("Python %d.%d is too old" % sys.version_info[:2], "install Python 3.11 or newer")
    checks.ok("git on PATH") if shutil.which("git") else checks.fail("git not on PATH", "install git")
    if not (repo / HUB).is_dir():
        checks.fail("%s does not exist" % (repo / HUB), "run: python3 %s %s" % (Path(__file__).resolve(), repo))
        checks.show()
        return 1
    checks.ok("docs/agent_hub exists")
    config = read_config(repo, checks)
    try:
        tools = local_tool_paths(repo)
    except (OSError, ValueError) as exc:
        tools = {}
        checks.fail("%s is not valid: %s" % (LOCAL_CONFIG, exc), "fix or delete %s" % (repo / LOCAL_CONFIG))
    if git(repo, "ls-files", "--error-unmatch", "--", LOCAL_CONFIG)[0] == 0:
        checks.fail("%s is tracked by git, so this machine's paths are shared" % LOCAL_CONFIG,
                    "git rm --cached %s, and add it to .gitignore" % LOCAL_CONFIG)
    if config:
        agents = config["job_agents"]
        for tag, argv in agents.items():
            name = argv[0] if argv else ""
            exe = tools.get(name, name)
            resolved = shutil.which(exe) if exe else None
            if os.path.isabs(name):
                checks.skip("%s: hub_config.json holds a full path, which git shares; keep the bare name there and "
                            "the path in %s (warning)" % (tag, LOCAL_CONFIG))
            if resolved and os.access(resolved, os.X_OK):
                checks.ok("%s: command %s is executable" % (tag, exe))
                if tag in dict(AGENTS):
                    check_login(checks, tag, resolved)
            else:
                checks.fail("%s: command %r not found or not executable" % (tag, exe),
                            "install it, or record its path: python3 %s %s --local-paths "
                            "(add --tool-path %s=/full/path if it is somewhere unusual)"
                            % (Path(__file__).resolve(), repo, name or "TOOL"))
        branch = config.get("task_branch", "main")
        if git(repo, "rev-parse", "--verify", "-q", "refs/heads/%s" % branch)[0] == 0:
            checks.ok("task_branch %s exists" % branch)
        else:
            checks.fail("task_branch %r is not a branch of this repository" % branch,
                        "set task_branch in %s/hub_config.json" % HUB)
    ignore = repo / ".gitignore"
    have = {line.strip() for line in ignore.read_text(encoding="utf-8").splitlines()} if ignore.is_file() else set()
    gone = [line for line in snippet_lines() if line not in have]
    if gone:
        checks.fail(".gitignore lacks %d hub lines" % len(gone), "append gitignore_snippet.txt of the kit to .gitignore")
    else:
        checks.ok(".gitignore has the hub lines")
    first = agents_block().splitlines()[0]
    for name in ("CLAUDE.md", "AGENTS.md"):
        path = repo / name
        if path.is_file() and first in path.read_text(encoding="utf-8"):
            checks.ok("%s has the hub block" % name)
        else:
            checks.skip("%s has no hub block (warning; agents may not read the hub)" % name)
    common = git(repo, "rev-parse", "--git-common-dir")[1].strip()
    hook = (repo / common / "hooks" / "commit-msg") if common else None
    if hook and hook.is_file() and "commit_msg_hook" in hook.read_text(encoding="utf-8", errors="replace"):
        checks.ok("commit guard installed")
    else:
        checks.fail("commit guard (.git/hooks/commit-msg) is not installed", GUARD_FIX)
    secret_scan(repo, checks)
    github_protection(repo, checks)
    if args.tests:
        self_test(repo, checks)
    if args.hello and config:
        hello(repo, checks, args.yes)
    after = git(repo, "status", "--porcelain")[1]
    if before != after:
        checks.fail("doctor changed the working tree (unexpected)", "check `git status`")
    checks.show()
    return 1 if checks.failed() else 0


# ---------------------------------------------------------------- hello

def error_text(value):
    """The message in an error value: plain text, an object, or JSON held in text (Codex nests it)."""
    if isinstance(value, str):
        try:
            inner = json.loads(value)
        except ValueError:
            return value
        return error_text(inner) if isinstance(inner, dict) else value
    if isinstance(value, dict):
        for key in ("error", "message"):
            text = error_text(value.get(key))
            if text:
                return text
    return ""


def wake_errors(out):
    """The error messages in a wake's JSON output, oldest first, without repeats.  Codex reports them
    as events and items, Claude as `is_error` with `result`."""
    found = []
    for line in out.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        kind = str(event.get("type"))
        if event.get("is_error"):
            text = error_text(event.get("result"))
        elif item.get("type") == "error":
            text = error_text(item)
        elif "error" in kind or "failed" in kind:
            text = error_text(event)
        else:
            continue
        if text and text not in found:
            found.append(text)
    return found


def wake_failure(out, err):
    """Why a wake failed, masked.  Its JSON output comes first: stderr may hold only warnings."""
    errors = redact("\n".join(text[:300] for text in wake_errors(out)[-3:]))
    return "\n".join(part for part in (errors, tail(err, 5)) if part) or tail(out, 3) or "no output"


def hello(repo, checks, assume_yes):
    print("\nHello: wake each configured agent once with %r (this spends a few tokens)." % HELLO_PROMPT)
    config = json.loads((repo / HUB / "hub_config.json").read_text(encoding="utf-8"))
    if not ask("Wake the agents?", assume_yes):
        checks.skip("hello skipped")
        return
    try:
        tools = local_tool_paths(repo)
    except (OSError, ValueError) as exc:
        checks.fail("hello not run: %s is not valid: %s" % (LOCAL_CONFIG, exc), "fix or delete %s" % (repo / LOCAL_CONFIG))
        return
    for tag, argv in config["job_agents"].items():
        cmd = []
        for item in [tools.get(argv[0], argv[0]), *argv[1:]] if argv else argv:
            for key, value in (("{repo}", str(repo)), ("{prompt}", HELLO_PROMPT), ("{tag}", "hello"),
                               ("{reason}", "hello"), ("{task}", "hello"), ("{revision}", "hello"),
                               ("{job}", "hello")):
                item = item.replace(key, value)
            cmd.append(item)
        code, out, err = run(cmd, cwd=repo, timeout=180)
        if code == 0 and "READY" in out:
            checks.ok("hello: %s answered READY" % tag)
        else:
            checks.fail("hello: %s did not answer READY (exit %d)" % (tag, code), wake_failure(out, err))


# ---------------------------------------------------------------- local paths of a clone

def record_local_paths(repo, tools, dry):
    """A clone that already has the hub: find this machine's tools and record them in the local file."""
    if not (repo / HUB).is_dir():
        raise Stop("%s does not exist; install the hub first" % (repo / HUB))
    if (repo / LOCAL_CONFIG).is_symlink():
        raise Stop("%s is a symbolic link; the wizard writes only inside %s" % (repo / LOCAL_CONFIG, repo))
    if git(repo, "check-ignore", "-q", "--no-index", "--", LOCAL_CONFIG)[0] != 0:
        raise Stop("%s is not git-ignored, so its paths could be committed. Add this line to .gitignore first:\n%s"
                   % (LOCAL_CONFIG, LOCAL_CONFIG))
    found = {command: path for _, command in AGENTS for path in [find_tool(command, tools)] if path}
    for _, command in AGENTS:
        print("  %s %s: %s" % ("✓" if command in found else "–", command, found.get(command, "not found")))
    if dry:
        print("\nDry run: nothing was changed.")
    elif found:
        write_local_config(repo, found)
        print("\nRecorded in %s (git-ignored)." % (repo / LOCAL_CONFIG))
    else:
        print("\nNo tool found, so nothing was recorded. Use --tool-path TOOL=/full/path.")
    return 0


# ---------------------------------------------------------------- main

def parse_tool_paths(parser, items):
    """{command: full path} from the TOOL=PATH values of --tool-path."""
    commands = [command for _, command in AGENTS]
    tools = {}
    for item in items or ():
        name, sep, value = item.partition("=")
        if not sep or name not in commands:
            parser.error("--tool-path needs TOOL=/full/path, with TOOL one of %s (got %r)" % (", ".join(commands), item))
        path = Path(value).expanduser().absolute()
        if not path.is_file() or not os.access(path, os.X_OK):
            parser.error("--tool-path: %s is not an executable file" % path)
        tools[name] = str(path)
    return tools


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("repo", nargs="?", help="the repository to set up (top level of a git work tree)")
    parser.add_argument("--yes", action="store_true", help="answer yes to every prompt")
    parser.add_argument("--dry-run", action="store_true", help="print the planned changes of an install; change nothing")
    parser.add_argument("--doctor", action="store_true", help="check an installed hub; change nothing")
    parser.add_argument("--tests", action="store_true", help="with --doctor: also run the self-test")
    parser.add_argument("--no-tests", action="store_true", help="install without the self-test")
    parser.add_argument("--fix-deepseek", action="store_true", help="enable dsh's stored credentials for headless runs")
    parser.add_argument("--hello", action="store_true", help="wake each configured agent once (spends a few tokens)")
    parser.add_argument("--tool-path", action="append", metavar="TOOL=PATH",
                        help="use this file for claude, codex or dsh (repeatable; for a tool the search misses)")
    parser.add_argument("--local-paths", action="store_true",
                        help="record this machine's tool paths in a clone that already has the hub")
    args = parser.parse_args(argv)
    if sys.version_info < (3, 11):
        print("error: Python 3.11 or newer is needed", file=sys.stderr)
        return 1
    if args.fix_deepseek:      # T051 F1: checked before anything is read or written
        clash = [flag for flag, on in (("REPO", args.repo), ("--dry-run", args.dry_run), ("--doctor", args.doctor),
                                       ("--tests", args.tests), ("--no-tests", args.no_tests),
                                       ("--hello", args.hello), ("--tool-path", args.tool_path),
                                       ("--local-paths", args.local_paths)) if on]
        if clash:
            parser.error("--fix-deepseek runs on its own; it cannot be combined with %s. "
                         "Run `setup_hub.py --fix-deepseek` alone; it shows the planned change and asks first"
                         % ", ".join(clash))
    elif not args.repo:
        parser.error("REPO is required (except with --fix-deepseek)")
    if args.doctor and args.tool_path:
        parser.error("--doctor changes nothing, so it takes no --tool-path; use --local-paths to record a path")
    if args.local_paths:
        clash = [flag for flag, on in (("--doctor", args.doctor), ("--tests", args.tests), ("--no-tests", args.no_tests),
                                       ("--hello", args.hello)) if on]
        if clash:
            parser.error("--local-paths cannot be combined with %s" % ", ".join(clash))
    args.tools = parse_tool_paths(parser, args.tool_path)

    if args.fix_deepseek:      # without --yes and a terminal it prints the plan and changes nothing
        return fix_deepseek(args.yes)
    prompts = (not args.doctor and not args.dry_run and not args.local_paths) or (args.hello and not args.dry_run)
    if prompts and not args.yes and not sys.stdin.isatty():
        print("rerun with --yes (or run it in a terminal)")
        return 2
    repo = Path(args.repo).expanduser().resolve()
    try:
        if args.local_paths:
            return record_local_paths(repo, args.tools, args.dry_run)
        if args.doctor:
            if not repo.is_dir():
                raise Stop("%s is not a folder" % repo)
            return doctor(repo, args)
        return install(repo, args)
    except Stop as exc:
        print("\nstopped: %s" % exc, file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    sys.exit(main())
