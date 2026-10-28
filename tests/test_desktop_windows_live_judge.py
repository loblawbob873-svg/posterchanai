"""scripts/check_desktop_windows_live.py's verdict, on sessions shaped like the tablet report ("social ~20 s")."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import check_desktop_windows_live as C  # noqa: E402


def _rounds(social=0.4, n=10, heap=8.0, **over):
    rows = [{"social": social, "messages": 0.1, "notifications": 0.1, "heapMB": heap} for _ in range(n)]
    for i, row in over.items():
        rows[int(i[1:])].update(row)
    return rows


def test_a_healthy_session_passes():
    assert C.judge(_rounds()) == []


def test_a_twenty_second_social_window_fails():
    assert any("social: slowest open 20" in f for f in C.judge(_rounds(r4={"social": 20.0})))


def test_opening_that_gets_slower_fails_even_under_budget():
    rows = _rounds()
    for r in rows[-3:]:
        r["social"] = 3.5
    assert any("got slower" in f for f in C.judge(rows))


def test_a_growing_heap_fails():
    rows = _rounds()
    rows[-1]["heapMB"] = 40.0
    assert any("heap grew" in f for f in C.judge(rows))


def test_a_window_that_never_opens_fails():
    assert any("never showed" in f for f in C.judge(_rounds(r2={"messages": None})))


def test_it_is_registered_as_a_live_check():
    assert '"check_desktop_windows_live"' in (Path(C.__file__).parent / "checkall.py").read_text()
