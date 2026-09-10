"""`evaluateJavascript` hands Java a JSON-ENCODED value, and a probe that forgets is always wrong.

The launcher-tile gate asked the page for `JSON.stringify({active, painted})` and then matched the
Java literal `"active":true` against the result. That text is never present: the WebView escapes it,
so what arrives is `"{\\"active\\":true,\\"painted\\":true}"`. The probe therefore NEVER succeeded —

  * `aColdLaunchAlsoLandsOnMessages` failed with `a cold launch did not land on Messages:
    "{\\"active\\":true,\\"painted\\":true}"` — a failure message stating, in its own evidence, that
    both conditions held;
  * `everyTileOpensTheScreenItNames` spent its full budget on every tile and blew the runner's
    90-second per-test timeout, whose stack was then attributed to whichever test ran next.

One escaping mistake, two red tests, and a report that argued against itself.
"""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
TEST = (ROOT / "mobile/android/app/src/androidTest/java/place/poster/app"
        / "LauncherTileLandsOnItsScreenDeviceTest.java").read_text(encoding="utf-8")
SETTLE = TEST.split("private static String settle(", 1)[1].split("\n    }", 1)[0]


def test_the_probe_answers_a_bare_token_not_json():
    """A token survives JSON encoding unchanged; a quoted key does not."""
    assert "JSON.stringify" not in SETTLE, SETTLE
    assert "'ok'" in SETTLE, SETTLE


def test_it_does_not_match_a_quoted_key_against_an_escaped_answer():
    assert '\\"active\\":true' not in SETTLE and '"active":true' not in SETTLE, SETTLE


def test_the_per_tile_budget_fits_the_runners_timeout():
    """This runs once per tile across the whole shipped catalogue — at least 30 of them — inside a
    90s per-test timeout. Six seconds a tile cannot fit and did not."""
    loops = int(re.search(r"i < (\d+); i\+\+", SETTLE).group(1))
    sleep = int(re.search(r"SystemClock\.sleep\((\d+)\)", SETTLE).group(1))
    worst_ms = loops * sleep
    assert worst_ms <= 2500, f"a tile that cannot settle costs {worst_ms}ms"
    assert worst_ms * 30 < 90000, f"30 tiles at {worst_ms}ms exceeds the 90s per-test timeout"


def test_the_gate_still_refuses_a_vacuous_run():
    """A catalogue that came back empty would make every assertion in the walk pass about nothing."""
    assert "covered >= 30" in TEST
