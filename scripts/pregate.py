#!/usr/bin/env python3
"""Run, BEFORE a deploy, the tests a change can break -- including the ones that never name the file.

Why this exists: on 2026-10-03 four deploy gates (~50 min each) failed on tests the change had broken,
and a hand-picked "targeted run" had passed every time. The hand-picked runs chose tests by grepping
for the changed file's name. The tests that failed were of two kinds that grep cannot find:

  * RULE tests that walk a whole directory -- every private document must be classified
    (test_client_private_docs_are_classified), every name a module uses must resolve
    (test_client_module_deps), every setting must hydrate. They never mention the file you edited.
  * Tests gated on an asset the worktree does not have (the LaMa model): they SKIP locally and RUN in
    the gate, so a local pass proves nothing.

So this selects:
  1. tests that name a changed file (path, basename, or module stem),
  2. every rule test that scans a directory a changed file lives in,
  3. every changed test file,
and links gate-only assets from the main checkout before running, then runs the selection in
parallel shards (the gate's own shape) and prints one line per failure.

  scripts/pregate.py                 # changes vs origin/master, run them
  scripts/pregate.py --list          # just print the selection
  scripts/pregate.py --base HEAD~3   # a different base
"""
from __future__ import annotations

import argparse
import fcntl
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN = Path(os.environ.get("PC_MAIN_CHECKOUT", str(Path.home() / "posterchanai")))
# Assets some tests need that a worktree does not carry (gitignored, large). The gate runs in the
# main checkout, where they exist -- so without them here, those tests skip locally and fail there.
GATE_ASSETS = ["assets/lama_fp32.onnx", "mobile/android/app/src/main/assets/public"]   # the model; the built APK web bundle
COMMON = {"client", "store", "admin", "index", "utils", "style", "styles", "main", "server", "config", "common",
          "helpers", "service", "router", "models", "schemas", "settings", "database", "preload", "upload"}
# A test that walks a directory: these words next to a path it names.
WALKS = re.compile(r"os\.walk|\.r?glob\(|listdir|scandir|iterdir")


def sh(*args, cwd=ROOT) -> str:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True).stdout


def changed_files(base: str) -> list[str]:
    files = set(sh("git", "diff", "--name-only", f"{base}...HEAD").split())
    files |= set(sh("git", "diff", "--name-only", "HEAD").split())          # uncommitted
    files |= set(sh("git", "ls-files", "--others", "--exclude-standard").split())
    return sorted(f for f in files if (ROOT / f).exists())


def all_tests() -> list[Path]:
    return sorted(p for p in (ROOT / "tests").rglob("test_*.py") if "fixtures" not in p.parts)


def needles(path: str) -> set[str]:
    """What a test would write to mean this file."""
    p = Path(path)
    out = {path, p.name}
    # A module's bare stem is how a test names it ("concord", "relay_lists") -- unless the stem is a word
    # every test says anyway ("client", "store", "main"), which would select the whole suite.
    if p.suffix in (".py", ".js", ".mjs", ".cjs", ".cpp", ".sh") and len(p.stem) >= 5 and p.stem.lower() not in COMMON:
        out.add(p.stem)
    if p.suffix == ".py" and p.parts[0] == "app":
        out.add(".".join(p.with_suffix("").parts))                          # app.services.x
    return out


def select(changed: list[str]) -> tuple[list[Path], dict[Path, str]]:
    why: dict[Path, str] = {}
    dirs = {str(Path(f).parent) for f in changed}
    texts = {t: t.read_text(errors="replace") for t in all_tests()}
    for t, text in texts.items():
        rel = str(t.relative_to(ROOT))
        if rel in changed:
            why[t] = "changed"
            continue
        for f in changed:
            if any(n in text for n in needles(f) if len(n) > 3):
                why[t] = "names " + f
                break
        else:
            if WALKS.search(text):
                for d in dirs:
                    spelled = (d, d.replace("/", '", "'), d.replace("/", "', '"))
                    if d not in (".", "tests") and any(s in text for s in spelled):
                        why[t] = "rule over " + d
                        break
    # Runtime helpers (.mjs/.js next to tests) are run by a test that names them.
    return sorted(why), why


def link_assets() -> list[str]:
    linked = []
    for a in GATE_ASSETS:
        here, there = ROOT / a, MAIN / a
        if not here.exists() and there.exists() and ROOT != MAIN:
            here.parent.mkdir(parents=True, exist_ok=True)
            here.symlink_to(there)
            linked.append(a)
    return linked


def run(tests: list[Path], shards: int) -> int:
    py = str(MAIN / "venv-unified/bin/python") if (MAIN / "venv-unified/bin/python").exists() else sys.executable
    groups = [tests[i::shards] for i in range(shards)]
    procs = []
    for i, g in enumerate(groups):
        if not g:
            continue
        log = open(ROOT / f".pregate.{i}.log", "w")
        procs.append((subprocess.Popen([py, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "faulthandler_timeout=300",
                                        *map(str, g)], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT), log, i))
    bad = 0
    for p, log, i in procs:
        p.wait(); log.close()
        text = re.sub(r"\x1b\[[0-9;]*m", "", (ROOT / f".pregate.{i}.log").read_text(errors="replace"))
        summary = [l for l in text.splitlines() if re.search(r"\d+ (passed|failed)", l)]
        print(f"shard {i}: {summary[-1].strip() if summary else 'no summary (killed or crashed?)'}")
        for l in text.splitlines():
            if l.startswith(("FAILED", "ERROR")):
                print("  " + l[:220]); bad += 1
        if p.returncode not in (0, 5):
            bad += bad == 0
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="origin/master")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--shards", type=int, default=3)
    a = ap.parse_args()
    # ONE RUN AT A TIME PER CHECKOUT. Every run writes .pregate.<shard>.log; a second run started beside a
    # first overwrote its logs, the first was then stopped as a duplicate, and a deploy went out on the
    # strength of a run that never covered all of its commits (deploy 81 aborted in the gate). --list
    # writes nothing and is always allowed.
    if not a.list:
        lock = open(os.environ.get("PREGATE_LOCK") or (ROOT / ".pregate.lock"), "w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("another pregate is already running in this checkout -- wait for it; its result is the one "
                  "that counts (not starting a second run that would overwrite its logs)")
            return 2
        lock.write(str(os.getpid()))
        lock.flush()
    changed = changed_files(a.base)
    if not changed:
        print("no changes against", a.base); return 0
    tests, why = select(changed)
    print(f"{len(changed)} changed files -> {len(tests)} test files")
    if a.list:
        for t in tests:
            print(f"  {t.relative_to(ROOT)}  ({why[t]})")
        return 0
    for x in link_assets():
        print("linked from the main checkout:", x)
    return run(tests, a.shards)


if __name__ == "__main__":
    sys.exit(main())
