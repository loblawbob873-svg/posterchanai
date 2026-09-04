"""AN ICON THAT NAMES NO SYMBOL DRAWS NOTHING AND LOGS NOTHING.

Reported: *"obs has no taskbar icon"*. Measured on the live desktop, on the OBS taskbar button:

    <svg class="ic"><use href="#grid"></use></svg><span>OBS 32.2.2 - Profile: …</span>

The sprite's symbol is `i-grid`. Two conventions meet at a data boundary: osshell.js's own `ICO(n)`
writes `#i-${n}`, so inside that file the short name is right — but the ROWS it publishes are
consumed by os.js, whose `iconSvg`/`appIcon` take a FULL symbol id and only prepend the `#`. So
`icon:'grid'` became `href="#grid"`, and every native window's taskbar button and every machine app
in the start menu had an invisible icon. There is no error for this: `<use>` at a missing id renders
empty.

The rule this file enforces is the general one, because the same mistake has been made in the other
direction before (`href="i-wot"` rather than `"#i-wot"`): every icon id that crosses from osshell
into os.js must be a symbol the shipped sprite actually defines.
"""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
SHELL = (ROOT / "static/js/client/osshell.js").read_text(encoding="utf-8")
SPRITE = (ROOT / "static/js/client/sprite.js").read_text(encoding="utf-8")
SYMBOLS = set(re.findall(r'<symbol id="([^"]+)"', SPRITE))


def test_the_sprite_was_actually_read():
    """A symbol table that came out empty would make every assertion below vacuous."""
    assert len(SYMBOLS) > 20, len(SYMBOLS)
    assert "i-grid" in SYMBOLS


def test_every_icon_osshell_publishes_is_a_real_symbol():
    # CODE, not prose: the comments above these fields quote the broken value on purpose.
    code = "\n".join(l for l in SHELL.splitlines()
                     if not l.lstrip().startswith(("*", "//", "/*")))
    published = re.findall(r"\bicon\s*:\s*'([^']+)'", code)
    assert published, "osshell no longer publishes any icon ids — has the field been renamed?"
    bad = [i for i in published if i not in SYMBOLS]
    assert bad == [], (
        "these cross into os.js, which renders them as <use href=\"#%s\"> — no such symbol: %s"
        % ("%s", bad))


def test_os_js_only_prepends_the_hash():
    """The other half of the contract: if `iconSvg` ever started adding the `i-` prefix too, the
    ids above would become `#i-i-grid` and this test would be guarding the wrong convention."""
    os_js = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
    fn = os_js.split("const iconSvg = (href) =>", 1)[1].split("};", 1)[0]
    assert "'#' + h" in fn, fn
    assert "'i-' +" not in fn, fn
