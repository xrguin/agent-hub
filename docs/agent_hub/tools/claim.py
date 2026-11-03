#!/usr/bin/env python3
"""Claim or release a protocol-v2 task's write scope, as one status event."""
import hub_events as events
import hub_state
import hubtx
import task_names


def _split(values):
    out = []
    for value in values or []:
        out.extend(part.strip() for part in value.split(",") if part.strip())
    return out


def change(tag, task, release=False, scope=()):
    """Append one claim/release event; return the commit hash."""
    with hubtx.transaction(tag, task) as tx:
        if release:
            line = events.format_event(tag, "release", {})
            action = "release"
        else:
            scope = _split(scope)
            if not scope:
                raise hubtx.BadInput("claim needs at least one --scope glob")
            line = events.format_event(tag, "claim", dict(scope=scope))
            action = "claim"
        tx.append(hub_state.status_path(task), line)
        return tx.commit([hub_state.status_path(task)], action + " " + task)


def main():
    def run():
        parser = hubtx.ArgumentParser(description=__doc__)
        parser.add_argument("tag")
        parser.add_argument("task")
        parser.add_argument("--release", action="store_true")
        parser.add_argument("--scope", action="append", default=[])
        args = parser.parse_args()
        print(change(args.tag, task_names.canonical(args.task), release=args.release, scope=args.scope))
    return hubtx.cli(run)


if __name__ == "__main__":
    raise SystemExit(main())
