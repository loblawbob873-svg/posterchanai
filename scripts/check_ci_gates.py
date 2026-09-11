#!/usr/bin/env python3
"""IS THE THING THAT CATCHES BUGS ITSELF BROKEN?

The Android emulator gate failed EVERY RUN for two days — eight consecutive runs, from the day the
test that broke it was added, so it had never once passed. 114 of its 117 device tests were green
the whole time; the three failures were a teardown artefact that overwrote a verdict the assertions
had already reached. Nobody looked, because nothing local says "your gate is red", and a red that
means nothing trains you to ignore reds.

So this asks GitHub. It is a CHECK and not a unit test because it needs the network and the `gh`
CLI, and because the honest answer when it cannot ask is SKIP (exit 2), never a pass.

WHAT IT REPORTS is the last COMPLETED run of each workflow — not the in-flight one, which is
almost always the commit you just pushed and tells you nothing yet. A workflow that has never run
is not a failure; a workflow whose most recent verdict was failure is.
"""
import json
import shutil
import subprocess
import sys

#: Gates whose red actually means something. A workflow not listed here is reported but not fatal,
#: so adding a new experimental workflow cannot start failing everybody's local run.
REQUIRED = ("Android APK", "Desktop apps", "Android emulator checks")


def runs(limit=40):
    out = subprocess.run(
        ["gh", "run", "list", "--limit", str(limit),
         "--json", "name,status,conclusion,displayTitle,headBranch,createdAt"],
        capture_output=True, text=True, timeout=90)
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout).strip()[:400])
    return json.loads(out.stdout or "[]")


def main():
    if not shutil.which("gh"):
        print("SKIP  the gh CLI is not installed, so CI cannot be asked")
        return 2
    try:
        rows = runs()
    except Exception as e:
        # No network, no auth, rate limited — all "could not ask", which is never "all clear".
        print(f"SKIP  could not read CI: {e}")
        return 2
    if not rows:
        print("SKIP  CI returned no runs")
        return 2

    latest = {}
    streak = {}
    for row in rows:                      # newest first
        name = row.get("name") or "?"
        if row.get("status") != "completed":
            continue
        if name not in latest:
            latest[name] = row
        if row.get("conclusion") == "failure" and latest[name].get("conclusion") == "failure":
            streak[name] = streak.get(name, 0) + 1

    problems, notes = [], []
    for name, row in sorted(latest.items()):
        verdict = row.get("conclusion") or "?"
        line = f"{name}: {verdict} — {(row.get('displayTitle') or '')[:60]}"
        if verdict == "success":
            notes.append("  ok    " + line)
            continue
        consecutive = streak.get(name, 1)
        detail = f"{line} ({consecutive} consecutive failing run(s))"
        if name in REQUIRED:
            problems.append(detail)
        else:
            notes.append("  warn  " + detail)

    for n in notes:
        print(n)
    missing = [n for n in REQUIRED if n not in latest]
    if missing:
        print("  warn  no completed run yet for: " + ", ".join(missing))
    if problems:
        print("FAIL " + "\nFAIL ".join(problems))
        print("     A gate that has been red for several runs is not telling anybody anything. "
              "Read the ASSERTION, not the pass/fail — the last one was a teardown overwriting a "
              "verdict its assertions had already reached.")
        return 1
    print("OK  every required CI gate's last completed run is green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
