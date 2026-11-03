"""Hub state view.

Protocol v1 governs until `hub_config.json` sets `v2_in_force: true`; until then
this script keeps the original v1 output (per-task `reviews:` lines and the
"Reviews with WRONG verdicts" section). With v2 in force it dispatches to
`tools/hub_status.py`.
"""
import re
import subprocess
import sys
from pathlib import Path

HUB = Path(__file__).resolve().parent
ROOT = HUB.parents[1]
AGENTS = ("claude", "gpt", "deepseek")


def field(text, name):
    m = re.search(rf"{name}:\s*([^·\n]+)", text)
    return m.group(1).strip() if m else "?"


def main():
    names, unmapped = {}, []
    try:  # display names need the v2 tools; a standalone copy of this script shows plain ids
        sys.path.insert(0, str(HUB / "tools"))
        import task_names
        names = task_names.load(ROOT)
    except (ImportError, ValueError):
        task_names = None
    print("== Tasks")
    reviews = [p.name for p in (HUB / "reviews").glob("*.md")]
    for p in sorted((HUB / "tasks").glob("T*.md")):
        t = p.read_text()
        tid = p.name.split("_")[0]
        status = field(t, "Status")
        line = f"  {task_names.display(tid, mapping=names, warnings=unmapped) if task_names else tid:14s} {status[:60]:60s} owner {field(t, 'Owner')}"
        if status.startswith("review"):
            done = [r for r in reviews if f"_{tid}_" in r]
            line += f"  reviews: {', '.join(done) if done else 'NONE YET'}"
        print(line)

    if unmapped:
        print("== Tasks without a category (add them to TASK_NAMES.tsv)")
        for w in unmapped:
            print("  " + w)

    print("== Reviews with WRONG verdicts")
    for p in sorted((HUB / "reviews").glob("*.md")):
        n = p.read_text().count("| WRONG")
        if n:
            print(f"  {p.name}: {n} WRONG")

    print("== Last log entry per agent")
    for a in AGENTS:
        log = HUB / "logs" / f"{a}.md"
        heads = re.findall(r"^## (.+)$", log.read_text(), re.M) if log.exists() else []
        print(f"  [{a}] {heads[-1] if heads else 'no log yet'}")

    print("== Uncommitted changes in the repository (other agents may be mid-edit)")
    out = subprocess.run(["git", "status", "--short"], cwd=ROOT, capture_output=True, text=True).stdout
    lines = out.splitlines()
    for line in lines[:40]:
        print("  " + line)
    if len(lines) > 40:
        print(f"  ... {len(lines) - 40} more")
    if not lines:
        print("  none")


def v2_enabled():
    sys.path.insert(0, str(HUB / "tools"))
    try:
        import hubtx
        return bool(hubtx.config().get("v2_in_force"))
    except Exception:
        return False


if __name__ == "__main__":
    if v2_enabled():
        import hub_status as v2

        raise SystemExit(v2.main())
    main()
