# Hub protocol v2: plan (T011), revision 3

**Author:** [claude]. Revision 1 was committed in `ec8dfb3` (00:49). Revision 2 was written 2026-10-31 at about 01:10, answering:
- [reviews/2026-10-31_T011_gpt.md](reviews/2026-10-31_T011_gpt.md): 7 blocking findings, plus changes to T012.
- [reviews/2026-10-31_T011_deepseek.md](reviews/2026-10-31_T011_deepseek.md): 7 blocking and 8 informational findings, plus changes to T013.

Appendix A maps every finding to its answer.

**Revision 3** (2026-10-31, about 01:25) changes three things only, answering the round-2 reviews in the same two files:
- the §2.4 cursor ([gpt] F8, blocking);
- the config path in §4;
- the test commands in §5 (two non-blocking notes from [deepseek]).

Nothing else changed since revision 2 (`712c60b`), which [deepseek] accepted.

**Status:** plan under review. **Nothing here is in force.** Protocol v1 (README.md) governs until the adoption steps in section 7 are complete. Section 6 lists recommended practice under v1.

**What stays from v1:** independent review with recomputation, append-only user decisions, no self-approval, user approval for every expensive batch (GPU, long compute), and no push without the user asking.

**What changes:** coordination state becomes typed lines in a fixed format that one shared library writes and reads under an operating-system lock. The tooling stays deliberately small and **cooperative, not secure**: raw `git` and `--no-verify` bypass it, and an agent tag is not proof of identity.

## 1. Decisions on the merged concerns

Revision 1's 23 rows still stand, except where this table or later sections change them.

| # | Topic | Decision (revision 2) |
|---|---|---|
| 1 | Commit isolation | All hub writes go through one transaction: an OS lock, an ownership check, then a path-limited commit (§3) |
| 2 | Shared append-only files | Appends are allowed only when the target is **clean in both the index and the working tree**; otherwise the tool refuses. It then verifies the diff is exactly the new line and commits that file alone (GPT F4 showed a foreign line otherwise slips in) |
| 3 | Agent identity in git | `--author="<tag>-agent <<tag>@agents.local>"` is replaced by `tag-agent <tag@agents.local>` (e.g. `gpt-agent <gpt@agents.local>`). It stays **off** until the user sets it (U2) |
| 4 | Task ids | Assigned inside the transaction as 1 + the highest existing id. The lock serializes this, so no reservation directory is needed |
| 5 | Claims | `claim` / `release` events in the task's status file, written inside the transaction. A claim fails if another tag holds an active claim on the task or on an overlapping scope. **No marker files** |
| 6 | Stale claims | `hub_status` warns after 12 h without a commit by the owner. Takeover needs the owner's `release` or a user ruling; being quiet is never enough |
| 7 | Lifecycle | Derived from typed events (§2). There is no generic "set state" event, so nobody can reset someone else's task |
| 8 | Acceptance | Every **required** reviewer must accept the **current revision** of the deliverables, and no blocking finding may be open. A changed deliverable reopens review; unrelated commits don't (GPT F1) |
| 9 | Review-only tasks | One task per reviewed document, owned by its author. **T005 is split into T005a and T005b during migration** (DeepSeek F4) |
| 13 | Decisions | New entries use the `D###` block format in §4. Legacy entries stay unchanged and are cited as "legacy" |
| 14 | Questions | The `Q###` block format in §4. A conflict between two decisions becomes a `Q###`, which STATE links (DeepSeek F12) |
| 24 | Adopted options | `hub_config.json`, tracked. It defaults to off and changes only through a user event that cites a `D###` (DeepSeek F2) |
| 25 | Dependencies and approvals | `created` carries `depends_on` (task or decision ids). Expensive batches (GPU, long compute) are recorded as `user … action=approve_batch` with scope and limits (DeepSeek F5) |
| 26 | Precedence | Platform and tool constraints > the user's explicit scope > approved hub protocol > client wrappers and skills |
| 27 | Factual disputes | Before `dispute`, owner and reviewer run one focused shared reproduction and record its result |
| 28 | Deferred | Portable data, model and environment manifests: deferred. Revisit before the next training run; that task's owner owns them. Separate per-agent worktrees: deferred (U5) |
| 29 | Round limit | After revision 2, only blocking findings on the *changes* reopen T011. Anything else goes into the T012–T014 reviews |

## 2. Status events (grammar owned by T012, `tools/hub_events.py`)

Each task has an owner-only `tasks/T###_<slug>.md` and an append-only `tasks/T###.status.md`, one event per line:

```text
<time> TAB <tag> TAB <event> TAB <key=value;key=value> TAB <note>
```

- **Time:** ISO-8601 with seconds and UTC offset, e.g. `2026-10-31T01:12:07-04:00`. It is the *recording* time. **Line order in the file is the authoritative order**, guaranteed by the lock.
- **Tag:** `claude`, `gpt` or `deepseek`.
- **Values:** percent-encoded for `%`, `;`, `=`, tab and newline. Lists are comma-separated, with commas inside items encoded.
- **Note:** free text; tabs and newlines become spaces.
- **Bad lines:** malformed, unknown or duplicate lines are reported by `hub_status` and ignored for state.

| Event | Fields | Who may write it |
|---|---|---|
| `created` | `owner` (tag or `open`), `reviewers`, `required=all\|any`, `kind`, `deliverables`, `depends_on`, `initial=open\|proposed\|awaiting_user` | any agent. The owner may not be a reviewer |
| `claim` | `scope` (globs) | owner, or anyone if owner is `open` (the claimant becomes owner) |
| `release` | — | the claim holder |
| `submit` | `rev` (§2.1) | owner |
| `review` | `file`, `rev` | a listed reviewer |
| `request_changes` | `file` | a listed reviewer |
| `accept` | `rev` | a listed reviewer |
| `response` | `finding=<review file>#F<n>` | owner |
| `disposition` | `finding`, `result=resolved\|upheld` | **only the author of that review file** |
| `dispute` | `reproduction=<path>` | owner or a listed reviewer, after one request → submit → request cycle |
| `user` | `decision=D###`, `action=open\|cancel\|accept_limitation\|resolve_dispute\|approve_batch\|set_config`, plus the action's fields (`task`, `finding`, `to`, `scope`, `limits`, `key`, `value`) | the agent that heard the user. The `D###` must hold the verbatim quote; other agents can challenge it in QUESTIONS |

**2.1 Revision.** `rev` = sha256 over the sorted pairs `path \0 sha256(file bytes)` of the task's `deliverables`.

**2.2 Derived state.**

| Event or condition | Resulting state |
|---|---|
| `created` | `initial` |
| `user action=open` | `open` |
| `claim` | `claimed` |
| `release` | `open` |
| `submit` | `review` |
| `request_changes` | `changes_requested` |
| a new `submit` | `review` |
| `dispute` | `disputed` |
| `user action=resolve_dispute to=…` | that state |
| `user action=cancel` | `cancelled` |
| every required reviewer's latest `accept` has `rev` = current rev, and no blocking finding is open (*amended 2026-11-03, user decision, T055: a reviewer's later `request_changes` withdraws its earlier `accept`; the latest verdict counts*) | **`accepted`** |
| unmet `depends_on` | shown as **blocked by** those ids |
| `accepted`, but current rev ≠ accepted rev | shown as **review (deliverables changed)** |

**2.3 Open finding (Boolean).** A review-table row is open when all three hold:
- its severity is `blocking`;
- its verdict is `WRONG`, `UNVERIFIED` or `UNVERIFIABLE`;
- it has not been closed.

How a row closes:
- **WRONG or UNVERIFIED:** a later `disposition … resolved` by that review's author. `resolved` means the reviewer re-checked and the corrected claim holds.
  *Amended 2026-11-03 (user decision, T054):* such a row is also closed while the author's latest verdict on the task (`accept` or `request_changes`) is `accept`, unless the user's `open` or the author's `disposition … upheld` for that row comes after that accept.
- **UNVERIFIABLE:** a `user action=accept_limitation`. Only the user can accept a limitation.

`CONFIRMED` rows are never open.

**Legacy rows:** missing `Severity` means `WRONG` → blocking, anything else → informational (DeepSeek F3). A missing `ID` means `F<row number>`. Bold and plain verdicts parse the same.

**2.4 Next actor and queues.**

| State | Next actor |
|---|---|
| `proposed`, `awaiting_user`, `disputed` | user |
| `open` | the owner, or any agent if the owner is `open` |
| `claimed`, `changes_requested` | owner |
| `review` | required reviewers with no `accept` for the current rev |
| blocked | the dependency's next actor |

- **`queue(X)`:** tasks where X is a next actor; open findings on X's tasks with no `response` from X; questions X asked that now have answers.
- **New since X last read** (revision 3): hub files changed in `git log S..HEAD`, where S is the `seen_through=<sha>` in X's latest log entry.
  - **What S is:** the HEAD that `hub_status --tag X` printed at the start of that session, i.e. the snapshot X actually read. It is not the commit that recorded the log.
  - **When S is missing** or is not an ancestor of HEAD, everything counts as new.
  - **Why:** a concurrent update B, landing after X read at A and before X's log commit C, keeps showing until X logs a later `seen_through`. Revision 2's rule (since C) skipped B; this was GPT F8, reproduced independently by [claude] in a throw-away repo: 0 commits shown against 2.

## 3. The shared library and CLIs (contract between T012 and T013)

`docs/agent_hub/tools/hubtx.py` (T012) uses only the standard library. Every function takes repo-relative POSIX paths, normalized; anything that leaves the repository raises `BadInput`.

```python
class HubError(Exception)              # Busy, Unauthorized, DirtyTarget, GitFailure, BadInput subclass it
def repo_root() -> Path                # git rev-parse --show-toplevel
def now() -> str                       # §2 time format
@contextmanager
def transaction(tag: str, task: str) -> Tx   # fcntl.flock(LOCK_EX) on .git/hub.lock, retry ≤ 30 s, else Busy.
                                             # The kernel frees the lock if the process dies; a paused live holder keeps it.
class Tx:
    def append(self, path, line)       # target must equal HEAD in index and working tree, else DirtyTarget;
                                       # appends line+"\n", checks the diff is exactly that line
    def write_new(self, path, text)    # O_EXCL create; FileExistsError if the path exists
    def commit(self, paths, message)   # owns() for every path, else Unauthorized; git add for new paths;
                                       # git commit -m "[tag] <task>: message" [--author if config] -- paths;
                                       # on failure raise GitFailure(stderr), leave files as they are,
                                       # never reset/stash/checkout
def owns(tag, path) -> bool            # logs/<tag>.md; reviews/*_<tag>.md; append-only files through append only;
                                       # tasks/T###_*.md by its created owner; False inside another tag's active claim
def config() -> dict                   # hub_config.json, defaults when absent
```

`tools/hub_events.py` (T012) provides `format_event(...)`, `parse_file(path) -> (events, warnings)` and `active_claims(repo) -> list`.

- **CLIs** `hub_append.py <tag> <task> <path> <line>` and `hub_commit.py <tag> <task> -m <msg> <paths…>` wrap these.
- **Exit codes:** 0 ok, 2 Busy, 3 Unauthorized, 4 DirtyTarget, 5 GitFailure, 6 BadInput.
- **One transaction per CLI call**, so nothing is nested. T013's tools import the library and open their own single transaction.
- **Hook:** `commit_msg_hook.py` checks the *candidate commit*, using `git diff --cached --name-status` under the hook's `GIT_INDEX_FILE`, including deletes and renames.
- **Installer:** `install_hooks.py` refuses, with instructions, if `core.hooksPath` is set or a `commit-msg` hook already exists.

**Ownership of the contract:**
- **T012 owns the library and grammar.** T013 may build its state logic and unit tests in parallel against fixture files written to §2.
- **The integration test** `tools/test_integration.py` is owned by T013 and runs after T012 is accepted.

## 4. Decision, question and config formats

`DECISIONS.md`: new entries are appended only, through `hub_append`.

```text
### D001 · <recording time> · recorded by [tag] · heard in [tag]'s chat
- event time: unknown | <time>
- quote: "<verbatim user words>"
- scope: <what it decides>
- supersedes: D### | none
- answers: Q### | none
```

`QUESTIONS.md`:

```text
### Q001 · <time> · asked by [tag]
- question: …
- options: …
- blocks: T###,… | none
```

An answer is a later appended line, `- Q001 answered by D### (<time>)`.

`docs/agent_hub/hub_config.json` keys and defaults (path pinned in revision 3):

| Key | Default |
|---|---|
| `author_per_agent` | `false` |
| `state_maintainer` | `null` |
| `memory_export` | `false` |
| `shared_tree` | `true` |
| `v2_in_force` | `false` |

The tool changes a key only through a `user … action=set_config`.

## 5. Work items

Acceptance tests use `unittest` on throw-away repositories. The commands (revision 3) put the tools directory on `sys.path`, which a file-path `unittest` call does not do (DeepSeek's note):
- T012: `python3 -m unittest discover -s docs/agent_hub/tools -p 'test_git_tools.py'`
- T013: `python3 -m unittest discover -s docs/agent_hub/tools -p 'test_task_tools.py'`
- T013, after T012 is accepted: `python3 -m unittest discover -s docs/agent_hub/tools -p 'test_integration.py'`

| Task | Owner → reviewer | Files | Acceptance additions in revision 2 |
|---|---|---|---|
| **T012** library and git side | [gpt] → [deepseek] | `tools/hubtx.py`, `hub_events.py`, `hub_append.py`, `hub_commit.py`, `commit_msg_hook.py`, `install_hooks.py`, `test_git_tools.py` | Revision 1's (a)–(f) plus GPT's five changes: (1) a live old lock holder (child process sleeping) is never displaced, and the lock is free after it is killed; (2) a dirty working tree **or** dirty staged target is refused, an exact append works on a clean baseline, unrelated staging is preserved, and hook or git failures leave recoverable state; (3) overlapping scopes, path traversal, and rename/delete targets are handled; (4) the hook checks the candidate commit, the installer preserves an existing hook setup, and `--author` is off by default; (5) one transaction per call, no nesting |
| **T013** state, ids, claims, status | [deepseek] → [claude] | `tools/hub_state.py`, `new_task.py`, `claim.py`, `hub_status.py` (v2; the old one becomes a shim), `test_task_tools.py`, `test_integration.py` | Revision 1's (a)–(g) plus: fixtures from the **five real reviews** (legacy fallback of §2.3); a T005a/T005b split fixture; next actor, queue and "new since" as defined in §2.4; a missing-second-reviewer and an author self-accept are both rejected; a changed deliverable reopens review and an unrelated commit doesn't; a dependency block; **the A→B→C cursor case: B stays listed until X logs a later `seen_through`, even after X commits its own log** (revision 3) |
| **T014** migration and README v2 | [claude] → [gpt] | README, STATE (snapshot, ≤ 600 words), PLANT, QUESTIONS, DECISIONS header, `hub_config.json`, KICKOFF, `.status.md` files for T001–T013, CLAUDE.md/AGENTS.md wrappers | First, the original reviewers write `disposition` lines for their earlier WRONGs ([gpt]: T001, T005b; [deepseek]: T005a). Migration never invents an acceptance, an approval or a reviewer completion. Authors add a current/superseded pointer to their own design documents ([gpt]: CASCADE_DESIGN; [claude]: the memo). README v2 also carries DeepSeek's F8–F13: dataset declarations for the allowlist, retiring "take the oldest review", KICKOFF placeholders, tool-inserted log times, the advisory limits. The log-entry template gains `seen_through=<sha>` (revision 3) |
| **T015** portable rules and runtime allowlist | [claude] → [deepseek] | an agent-neutral rules file, plus the allowlist with units, frame, reference point and provenance per channel | **No U4 gate** (GPT F5) |
| **T016** export of Claude's project memories | [claude] → [deepseek] | `memos/claude_memory_export_2026-10-31/` | Only if the user approves U4. The reviewer samples 5 DECISIONS entries against it |

**Order:**
1. User approval of this revision.
2. T012, with T013's unit work in parallel.
3. T013's integration test.
4. T014.
5. Adoption (section 7).

T015 can start any time after approval; T016 waits for U4.

## 6. Recommended practice under v1 until adoption (not rules)

- **Committing:** commit named paths only: `git add <new files>; git commit -m "…" -- <paths>`. Never `git add <dir>`, `-A`, or a plain `git commit`.
- **Shared files:** commit a line you append right away. At 01:04 both [gpt] and [deepseek] had uncommitted status lines in T011's task file.
- **Task ids:** the race is acknowledged until T013's allocator exists. On a collision the later file renames, as T009 did. `ls` plus exclusive creation of differently named files does **not** reserve an id (GPT F6).
- **Times:** `date` gives the recording time only; an earlier event's time stays "unknown".

## 7. Adoption

Protocol v2 is in force once all of these hold:
1. T012 and T013 are accepted.
2. T014 is accepted.
3. The user's `D###` "adopt v2" is recorded and `v2_in_force=true` is set.

Until then README v1 governs, and agents follow it during T012–T014 (DeepSeek F14).

## 8. Decisions for the user

| # | Question | Recommendation |
|---|---|---|
| U1 | Approve revision 2 so T012, T013 and T015 can start? | Yes |
| U2 | Per-agent git author (`tag-agent <tag@agents.local>`), with you as committer and pusher? | Yes |
| U3 | STATE.md and INDEX.md maintainer? | [claude], or name another agent |
| U4 | Export Claude's project memories (not the tutoring notes) into the private repo? | Yes |
| U5 | One shared folder with the lock, worktrees deferred? | Yes |

## Appendix A: answers to the T011 findings

| Finding | Answer |
|---|---|
| GPT F1 (revision and all reviewers) | §2.1 rev and §2.2 acceptance by every required reviewer; changed deliverables reopen review |
| GPT F2 (grammar, actors, user events, cursor) | §2 table of events and writers, the §2.3 Boolean rule, the `user` event with an action and a `D###`, §2.4's commit cursor, line order as authority |
| GPT F3 (locks and claims) | `flock`: the kernel releases on process death, and a live holder is never displaced. Claims are events, so no marker is left behind. Stale claims only warn; takeover needs a release or a user ruling (§1 rows 5–6) |
| GPT F4 (exact append) | A clean target in index and working tree, verified delta, one-file commit (§1 row 2, §3 `append`) |
| GPT F5 (shared API, bootstrap) | The §3 contract, a single transaction owner, the integration-test owner named, a legacy adapter in T014, T015 split from the U4 export (T016) |
| GPT F6 (adoption vs interim) | §6 relabelled as recommendations, §7 adoption criterion, the id-race claim withdrawn, `date` = recording time |
| GPT F7 (unmapped items) | Manifests deferred (§1 row 28); allowlist metadata in T015; stale-design pointers and legacy review visibility in T014; batch approval through `approve_batch` (§1 row 25); platform ceiling (§1 row 26); focused reproduction before a dispute (row 27); INDEX stays append-only through the tool |
| GPT T012 changes 1–5 | §5 T012 row |
| DeepSeek F1 (T012↔T013 interface) | §3 |
| DeepSeek F2 (option storage) | `hub_config.json` (§4), fail-closed defaults |
| DeepSeek F3 (legacy reviews) | §2.3 legacy fallback; T013 fixtures; T014 dispositions by the original reviewers |
| DeepSeek F4 (T005) | Split into T005a and T005b (§1 row 9; T013 fixture; T014) |
| DeepSeek F5 (dependencies, GPU approval) | `depends_on`, `approve_batch` (§1 row 25, §2) |
| DeepSeek F6 (state-view definitions) | §2.4 |
| DeepSeek F7 (D/Q format) | §4 |
| DeepSeek F8–F13 | T014 README v2 (§5) |
| DeepSeek F14 (when v2 is in force) | §7 |
| DeepSeek F15 (author string) | §1 row 3 |
| DeepSeek C3b, C4b, C9b, §7 Q3 | `depends_on` and `approve_batch`; T005 split; §2.4; T005 split |
| **Round 2:** GPT F8 (cursor skips a concurrent update) | §2.4 `seen_through` read baseline; the T013 A→B→C acceptance case; the log template field in T014 (revision 3) |
| **Round 2:** DeepSeek's `sys.path` note | §5 `discover -s` commands (revision 3) |
| **Round 2:** DeepSeek's config-path note | §4 path pinned to `docs/agent_hub/hub_config.json` (revision 3) |

## Decisions recorded after revision 3
- 2026-10-31 14:24 [claude]: the user approved **U1 and U5** (J1 of memos/2026-10-31_job_board_plan.md, "都按推荐"; DECISIONS.md). T012, T013 and T015 may start. U2, U3 and U4 remain open. Adoption still follows section 7.
- 2026-10-31 19:32 [claude]: two rule amendments made in T-hub-028, pending review by [gpt] and [deepseek].
  - (a) A holder's `release` returns a task whose created owner is "open" to "open", so another non-reviewer may claim it. This changes the earlier rule, under which release kept the first claimant as owner.
  - (b) A `user action=open` clears the reviewers' earlier accepts, so a user's "not OK" forces re-review. This amends §2.2, under which accepted always won.
