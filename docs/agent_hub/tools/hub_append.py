#!/usr/bin/env python3
"""Append to a clean file and commit only that exact delta under one lock."""
import hubtx
import task_names


def main():
    def run():
        parser = hubtx.ArgumentParser(description=__doc__)
        parser.add_argument("tag")
        parser.add_argument("task")
        parser.add_argument("path")
        parser.add_argument("line")
        args = parser.parse_args()
        with hubtx.transaction(args.tag, task_names.canonical(args.task)) as tx:
            tx.append(args.path, args.line)
            print(tx.commit([args.path], "append " + args.path))
    return hubtx.cli(run)


if __name__ == "__main__":
    raise SystemExit(main())
