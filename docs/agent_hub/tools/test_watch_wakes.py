"""Tests for watch_wakes.py (small inline fixtures, temp dirs; no agent or dispatcher is started).

Run from the repository root:
    python3 -m unittest discover -s docs/agent_hub/tools -p 'test_watch_wakes.py' -v
or:
    python3 docs/agent_hub/tools/test_watch_wakes.py -v
"""
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import watch_wakes as ww  # noqa: E402

EPOCH = 1793510375


def j(**kw):
    return json.dumps(kw, ensure_ascii=False)


def jl(*objs):
    return "".join((o if isinstance(o, str) else json.dumps(o, ensure_ascii=False)) + "\n" for o in objs)


class Base(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.dir = Path(self._td.name)
        self.addCleanup(self._td.cleanup)

    def put(self, name, text, mode="w"):
        with open(self.dir / name, mode, encoding="utf-8") as fh:
            fh.write(text)
        return self.dir / name

    def run_once(self, *args):
        out = io.StringIO()
        rc = ww.main(["--once", "--no-color", "--dir", str(self.dir), "--width", "100", *args], out=out)
        self.assertEqual(rc, 0)
        return out.getvalue()

    def watcher(self, *args):
        opts = ww.build_parser().parse_args(["--no-color", "--dir", str(self.dir), "--width", "100", *args])
        opts.color = False
        out = io.StringIO()
        return ww.Watcher(opts, out), out


GPT = f"{EPOCH}_0002_gpt_T031_claim.out"
DSH = f"{EPOCH}_0001_deepseek_T032_claim.out"
CLA = f"{EPOCH + 5}_0003_claude_T040_review.out"


class TestNames(unittest.TestCase):
    def test_parse(self):
        m = ww.parse_name(GPT)
        self.assertEqual((m["epoch"], m["seq"], m["agent"], m["task"], m["reason"]), (EPOCH, 2, "gpt", "T031", "claim"))
        m = ww.parse_name("1793510375_0011_deepseek_T-loc-004_answer.out")
        self.assertEqual((m["task"], m["reason"]), ("T-loc-004", "answer"))
        self.assertIsNone(ww.parse_name("dispatcher.out"))
        self.assertIsNone(ww.parse_name("notes.txt"))

    def test_header_has_agent_task_reason_and_local_time(self):
        d = tempfile.mkdtemp()
        Path(d, GPT).write_text("")
        out = io.StringIO()
        ww.main(["--once", "--no-color", "--dir", d], out=out)
        want = time.strftime("%H:%M:%S", time.localtime(EPOCH))
        self.assertIn(f"=== [gpt] T031 claim · started {want} ===", out.getvalue())

    def test_strip_shell(self):
        self.assertEqual(ww.strip_shell("/bin/bash -lc 'cat a.txt'"), "cat a.txt")
        self.assertEqual(ww.strip_shell("/bin/bash -lc \"python3 - <<'PY'\nprint(1)\nPY\""), "python3 - <<'PY'\nprint(1)\nPY")
        self.assertEqual(ww.strip_shell("ls -la"), "ls -la")


class TestCodex(Base):
    def test_full_run(self):
        long_out = "\n".join(f"line{i}" for i in range(10)) + "\n"
        self.put(GPT, jl(
            "Reading additional input from stdin...",
            {"type": "thread.started", "thread_id": "tid-123"},
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"id": "i0", "type": "agent_message", "text": "Hello there, checking."}},
            {"type": "item.started", "item": {"id": "i1", "type": "command_execution",
                                              "command": "/bin/bash -lc 'cat docs/x.md'", "aggregated_output": "",
                                              "exit_code": None, "status": "in_progress"}},
            {"type": "item.completed", "item": {"id": "i1", "type": "command_execution",
                                                "command": "/bin/bash -lc 'cat docs/x.md'", "aggregated_output": long_out,
                                                "exit_code": 0, "status": "completed"}},
            {"type": "item.completed", "item": {"id": "i2", "type": "command_execution", "command": "/bin/bash -lc false",
                                                "aggregated_output": "", "exit_code": 1, "status": "failed"}},
            {"type": "item.completed", "item": {"id": "i3", "type": "file_change",
                                                "changes": [{"path": str(ww.REPO / "docs/a/B.md"), "kind": "add"}],
                                                "status": "completed"}},
            {"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 60,
                                                 "output_tokens": 7, "reasoning_output_tokens": 3}},
        ))
        txt = self.run_once("--lines", "3")
        self.assertIn("session: codex resume tid-123", txt)
        self.assertIn("says: Hello there, checking.", txt)
        self.assertIn("$ cat docs/x.md", txt)
        self.assertNotIn("bash -lc", txt)
        self.assertEqual(txt.count("$ cat docs/x.md"), 1)          # started + completed print the command once
        self.assertIn("-> exit 0", txt)
        self.assertIn("line0", txt)
        self.assertIn("line2", txt)
        self.assertNotIn("line3", txt)
        self.assertIn("… (+7 lines)", txt)
        self.assertIn("-> exit 1", txt)
        self.assertIn("edits docs/a/B.md (add)", txt)              # relative to the repo root
        self.assertIn("done: tokens in 100 (cached 60) out 7", txt)
        self.assertNotIn("Reading additional input", txt)
        self.assertTrue(all(l.startswith("[gpt]") or l.startswith("===") or not l for l in txt.splitlines()))

    def test_full_shows_everything_and_does_not_cut(self):
        out = "\n".join(f"line{i}" for i in range(20)) + "\n" + "x" * 500 + "\n"
        self.put(GPT, jl({"type": "item.completed", "item": {"id": "a", "type": "command_execution", "command": "echo",
                                                              "aggregated_output": out, "exit_code": 0, "status": "completed"}}))
        cut = self.run_once()
        self.assertIn("+15 lines", cut)
        full = self.run_once("--full")
        self.assertIn("line19", full)
        self.assertIn("x" * 500, full)
        self.assertNotIn("… (+", full)
        # a long line is cut at 400 characters without --full
        self.put(GPT, jl({"type": "item.completed", "item": {"id": "a", "type": "command_execution", "command": "echo",
                                                              "aggregated_output": "y" * 500 + "\n", "exit_code": 0,
                                                              "status": "completed"}}))
        c = self.run_once()
        self.assertIn("y" * 400 + "… (+100 chars)", c)

    def test_reasoning_and_no_thinking(self):
        self.put(GPT, jl({"type": "item.completed", "item": {"id": "r", "type": "reasoning", "text": "pondering hard"}}))
        self.assertIn("thinks: pondering hard", self.run_once())
        self.assertNotIn("pondering", self.run_once("--no-thinking"))

    def test_error_events_and_unknown(self):
        self.put(GPT, jl({"type": "error", "message": "boom"}, {"type": "weird.event", "x": 1}))
        txt = self.run_once()
        self.assertIn("error: boom", txt)
        self.assertIn('?? weird.event: {"type": "weird.event", "x": 1}', txt)

    def test_interleaved_results_are_attributed(self):
        self.put(GPT, jl(
            {"type": "item.started", "item": {"id": "a", "type": "command_execution", "command": "echo AAA", "status": "in_progress"}},
            {"type": "item.started", "item": {"id": "b", "type": "command_execution", "command": "echo BBB", "status": "in_progress"}},
            {"type": "item.completed", "item": {"id": "a", "type": "command_execution", "command": "echo AAA",
                                                "aggregated_output": "AAA\n", "exit_code": 0, "status": "completed"}},
        ))
        self.assertIn("(of: echo AAA)", self.run_once())


class TestDsh(Base):
    def test_run_with_pairing_and_final(self):
        self.put(DSH, jl(
            {"type": "session", "sessionId": "session-abc", "cwd": str(ww.REPO)},
            {"type": "status", "phase": "turn_start", "turn": 1},
            {"type": "status", "phase": "step_start", "turn": 1, "step": 1},
            {"type": "thinking", "text": "let me look"},
            {"type": "tool_call", "callId": "c1", "tool": "bash", "input": {"command": "ls docs", "description": "List docs"}},
            {"type": "tool_call", "callId": "c2", "tool": "read", "input": {"file_path": str(ww.REPO / "docs/README.md")}},
            {"type": "tool_result", "callId": "c1", "status": "completed", "result": "a\nb\nc\nd\n"},
            {"type": "tool_result", "callId": "c2", "status": "completed", "result": "1: x\n2: y\n"},
            {"type": "tool_call", "callId": "c3", "tool": "write", "input": {"file_path": "out.py", "content": "print(1)\n" * 20}},
            {"type": "tool_result", "callId": "c3", "status": "error", "result": "permission denied"},
            {"type": "text", "text": "I am done with the work."},
            {"type": "status", "phase": "step_end", "turn": 1, "step": 1,
             "usage": {"inputTokens": 10, "outputTokens": 2, "cacheReadTokens": 5, "totalTokens": 17}},
            {"type": "status", "phase": "step_end", "turn": 1, "step": 2,
             "usage": {"inputTokens": 20, "outputTokens": 3, "cacheReadTokens": 6}},
            {"type": "status", "phase": "turn_end", "turn": 1, "reason": "completed"},
            {"type": "final", "text": "I am done with the work."},
        ))
        txt = self.run_once("--lines", "2")
        self.assertIn("session: dsh tui --resume session-abc", txt)
        self.assertIn("thinks: let me look", txt)
        self.assertIn("$ ls docs", txt)
        self.assertIn("# List docs", txt)
        self.assertIn("-> completed", txt)
        self.assertIn("… (+2 lines)", txt)                          # a,b shown, c,d cut
        self.assertIn("tool read: docs/README.md", txt)
        self.assertIn("-> completed (2 lines)", txt)
        self.assertIn("tool write: out.py, content=<180 chars>", txt)
        self.assertIn("-> error", txt)
        self.assertIn("permission denied", txt)
        self.assertEqual(txt.count("I am done with the work."), 1)  # final == last text: not repeated
        self.assertIn("done: tokens in 30 (cached 11) out 5", txt)  # summed over steps
        self.assertNotIn("step_start", txt)                         # status only with --verbose
        v = self.run_once("--verbose")
        self.assertIn("status phase=step_start turn=1 step=1", v)

    def test_final_with_new_text_and_failed_turn_and_error_event(self):
        self.put(DSH, jl(
            {"type": "text", "text": "partial"},
            {"type": "status", "phase": "turn_end", "turn": 1, "reason": "aborted"},
            {"type": "final", "text": "the answer"},
            {"type": "error", "message": "usage: bad option"},
        ))
        txt = self.run_once()
        self.assertIn("error: turn ended: aborted", txt)
        self.assertIn("final: the answer", txt)
        self.assertIn("error: usage: bad option", txt)

    def test_no_thinking(self):
        self.put(DSH, jl({"type": "thinking", "text": "secret thoughts"}))
        self.assertNotIn("secret", self.run_once("--no-thinking"))


class TestClaude(Base):
    def test_result_object(self):
        self.put(CLA, json.dumps({
            "type": "result", "subtype": "success", "is_error": False, "result": "Reviewed T040: all fine.",
            "session_id": "sess-9", "num_turns": 4, "duration_ms": 12500, "total_cost_usd": 0.1234,
            "usage": {"input_tokens": 10, "cache_read_input_tokens": 90, "cache_creation_input_tokens": 5, "output_tokens": 40}}) + "\n")
        txt = self.run_once()
        self.assertIn("session: claude --resume sess-9", txt)
        self.assertIn("says: Reviewed T040: all fine.", txt)
        self.assertIn("done: tokens in 105 (cached 90) out 40 · 4 turns · 12.5s · $0.1234", txt)

    def test_error_result(self):
        self.put(CLA, json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": True,
                                  "result": "", "session_id": "s"}) + "\n")
        self.assertIn("error: error_max_turns", self.run_once())

    def test_stream_json_shapes(self):
        self.put(CLA, jl(
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"},
                                                           {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "f1\nf2"}]}},
        ))
        txt = self.run_once()
        self.assertIn("says: hi", txt)
        self.assertIn("$ ls", txt)
        self.assertIn("f2", txt)


class TestRobustness(Base):
    def test_malformed_and_non_json(self):
        self.put(GPT, "not json at all\n{broken json\n[1,2,3]\n42\n\n"
                      + '{"type":"item.completed","item":"notadict"}\n'
                      + '{"type":"item.completed","item":{"id":"x","type":"command_execution","command":null,"exit_code":"zz"}}\n'
                      + "2025-01-01T00:00:00Z ERROR rmcp::transport: worker quit\n"
                      + jl({"type": "item.completed", "item": {"id": "ok", "type": "agent_message", "text": "still alive"}}))
        self.put(DSH, "}{\n" + jl({"type": "tool_result", "callId": "never-called", "status": "completed", "result": None},
                                    {"type": "tool_call", "callId": "x", "tool": "mystery", "input": None},
                                    {"type": "totally_new"}))
        self.put(CLA, "plain text from stderr\n{\"type\":\"result\"}\n")
        txt = self.run_once()
        self.assertIn("not json at all", txt)
        self.assertIn("{broken json", txt)
        self.assertIn("says: still alive", txt)
        self.assertIn("ERROR rmcp", txt)
        self.assertIn("?? totally_new", txt)
        self.assertIn("tool mystery:", txt)

    def test_missing_dir_and_empty_dir(self):
        out = io.StringIO()
        self.assertEqual(ww.main(["--once", "--no-color", "--dir", str(self.dir / "nope")], out=out), 0)
        self.assertEqual(out.getvalue(), "")

    def test_dispatcher_lines(self):
        self.put("dispatcher.out", "started pilot\npaused: budget\n")
        txt = self.run_once()
        self.assertIn("[dispatcher] started pilot", txt)
        self.assertIn("[dispatcher] paused: budget", txt)

    def test_colors_only_on_tty(self):
        self.put(GPT, jl({"type": "item.completed", "item": {"id": "a", "type": "agent_message", "text": "hi"}}))

        class Tty(io.StringIO):
            def isatty(self):
                return True

        out = Tty()
        ww.main(["--once", "--dir", str(self.dir)], out=out)
        self.assertIn("\x1b[32m", out.getvalue())               # gpt green
        out = Tty()
        ww.main(["--once", "--no-color", "--dir", str(self.dir)], out=out)
        self.assertNotIn("\x1b[", out.getvalue())
        self.assertNotIn("\x1b[", self.run_once())


class TestFilters(Base):
    def setUp(self):
        super().setUp()
        self.put(GPT, jl({"type": "item.completed", "item": {"id": "a", "type": "agent_message", "text": "from gpt"}}))
        self.put(DSH, jl({"type": "text", "text": "from deepseek"}))
        self.put(f"{EPOCH + 9}_0004_gpt_T099_review.out",
                 jl({"type": "item.completed", "item": {"id": "a", "type": "agent_message", "text": "gpt other task"}}))

    def test_order_by_epoch_then_seq(self):
        txt = self.run_once()
        self.assertLess(txt.index("deepseek] T032 claim"), txt.index("gpt] T031 claim"))
        self.assertLess(txt.index("gpt] T031 claim"), txt.index("gpt] T099 review"))

    def test_agent_filter(self):
        txt = self.run_once("--agent", "gpt")
        self.assertIn("from gpt", txt)
        self.assertIn("gpt other task", txt)
        self.assertNotIn("deepseek", txt)
        both = self.run_once("--agent", "gpt", "--agent", "deepseek")
        self.assertIn("from deepseek", both)

    def test_task_filter(self):
        txt = self.run_once("--task", "T031")
        self.assertIn("from gpt", txt)
        self.assertNotIn("T099", txt)
        self.assertNotIn("T032", txt)
        two = self.run_once("--task", "T031", "--task", "t032")
        self.assertIn("from deepseek", two)
        self.assertNotIn("T099", two)
        self.assertEqual(self.run_once("--agent", "gpt", "--task", "T032").strip(), "")

    def test_dispatcher_lines_filtered(self):
        self.put("dispatcher.out", "wake gpt T031 claim\nwake deepseek T032 claim\n")
        txt = self.run_once("--task", "T031")
        self.assertIn("wake gpt T031", txt)
        self.assertNotIn("T032", txt)


class TestFollow(Base):
    def test_partial_line_waits_for_newline(self):
        w, out = self.watcher()
        line = jl({"type": "item.completed", "item": {"id": "a", "type": "agent_message", "text": "complete message"}})
        path = self.put(GPT, line[:30])
        w.scan()
        self.assertIn("=== [gpt] T031 claim", out.getvalue())
        self.assertNotIn("complete message", out.getvalue())
        self.assertNotIn("item.completed", out.getvalue())
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line[30:])
        w.scan()
        self.assertIn("says: complete message", out.getvalue())
        self.assertEqual(out.getvalue().count("complete message"), 1)
        w.scan()
        self.assertEqual(out.getvalue().count("complete message"), 1)   # nothing is printed twice

    def test_once_flushes_last_partial_line(self):
        self.put(GPT, '{"type":"item.completed","item":{"id":"a","type":"agent_message","text":"no newline"}}')
        self.assertIn("says: no newline", self.run_once())

    def test_follow_stamps_read_time_and_backlog_marker(self):
        self.put(GPT, jl({"type": "item.completed", "item": {"id": "a", "type": "agent_message", "text": "old"}}))
        w, out = self.watcher()
        w.printer.follow = True
        w.scan()
        w.scan()
        with open(self.dir / GPT, "a") as fh:
            fh.write(jl({"type": "item.completed", "item": {"id": "b", "type": "agent_message", "text": "new"}}))
        w.scan()
        lines = out.getvalue().splitlines()
        old = next(l for l in lines if "says: old" in l)
        new = next(l for l in lines if "says: new" in l)
        self.assertTrue(old.startswith("--:--:-- [gpt]"), old)
        self.assertRegex(new, r"^\d\d:\d\d:\d\d \[gpt\]")

    def test_new_file_after_start_is_picked_up(self):
        w, out = self.watcher()
        w.scan()
        self.assertEqual(out.getvalue(), "")
        self.put(DSH, jl({"type": "session", "sessionId": "s1", "cwd": "."}, {"type": "text", "text": "late file"}))
        w.scan()
        txt = out.getvalue()
        self.assertIn("=== [deepseek] T032 claim", txt)
        self.assertIn("says: late file", txt)

    def test_end_to_end_follow_with_max_seconds(self):
        self.put(GPT, jl({"type": "item.completed", "item": {"id": "a", "type": "agent_message", "text": "first"}}))
        out = io.StringIO()
        rcs = []
        t = threading.Thread(target=lambda: rcs.append(ww.main(
            ["--dir", str(self.dir), "--no-color", "--max-seconds", "2.5", "--poll", "0.1"], out=out)))
        t0 = time.monotonic()
        t.start()
        time.sleep(0.6)
        self.put(CLA, json.dumps({"type": "result", "result": "created later", "session_id": "z", "usage": {}}) + "\n")
        with open(self.dir / GPT, "a") as fh:
            fh.write(jl({"type": "item.completed", "item": {"id": "b", "type": "agent_message", "text": "appended"}}))
        t.join(timeout=10)
        self.assertFalse(t.is_alive())
        self.assertEqual(rcs, [0])
        self.assertGreaterEqual(time.monotonic() - t0, 2.4)
        txt = out.getvalue()
        self.assertIn("says: first", txt)
        self.assertIn("says: appended", txt)
        self.assertIn("=== [claude] T040 review", txt)
        self.assertIn("says: created later", txt)

    def test_keyboard_interrupt_exits_cleanly(self):
        real = ww.time.sleep

        def boom(_):
            raise KeyboardInterrupt

        ww.time.sleep = boom
        try:
            self.assertEqual(ww.main(["--dir", str(self.dir), "--no-color"], out=io.StringIO()), 0)
        finally:
            ww.time.sleep = real

    def test_read_only(self):
        p = self.put(GPT, jl({"type": "item.completed", "item": {"id": "a", "type": "agent_message", "text": "x"}}))
        before = (p.stat().st_mtime_ns, p.read_bytes(), sorted(os.listdir(self.dir)))
        self.run_once()
        self.assertEqual((p.stat().st_mtime_ns, p.read_bytes(), sorted(os.listdir(self.dir))), before)


class TestWrapping(Base):
    def test_long_message_is_wrapped_with_prefix_on_every_line(self):
        text = " ".join(f"word{i}" for i in range(60))
        self.put(GPT, jl({"type": "item.completed", "item": {"id": "a", "type": "agent_message", "text": text}}))
        lines = [l for l in self.run_once().splitlines() if "word" in l]
        self.assertGreater(len(lines), 2)
        self.assertTrue(all(l.startswith("[gpt]") and len(l) <= 100 for l in lines))
        self.assertIn("word59", lines[-1])


if __name__ == "__main__":
    unittest.main()
