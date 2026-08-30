"""A one-second notification sound must not become a permanent display idle inhibitor."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OS = (ROOT / "static/js/client/os.js").read_text()


def test_notification_audio_context_is_short_lived_and_closed():
    ding = OS[OS.index("function ding()") : OS.index("function osToast", OS.index("function ding()"))]
    assert "const ac = new AC()" in ding
    assert "ac.close()" in ding
    assert "let _ac" not in ding
    assert ding.index("o.stop(t0 + 1.2)") < ding.index("ac.close()")
