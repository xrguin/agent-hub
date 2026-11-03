#!/usr/bin/env python3
"""Create a protocol-v2 task: allocate the id under the hub lock, write the
append-only `created` status file and the owner task card, commit both."""
import re

import hub_events as events
import hub_state
import hubtx
import job_adapters as ja
import task_names


def _split(values):
    out = []
    for value in values or []:
        out.extend(part.strip() for part in value.split(",") if part.strip())
    return out


def _slug(title):
    value = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")
    return value[:60] or "task"


def _card(task, title, owner, reviewers, initial, tag, time):
    owner_text = "[%s]" % owner if owner in events.TAGS else owner
    review_text = ", ".join("[%s]" % tag for tag in reviewers) if reviewers else "open"
    return f"""# {task}: {title}
Owner: {owner_text} · Reviewer: {review_text} · Status: {initial}
Created {time[:10]} by [{tag}]

## Goal
{title}

## Files this task may change
- (owner to fill)

## Must not change
- (owner to fill)

## Acceptance check
(owner to fill)

## Result
(owner to fill)

## Status history
- {time[:16].replace('T', ' ')} [{tag}] created; initial {initial}
"""


def _number(key):
    """The full numeric part of an id: T1000 -> 1000, T005a -> 5 (suffix ignored)."""
    return int(re.match(r"T([0-9]+)", key)[1])


def _next_id(repo):
    """max(status files, task cards, mapping lines) + 1 over the complete numbers.
    `T%03d` keeps three digits for small numbers and grows for T1000 and up."""
    keys = list(hub_state.task_ids(repo))
    if task_names.exists(repo):
        keys += list(task_names.load(repo))
    return "T%03d" % (max((_number(key) for key in keys), default=0) + 1)


def _created_fields(owner, reviewers, required, kind, deliverables, depends_on, initial, job=None):
    fields = dict(owner=owner, reviewers=reviewers, required=required, kind=kind,
                  deliverables=deliverables, depends_on=depends_on, initial=initial)
    if job is not None:
        fields["job"] = job
    return fields


def _choose_job(root, job):
    """The `job` field of the new task (None = write no field), decided inside the transaction.

    Reads JOB.json without ja.lock(): that is the same flock file as the transaction, so taking
    it here would deadlock.  `--job none` -> none; `--job ID` needs an active job; without
    --job: 0 active jobs -> no field, 1 -> its id, 2 or more -> refused."""
    if job == "none":
        return "none"
    active = ja.active_jobs(root)
    listing = ", ".join("%s (goal: %s)" % (j["id"], j.get("goal")) for j in active) or "none"
    if job is not None:
        if job not in [j["id"] for j in active]:
            raise hubtx.BadInput("Job %s is not an active job (active: %s)" % (job, listing))
        return job
    if len(active) >= 2:
        raise hubtx.BadInput("%d jobs are active: %s; pass --job <id> or --job none" % (len(active), listing))
    return active[0]["id"] if active else None


def _validate(tag, title, category, owner, reviewers, required, kind, deliverables, depends_on, initial,
              job=None):
    """Check every creation argument before anything is written."""
    if category is not None and category not in task_names.CATEGORIES:
        raise hubtx.BadInput("Unknown category %r (categories: %s)" % (category, ", ".join(task_names.CATEGORIES)))
    if category is None and task_names.exists():
        # Once TASK_NAMES.tsv exists every new task needs a category.
        raise hubtx.BadInput("--category is required (one of: %s)" % ", ".join(task_names.CATEGORIES))
    if not isinstance(title, str) or not title.strip() or re.search(r"[\x00-\x1f\x7f]", title):
        raise hubtx.BadInput("Task title must be nonempty text without control characters")
    try:
        events.format_event(tag, "created",
                            _created_fields(owner, reviewers, required, kind, deliverables, depends_on, initial,
                                            job),
                            note="created T000")
    except (ValueError, TypeError) as exc:
        raise hubtx.BadInput("Invalid task: %s" % exc) from exc


def _check_task_branch(root):
    """Refuse unless HEAD is the `task_branch` of the main checkout's committed hub_config.json
    (no key = no restriction).  The policy is read from the main checkout, not from this worktree's
    branch, so a branch made before the key existed is refused too (T046 F1)."""
    wanted = hubtx.policy_config(root)["task_branch"]
    if wanted is None:
        return
    current = hubtx.git(root, "symbolic-ref", "--quiet", "--short", "HEAD", check=False).stdout.decode().strip()
    if current != wanted:
        common = (root / hubtx.git(root, "rev-parse", "--git-common-dir").stdout.decode().strip()).resolve()
        raise hubtx.BadInput(
            "Tasks are created only on branch %r (hub_config.json task_branch), but HEAD is %s here. "
            "Run new_task.py in the main checkout: %s" % (wanted, repr(current) if current else "detached", common.parent))


def create(tag, title, owner="open", reviewers=(), required="all", kind="documentation",
           deliverables=(), depends_on=(), initial="open", category=None, job=None):
    reviewers = list(reviewers)
    deliverables = list(deliverables)
    depends_on = list(depends_on)
    # All input is validated before the first write (mapping, status file, card).
    _validate(tag, title, category, owner, reviewers, required, kind, deliverables, depends_on, initial, job)
    last = None
    for _ in range(20):
        repo = hubtx.repo_root()
        task = _next_id(repo)  # candidate; the authoritative id is computed under the lock
        status = hub_state.status_path(task)
        card = hub_state.HUB + "tasks/" + task + "_" + _slug(title) + ".md"
        try:
            with hubtx.transaction(tag, task) as tx:
                _check_task_branch(tx.root)  # before the first write
                # The committed state may have moved while we waited for the lock
                # (another task, or a mapping line with a higher number): retry with a fresh id.
                if _next_id(tx.root) != task:
                    continue
                if (tx.root / status).exists() or (tx.root / card).exists():
                    continue
                if category is None and task_names.exists(tx.root):
                    raise hubtx.BadInput("--category is required (one of: %s)" % ", ".join(task_names.CATEGORIES))
                job_field = _choose_job(tx.root, job)  # before the first write
                time = hubtx.now()
                created = events.format_event(
                    tag, "created",
                    _created_fields(owner, reviewers, required, kind, deliverables, depends_on, initial,
                                    job_field),
                    note="created " + task)
                if category is not None:
                    if task in task_names.load(tx.root):
                        raise hubtx.BadInput(task + " already has a line in " + task_names.MAPPING)
                    # First write: if the mapping is not appendable nothing else is written.
                    tx.append(task_names.MAPPING, task + "\t" + category)
                tx.write_new(status, created + "\n")
                tx.write_new(card, _card(task, title, owner, reviewers, initial, tag, time))
                commit = tx.commit([status, card] + ([task_names.MAPPING] if category is not None else []),
                                "create " + task + " " + title[:60])
                return dict(task=task, status=status, card=card, commit=commit, created=created)
        except hubtx.BadInput:
            raise
        except hubtx.Unauthorized:
            raise
        except hubtx.HubError as exc:
            last = exc
            continue
    raise hubtx.Busy("Could not allocate a task id: " + str(last))


def main():
    def run():
        parser = hubtx.ArgumentParser(description=__doc__)
        parser.add_argument("tag")
        parser.add_argument("title")
        parser.add_argument("--owner", default=None)
        parser.add_argument("--reviewers", action="append", default=[])
        parser.add_argument("--required", choices=("all", "any"), default="all")
        parser.add_argument("--kind", default="documentation")
        parser.add_argument("--deliverables", action="append", required=True)
        parser.add_argument("--depends-on", action="append", default=[])
        parser.add_argument("--category", choices=task_names.CATEGORIES, default=None,
                            help="task category; required once docs/agent_hub/TASK_NAMES.tsv exists")
        parser.add_argument("--initial", choices=("open", "proposed", "awaiting_user"), default="open")
        parser.add_argument("--job", default=None, metavar="ID|none",
                            help="the job the task belongs to: an active job id, or none; default: the "
                                 "only active job (refused when several are active)")
        args = parser.parse_args()
        depends_on = [v if re.fullmatch(r"D\d{3,}", v) else task_names.canonical(v)
                      for v in _split(args.depends_on)]
        info = create(args.tag, args.title, owner=args.owner or args.tag,
                      reviewers=_split(args.reviewers), required=args.required, kind=args.kind,
                      deliverables=_split(args.deliverables), depends_on=depends_on,
                      initial=args.initial, category=args.category, job=args.job)
        print(info["task"])
        print(info["commit"])
    return hubtx.cli(run)


if __name__ == "__main__":
    raise SystemExit(main())
