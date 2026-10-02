"""A button with an icon AND a label must use `b-ic` ("The AI Actions in windows -> Continue icon and text
in the button are not aligned"). client.css centres icon and label only for `.btn:has(.b-ic)`; a bare
`svg.ic` sits on the baseline beside the text and reads as misaligned. Scans the shipped client."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BUTTON = re.compile(r'<button class="btn[^"]*"[^>]*>(.*?)</button>', re.S)


def test_every_button_with_an_icon_and_a_label_centres_them():
    bad = []
    for f in sorted((ROOT / "static/js/client").glob("*.js")):
        for m in BUTTON.finditer(f.read_text(encoding="utf-8")):
            inner = m.group(1)
            icon = re.search(r'<svg class="ic([^"]*)"', inner)
            if not icon or "b-ic" in icon.group(1):
                continue
            label = re.sub(r"<svg.*?</svg>|<[^>]+>|\$\{[^}]*\}", "", inner, flags=re.S).strip()
            if label:
                line = f.read_text(encoding="utf-8")[:m.start()].count("\n") + 1
                bad.append(f"{f.name}:{line} {label[:40]!r}")
    assert not bad, "icon without b-ic beside a label (misaligned): " + "; ".join(bad)


def test_the_centring_rule_is_still_there():
    css = (ROOT / "static/css/client.css").read_text(encoding="utf-8")
    assert ".btn:has(.b-ic){display:inline-flex;align-items:center" in css
