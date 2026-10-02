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


# ---- the APK's answer, and the person's -------------------------------------------------------------
JAVA = ROOT / "mobile/android/app/src/main/java/place/poster/app/home/FormFactor.java"

# (name, voiceCapable, real px w, h, xdpi, ydpi, expected)
APK_DEVICES = [
    # "I need telegram to be available on android tablets": an LTE Galaxy Tab CAN place calls.
    ("Galaxy Tab S9 LTE", True, 2560, 1600, 274.0, 274.0, "tablet"),
    ("Galaxy Tab A9+ 5G", True, 1920, 1200, 205.0, 205.0, "tablet"),
    ("Galaxy Tab A7 Lite Wi-Fi", False, 1340, 800, 179.0, 179.0, "tablet"),
    ("Fire 7", False, 1024, 600, 170.0, 170.0, "tablet"),
    ("Galaxy S23", True, 1080, 2340, 425.0, 425.0, "phone"),
    ("Galaxy S24 Ultra", True, 1440, 3120, 501.0, 501.0, "phone"),
    ("Z Fold cover", True, 904, 2316, 402.0, 402.0, "phone"),
    ("Z Fold unfolded", True, 1812, 2176, 374.0, 374.0, "tablet"),
    # a display that does not report its density falls back to voice capability
    ("emulator, no dpi, voice", True, 1080, 2400, 0.0, 0.0, "phone"),
    ("emulator, no dpi, no voice", False, 1080, 2400, 0.0, 0.0, "tablet"),
]


@pytest.mark.skipif(not (shutil.which("javac") and shutil.which("java")), reason="needs a JDK")
def test_the_apk_decides_by_the_physical_screen_not_by_whether_it_can_call(tmp_path):
    pkg = tmp_path / "place/poster/app/home"
    pkg.mkdir(parents=True)
    (pkg / "FormFactor.java").write_text(JAVA.read_text())
    rows = ";".join(f'"{n}",{str(v).lower()},{w},{h},{x}f,{y}f,"{e}"' for n, v, w, h, x, y, e in APK_DEVICES)
    (tmp_path / "Run.java").write_text(
        "import place.poster.app.home.FormFactor;\npublic class Run { public static void main(String[] a){\n"
        + "\n".join(f'  {{ double in = FormFactor.inches({w},{h},{x}f,{y}f); String got = FormFactor.classify({str(v).lower()}, in);'
                    f' System.out.println("{n}|" + got + "|" + in); }}' for n, v, w, h, x, y, e in APK_DEVICES)
        + "\n}}\n")
    subprocess.run(["javac", "-d", str(tmp_path), str(pkg / "FormFactor.java"), str(tmp_path / "Run.java")],
                   check=True, capture_output=True, timeout=120)
    out = subprocess.run(["java", "-cp", str(tmp_path), "Run"], check=True, capture_output=True, text=True,
                         timeout=60).stdout.strip().splitlines()
    got = {line.split("|")[0]: line.split("|")[1] for line in out}
    want = {n: e for n, _v, _w, _h, _x, _y, e in APK_DEVICES}
    assert got == want, {n: (got.get(n), want[n]) for n in want if got.get(n) != want[n]}


CHOICE = r"""
const store = new Map(JSON.parse(process.argv[4] || '[]'));
globalThis.localStorage = { getItem: k => store.has(k) ? store.get(k) : null, setItem: (k, v) => store.set(k, String(v)),
                            removeItem: k => store.delete(k) };
""" + HARNESS.replace(
    "process.stdout.write(JSON.stringify({ hidden: classes.has('pc-no-tg'), isPhone: window.PCTelegram.isPhone() }));",
    "window.PCTelegram._setForm(process.argv[5] || ''); const a = { hidden: classes.has('pc-no-tg'), isPhone: window.PCTelegram.isPhone() };"
    "window.PCTelegram.setHere(false); a.afterOff = window.PCTelegram.isPhone();"
    "process.stdout.write(JSON.stringify(a));")


@pytest.mark.skipif(not shutil.which("node"), reason="node required")
def test_this_device_can_be_given_telegram_whatever_it_is_taken_for(tmp_path):
    h = tmp_path / "c.cjs"
    h.write_text(CHOICE)
    run = lambda stored, form: json.loads(subprocess.run(
        ["node", str(h), str(ROOT / "static/js/client/telegram.js"), json.dumps([360, 780]), json.dumps(stored), form],
        capture_output=True, text=True, timeout=30, check=True).stdout)
    chosen = run([["pc.tg.onThisDevice", "1"]], "phone")
    assert chosen["hidden"] is False and chosen["isPhone"] is False, "the person's choice lost to the rule"
    assert chosen["afterOff"] is True, "turning the switch off did not hand the decision back to the rule"
    assert run([], "phone")["isPhone"] is True


def test_the_switch_is_in_settings_where_a_hidden_telegram_can_still_be_reached():
    settings = (ROOT / "static/js/client/settings.js").read_text()
    assert 'id="us-tg-here"' in settings and "T.setHere(here.checked)" in settings
