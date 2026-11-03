#!/usr/bin/env python3
"""Commit named, owned files while preserving other agents' staging."""
import hubtx
import task_names


def main():
    def run():
        parser = hubtx.ArgumentParser(description=__doc__)
        parser.add_argument("tag")
        parser.add_argument("task")
        parser.add_argument("-m", "--message", required=True)
        parser.add_argument("paths", nargs="+")
        args = parser.parse_args()
        with hubtx.transaction(args.tag, task_names.canonical(args.task)) as tx:
            print(tx.commit(args.paths, args.message))
    return hubtx.cli(run)


if __name__ == "__main__":
    raise SystemExit(main())
