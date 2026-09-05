import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_wayfire_desktop_never_covers_normal_windows():
    run = subprocess.run(
        ["node", str(ROOT / "tests/client/desktop_bottom_sim.js")],
        cwd=ROOT, text=True, capture_output=True, timeout=10,
    )
    assert run.returncode == 0, run.stderr
    assert "behavioral simulation: ok" in run.stdout


def test_main_tells_the_guard_when_the_desktop_has_a_window_of_its_own():
    """The guard's exception is useless unless main actually wires the predicate.

    Two mechanisms lower the desktop -- `sinkShellSurfaces` (send-to-back) and this guard (refocus a
    sibling) -- and only the first ever honoured `pc:wm:shell-front`. The second overrode it within
    milliseconds, which is why "System settings never gets focus" survived every renderer-side fix.
    """
    main = (ROOT / "desktop/main.js").read_text(encoding="utf-8")
    at = main.index("createDesktopBottomGuard({")
    wiring = main[at: main.index("}));", at)]
    assert "wantsFront:" in wiring, "the bottom guard is wired without the front-wish exception"
    assert "_shellWantsFront" in wiring, (
        "the predicate does not read the set pc:wm:shell-front writes, so it can never say yes")
    assert "conId" in wiring, (
        "a compositor view id must be mapped back to the renderer that owns it")
