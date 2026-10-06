"""On the APK a copy is only "copied" once the clipboard reads back the value.

Reported from an Android tablet: "it says link copied but nothing pastes". The Capacitor Clipboard
write resolved, the toast claimed success, and the clipboard held something else. The shipped
copyValue now reads the clipboard back: the value there -> the toast; something else -> the value is
put on screen to copy by hand (never the execCommand fallback, which reports true without copying in
the same WebView); a read that could not be made -> the old answer, because "could not ask" is not
"missing".
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(not shutil.which("node"), reason="node is required")
def test_the_apk_never_claims_a_copy_the_clipboard_does_not_hold():
    out = json.loads(subprocess.run(["node", str(ROOT / "tests/client/copy_value_apk_sim.mjs")],
                                    capture_output=True, text=True, check=True, timeout=60).stdout)
    assert out["keeps"] == {"result": True, "toasts": ["link copied"], "fallbacks": []}
    dropped = out["drops"]
    assert "link copied" not in dropped["toasts"], "the toast claimed a copy the clipboard does not hold"
    assert dropped["fallbacks"] == ["https://poster.place/nevent1abc"], "the link was not put on screen"
    assert dropped["result"] is False
    assert out["unreadable"]["toasts"] == ["link copied"] and not out["unreadable"]["fallbacks"], \
        "a clipboard that cannot be READ must not turn every copy into a dialog"
