"""A browser test that waits for something to APPEAR allows it at least 10 seconds -- or says why not.

Twice on 2026-10-04 a pregate shard failed on a test that passed alone: "Relay is not defined" (the Notes
search runtime gave its page 3 s to boot) and "Notes module did not boot" (4 s). Both waited for a page
or a module, and a gate running three shards of Chrome at once is slower than a laptop running one test.
A wait that polls until something appears costs nothing extra when the thing is fast -- it returns at
once -- so its limit should be about failure, not speed: >= 10 s.

A wait that IS a speed requirement (a menu must paint within 1 s) is marked `# deadline: <why>` on the
loop or the line above it, and is left alone. Loops that repeat an action a fixed number of times (no
break/return) are not waits and are not checked.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIN_SECONDS = 10
LOOP = re.compile(r"(?P<ind>[ \t]*)for _ in range\((?P<n>\d+)\):[ \t]*(?P<tail>#[^\n]*)?\n(?P<body>(?:(?P=ind)[ \t]+[^\n]*\n){1,4})")


def short_waits(files=None):
    out = []
    for f in files or sorted((ROOT / "tests" / "client").glob("*.py")):
        src = f.read_text(errors="replace")
        for m in LOOP.finditer(src):
            body = m.group("body")
            sleep = re.search(r"asyncio\.sleep\(\s*([0-9.]+)\s*\)", body)
            if not sleep or not re.search(r"\b(break|return)\b", body):
                continue
            prev = src[:m.start()].rstrip("\n").rsplit("\n", 1)[-1]
            if "deadline:" in (m.group("tail") or "") or "deadline:" in prev:
                continue
            total = int(m.group("n")) * float(sleep.group(1))
            if total < MIN_SECONDS:
                out.append(f"{f.name}:{src.count(chr(10), 0, m.start()) + 1} waits {total:g}s")
    return out


def test_every_wait_for_a_page_allows_ten_seconds():
    bad = short_waits()
    assert not bad, ("waits that give up before a loaded gate's page has booted (raise the count, or mark "
                     "`# deadline: <why>` if the limit IS the test):\n  " + "\n  ".join(bad))


def test_the_scan_sees_the_wait_that_flaked(tmp_path):
    f = tmp_path / "test_x.py"
    f.write_text("async def t(js, asyncio):\n"
                 "    for _ in range(100):\n"
                 "        if await js('!!window.PCNotes'): break\n"
                 "        await asyncio.sleep(.03)\n"
                 "    # deadline: the menu must paint within 1 s\n"
                 "    for _ in range(10):\n"
                 "        if await js('x'): break\n"
                 "        await asyncio.sleep(.1)\n"
                 "    for _ in range(4):\n"
                 "        await js('click()')\n"
                 "        await asyncio.sleep(.3)\n")
    assert short_waits([f]) == ["test_x.py:2 waits 3s"], short_waits([f])
