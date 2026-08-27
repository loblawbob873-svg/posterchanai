"""Saved icon coordinates are fitted per viewport without mutating the saved layout."""

import json

from tests.test_desktop_layout import _node


ITEMS = [{"view": x} for x in ("a", "b", "c", "d", "e", "f")]


def fit(pos, width, height):
    return _node(
        f"console.log(JSON.stringify(PCOS.__fitIcons({json.dumps(ITEMS)},"
        f"{json.dumps(pos)},{width},{height})));"
    )


def _no_overlap(points):
    vals = list(points.values())
    return all(abs(a[0] - b[0]) >= 126 or abs(a[1] - b[1]) >= 106
               for i, a in enumerate(vals) for b in vals[i + 1:])


def test_preserves_valid_positions_and_rehomes_only_conflicts_or_offscreen_icons():
    saved = {"a": [20, 20], "b": [160, 20], "c": [160, 20],
             "d": [5000, 5000], "e": [20, 180]}
    out = fit(saved, 620, 420)
    assert out["a"] == saved["a"]
    assert out["b"] == saved["b"]
    assert out["e"] == saved["e"]
    assert out["c"] != saved["c"] and out["d"] != saved["d"]
    assert _no_overlap(out)
    assert all(0 <= x <= 620 and 0 <= y <= 420 for x, y in out.values())


def test_large_tablet_portrait_large_reflow_is_stable_and_restores_saved_positions():
    saved = {it["view"]: [20 + i * 140, 20] for i, it in enumerate(ITEMS)}
    large1 = fit(saved, 1200, 700)
    tablet = fit(saved, 620, 420)
    portrait = fit(saved, 360, 650)
    large2 = fit(saved, 1200, 700)
    assert large1 == large2 == saved
    assert _no_overlap(tablet) and _no_overlap(portrait)
    assert all(x <= 360 and y <= 650 for x, y in portrait.values())
