# Set up the agent hub with an agent

Start Claude Code, Codex or DeepSeek's `dsh` in your repository (the one that should get the hub), then paste the prompt below. Replace `<KIT_PATH>` with the folder of this starter kit first.

```text
Please set up the agent hub in this repository, using the starter kit at <KIT_PATH>.
The wizard is <KIT_PATH>/setup_hub.py. Run it with python3 and the absolute path of this repository as REPO.
Your shell tool may have no terminal, so the wizard cannot ask questions itself: you ask me in chat instead.

1. First run `python3 <KIT_PATH>/setup_hub.py <REPO> --dry-run` (a fresh repository) or `--doctor`
   (a repository that already has docs/agent_hub). Explain the output to me in my own language:
   what would change, which agents it found, and which logins are missing.
2. The wizard looks for claude, codex and dsh on PATH and in the usual install places. If it reports a tool
   as not found, look for it yourself before you call it missing: try `command -v <tool>` in my login shell
   (for example `zsh -lic` or `bash -lic`), then application folders, version managers and package manager
   folders. dsh may sit inside the DeepSeek Harness app. Show me each path you found and ask me to confirm it.
   Then add `--tool-path TOOL=/full/path` to the wizard's command, with TOOL one of claude, codex or dsh.
3. Full paths belong only in docs/agent_hub/hub_config.local.json, which the wizard writes and git ignores.
   Never put a path, a key or a login into hub_config.json, CLAUDE.md, AGENTS.md, a task card, a log, a review
   or any other file git tracks. If this repository already has the hub (a fresh clone, or a new machine),
   record this machine's paths with `python3 <KIT_PATH>/setup_hub.py <REPO> --local-paths`, which also takes
   --tool-path.
4. Ask me in chat before you run the install. After I agree, run the same command with `--yes`
   (add `--no-tests` only if I ask for a quick install).
5. If a login is missing, give me the command to run myself in my own terminal (for example
   `claude auth login` or `codex login`). Never ask me to paste a password or an API key into this chat,
   and never type one yourself.
6. If DeepSeek's stored credentials are disabled for headless runs, run
   `python3 <KIT_PATH>/setup_hub.py --fix-deepseek` (alone, without --yes: it only prints the planned
   change), explain the change, and run it again with `--yes` only after I agree. It edits one file of dsh
   (with a .bak copy) and nothing else outside this repository. If it says the profile is missing, it prints
   the dsh command that creates the profile: run that command, then try again.
7. Offer `--hello` (it wakes each configured agent once with a one-word question). Say that it spends
   a few tokens, and run it only if I say yes.
8. At the end show me the checklist (✓ done, ✗ needs a fix, – not applicable). Say where each kickoff
   prompt goes: paste the GPT one into the Codex chat, the DeepSeek one into the dsh chat, and the Claude
   one into a Claude Code chat once.
9. Never push. Never commit anything beyond what the wizard does. Do not read or edit anything under
   ~/.claude, ~/.codex or ~/.dsh yourself.
```
