# Kickoff prompts for the other agents

Replace `<REPO_PATH>` with the absolute path of your repository and `<TAG>` with the agent's tag (`gpt` or `deepseek`; `claude` for Claude Code). Paste the first prompt once into each agent's chat. After that, the config pointer (section 3) makes each agent read the hub on its own. Paste the second prompt whenever you start a new session and want to be sure.

## 1. First session (paste once)

```text
You are [<TAG>] on the repository <REPO_PATH>.
Other AI agents work in this same folder at the same time: [claude] (Claude Code), [gpt] (Codex) and [deepseek].
We share one hub, docs/agent_hub/, where every agent records its work and checks the others'.

Before anything else:
1. Read docs/agent_hub/README.md in full, and follow it from now on. Where your usual habits conflict with it
   (e.g. where to write notes, whether to mark your own work done), the README wins.
2. Read STATE.md, DECISIONS.md, INDEX.md, every file in tasks/, and reviews/.
   Run: python3 docs/agent_hub/hub_status.py
3. Create docs/agent_hub/logs/<TAG>.md with a first entry (template: README section 7) covering:
   - what you have worked on in this repository before today: files you wrote or changed, with dates;
   - anything in STATE.md or DECISIONS.md that you believe is wrong or missing. Do not rewrite those files;
     add a dated "Correction [<TAG>] YYYY-MM-DD: ..." line, or say it in your log.
4. Never run an expensive or irreversible job (long compute, GPU runs, deleting data, network access, a push)
   without asking me first.
5. At the end of every session, append to logs/<TAG>.md, then commit only the files you changed
   (git add <paths>; message "[<TAG>] T###: ..."). Do not push.

Then tell me in chat: what you read, what you claimed, and anything you think the hub protocol gets wrong.
```

## 2. Every later session (optional reminder)

```text
You are [<TAG>]. Start with docs/agent_hub/README.md section 1: run python3 docs/agent_hub/hub_status.py,
answer any WRONG verdict on your work, then take the oldest task in `review` that you did not write,
or a task the user assigned you. Log and commit (your files only) before you stop.
```

## 3. Where each agent's config pointer lives

| Agent | File it reads automatically | Status |
|---|---|---|
| Claude Code | `CLAUDE.md` (repo root) | Add the block from `AGENTS_BLOCK.md` |
| GPT through Codex | `AGENTS.md` (repo root) | Add the same block |
| DeepSeek (`dsh`) | `AGENTS.md` **and** `CLAUDE.md` (its loader reads both from the repo root) | Covered by the two files above |

`CLAUDE.md` and `AGENTS.md` may stay git-ignored; then they stay on your machine. Everything under `docs/agent_hub/` is in git.

## 4. When you have a new task

The steps are the same with or without the job board; the tools only automate the bookkeeping.

![Life of a hub task: you give the task to one agent; the owner opens a task file, does the work and submits it; one or two reviewers recompute the results; a blocking finding sends it back to the owner; it is accepted when every reviewer accepts this exact version. You approve expensive runs, decide questions and push.](task_workflow.svg)

1. **Pick one owner and one or two reviewers.** The owner never reviews its own task. A simple rotation: claude → gpt → deepseek → claude.
2. **Give the task to the owner only, in that agent's chat.** Giving the same task to two chats makes duplicates and conflicting decisions.
3. **When the owner says "in review", ask each reviewer** with the prompt below.
4. **If a reviewer requests changes,** tell the owner to answer, and repeat until every reviewer accepts.
5. **Answer when an agent asks you**, and approve every expensive batch explicitly. The agent that hears your decision records it in DECISIONS.md.
6. **Push when you want:** say "push" to one agent, or run `git push`.

To see where everything stands at any time, run `python3 docs/agent_hub/hub_status.py`.

**To the owner:**

```text
New hub task: <goal in one or two sentences>. You own it; <reviewer tag(s)> review it. First create and commit the task file (owner, reviewers, files you may change, what must not change, the exact acceptance check). Then do the work, committing only your own files. Ask me before any expensive or irreversible run. When done, log it and set the task to review.
```

**To each reviewer:**

```text
Review T0xx in the hub (run python3 docs/agent_hub/hub_status.py). Follow README section 5: rerun its acceptance check, recompute the key numbers from raw data with your own script, say what is missing, write reviews/<date>_T0xx_<your tag>.md, and commit it together with your status line.
```

**To the owner, after a review:**

```text
Answer the review(s) of T0xx: fix each blocking finding or explain why it should stand, commit, and set the task back to review.
```

Refer to tasks by their display name (`T-<category>-NNN`, from `docs/agent_hub/TASK_NAMES.tsv`); the number is the canonical id, and the tools accept either form.
