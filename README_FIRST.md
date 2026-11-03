# Agent hub starter kit: read this first

## 1. What it is

The agent hub is a shared folder, `docs/agent_hub/`, through which several AI agents work on one repository. Claude Code (`[claude]`), Codex (`[gpt]`) and DeepSeek (`[deepseek]`) coordinate through it. The protocol is simple. An agent claims a task before it edits files. It logs what it did. It sets the task to review. Another agent recomputes the results and writes a review. No agent marks its own work done. On top of this sits the job board. You tell one agent "job start: <goal>". The agent splits the goal into tasks and posts them. A small program, the dispatcher, wakes the agents one by one to claim and review tasks. The dispatcher uses no model and does nothing when there is no work. Several jobs can run at the same time.

The full protocol is `docs/agent_hub/README.md`. The design of the job board is `docs/agent_hub/memos/2026-10-31_job_board_plan.md`.

## 2. Quick setup

From the kit folder, with Python 3.11 or newer:

```
python3 setup_hub.py /path/to/your/repo --dry-run    # shows every change, changes nothing
python3 setup_hub.py /path/to/your/repo              # installs, asking before each step
```

The wizard checks your tools and logins, writes `hub_config.json` for your machine, copies the hub, updates `.gitignore`, `CLAUDE.md` and `AGENTS.md`, commits (never pushes), installs the commit guard, runs the self-test and prints the kickoff prompts. It ends with a checklist (`✓` done, `✗` needs a fix, `–` not applicable). It never asks for or stores a password or API key; when a command fails, its message is shown with emails and key-like strings masked (a best effort). It writes only inside your repository and refuses to start if `docs`, `.gitignore`, `CLAUDE.md` or `AGENTS.md` is a symbolic link. If `.gitignore` has uncommitted changes of yours, the hub commit leaves `.gitignore` out for you to commit. Options: `--yes` (answer yes everywhere), `--no-tests`, `--hello` (wake each agent once; spends a few tokens), `--fix-deepseek` (run on its own; enables the stored DeepSeek credentials for headless runs; edits one dsh file, with a `.bak` copy; without `--yes` outside a terminal it only prints the planned change).

**With Claude Code:** start it in your repository and paste the prompt in `SETUP_WITH_CLAUDE.md`. Claude runs the wizard for you (first `--dry-run`, then `--yes` after you agree) and explains each result.

**Later:** `python3 setup_hub.py /path/to/your/repo --doctor` checks an installed hub and changes nothing (add `--tests` to run the self-test).

## 3. Requirements

- Python 3.11 or newer (`job.py` uses `contextlib.chdir`).
- git.
- The command-line tools `codex`, `claude` and `dsh`, each installed and logged in. You can start with fewer agents. Then edit `job_agents` in `hub_config.json` (step 4 below), and post tasks only to agents you have.
- For unattended DeepSeek wakes you need a DeepSeek API key, stored through `dsh`. The shipped `headless` profile disables the stored credentials. Add three rows to the "Credentials" list in `~/.dsh/profiles/headless/cordis.patch.yml`: `credentials`, `authorization` and `deepseek-account`, each with `disabled: false`. The memo `docs/agent_hub/memos/2026-10-31_job_board_plan.md` describes this in its status entry of 2026-10-31 23:58. Without it, DeepSeek works only when you start it by hand. `python3 setup_hub.py --fix-deepseek` makes this change for you.

## 4. Manual install (what setup_hub.py does)

Run these steps from the root of your repository.

1. Copy the folder `docs/agent_hub/` of this kit into your repository root, so that you have `<repo>/docs/agent_hub/README.md`.
2. Append `gitignore_snippet.txt` to your `.gitignore`. It ignores the files the job board writes at run time.
3. Put the block in `AGENTS_BLOCK.md` into `CLAUDE.md` (Claude Code) and into `AGENTS.md` (Codex, and DeepSeek's `dsh`, which reads both files). Both files sit at the repository root. They may stay git-ignored.
4. Edit `docs/agent_hub/hub_config.json`. The `job_agents` commands use bare names (`codex`, `claude`, `dsh`) that must be on your `PATH`. Change them to full paths or other flags if your machine needs it. Set `task_branch` to your default branch (`main` or `master`): tasks are created only there, so task ids cannot collide across branches.
5. Commit the hub: `git add docs/agent_hub .gitignore && git commit -m "Add agent hub"`.
6. Install the commit guard once per clone: `python3 docs/agent_hub/tools/install_hooks.py --tag claude --task T000`. It writes `.git/hooks/commit-msg` (it refuses if you already have one, or if `core.hooksPath` is set). With `commit_guard` set to `"scoped"`, every commit is checked: append-only files may only grow, and an agent commit (subject `[claude] ...`, `[gpt] ...` or `[deepseek] ...`) may not touch another agent's claimed files, logs or reviews. Your own commits are not blocked; `git commit --no-verify` skips the guard. README section 3, "Commit guard", has the rules.
7. Run the tests: `python3 -m unittest discover -s docs/agent_hub/tools -p 'test_*.py'`. They should end with `OK`. Two tests in `test_watch_wakes.py` depend on the terminal environment (`NO_COLOR` set, or a very long folder path) and can fail for that reason alone; run them with `env -u NO_COLOR` from a short path before suspecting the hub.
8. Run `python3 docs/agent_hub/hub_status.py`. It prints the state of the hub.
9. Paste `docs/agent_hub/KICKOFF_PROMPT.md`, section 1, into the GPT chat and into the DeepSeek chat. Fill in `<REPO_PATH>` and `<TAG>` first. Claude Code reads `CLAUDE.md`, so it needs the same prompt only once, with `<TAG>` set to `claude`.

## 5. Your first job

Tell one agent, for example Claude Code, in its chat: `job start: <your goal>`. The agent runs these commands from the repository root. You can also run them yourself.

1. Start the job. It prints the job id, for example `job-1`.
   `python3 docs/agent_hub/tools/job.py start --goal "<goal>" --quote "<your exact words>" --by claude`
2. Start the dispatcher in the background. It serves every active job.
   `bash docs/agent_hub/tools/job_up.sh`
3. Post the tasks of the job. Use the job id from step 1.
   `python3 docs/agent_hub/tools/new_task.py claude "<title>" --job job-1 --category hub --owner open --reviewers gpt --deliverables <path>`
   Then fill in the task card: the goal, the files the task may change, the exact acceptance command.
   You can also post a task for [claude] to do itself: `--owner claude --reviewers gpt --reviewers deepseek`. Then the agent claims it with `python3 docs/agent_hub/tools/job_act.py claim claude <task>`, does the work, and runs `python3 docs/agent_hub/tools/job_act.py submit claude <task> -m "<one line>"`.
4. Watch the wakes live.
   `python3 docs/agent_hub/tools/watch_wakes.py`
5. See the state of every job.
   `python3 docs/agent_hub/tools/job.py status`
   The generated `docs/agent_hub/BOARD.md` lists open tasks, tasks in review and tasks for you to check. Never edit it.
6. When you are satisfied, tell the agent `job end`. It runs:
   `python3 docs/agent_hub/tools/job.py end --job job-1 --quote "<your exact words>" --by claude`
   The dispatcher exits once no job is active and the running wakes have finished.

For a task under "For the user to check", you say OK or not OK. The agent then runs `job.py check <task> --ok` or `--not-ok`, with your words in `--quote`. Section 9 of `docs/agent_hub/README.md` has the full list of commands.

## 6. What to adapt

- **README section 6 (hard rules).** It asks the agents to check with you before every expensive or irreversible job. Name your own (GPU runs, long compute, paid API batches) and add your project's rules. The block in `AGENTS_BLOCK.md` says the same in one line.
- **Task categories.** They are the tuple `CATEGORIES` in `docs/agent_hub/tools/task_names.py` (`hub`, `collect`, `model`, `loc`, `ctrl`, `check`). The README section "Task names" repeats them. Change both, then run the tests again.
- **Agent tags.** The three tags are fixed in `TAGS` in `docs/agent_hub/tools/hub_events.py`. Fewer agents work without code changes. A fourth agent needs a code change.
- **README section 9.** It mentions the original project's `job-1` and its dates. Your first job gets the id `job-1` too. The sentence about older tasks does not apply to you. You can delete it.
- **STATE.md, DECISIONS.md, INDEX.md.** They are empty skeletons. Fill them as the project goes on.

## 7. Caveats

- The hub is cooperative, not secure. The commit guard catches mistakes, not bad intent: an agent that leaves out its tag is treated as you, and `--no-verify` skips it. A woken agent has write access to the repository. Headless DeepSeek runs shell commands without asking for approval. Run it only in a repository where you accept this.
- The job board is in trial. Expect rough edges, and read a dispatcher run in `logs/dispatcher_runs/` when a wake does not do what you expect.
- Limits per job: 10 tasks posted, 6 wakes per agent per hour, 40 wakes, 30 minutes per wake. When a job reaches a limit, its wakes pause until you resume it with `job.py resume --job <id>`. The token limit is not enforced yet.
