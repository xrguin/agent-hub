# Job board: agents post, claim and finish tasks; the user only checks (plan, revision 1)

[claude] 2026-10-31. **Status: proposal, nothing is in force.** It needs the user's answers to J1–J5 (§8). It builds on protocol v2's commit lock (T012) and claims (T013), which are not built yet.

## 1. What the user asked
The user's words, in [claude]'s chat:
1. "有没有什么办法，弄一个共享文档…设置job start然后deepseek和gpt都actively check这个文档，有变化了就查然后领任务，认为任务完成了给我查，然后我说job end就不用actively查这个文档了。任务背领了就把任务删除。这样会不会很费token"
2. "这个文档贴任务删任务都是agent自己弄的，我只查"
3. "写方案"

## 2. Cost: who watches decides the bill (estimates, not measured)

| Who watches | While nothing happens | Per new task |
|---|---|---|
| Each agent re-reads the board every few minutes | 1–5 × 10⁴ tokens per check; about 3–15 M tokens per agent in a 10 h day | the same, plus the work |
| **A script with no model** watches, and wakes one agent only when there is something for it | **0** | **about 2–5 × 10⁴ tokens** to start and read the rules, plus the work (which costs the same either way) |

This plan uses the second row.

## 3. Parts

| Part | What it is | Writes |
|---|---|---|
| `BOARD.md` | The shared document. Sections: **Open**, **For the user to check**, **Questions for the user**, **Paused**. The header holds the job goal, start time, limits and counters | Agents only, through the tools; the user only reads |
| `JOB.json` | `{active, goal, started, ended, limits, quote}` | `tools/job.py start|end`, run by the agent that heard the user say it |
| `tools/job_dispatcher.py` | Standard-library loop. No model; costs nothing while it waits | `logs/dispatcher.jsonl`: one line per wake (time, agent, reason, exit code, minutes, tokens if the CLI reports them) |
| T012's `hubtx` lock and T013's `new_task.py`, `claim.py`, `hub_state.py` | Ids, claims and state under one OS lock. Two agents cannot take the same task, and two commits cannot collide | Task cards, `.status.md` events |

## 4. Flow
1. **Start.** The user says `job start: <goal>` in any agent's chat. That agent records the quote in DECISIONS.md and runs `job.py start`. The user, or [claude] at the user's request, starts the dispatcher in a terminal tab.
2. **Post.** An agent creates a task card with `new_task.py`. The card has a goal, the files it may change, an acceptance command and a reviewer. In the same transaction it appends a short entry under **Open**. [claude] does most of the splitting into tasks, but any agent may post.
3. **Wake.** Every 10 s the dispatcher checks git HEAD and the file times. It computes each agent's next action with `hub_state.py` and wakes an agent only when one of these holds:
   - (a) an **Open** entry exists that the agent may claim, and the agent has no task in progress;
   - (b) the agent is the reviewer of a task in review at a revision it has not yet reviewed;
   - (c) the user answered a question the agent asked.

   At most one run per agent at a time. Changes an agent makes itself do not wake it again.
4. **Claim.** The woken agent runs `claim.py`. This writes the claim event and removes the entry from **Open** in one transaction. The task text survives in the card, which is the user's "领了就删除".
5. **Finish.**
   - The owner delivers and sets the task to review. The dispatcher then wakes the reviewer.
   - **Accepted:** the reviewer moves the task to **For the user to check**, as one line: the result in a sentence and a link.
   - **Changes requested:** the owner is woken again.
6. **User check.** The user reads only **For the user to check** and **Questions for the user**, then says OK or not OK in any chat. The agent who hears it records it:
   - OK → done;
   - not OK → the task is reopened with the user's words.
7. **End.** The user says `job end`. `job.py end` sets `active=false`. The dispatcher starts no new runs, lets running ones finish and exits.

**Wake commands.** Each is run from the repo root with a fixed prompt: "[tag] job-board wake, reason <reason>, task <id>: follow docs/agent_hub/README.md and the job rules; do this one action, then exit."

| Agent | Command |
|---|---|
| GPT | `codex exec -C <repo> -s workspace-write "<prompt>"` |
| DeepSeek | `dsh --profile headless "<prompt>"` |
| Claude, only if J4 says yes | `claude -p "<prompt>"` with a restricted tool list |

## 5. Rules during a job (they change protocol v1 for the job's duration only)
- **J-R1:** `job start: <goal>` is a standing go-ahead for **CPU-only** tasks that serve the goal. This replaces the per-check go-ahead of S4 A+ until `job end`.
- **J-R2:** these stay with the user:
  - GPU runs and long compute, network access, `git push`, deleting files, and changes outside the repository;
  - every choice that is the user's (for example, which start-heading source).

  For any of these, the agent writes an entry under **Questions for the user** and stops that task.
- **J-R3:** independence.
  - An agent may not claim a task it posted, or review its own work.
  - Every posted task needs an acceptance command and one sentence saying how it serves the goal.
- **J-R4:** nothing leaves the record. Entries leave the board when claimed; cards, status events, logs and DECISIONS stay append-only as now.
- **J-R5:** the dispatcher enforces the limits (§6). At a limit it pauses all wakes and writes the reason under **Paused**. Only the user can resume.

## 6. Limits (defaults; the user may change them)

| Limit | Default |
|---|---|
| Tasks posted per job | 10 |
| Wakes per agent per hour | 6 |
| Wakes per job, all agents | 40 |
| Wall time per wake | 30 min; then the run is stopped and its task returns to **Open** with a note |
| Tokens per job | Summed where the CLI reports them (`codex exec --json`, `dsh --json`); pause at 3 M per agent |

**Worst case:** about 40 wakes × 5 × 10⁴ = 2 M tokens of start-up overhead per job, plus the work itself.

## 7. Build order
1. **T012 and T013** (protocol v2 lock, ids, claims, state). They need U1 and U5 of PROTOCOL_V2_PLAN.md.
2. **T023, new: the board, `job.py`, `job_dispatcher.py`.** Owner [claude], reviewer [gpt].
   - Tests run in a throw-away repository with **fake agents**: scripts that claim, deliver and review. So testing spends no tokens.
   - Test cases:
     - two agents racing for one entry;
     - a limit being hit;
     - `job end` during a run;
     - a crashed run returning its task to **Open**;
     - no self-wake.
3. **Pilot.** One small CPU job with one or two tasks, while the user watches. It checks the points in §9 and measures the tokens per wake.
4. **Adoption.** The user says the job board is in use; this is recorded in DECISIONS.md.

## 8. Decisions for the user

| # | Question | Recommendation |
|---|---|---|
| J1 | Approve U1 and U5 of PROTOCOL_V2_PLAN.md, so that T012 and T013 can start (prerequisite)? | Yes |
| J2 | Accept J-R1: during a job, `job start` is the go-ahead for CPU tasks that serve the goal? | Yes |
| J3 | Accept the default limits (§6)? | Yes |
| J4 | Should the dispatcher also wake [claude] (`claude -p`), or does [claude] work only in your chat? | Wake [claude] too. Otherwise its reviews wait for you |
| J5 | Give `job start` and `job end` by saying them in any agent's chat (that agent records your words and runs `job.py`)? | Yes |

## 9. Not verified
- **DeepSeek headless approvals:** how `dsh --profile headless` handles a tool that needs approval. It has no approval UI, so it may abort or block. The pilot must show it.
- **Token reporting:** whether the CLIs report token use per run, so the token limit can be enforced.
- **Start-up cost:** the 2–5 × 10⁴ tokens per wake is an estimate, not a measurement.
- **Cooperative, not secure:** the scheme only works if the agents follow it. Raw `git` bypasses the lock, as in protocol v2.

## Status
- 2026-10-31 14:24 [claude]: the user answered J1–J5 "都按推荐" (all as recommended; DECISIONS.md). Build task: T023. Not in force until T023 is accepted, the pilot has run and the user says the board is in use.
- 2026-10-31 23:49 [claude]: §9 "DeepSeek headless", first finding. The headless profile ships with dsh and runs on Node 24 (nvm default since 2026-10-31 23:49; a second, system copy on old Node exited silently and was removed at the user's request). A read-only headless task stopped before any model call, so approvals and token reporting are still untested:
  - default route `deepseek-official`: MISSING_CREDENTIAL (needs DEEPSEEK_API_KEY);
  - with the web profile's `provider: deepseek-account` copied in: ACCOUNT_SIGN_IN_REQUIRED. The account login works in the web profile only, and the account route never falls back to an API key. The profile patch was restored afterwards.
  - Unattended DeepSeek wakes therefore need a DeepSeek API key that the user stores (the web Models page, or DEEPSEEK_API_KEY in the dispatcher's environment); otherwise DeepSeek stays manual.
- 2026-10-31 23:58 [claude]: §9 "DeepSeek headless", resolved. The user saved a DeepSeek API key through dsh. The shipped headless profile disables the stored-credential store. I added the three rows its README gives ("Credentials": `credentials`, `authorization`, `deepseek-account` with `disabled: false`) to `~/.dsh/profiles/headless/cordis.patch.yml`. The same read-only task then completed (exit 0, 4 s, correct answer).
  - **Approvals:** the bash tool ran the command with no approval step. Headless DeepSeek runs shell commands unattended, so the board stays cooperative, not secure (as stated above).
  - **Tokens:** `--json` reports usage on every `status` / `step_end` event (`inputTokens`, `outputTokens`, `cacheReadTokens`, `cacheWriteTokens`, `totalTokens`). This trivial task took 2 steps with totalTokens 8,743 and 8,761; 8,576 of the second were cache reads.
  - **Gap:** `job_dispatcher.parse_tokens` is still the stub that returns None for every agent, so the per-agent token limit is not enforced. The pilot will measure tokens from the saved run outputs; a parser is a follow-up.
- Correction [claude] 2026-10-31 23:58: the previous entry's conclusion, that headless account login is web-only, was premature. The headless profile only disables the stored credentials, and the README rows above restore them, including the account consumer. The API-key route is what this test used.
