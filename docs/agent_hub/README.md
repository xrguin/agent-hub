# Agent hub: shared working folder for all agents

Several AI agents work on this repository at the same time, in this same folder: Claude Code, GPT (Codex) and DeepSeek. They check each other's work, catch each other's errors and suggest next steps. The user decides; agents propose, implement and check.

Every agent's config points to this file. Read it at the start of every session.

| Tag | Agent |
|---|---|
| `[claude]` | Claude Code |
| `[gpt]` | GPT through Codex |
| `[deepseek]` | DeepSeek |

Sign every entry with your tag and the date (YYYY-MM-DD).

## 1. Reading order at the start of every session

1. This README.
2. [STATE.md](STATE.md): the current goal, what is established, open questions, and the current settings.
3. [DECISIONS.md](DECISIONS.md): the user's decisions. Never act against one without asking the user.
4. [tasks/](tasks/): every task with status `open`, `claimed` or `review`.
5. [reviews/](reviews/): reviews of your own work dated after your last log entry. Answer every `WRONG` verdict before you start new work.
6. The last entries of your own `logs/<tag>.md`.
7. `CLAUDE.md` at the repository root (local, not in git): the environment, units, frames and module discipline. `AGENTS.md` points to it.

**A job-board wake (§9) skips this list.** It reads only what its wake prompt names: the task card, its status file, its deliverables, and the task's reviews and responses (user decision D004).

[INDEX.md](INDEX.md) lists where the experiment records live. The records stay in place; this hub only links to them.

## 2. Files and who writes them

| File | Contents | Who writes |
|---|---|---|
| `README.md` | This protocol | Changed only on a user request |
| `STATE.md` | One shared picture of where the project stands | Any agent, under the rules in section 3 |
| `DECISIONS.md` | The user's decisions, dated, append-only | Any agent, only to record what the user said |
| `INDEX.md` | Links to records elsewhere in the repository | Any agent, one line per record |
| `tasks/T###_<slug>.md` | One task per file | Its owner; others append status lines only |
| `logs/<tag>.md` | What that agent did, session by session | That agent only |
| `reviews/YYYY-MM-DD_T###_<tag>.md` | One review of one task | Its author only |
| `memos/YYYY-MM-DD_<topic>.md` | A note on one topic: a formulation, a plan, a derivation | Its author; others respond in a review |

One writer per file wherever possible: Markdown files have no locking, so two agents rewriting the same file lose each other's edits.

## 3. Write rules

- **Read a shared file immediately before you change it**, since another agent may have changed it. Change only the sections your task covers.
- **STATE.md:** every change cites a task id and adds a dated line to its change list at the bottom.
- **DECISIONS.md:** append only. Record only what the user stated in chat, with the date, quoting the user where you can. A proposal, a recommendation or an inference is not a decision. When a new decision overrides an old one, add the new entry, and add a "Superseded YYYY-MM-DD" note to the old one.
- **Corrections** to any shared file are dated lines, `Correction [tag] YYYY-MM-DD: ...`. Never rewrite silently, and never delete a file.
- **Numbers:** every number you report comes with the command that reproduces it, or the file it was read from.
- **Git:** commit only the files you changed (`git add <paths>`, never `git add -A`). Start the message with your tag and the task id, e.g. `[gpt] T003: ...`. Do not push unless the user asks.
- **Tasks are created only in the checkout of `main`** (hub_config `task_branch`), so task ids cannot collide across branches. Work on a branch claims and submits tasks that were created in main.

### Commit guard

With hub_config `commit_guard` set to `"scoped"`, the `commit-msg` hook checks every commit. A subject that starts with `[claude]`, `[gpt]` or `[deepseek]` is an agent commit (a task id is not required). Any other subject is a user commit.

- For everyone: append-only files (DECISIONS.md, INDEX.md, QUESTIONS.md, TASK_NAMES.tsv, `tasks/T###.status.md`) may only grow, and appended status lines must parse.
- For agent commits only: each status event must be authorized for the committing tag, and the commit may not touch a path inside another agent's active claim, another agent's log or review, or hub_config.json without the user's `set_config` decision (recorded with `job.py set-config`, section 9). A claim stops guarding its files once its task is accepted or cancelled.
- A merge commit gets the checks of a user commit, whatever its subject: it brings in work already committed on its branch, including other agents' events.
- For agent commits only: a task card may be changed only by the task's owner or its creator.
- For everyone, in every `commit_guard` mode: a commit is refused if it adds a key-like string, in a file or in its message, or a secret file such as `.env` or `*.pem`, or `hub_config.local.json`. The refusal names the file, the line and the kind of secret, never the value. For a false alarm, add `secret-guard: allow` to that line. `python3 docs/agent_hub/tools/secret_guard.py --all` checks every tracked file. Never paste a key into a log, review, task card or chat.
- Everything else (code, data, docs, slides) is allowed.

The user can bypass the guard with `git commit --no-verify`; an agent must not. The guard is cooperative: an agent that leaves out its tag is treated as the user. Install it once per clone with `python3 docs/agent_hub/tools/install_hooks.py --tag <tag> --task <task>`. The hook lives in the common git directory, so it covers linked worktrees, and it runs the main checkout's `commit_msg_hook.py`.

## 4. Task lifecycle

`open` → `claimed` (owner set) → `review` → `done`.

- **Claim before you edit.** Do not edit files that another agent's `claimed` task lists.
- **When you finish:** append a log entry, then set the task to `review`.
- **Done:** a task becomes `done` only when a reviewer marks every claim `CONFIRMED`, or when the user says so. **No agent marks its own task done.**
- **WRONG:** the task goes back to `claimed`, and the owner answers in the task file.
- **Disagreement:** if owner and reviewer still disagree after one exchange, set the task to `disputed`, and the user decides.

Task file template:

```markdown
# T###: <title>
Owner: [tag] · Reviewer: [tag or open] · Status: open | claimed | review | done | disputed
Created YYYY-MM-DD by [tag] · User request: "<quote>" (or: proposed, awaiting user go-ahead)

## Goal
## Files this task may change
## Must not change
## Acceptance check (exact command, interpreter, expected output)
## Result (owner fills in: what changed, numbers with their commands, doubts)
## Status history
- YYYY-MM-DD [tag] <status change, one line>
```

## 5. Review protocol

To review, take the oldest task in `review` that you did not write.

1. **Rerun** its acceptance check yourself.
2. **Recompute at least three key numbers from the raw data**, with your own script, not the author's.
3. **Check the code** that the claims depend on: frames, signs, units, indices, train/test leakage.
4. **Look for what is missing**, not only for what is wrong. Ask what question the author did not ask: where did the failures happen, and is a pass judged on the estimate or on the true state? Re-checking an author's numbers does not catch this kind of gap (see the 2026-10-30 diagnosis, section 6).
5. **Write** `reviews/YYYY-MM-DD_T###_<tag>.md`, then append one status line to the task file.

Review file template:

```markdown
# Review of T###, by [tag], YYYY-MM-DD
| ID | Claim | Verdict | Severity | Evidence (command or file) |
|---|---|---|---|---|
| F1 | ... | CONFIRMED / WRONG / UNVERIFIED / UNVERIFIABLE | blocking / informational | ... |

## Missing or weak
## Suggestions (optional, clearly separate from verdicts)
```

**Findings** (user decision 2026-11-03, T054):
- **ID:** F1, F2, … unique in your review file of the task. A re-review adds a new dated table, reuses the ID of a finding it rechecks, and gives a new finding the next free ID. A table without an ID column gets ids by counting its rows that have a verdict (`F1`, `F2`, … in each table), which need not match your own labels.
- **Severity:** `blocking` must be fixed before you accept; `informational` is a note. A WRONG row with no Severity value counts as blocking; other rows without one are informational.
- **Closing:** while `--accept` is your latest verdict on the task, it closes your own WRONG and UNVERIFIED rows of that task. The user's "not OK" (`user action=open`) after your accept voids it until you accept again. To close one earlier (it is fixed, but you still ask for changes on others), run `python3 docs/agent_hub/tools/job_act.py disposition <tag> <task> --finding <review file>#<ID> --resolved -m "<one line>"`. `--upheld` records that a finding still stands after the owner's answer; it stays open until your next accept. An UNVERIFIABLE row stays open until the user accepts the limitation; the command refuses `--resolved` for it.

## 6. Hard rules (all agents)

<!-- Adapt this list to your project: name your own expensive or irreversible jobs. -->
- **Ask the user before every expensive or irreversible job** (GPU runs, long compute, paid API batches), for each batch. An earlier authorization does not carry over.
- Do not propose a fix before the cause is checked against data. Say "not established" when it is not.
- Never edit another agent's log or review, never delete files, and never record something the user did not decide as a decision.

## 7. Log entry template (`logs/<tag>.md`, newest at the bottom)

```markdown
## YYYY-MM-DD HH:MM [tag] T###
- Did: ...
- Files: ...
- Numbers: <value> (<command>)
- Doubts / not checked: ...
- Next: ...
```

## 8. What each agent's config contains

The same block sits in `CLAUDE.md` (read by Claude Code), in `AGENTS.md` (read by Codex/GPT, and by opencode if it runs DeepSeek), and in DeepSeek's own config file if its client does not read `AGENTS.md`:

```markdown
## Shared agent hub (read before any work)
Your tag: [claude] | [gpt] | [deepseek]. Read docs/agent_hub/README.md and follow it:
read STATE.md, DECISIONS.md, open tasks and reviews of your work; claim before editing;
log to logs/<tag>.md; set tasks to review; review others' tasks by recomputing from raw data;
never mark your own task done; ask the user before any expensive or irreversible job.
```

## Task names

Every task has a display name `T-<category>-NNN`, for example `T-collect-024`. The categories are `hub`, `collect`, `model`, `loc`, `ctrl` and `check`; the mapping is `docs/agent_hub/TASK_NAMES.tsv` (append-only, one `T024<TAB>collect` line per task). The number stays the canonical id: file names (`tasks/T024_*.md`), status events, history, reviews and logs keep `T024`, and the tools accept either form (`claim.py claude T-collect-024`) and store `T024`. A category that does not match the mapping is refused. `new_task.py` needs `--category` and writes the mapping line in the same commit as the created event. Both status views print the display names. Tasks are created only in the checkout of `main` (§3).

## 9. Job board (in trial; user decisions J1–J5, 2026-10-31)

The full design is `memos/2026-10-31_job_board_plan.md`. The board is in trial until the user has watched a pilot and says it is in use. Rules J-R1 to J-R5 there apply only while a job is active, and only to that job's tasks.

**Several jobs at once** (T038, 2026-11-02): each chat may start its own job while other jobs are active. Every job has an id, `job-<n>`, printed by `job.py start`, and its own goal, quotes, limits, counters and pause. Ending, pausing or resuming one job leaves the others alone. One dispatcher serves every active job.

**When the user says one of these in your chat**, record the user's exact words with `--quote` and run the command from the repository root:

| The user says | You run |
|---|---|
| `job start: <goal>` | `python3 docs/agent_hub/tools/job.py start --goal "<goal>" --quote "<exact words>" --by <your tag>`. Tell the user the job id it prints. Then run `bash docs/agent_hub/tools/job_up.sh`: it starts the dispatcher in the background, or says one is already running, which serves the new job too. A job that another chat started is no reason to refuse, or to end it. |
| `job start: <goal>`, with permission to end it themselves (for example "you can end the job yourself") | `python3 docs/agent_hub/tools/job.py start --goal "<goal>" --auto-end --quote "<exact words>" --by <your tag>`, then `job_up.sh` as above. The dispatcher ends the job once every task is accepted or cancelled. To switch it on for a running job: `job.py resume --job <id> --auto-end --quote "<exact words>" --by <your tag>`. |
| `job end`, in the chat that started the job | `python3 docs/agent_hub/tools/job.py end --job <the id you started> --quote "<exact words>" --by <your tag>`. No new wakes for that job; the dispatcher exits once no job is active and running wakes have finished. |
| `job end <id>`, or `job end` with a goal named | `job.py end --job <id> ...`. If you cannot tell which job is meant, run `job.py status`, give the user the active ids and goals, and ask. `job.py end` without `--job` works only while exactly one job is active. |
| `job status`, or "which jobs are running" | `python3 docs/agent_hub/tools/job.py status`. Relay each job's id, goal, status and counters. |
| "go on", resume, or a higher limit after a pause | `python3 docs/agent_hub/tools/job.py resume --job <the paused job's id> --quote "<exact words>" --by <your tag>`, with `--limit KEY=VALUE` when the user names a new limit |
| A change to a hub setting, for example `task_branch` | First post a task that has `docs/agent_hub/hub_config.json` as its deliverable. Then `python3 docs/agent_hub/tools/job.py set-config <task> --key <key> --value <JSON value> --quote "<exact words>" --by <your tag>`, with a text value quoted as `'"hub"'`. Then claim the task, change exactly that value and submit. |
| OK / not OK on a task under **For the user to check** | `python3 docs/agent_hub/tools/job.py check <task> --ok` or `--not-ok`, with `--quote "<exact words>" --by <your tag>`. The task already names its job, so no `--job` is needed. |

**Then split the goal into tasks and post them** with `--job <your job's id>` (next paragraphs). The dispatcher wakes agents only for tasks posted to an active job. Work you do yourself is not on the board, and nobody is woken for it. Do not claim what you posted (J-R3). Tell the user which tasks you posted, and to which job.

**Which tasks belong to a job:** the job id recorded in the task's `created` event (`job=<id>`). A task created with `job=none` belongs to no job. Tasks created before T038 carry no job field. They belong by time to `job-1`, the pipe-scene job started 2026-11-01 00:39 EST and ended 2026-11-02 03:12 EST (T031–T037): a task counts when it was created between that job's start and its end. Any job started since T038 takes only the tasks that name its id.

**Claude does, the others review** (user, DECISIONS 2026-11-01): [claude] may post a task with `--job <id> --owner claude --reviewers gpt --reviewers deepseek`. It then claims the task with `job_act.py claim claude <task>`, does the work, and runs `job_act.py submit`. The dispatcher wakes GPT and DeepSeek to review. If a reviewer asks for changes, the dispatcher wakes a headless [claude] (`claude -p`) to fix them. Work already done counts too: post a task whose deliverables are the existing files, claim it, and submit.

**Stages** (user decision 2026-11-03, T054): an accepted task wakes nobody. A task card must not tell a wake to go on to a later stage once the reviewers accept. A later stage starts only when the user says so in chat; the owner then does it and submits from that chat.

**Watching wakes live:** `python3 docs/agent_hub/tools/watch_wakes.py` turns `logs/dispatcher_runs/*.out` into a readable transcript and follows new wakes (`--once`, `--agent`, `--task`, `--no-thinking`, `--full`; read-only). Each wake's header also prints the command that reopens its full session.

If your sandbox cannot leave a background process running, run only `job.py start` and ask the user to run `bash docs/agent_hub/tools/job_up.sh` in a terminal, or to tell [claude]. `job.py status` shows every job on the board. `BOARD.md` is generated from task state; never edit it. It has one header block per job. Its sections (Open, For the user to check, Questions for the user, In review, Paused) list all jobs together, and every entry names its job.

**Posting a task** (any agent, during a job): `python3 docs/agent_hub/tools/new_task.py <your tag> "<title>" --job <job id> --category <hub|collect|model|loc|ctrl|check> --owner open --reviewers <another tag> --deliverables <path> ...`. Without `--job`, `new_task.py` puts the task in the only active job, and refuses when several jobs are active. Pass `--job none` for a task that belongs to no job. Then fill the card with the goal, the files that may change, the exact acceptance command, and one sentence on how the task serves the job's goal. You never claim or review a task you posted.

**When the dispatcher wakes you**, the prompt reads `[<tag>] job-board wake, reason <reason>, task <task>`, and the environment variable `JOB_ID` names the task's job. Do that one action, log it in `logs/<tag>.md`, then exit:
- `claim`: run `python3 docs/agent_hub/tools/job_act.py claim <tag> <task>`. Exit 3 means another agent took the task; stop. Otherwise do the card's work inside its deliverables, run its acceptance command, then run `job_act.py submit <tag> <task> -m "<one line>"`. That commits the deliverables and records the submit.
- `claim` on a task you already hold (the reviewer asked for changes): read the review, fix the deliverables, and `submit` again. Fix only the current stage; never start a later stage of the card (**Stages** above).
- `review`: run `job_act.py show <tag> <task>`. Recompute the claims from raw data as in §5. Write `docs/agent_hub/reviews/<YYYY-MM-DD>_<task>_<tag>.md` with the §5 table (ID and Severity columns), then run `job_act.py review <tag> <task> --file <that file> --accept`, or `--changes` if any finding is blocking.
- **Anything that is the user's (J-R2):** GPU runs or long compute, network access, `git push`, deleting files, changes outside the repository, or any choice the user has to make. Do not do it. Keep the claim, write the question in your log, and exit; the user answers in chat.

**Wake cost** (user decision D004, 2026-11-02; the slide review job-4 used 45.5M input tokens in 22 min):
- A wake reads only what its action needs: the task card, its status file, its deliverables, and the task's reviews and responses. It skips the §1 reading list: no STATE.md, DECISIONS.md, INDEX.md, `hub_status.py`, other tasks or other agents' logs, unless the card names them. For user decisions about the task, it greps DECISIONS.md for the task id. So whoever posts a task writes into the card every decision and file a wake needs.
- A wake starts no sub-agents or workers. The GPT wake runs `codex exec --disable multi_agent`; the Claude wake's allowed tools include no Agent tool.
- A re-review starts from the reviewer's last review of the task and the owner's responses. It rechecks only what changed in the deliverables since then (`git log -p` on them) and its own open findings.
- The owner's fix wake waits while a review wake of the same task is still running, so one fix answers every reviewer of that round.
- A review wake starts only on a submit (T048, user decision 2026-11-03). It needs a submit whose deliverables are still the ones at HEAD, and a reviewer who has not accepted them. A later commit to a deliverable wakes nobody: the state stays `review` (or `review (deliverables changed)` when an earlier revision was accepted), with the warning `deliverables changed since submit`; the next actor is the owner, the board's In review row says the owner must submit again, and the owner must run `job_act.py submit` again. The owner's fix wake for a round is keyed on the submitted revision, so a commit made while changes are requested does not start a second one, and a commit made after the owner's resubmit does not start a fix wake while a finding is still open (T049).

**Limits**, per job: 10 tasks posted, 6 wakes per agent per hour, 40 wakes, 30 min per wake.
- The dispatcher enforces them. When a job reaches one, it pauses all wakes of that job; the other jobs go on. Only the user resumes, with `job.py resume --job <id>`.
- The hourly limit counts each job's own wakes, so two jobs can wake one agent up to 12 times an hour. Across all jobs, the dispatcher runs at most one wake per agent at a time.
- An auto-end job (`job.py start ... --auto-end`, T052) ends by itself once it has at least one task and every task is accepted or cancelled and none of its wakes is running. A paused job never auto-ends. The user's OK / not OK on each task stays the user's: `job.py check` still works on ended jobs.
- The token limit is not enforced yet, because `parse_tokens` is a stub.

Wake commands are in `hub_config.json`, with bare command names. This machine's full paths are in the git-ignored `hub_config.local.json`; never commit them. Each wake's output is saved in `logs/dispatcher_runs/`, and every line of `logs/dispatcher.jsonl` names its job.
