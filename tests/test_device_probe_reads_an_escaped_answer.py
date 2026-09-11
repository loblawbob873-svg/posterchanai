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
    """A catalogue that came back empty would make every assertion in the walk pass about nothing.

    THE WALK IS SHARDED NOW (three methods, ~30 activity launches did not fit one 90s budget), so
    the floor moved from "the whole catalogue" to "a third of it" — and this test broke by pinning
    the old literal rather than the rule. What has to hold is that each shard still refuses to pass
    having covered nothing, and that the shards between them cover EVERY tile: the partition is
    `index % shards != shard`, which leaves no index out only while every shard is actually run.
    """
    floors = [int(n) for n in re.findall(r"covered >= (\d+)", TEST)]
    assert floors, "the shard's vacuity floor is gone — an empty catalogue would now pass silently"
    shards = re.findall(r"tiles\((\d+), (\d+)\)", TEST)
    assert shards, "the sharded entry points moved — re-point this test"
    counts = {int(total) for _, total in shards}
    assert len(counts) == 1, "the shards disagree about how many there are: %r" % (shards,)
    total = counts.pop()
    assert sorted(int(i) for i, _ in shards) == list(range(total)), (
        "the shards do not partition the catalogue — these indices are covered: %r" % (shards,))
    assert min(floors) * total >= 24, (
        "the shards together only insist on %d tiles, which is well under the catalogue"
        % (min(floors) * total))
    assert "index % shards != shard" in TEST, (
        "the shard partition changed shape; check that no tile falls outside every shard")


def test_teardown_cannot_overwrite_a_verdict_the_assertions_already_reached():
    """THE DEVICE GATE WAS RED FOR EIGHT CONSECUTIVE RUNS AND THE PRODUCT WAS FINE.

    The walk starts MainActivity with FLAG_ACTIVITY_NEW_TASK, because that is what a launcher press
    really is — and `ActivityScenario.close()` then waits for DESTROYED on a handle the launch left
    PAUSED, throwing

        Activity never becomes requested state "[DESTROYED]" (last lifecycle transition = "PAUSED")

    out of the `finally`, after every assertion above it had already passed. All three shards
    reported failure while proving exactly what they were written to prove, 114 of the other 117
    device tests were green, and the gate stayed red from the day the test was added — so it had
    never once passed. A device test that always fails is a device test nobody reads.

    The rule this pins is narrow on purpose: the teardown may tolerate THAT lifecycle timeout and
    nothing else. Swallowing every AssertionError there would hide a real wedge, which is the
    failure this whole test class exists to catch.
    """
    tail = TEST[TEST.index("} finally {"):]
    assert "scenario.close()" in tail, "the scenario is no longer closed at all"
    assert "catch (AssertionError" in tail, (
        "close() can once again fail a shard that passed — the lifecycle timeout from launching "
        "into a NEW_TASK is not a product failure and must not overwrite the verdict")
    assert "never becomes requested state" in tail, (
        "the teardown tolerance is no longer pinned to the one lifecycle timeout it is for; a bare "
        "catch would hide a genuine wedge")
    assert "throw e" in tail, "anything other than that timeout must still fail the test"
