"""Every `var(--x)` the client's stylesheets use is defined somewhere.

`var()` of an undefined property is not an error — the declaration silently becomes "unset". That is
how the Telegram client's "Send code" became a transparent box of near-black text ("the telegram
button is all black"): 36 rules across Telegram, the calculator, the ✨ AI button, the screenshot
prompt, Alt+Tab and System Settings were written against `--accent`, which no theme ever set.
A `var(--x, fallback)` is allowed — it says what happens when --x is missing.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECKED = ["static/css/client.css", "static/css/concord.css"]


def _strip_comments(s):
    return re.sub(r"/\*.*?\*/", "", s, flags=re.S)


def _defined():
    out = set()
    for p in [*ROOT.glob("static/css/*.css"), *ROOT.glob("static/js/**/*.js"), *ROOT.glob("templates/**/*.html")]:
        s = p.read_text(errors="ignore")
        out |= set(re.findall(r"(--[\w-]+)\s*:", s))
        out |= set(re.findall(r"setProperty\(\s*['\"](--[\w-]+)", s))
    return out


def test_every_custom_property_the_client_css_uses_is_defined():
    defined = _defined()
    missing = {}
    for rel in CHECKED:
        used = set(re.findall(r"var\(\s*(--[\w-]+)\s*\)", _strip_comments((ROOT / rel).read_text())))
        if used - defined:
            missing[rel] = sorted(used - defined)
    assert not missing, f"var() of a property nothing defines renders as nothing: {missing}"


def test_the_accent_is_defined_on_the_root_so_every_theme_resolves_it():
    css = (ROOT / "static/css/client.css").read_text()
    root = re.search(r"^:root\{(.*?)^\}", css, re.S | re.M).group(1)
    assert re.search(r"--accent\s*:\s*rgb\(var\(--accent-rgb\)\)", root)
