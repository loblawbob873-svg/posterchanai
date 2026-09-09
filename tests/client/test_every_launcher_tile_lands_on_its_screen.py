"""EVERY tile the launcher offers, walked through the handoff, lands on ITS OWN screen.

    "when I click on Messages from the android launcher, it does not load messages for me"

That report went unanswered by three existing gates because all three sit beside this seam rather
than across it:

  * `AppViewsLaunchSmokeTest` opens every shipped view — by calling `__PC.switchView(view)` directly
    in JS. It proves the SCREENS work; it never touches a launcher intent.
  * `test_android_launch_view.py::test_the_catalogue_matches_the_sidebar` is a NAME check: every
    tile's slug appears as a `data-view=` in the shipped template. A tile can name a perfectly valid
    view and still land nowhere.
  * `LauncherDeviceTest` walks ONE tile end to end, and that tile is Texts — a native Activity, not
    a WebView view, so it exercises a different code path entirely.

Here the tile list comes from the shipped `HomeTiles.java` catalogue and nowhere else, so a tile
added later joins this gate the moment it ships — and if the parse ever comes back short, that is a
failure rather than a green no-op.

WHICH HALVES RUN WHERE (there is no KVM on this box, so the emulator gate cannot run locally):
  * the CLIENT half runs here, per tile, against the shipped phoneshell.js — cold (parked request)
    and warm (the native `launchView` announcement), because those are different carriers and the
    warm one is where the extras used to be dropped;
  * the PARKING half runs here too, under javac + java, per tile — the real `LaunchView`;
  * the whole chain through a real Activity and a real Chromium renderer is
    `LauncherTileLandsOnItsScreenDeviceTest`, which runs on the emulator in CI.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOME = ROOT / "mobile/android/app/src/main/java/place/poster/app/home"

# Tiles that deliberately do NOT open a client view. Each is a documented native destination in
# HomeTiles (`nativeTarget`) or the phone's own Settings; anything else in the catalogue is a view.
NOT_A_VIEW = {"app", "_settings", "_phone", "_texts"}


def tiles():
    """The shipped catalogue, read from HomeTiles.java. Never a list typed into this file."""
    src = (HOME / "HomeTiles.java").read_text()
    consts = dict(re.findall(r'String (VIEW_[A-Z]+) = "([^"]+)"', src))
    out = []
    for m in re.finditer(r'new Tile\((VIEW_[A-Z]+|"[a-z0-9-]+"),', src):
        raw = m.group(1)
        out.append(consts[raw] if raw in consts else raw.strip('"'))
    return out


def view_tiles():
    return [t for t in tiles() if t not in NOT_A_VIEW]


def test_the_catalogue_parses_and_is_not_quietly_empty():
    """A parse that silently returns [] turns every assertion below into a no-op."""
    all_tiles = tiles()
    assert len(all_tiles) >= 30, all_tiles
    assert "messages" in all_tiles, "the reported tile is gone from the catalogue — re-read this"
    # And every exclusion must still BE in the catalogue, or the skip list is stale and silently
    # excusing a tile that no longer exists while a real one goes uncovered.
    for name in NOT_A_VIEW:
        assert name in all_tiles, "%s is excluded from this gate but no longer a tile" % name


def test_every_tile_reaches_its_own_view_in_the_shipped_client():
    env = dict(os.environ, PC_TILES=json.dumps(view_tiles()))
    r = subprocess.run(["node", str(ROOT / "tests/client/launcher_tile_landing_runtime.mjs")],
                       capture_output=True, text=True, timeout=180, env=env)
    assert r.returncode == 0, r.stdout + r.stderr


HARNESS = r"""
import place.poster.app.home.LaunchView;

public class Tiles {
    public static void main(String[] a) {
        long t = 1_000_000_000L;
        int bad = 0;
        for (String view : a) {
            LaunchView.clear();
            LaunchView.request(view, t);
            String got = LaunchView.take(t + 1);
            if (!view.equals(got)) { bad++; System.out.println("FAIL park " + view + " -> '" + got + "'"); }

            // And through the delivery carrier a NOTIFICATION depends on, which re-stamps at the
            // moment Android hands the intent over rather than trusting the extra's own age.
            LaunchView.clear();
            if (!LaunchView.deliver(view, 0, false, t)) { bad++; System.out.println("FAIL deliver " + view); }
            String got2 = LaunchView.take(t + 1);
            if (!view.equals(got2)) { bad++; System.out.println("FAIL delivered " + view + " -> '" + got2 + "'"); }
        }
        System.out.println(bad == 0 ? "ALL OK" : (bad + " FAILED"));
        if (bad != 0) System.exit(1);
    }
}
"""


@pytest.mark.skipif(not (shutil.which("javac") and shutil.which("java")),
                    reason="javac/java not installed")
def test_every_tile_survives_the_parking_layer():
    """A slug that the park/take layer trims, drops or mangles never reaches the client at all."""
    names = view_tiles()
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        pkg = d / "place/poster/app/home"
        pkg.mkdir(parents=True)
        shutil.copy(HOME / "LaunchView.java", pkg / "LaunchView.java")
        (d / "Tiles.java").write_text(HARNESS)
        c = subprocess.run(["javac", "-d", str(d), str(pkg / "LaunchView.java"), str(d / "Tiles.java")],
                           capture_output=True, text=True, cwd=d)
        assert c.returncode == 0, c.stderr
        r = subprocess.run(["java", "-cp", str(d), "Tiles"] + names,
                           capture_output=True, text=True, cwd=d)
        assert "ALL OK" in r.stdout, r.stdout + r.stderr


def test_the_device_gate_covers_the_same_catalogue():
    """The emulator test must derive its list from HomeTiles too, or the two drift apart.

    There is no KVM here, so that test cannot be RUN locally — this is the floor that stops it
    silently becoming a hand-copied subset while nobody is watching it run in CI.
    """
    dev = (ROOT / "mobile/android/app/src/androidTest/java/place/poster/app"
           / "LauncherTileLandsOnItsScreenDeviceTest.java").read_text()
    assert "HomeTiles.catalogue()" in dev, "the device gate types its own tile list"
    for name in sorted(NOT_A_VIEW):
        assert name in dev or "nativeTarget" in dev, \
            "the device gate has no documented reason to skip %s" % name
