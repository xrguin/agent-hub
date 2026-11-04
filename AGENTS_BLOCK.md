# Agent config block

Put the block below into two files at the repository root:

- `CLAUDE.md`, which Claude Code reads.
- `AGENTS.md`, which Codex reads. DeepSeek's `dsh` reads both files.

These two files may stay git-ignored. Then they stay on your machine, and everything under `docs/agent_hub/` is in git.

Two agents read `AGENTS.md`, so the tag line lists all three tags. Each agent takes its own tag. If one is unsure, tell it its tag in the chat.

```markdown
## Shared agent hub (read before any work)
Your tag: [claude] | [gpt] | [deepseek]. Read docs/agent_hub/README.md and follow it:
read STATE.md, DECISIONS.md, open tasks and reviews of your work; claim before editing;
log to logs/<tag>.md; set tasks to review; review others' tasks by recomputing from raw data;
never mark your own task done; ask the user before any expensive or irreversible job.
Never put a key, token, password or login in any file, log, review, task card or commit message,
and never use git commit --no-verify: the commit guard refuses key-like strings.
In a job-board wake (prompt "[tag] job-board wake ..."), skip this reading list and read only what the prompt names.
```

Name your own expensive or irreversible jobs in README section 6.
