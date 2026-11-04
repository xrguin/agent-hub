#!/usr/bin/env python3
"""Refuse commits that add a key-like string or a secret file; commit_msg_hook.py runs this first.

    python3 docs/agent_hub/tools/secret_guard.py          check what is staged
    python3 docs/agent_hub/tools/secret_guard.py --all    check every tracked file

A report names the file, the line and the kind of secret, never the value: hook output can end up
in wake logs.  This catches mistakes, not bad intent.  It knows common key formats, not every
secret, and `git commit --no-verify` skips it.  A line holding `secret-guard: allow` is not checked.
"""
import re

import hubtx

ALLOW = "secret-guard: allow"
MAX_SHOWN = 20
# Prefixes so distinctive that a match needs no further test.
RULES = (
    ("private key", re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----")),
    ("GitHub token", re.compile(r"(?<![A-Za-z0-9_])(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})")),
    ("AWS access key", re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![0-9A-Z])")),
    ("Slack token", re.compile(r"(?<![A-Za-z0-9])xox[abeoprs]-[A-Za-z0-9-]{10,}")),
    ("Google API key", re.compile(r"(?<![A-Za-z0-9_-])AIza[0-9A-Za-z_-]{35}")),
    ("JSON web token", re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
)
# These also match ordinary text, so the value must look random as well.
RANDOM_RULES = (
    ("API key (sk-)", re.compile(r"(?<![A-Za-z0-9_-])(sk-[A-Za-z0-9_-]{20,})")),
    ("assigned secret", re.compile(r"(?i)(?:api[_-]?key|secret|password|passwd|auth[_-]?token|access[_-]?token)"
                                   r"[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9_+/=.-]{20,})")),
)
PLACEHOLDER = re.compile(r"(?i)example|placeholder|your|xxxx|changeme|dummy|redacted|masked")
SECRET_FILE = re.compile(r"(?i)(?:^|/)(?:\.env(?:\.[^/]+)?|[^/]+\.(?:pem|p12|pfx)|id_(?:rsa|dsa|ecdsa|ed25519)"
                         r"|\.netrc|\.credentials[^/]*|\.codex/auth\.json)$")
SAMPLE_ENV = re.compile(r"(?i)\.env\.(?:example|sample|template|dist)$")
MACHINE_PATHS = hubtx.HUB + "hub_config.local.json"
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def looks_random(word):
    """A digit and a letter, plus an upper-case letter or 8+ digits, as setup_hub.py masks keys."""
    digits = sum(c.isdigit() for c in word)
    return bool(digits) and any(c.isalpha() for c in word) and (any(c.isupper() for c in word) or digits >= 8)


def scan_line(line):
    """The kind of secret on this line, or None."""
    if ALLOW in line:
        return None
    for kind, rule in RULES:
        if any(not PLACEHOLDER.search(match[0]) for match in rule.finditer(line)):
            return kind
    for kind, rule in RANDOM_RULES:
        if any(looks_random(match[1]) and not PLACEHOLDER.search(match[1]) for match in rule.finditer(line)):
            return kind
    return None


def secret_file(path):
    """Why this path must not be committed, or None."""
    if path == MACHINE_PATHS:
        return "this machine's tool paths"
    if SECRET_FILE.search(path) and not SAMPLE_ENV.search(path):
        return "secret file"
    return None


def added_lines(diff):
    """(path, line number, text) of each added line of a `git diff --unified=0`.  The hunk header gives
    the count of added lines, so an added line that starts with `++ ` is not taken for a file header."""
    path, number, left = None, 0, 0
    for raw in diff.split("\n"):
        if left:
            if raw.startswith("+"):
                yield path, number, raw[1:]
                number, left = number + 1, left - 1
            continue
        if raw.startswith("+++ "):
            path = raw[4:].rstrip("\t").removeprefix("b/")
            continue
        hunk = HUNK.match(raw)
        if hunk:
            number, left = int(hunk[1]), 1 if hunk[2] is None else int(hunk[2])


def staged_findings(root):
    """[(path, line or 0, kind)] for what the candidate index adds.  Git's hook environment names that
    index, so `git commit --only` is checked as well."""
    names = hubtx.git(root, "diff", "--cached", "--name-only", "--diff-filter=ACMRT", "-z").stdout
    found = [(name, 0, secret_file(name)) for name in names.decode(errors="replace").split("\0") if secret_file(name)]
    diff = hubtx.git(root, "-c", "core.quotePath=false", "diff", "--cached", "--no-color", "--no-ext-diff",
                     "--unified=0", "--diff-filter=ACMRT", "--src-prefix=a/", "--dst-prefix=b/").stdout
    for path, number, text in added_lines(diff.decode(errors="replace")):
        kind = scan_line(text)
        if kind:
            found.append((path, number, kind))
    return found


def commit_findings(root, message):
    """The staged findings, then those of the commit message."""
    found = staged_findings(root)
    for number, line in enumerate(message.split("\n"), 1):
        kind = scan_line(line)
        if kind:
            found.append(("commit message", number, kind))
    return found


def tracked_findings(root):
    """[(path, line or 0, kind)] for every tracked file as it is on disk.  Binary files are skipped."""
    found = []
    names = hubtx.git(root, "ls-files", "-z").stdout.decode(errors="replace").split("\0")
    for name in filter(None, names):
        if secret_file(name):
            found.append((name, 0, secret_file(name)))
        path = root / name
        if path.is_symlink() or not path.is_file():
            continue
        data = path.read_bytes()
        if b"\0" in data[:8192]:
            continue
        for number, line in enumerate(data.decode(errors="replace").split("\n"), 1):
            kind = scan_line(line)
            if kind:
                found.append((name, number, kind))
    return found


def listing(found):
    """One line per finding, with no value in it."""
    lines = ["  %s%s: %s" % (path, ":%d" % number if number else "", kind) for path, number, kind in found[:MAX_SHOWN]]
    if len(found) > MAX_SHOWN:
        lines.append("  ... and %d more" % (len(found) - MAX_SHOWN))
    return "\n".join(lines)


def refusal(found):
    return ("Commit refused: it adds what looks like a secret, so nothing was committed.\n%s\n"
            "Take it out, and replace the key if it was ever pushed. Keep secret files out of git with .gitignore.\n"
            "For a false alarm, add `%s` to that line. Only the user may use git commit --no-verify."
            % (listing(found), ALLOW))


def main():
    def run():
        parser = hubtx.ArgumentParser(description=__doc__.split("\n")[0])
        parser.add_argument("--all", action="store_true", help="check every tracked file, not only what is staged")
        args = parser.parse_args()
        root = hubtx.repo_root()
        found = tracked_findings(root) if args.all else staged_findings(root)
        if found:
            raise hubtx.BadInput("Please check these places, they may hold a key or a secret file:\n" + listing(found))
        print("no key-like strings or secret files found")
    return hubtx.cli(run)


if __name__ == "__main__":
    raise SystemExit(main())
