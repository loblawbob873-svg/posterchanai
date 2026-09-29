"""Telegram is offered on every tablet and hidden on phones — measured against real screen sizes.

Reported: "i don't see telegram on android tablet". `isPhone()` drew the line at a 600 CSS px short
side (Android's sw600dp bucket), and plenty of real tablets are under it: an 8" 800x1280 panel at
240dpi is 533dp across, a 7" 600x1024 at mdpi-ish densities lands the same way. Every entry point
(sidebar, More sheet, desktop icons) hangs off one root class, so a misjudged tablet had no way in.

Runs the SHIPPED telegram.js under node with a stub screen/document and reads the class it sets.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = (ROOT / "static/js/client/telegram.js").read_text()

HARNESS = r"""
const [w, h] = JSON.parse(process.argv[3]);
const classes = new Set();
globalThis.screen = { width: w, height: h };
globalThis.window = globalThis;
window.__PC_BOOTED = false;
window.addEventListener = () => {};
globalThis.setTimeout = () => 0;
globalThis.document = {
  documentElement: { classList: { toggle(c, on){ on ? classes.add(c) : classes.delete(c); } } },
  addEventListener(){},
};
require(process.argv[2]);
process.stdout.write(JSON.stringify({ hidden: classes.has('pc-no-tg'), isPhone: window.PCTelegram.isPhone() }));
"""

DEVICES = {
    # phones — Telegram stays off (they run Telegram already)
    "Galaxy S23 portrait": ((360, 780), True),
    "Pixel 8 Pro": ((412, 915), True),
    "iPhone 15 Pro Max": ((430, 932), True),
    "Pixel 8 Pro landscape": ((915, 412), True),
    "Z Fold cover screen": ((344, 882), True),
    # tablets — Telegram is on
    '8" 800x1280 @240dpi': ((533, 853), False),
    '7" 1024x600 @160dpi': ((1024, 600), False),
    "Galaxy Tab A7 Lite": ((600, 1004), False),
    "Pixel Tablet landscape": ((1280, 800), False),
    "Z Fold unfolded": ((673, 841), False),
    "iPad mini portrait": ((744, 1133), False),
}


@pytest.mark.skipif(not shutil.which("node"), reason="node required")
@pytest.mark.parametrize("name", list(DEVICES))
def test_telegram_is_offered_on_tablets_and_not_phones(tmp_path, name):
    size, phone = DEVICES[name]
    h = tmp_path / "h.cjs"
    h.write_text(HARNESS)
    r = subprocess.run(["node", str(h), str(ROOT / "static/js/client/telegram.js"), json.dumps(size)],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout)
    assert got["isPhone"] is phone and got["hidden"] is phone, \
        f"{name} {size}: Telegram {'shown on a phone' if phone else 'hidden on a tablet'}"
