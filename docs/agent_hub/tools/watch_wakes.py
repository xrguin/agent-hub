#!/usr/bin/env python3
"""Read-only live viewer for the job-board wake logs.

The dispatcher (`job_dispatcher.py`) saves the stdout of every headless agent
wake to `docs/agent_hub/logs/dispatcher_runs/<epoch>_<seq>_<agent>_<task>_<reason>.out`.
Those files are JSON lines (codex `--json`, dsh `--json`) or one JSON object
(`claude -p --output-format json`) and are hard to read. This tool prints them
as a transcript and, by default, keeps following them (like `tail -f`).

Usage, from the repository root:

    python3 docs/agent_hub/tools/watch_wakes.py                # show all, then follow
    python3 docs/agent_hub/tools/watch_wakes.py --once         # show what exists, exit
    python3 docs/agent_hub/tools/watch_wakes.py --agent gpt --task T031 --no-thinking

Strictly read-only: it opens the log files for reading only and never writes,
renames or locks anything. Python 3 standard library only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import sys
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
DEFAULT_DIR = REPO / "docs" / "agent_hub" / "logs" / "dispatcher_runs"

NAME_RE = re.compile(r"^(?P<epoch>\d+)_(?P<seq>\d+)_(?P<agent>gpt|deepseek|claude)_(?P<task>.+)_(?P<reason>[A-Za-z0-9-]+)\.out$")
DISPATCHER_FILE = "dispatcher.out"

MAX_LINE = 400          # a single line is cut here unless --full
MAX_JSON = 200          # dim JSON of unknown events
CMD_LINES = 8           # a long (heredoc) command is cut after this many lines unless --full
TAG_W = 10              # len("[deepseek]")
TASK_W = 7

COLORS = {"gpt": "32", "deepseek": "94", "claude": "38;5;208", "dispatcher": "90"}
RESUME = {
    "gpt": "codex resume {id}",
    "deepseek": "dsh tui --resume {id}",
    "claude": "claude --resume {id}",
}


def parse_name(name: str):
    """Parse '<epoch>_<seq>_<agent>_<task>_<reason>.out' -> dict, or None."""
    m = NAME_RE.match(name)
    if not m:
        return None
    return {"epoch": int(m["epoch"]), "seq": int(m["seq"]), "agent": m["agent"],
            "task": m["task"], "reason": m["reason"]}


def strip_shell(cmd: str) -> str:
    """Remove a `/bin/bash -lc '...'` wrapper from a command line."""
    if not isinstance(cmd, str):
        return str(cmd)
    try:
        toks = shlex.split(cmd)
    except ValueError:
        toks = None
    if (toks and len(toks) == 3 and os.path.basename(toks[0]) in ("bash", "sh", "zsh")
            and re.fullmatch(r"-[a-z]*c", toks[1])):
        return toks[2]
    m = re.match(r"^\S*(?:bash|zsh|sh)\s+-[a-z]*c\s+(['\"])(.*)\1\s*$", cmd, re.S)
    return m.group(2) if m else cmd


@dataclass
class Out:
    """One logical piece of output: style key, text (may hold newlines), wrap flag, hanging indent."""
    style: str
    text: str
    wrap: bool = False
    hang: int = 0


def _int(x):
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def _tokens_line(tin, cached, out, reasoning=None, extra=""):
    s = f"done: tokens in {tin if tin is not None else '?'}"
    if cached is not None:
        s += f" (cached {cached})"
    s += f" out {out if out is not None else '?'}"
    if reasoning:
        s += f" (reasoning {reasoning})"
    return s + extra


class Wake:
    """State and renderer for one wake file (one agent run)."""

    def __init__(self, path: Path, meta: dict, opts):
        self.path = path
        self.meta = meta
        self.opts = opts
        self.offset = 0
        self.buf = b""
        self.session_id = None
        self.printed_items = set()     # codex item ids whose command line was already shown
        self.calls = {}                # dsh callId -> (tool, is_command)
        self.usage = {"in": 0, "cached": 0, "out": 0, "reasoning": 0, "seen": False}
        self.last_said = None
        self.last_cmd = None           # key of the command printed last (to attribute interleaved results)
        self.cmd_names = {}            # key -> short command text

    # ----- helpers -------------------------------------------------------
    def rel(self, s):
        if not isinstance(s, str):
            return s
        r = str(REPO)
        return s.replace(r + "/", "").replace(r, ".")

    def cut(self, s, limit=MAX_LINE):
        s = self.rel(s)
        if self.opts.full or len(s) <= limit:
            return s
        return s[:limit] + f"… (+{len(s) - limit} chars)"

    def session(self, sid):
        if self.session_id or not sid:
            return []
        self.session_id = sid
        return [Out("info", "session: " + RESUME[self.meta["agent"]].format(id=sid))]

    def command_outs(self, cmd, desc=None, key=None):
        cmd = strip_shell(cmd)
        if key is not None:
            self.last_cmd = key
            self.cmd_names[key] = self.rel(cmd).split("\n")[0][:40]
        lines = self.rel(cmd).split("\n")
        hidden = 0
        if not self.opts.full and len(lines) > CMD_LINES:
            hidden = len(lines) - CMD_LINES
            lines = lines[:CMD_LINES]
        shown = [self.cut(l) for l in lines]
        text = "$ " + shown[0] + "".join("\n  " + l for l in shown[1:])
        if hidden:
            text += f"\n  … (+{hidden} lines)"
        outs = [Out("cmd", text)]
        if desc:
            outs.append(Out("dim", "  # " + self.cut(str(desc))))
        return outs

    def result_outs(self, head, text, bad, key=None):
        if key is not None and key != self.last_cmd:     # another command ran in between
            head += f"  (of: {self.cmd_names.get(key, '?')})"
        self.last_cmd = key if key is not None else self.last_cmd
        outs = [Out("bad" if bad else "res", "  -> " + head)]
        text = (text or "").rstrip("\n")
        if not text:
            return outs
        lines = text.split("\n")
        n = max(0, self.opts.lines)
        shown = lines if self.opts.full else lines[:n]
        body = [("    " + self.cut(l)) for l in shown]
        if len(shown) < len(lines):
            body.append(f"    … (+{len(lines) - len(shown)} lines)")
        outs.append(Out("out", "\n".join(body)))
        return outs

    def summarize_input(self, inp):
        if not isinstance(inp, dict):
            return self.cut(json.dumps(inp, ensure_ascii=False), 120) if inp is not None else ""
        parts = []
        for k, v in inp.items():
            if isinstance(v, str):
                if len(v) > 80 or "\n" in v:
                    parts.append(f"{k}=<{len(v)} chars>")
                elif k in ("file_path", "path"):
                    parts.append(self.rel(v))      # the path alone reads best
                else:
                    parts.append(f"{k}={self.rel(v)}")
            else:
                parts.append(f"{k}={json.dumps(v, ensure_ascii=False)[:60]}")
        return self.cut(", ".join(parts), 160)

    def tool_call_outs(self, name, inp, call_id=None):
        is_cmd = isinstance(inp, dict) and isinstance(inp.get("command"), str)
        if call_id is not None:
            self.calls[call_id] = (name, is_cmd)
        if is_cmd:
            return self.command_outs(inp["command"], inp.get("description"), key=call_id)
        return [Out("tool", f"tool {name}: {self.summarize_input(inp)}")]

    def say(self, text):
        self.last_said = text
        return [Out("say", "says: " + self.rel(str(text)), wrap=True, hang=6)]

    def think(self, text):
        if self.opts.no_thinking:
            return []
        return [Out("think", "thinks: " + self.rel(str(text)), wrap=True, hang=8)]

    def err(self, text):
        return [Out("bad", "error: " + self.cut(str(text), 1000), wrap=True, hang=7)]

    def unknown(self, obj):
        t = obj.get("type", "?") if isinstance(obj, dict) else "?"
        return [Out("dim", f"?? {t}: " + self.cut(json.dumps(obj, ensure_ascii=False), MAX_JSON))]

    # ----- line entry point ---------------------------------------------
    def handle_line(self, line: str):
        line = line.rstrip("\r")
        if not line.strip():
            return []
        try:
            obj = json.loads(line)
        except (ValueError, RecursionError):
            obj = None
        if not isinstance(obj, (dict, list)):
            return self.plain(line)
        try:
            if isinstance(obj, list):
                outs = []
                for o in obj:
                    outs += self.handle_obj(o) if isinstance(o, dict) else self.plain(json.dumps(o))
                return outs
            return self.handle_obj(obj)
        except Exception as exc:  # never crash on a surprising event
            return [Out("dim", f"?? unrenderable event ({type(exc).__name__}): " + self.cut(line, MAX_JSON))]

    def plain(self, line):
        if "Reading additional input from stdin" in line and not self.opts.verbose:
            return []
        style = "bad" if re.search(r"\bERROR\b", line) else "dim"
        return [Out(style, self.cut(line))]

    def handle_obj(self, obj):
        agent = self.meta["agent"]
        t = obj.get("type")
        if agent == "gpt" or (isinstance(t, str) and ("." in t) and agent != "deepseek"):
            return self.codex(obj, t)
        if agent == "deepseek":
            return self.dsh(obj, t)
        return self.claude(obj, t)

    # ----- codex ---------------------------------------------------------
    def codex(self, ev, t):
        if t == "thread.started":
            return self.session(ev.get("thread_id"))
        if t == "turn.started":
            return [Out("dim", "turn started")] if self.opts.verbose else []
        if t == "turn.completed":
            u = ev.get("usage") or {}
            return [Out("done", _tokens_line(_int(u.get("input_tokens")), _int(u.get("cached_input_tokens")),
                                            _int(u.get("output_tokens")), _int(u.get("reasoning_output_tokens"))))]
        if t == "turn.failed":
            e = ev.get("error")
            return self.err(e.get("message") if isinstance(e, dict) else e)
        if t == "error":
            return self.err(ev.get("message", ev))
        if t in ("item.started", "item.updated", "item.completed"):
            return self.codex_item(t, ev.get("item") or {})
        return self.unknown(ev)

    def codex_item(self, t, it):
        kind = it.get("type")
        iid = it.get("id")
        done = t == "item.completed"
        if kind == "agent_message":
            return self.say(it.get("text", "")) if done else []
        if kind == "reasoning":
            return self.think(it.get("text", "")) if done else []
        if kind == "command_execution":
            outs = []
            if iid not in self.printed_items:
                self.printed_items.add(iid)
                outs += self.command_outs(it.get("command", ""), key=iid)
            if done:
                code, status = it.get("exit_code"), it.get("status")
                head = f"exit {code}" if code is not None else str(status)
                bad = (code not in (None, 0)) or status in ("failed", "declined")
                outs += self.result_outs(head, it.get("aggregated_output"), bad, key=iid)
            return outs
        if kind == "file_change":
            if not done:
                return []
            failed = it.get("status") == "failed"
            outs = []
            for ch in it.get("changes") or []:
                outs.append(Out("bad" if failed else "edit",
                                f"edits {self.rel(ch.get('path', '?'))} ({ch.get('kind', '?')})"
                                + (" FAILED" if failed else "")))
            return outs or [Out("edit", "edits (no changes listed)")]
        if kind == "error":
            return self.err(it.get("message", it)) if done else []
        if kind == "mcp_tool_call":
            if not done:
                return []
            outs = [Out("tool", f"tool {it.get('server', '?')}.{it.get('tool', '?')}: "
                                 f"{self.summarize_input(it.get('arguments'))}")]
            if it.get("status") == "failed":
                outs.append(Out("bad", "  -> failed"))
            return outs
        if kind == "web_search":
            return [Out("tool", f"tool web_search: {self.cut(str(it.get('query', '')), 160)}")] if done else []
        if kind == "todo_list" and not done:
            return []
        return self.unknown({"type": f"{t}/{kind}", **{k: v for k, v in it.items() if k != "type"}}) if done else []

    # ----- dsh -----------------------------------------------------------
    def dsh(self, ev, t):
        if t == "session":
            return self.session(ev.get("sessionId"))
        if t == "thinking":
            return self.think(ev.get("text", ""))
        if t == "text":
            return self.say(ev.get("text", ""))
        if t == "tool_call":
            return self.tool_call_outs(ev.get("tool", "?"), ev.get("input"), ev.get("callId"))
        if t == "tool_result":
            name, is_cmd = self.calls.get(ev.get("callId"), ("?", False))
            status = ev.get("status", "?")
            bad = status == "error"
            res = ev.get("result")
            res = res if isinstance(res, str) else json.dumps(res, ensure_ascii=False)
            if is_cmd or bad:
                return self.result_outs(status, res, bad, key=ev.get("callId") if is_cmd else None)
            n = len((res or "").splitlines())
            return [Out("res", f"  -> {status} ({n} lines)")]
        if t == "status":
            phase = ev.get("phase")
            if phase == "step_end":
                u = ev.get("usage")
                if isinstance(u, dict):
                    self.usage["seen"] = True
                    self.usage["in"] += _int(u.get("inputTokens")) or 0
                    self.usage["cached"] += _int(u.get("cacheReadTokens")) or 0
                    self.usage["out"] += _int(u.get("outputTokens")) or 0
                    self.usage["reasoning"] += _int(u.get("reasoningTokens")) or 0
            if phase == "turn_end":
                reason = ev.get("reason")
                kind = reason.get("kind") if isinstance(reason, dict) else reason
                if kind not in (None, "completed", "complete", "end_turn", "stop"):
                    return self.err(f"turn ended: {kind}")
            if self.opts.verbose:
                bits = " ".join(f"{k}={v}" for k, v in ev.items() if k not in ("type", "usage"))
                return [Out("dim", "status " + bits)]
            return []
        if t == "final":
            outs = []
            text = ev.get("text")
            if isinstance(text, str) and text.strip() and text.strip() != (self.last_said or "").strip():
                outs += [Out("say", "final: " + self.rel(text), wrap=True, hang=7)]
            u = self.usage
            if u["seen"]:
                outs.append(Out("done", _tokens_line(u["in"], u["cached"], u["out"], u["reasoning"] or None)))
            else:
                outs.append(Out("done", "done"))
            return outs
        if t == "error":
            return self.err(ev.get("message", ev))
        return self.unknown(ev)

    # ----- claude --------------------------------------------------------
    def claude(self, ev, t):
        if t == "result":
            outs = self.session(ev.get("session_id"))
            res = ev.get("result")
            if ev.get("is_error"):
                outs += self.err(f"{ev.get('subtype', 'error')}: {res or ''}".strip())
            elif res:
                outs += self.say(res)
            u = ev.get("usage") or {}
            cr = _int(u.get("cache_read_input_tokens")) or 0
            cc = _int(u.get("cache_creation_input_tokens")) or 0
            tin = (_int(u.get("input_tokens")) or 0) + cr + cc
            extra = ""
            if ev.get("num_turns") is not None:
                extra += f" · {ev['num_turns']} turns"
            if _int(ev.get("duration_ms")) is not None:
                extra += f" · {ev['duration_ms'] / 1000:.1f}s"
            if _int(ev.get("total_cost_usd")) is not None:
                extra += f" · ${ev['total_cost_usd']:.4f}"
            outs.append(Out("done", _tokens_line(tin, cr, _int(u.get("output_tokens")), None, extra)))
            return outs
        # stream-json shapes (not used by the current wake command, handled for robustness)
        if t == "system":
            return self.session(ev.get("session_id")) or ([Out("dim", "system " + str(ev.get("subtype", "")))]
                                                          if self.opts.verbose else [])
        if t in ("assistant", "user"):
            msg = ev.get("message") or {}
            content = msg.get("content")
            if isinstance(content, str):
                return self.say(content) if t == "assistant" else []
            outs = []
            for b in content or []:
                bt = b.get("type")
                if bt == "text" and t == "assistant":
                    outs += self.say(b.get("text", ""))
                elif bt == "thinking":
                    outs += self.think(b.get("thinking", ""))
                elif bt == "tool_use":
                    outs += self.tool_call_outs(b.get("name", "?"), b.get("input"), b.get("id"))
                elif bt == "tool_result":
                    name, is_cmd = self.calls.get(b.get("tool_use_id"), ("?", False))
                    c = b.get("content")
                    if isinstance(c, list):
                        c = "\n".join(x.get("text", "") for x in c if isinstance(x, dict))
                    bad = bool(b.get("is_error"))
                    if is_cmd or bad:
                        outs += self.result_outs("error" if bad else "ok", c if isinstance(c, str) else "", bad, key=b.get("tool_use_id") if is_cmd else None)
                    else:
                        outs.append(Out("res", f"  -> ok ({len((c or '').splitlines())} lines)"))
            return outs
        return self.unknown(ev)


class Printer:
    def __init__(self, out, color, follow, width):
        self.out = out
        self.color = color
        self.follow = follow
        self.width = width

    def paint(self, code, s):
        return f"\x1b[{code}m{s}\x1b[0m" if self.color and code else s

    def write(self, s):
        self.out.write(s + "\n")

    def header(self, meta):
        t = time.strftime("%H:%M:%S", time.localtime(meta["epoch"]))
        s = f"=== [{meta['agent']}] {meta['task']} {meta['reason']} · started {t} ==="
        self.write("")
        self.write(self.paint("1;" + COLORS[meta["agent"]], s))

    def prefix(self, agent, task, ts):
        p = f"[{agent}]".ljust(TAG_W) + " " + task.ljust(TASK_W) + " │ "
        stamp = ""
        if self.follow:
            stamp = self.paint("90", (ts or "--:--:--") + " ")
        return stamp, p, (len(ts or "--:--:--") + 1 if self.follow else 0) + len(p)

    STYLE = {"say": None, "think": "2", "cmd": "1", "res": None, "bad": "31", "out": "2", "tool": "36",
             "edit": "33", "done": "1", "info": "2", "dim": "2"}

    def emit(self, agent, task, outs, ts=None):
        stamp, pre, plen = self.prefix(agent, task, ts)
        pre_c = self.paint(COLORS[agent], pre)
        width = max(40, self.width - plen)
        code = None
        for o in outs:
            code = self.STYLE.get(o.style)
            for phys in o.text.split("\n"):
                if o.wrap:
                    parts = textwrap.wrap(phys, width, subsequent_indent=" " * o.hang,
                                          replace_whitespace=False, drop_whitespace=True,
                                          break_long_words=False, break_on_hyphens=False) or [""]
                else:
                    parts = [phys]
                for p in parts:
                    self.write(stamp + pre_c + self.paint(code, p.rstrip()))
        self.out.flush()


class Watcher:
    def __init__(self, opts, out):
        self.opts = opts
        self.dir = Path(opts.dir)
        color = opts.color
        width = opts.width or (shutil.get_terminal_size((120, 24)).columns if getattr(out, "isatty", lambda: False)() else 120)
        self.follow = not opts.once
        self.printer = Printer(out, color, self.follow, width)
        self.wakes = {}         # file name -> Wake (None when filtered out)
        self.disp_offset = 0
        self.disp_buf = b""
        self.backlog = True     # first scan: lines already there get no read-time stamp

    # ----- filters -------------------------------------------------------
    def wanted(self, meta):
        if self.opts.agent and meta["agent"] not in self.opts.agent:
            return False
        if self.opts.task and meta["task"].lower() not in {t.lower() for t in self.opts.task}:
            return False
        return True

    def dispatcher_wanted(self, line):
        if not (self.opts.agent or self.opts.task):
            return True
        needles = [x.lower() for x in (self.opts.agent or []) + (self.opts.task or [])]
        return any(n in line.lower() for n in needles)

    # ----- reading -------------------------------------------------------
    @staticmethod
    def read_new(path, offset):
        try:
            size = path.stat().st_size
            if size < offset:       # truncated or replaced: start over
                offset = 0
            if size == offset:
                return offset, b""
            with open(path, "rb") as fh:
                fh.seek(offset)
                data = fh.read()
            return offset + len(data), data
        except OSError:
            return offset, b""

    def stamp(self):
        return None if self.backlog else time.strftime("%H:%M:%S")

    def scan(self, final=False):
        """One pass over the directory. `final` flushes partial last lines (used by --once)."""
        try:
            names = sorted(os.listdir(self.dir))
        except OSError:
            names = []
        metas = []
        for n in names:
            m = parse_name(n)
            if m:
                metas.append((m["epoch"], m["seq"], n, m))
        metas.sort(key=lambda x: x[:2])
        if DISPATCHER_FILE in names:
            self.scan_dispatcher(final)
        for _, _, n, m in metas:
            if n not in self.wakes:
                self.wakes[n] = None
                if self.wanted(m):
                    self.wakes[n] = Wake(self.dir / n, m, self.opts)
                    self.printer.header(m)
            w = self.wakes[n]
            if w is not None:
                self.scan_wake(w, final)
        self.backlog = False

    def scan_wake(self, w, final):
        w.offset, data = self.read_new(w.path, w.offset)
        w.buf += data
        parts = w.buf.split(b"\n")
        w.buf = parts.pop()
        if final and w.buf:
            parts.append(w.buf)
            w.buf = b""
        for raw in parts:
            outs = w.handle_line(raw.decode("utf-8", "replace"))
            if outs:
                self.printer.emit(w.meta["agent"], w.meta["task"], outs, self.stamp())

    def scan_dispatcher(self, final):
        self.disp_offset, data = self.read_new(self.dir / DISPATCHER_FILE, self.disp_offset)
        self.disp_buf += data
        parts = self.disp_buf.split(b"\n")
        self.disp_buf = parts.pop()
        if final and self.disp_buf:
            parts.append(self.disp_buf)
            self.disp_buf = b""
        for raw in parts:
            line = raw.decode("utf-8", "replace").rstrip("\r")
            if line.strip() and self.dispatcher_wanted(line):
                p = self.printer
                stamp = p.paint("90", (self.stamp() or "--:--:--") + " ") if self.follow else ""
                p.write(stamp + p.paint(COLORS["dispatcher"], "[dispatcher] " + line))
                p.out.flush()


def build_parser():
    ap = argparse.ArgumentParser(description="Read-only live viewer for the job-board wake logs.")
    ap.add_argument("--once", action="store_true", help="print what exists and exit (default: then follow)")
    ap.add_argument("--agent", action="append", choices=["gpt", "deepseek", "claude"],
                    help="only this agent (repeatable)")
    ap.add_argument("--task", action="append", help="only this task id, e.g. T031 (repeatable)")
    ap.add_argument("--lines", type=int, default=6, help="output lines shown per command (default 6)")
    ap.add_argument("--full", action="store_true", help="show all output lines and do not cut long lines")
    ap.add_argument("--no-thinking", action="store_true", help="hide thinking / reasoning")
    ap.add_argument("--verbose", action="store_true", help="also show status events and stderr noise")
    ap.add_argument("--no-color", action="store_true", help="no ANSI colours (automatic when not a TTY)")
    ap.add_argument("--dir", default=str(DEFAULT_DIR), help="folder with the .out files")
    ap.add_argument("--max-seconds", type=float, default=None, help="stop following after S seconds")
    ap.add_argument("--poll", type=float, default=1.0, help=argparse.SUPPRESS)
    ap.add_argument("--width", type=int, default=0, help=argparse.SUPPRESS)
    return ap


def main(argv=None, out=None):
    out = out or sys.stdout
    opts = build_parser().parse_args(argv)
    isatty = getattr(out, "isatty", lambda: False)()
    opts.color = isatty and not opts.no_color and "NO_COLOR" not in os.environ
    try:
        out.reconfigure(errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    w = Watcher(opts, out)
    try:
        if opts.once:
            w.scan(final=True)
            return 0
        deadline = None if opts.max_seconds is None else time.monotonic() + opts.max_seconds
        while True:
            w.scan()
            if deadline is not None and time.monotonic() >= deadline:
                return 0
            sleep = opts.poll
            if deadline is not None:
                sleep = max(0.0, min(sleep, deadline - time.monotonic()))
            time.sleep(sleep)
    except KeyboardInterrupt:
        return 0
    except BrokenPipeError:
        try:
            sys.stdout = open(os.devnull, "w")
        except OSError:
            pass
        return 0


if __name__ == "__main__":
    sys.exit(main())
