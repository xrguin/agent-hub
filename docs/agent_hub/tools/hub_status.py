#!/usr/bin/env python3
"""Print the protocol-v2 hub state: tasks, next actors, findings, queue, cursor.

Run from anywhere inside the repository:
    python3 docs/agent_hub/tools/hub_status.py [--tag TAG]

The first line is `seen_through=<HEAD>`, the snapshot an agent actually reads;
record it in the log so the next run can list the hub commits it missed.
"""
from __future__ import annotations

from pathlib import Path

import hub_state
import hubtx
import task_names


def _short(value):
    return (value or "")[:12]


def render(repo, tag=None):
    view = hub_state.status_view(repo, tag)
    lines = ["seen_through=" + (view["head"] or "UNKNOWN")]
    lines.append("== Tasks")
    names, unmapped = task_names.load(repo), []
    for task in sorted(view["tasks"].values(), key=lambda item: item["task"]):
        accepted = "yes" if task["accepted"] else "no"
        line = (f"  {task_names.display(task['task'], mapping=names, warnings=unmapped):<14s} state={task['state']:<28s} owner={str(task['owner']):<9s} "
                f"next={task['next_actor']:<18s} accepted={accepted:<3s} rev={_short(task['revision'])}")
        if task.get("next_via"):
            line += " (via " + ",".join(task["next_via"]) + ")"
        if task.get("review_files_all"):
            line += "  reviews=" + ",".join(Path(name).name for name in task["review_files_all"])
        if task["open_findings"]:
            line += "  open_findings=" + ",".join(f["finding"] for f in task["open_findings"])
        if task["blocked_by"]:
            line += "  blocked_by=" + ",".join(task["blocked_by"])
        if task["warnings"]:
            line += "  warnings=" + "; ".join(task["warnings"])
        lines.append(line)
    if unmapped:
        lines.append("== Tasks without a category (add them to TASK_NAMES.tsv)")
        lines.extend("  " + w for w in unmapped)
    if view["duplicates"]:
        lines.append("== Duplicate task ids")
        for task, names in sorted(view["duplicates"].items()):
            lines.append(f"  {task}: {', '.join(names)}")
    if view["broken_links"]:
        lines.append("== Broken hub links")
        for item in view["broken_links"]:
            lines.append(f"  {item['file']} -> {item['target']}")

    if tag:
        lines.append(f"== Queue [{tag}]")
        queue = view["queue"]
        if not queue["tasks"] and not queue["answered_questions"]:
            lines.append("  (empty)")
        for item in queue["tasks"]:
            if "finding" in item:
                lines.append(f"  finding {item['finding']} on {item['task']}")
            else:
                lines.append(f"  task {item['task']} state={item['state']} next={item['next_actor']}")
        for item in queue["answered_questions"]:
            lines.append(f"  question {item['qid']} answered by {','.join(item['answered_by'])}")
        new = view["new"]
        lines.append("== New since " + (new["seen_through"] or "missing/non-ancestor seen_through"))
        if not new["commits"]:
            lines.append("  (none)")
        for commit in new["commits"]:
            lines.append("  " + commit)

    lines.append("== Last log entry per agent")
    for agent in sorted(hub_state.events.TAGS):
        entries = hub_state._log_entries(repo, agent)
        lines.append(f"  [{agent}] {entries[-1].splitlines()[0] if entries else 'no log yet'}")
    lines.append("== Uncommitted changes in the repository (other agents may be mid-edit)")
    status = hubtx.git(repo, "status", "--short", check=False).stdout.decode(errors="replace").splitlines()
    for line in status[:40]:
        lines.append("  " + line)
    if len(status) > 40:
        lines.append(f"  ... {len(status) - 40} more")
    if not status:
        lines.append("  none")
    return "\n".join(lines)


def main():
    def run():
        parser = hubtx.ArgumentParser(description=__doc__)
        parser.add_argument("--tag", default=None)
        args = parser.parse_args()
        repo = hubtx.repo_root()
        print(render(repo, args.tag))
    return hubtx.cli(run)


if __name__ == "__main__":
    raise SystemExit(main())
