"""Every Concord surface draws from the theme tokens, so every theme reaches it.

Concord's themes (concord.css, "CONCORD THEMES") are sets of `--cct-*` values. A rule written with a
raw colour draws the same in all of them -- the Cyberpunk default would grow a grey Classic panel, the
Light theme a white-on-white label -- and nothing would fail, because the rule itself is fine. So the
stylesheet outside the token block may not carry a raw hex colour, nor a white/black `rgba()` overlay
(those are `rgba(var(--cct-tint-rgb),a)` / `rgba(var(--cct-shade-rgb),a)` and flip on a light theme).

Also: every `--cct-*` a rule reads is defined, and Classic -- which pins every role to the literal it
had before themes -- defines every role the other presets derive, or a role added later would make
Classic quietly change look.
"""
import re
from pathlib import Path

CSS = Path(__file__).resolve().parents[2] / "static/css/concord.css"
START, END = "/* ==== CONCORD THEMES", "/* ==== END CONCORD THEMES ==== */"


def _split():
    text = CSS.read_text()
    assert START in text and END in text, "the theme token block is missing from concord.css"
    a, b = text.index(START), text.index(END) + len(END)
    return text[a:b], text[:a] + text[b:]


def _bodies(css):
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    return re.findall(r"\{([^{}]*)\}", css)


def _block(theme_css, selector):
    m = re.search(re.escape(selector) + r"\{([^{}]*)\}", theme_css)
    assert m, f"no block {selector!r} in the theme section"
    return set(re.findall(r"(--cct-[a-z0-9-]+)\s*:", m.group(1)))


def test_no_raw_colours_outside_the_theme_block():
    _, rest = _split()
    bad = []
    for body in _bodies(rest):
        for decl in body.split(";"):
            if re.search(r"#[0-9a-fA-F]{3,8}\b", decl) and "var(--cc-main," not in decl:
                bad.append(decl.strip())
            if re.search(r"rgba?\(\s*(255\s*,\s*255\s*,\s*255|0\s*,\s*0\s*,\s*0)\s*,", decl):
                bad.append(decl.strip())
    assert not bad, ("Concord rules with a raw colour -- use a --cct-* token so every theme reaches them "
                     "(see the CONCORD THEMES block in concord.css):", bad[:20])


def test_every_token_read_is_defined_and_classic_pins_every_role():
    theme, rest = _split()
    defined = set(re.findall(r"(--cct-[a-z0-9-]+)\s*:", theme))
    used = set(re.findall(r"var\((--cct-[a-z0-9-]+)", rest + theme))
    assert not used - defined, ("Concord reads theme tokens nothing defines", sorted(used - defined))
    roles = _block(theme, ":root")
    classic = _block(theme, '[data-cc-theme="classic"]')
    unpinned = sorted(r for r in roles - classic
                      if r not in {"--cct-ok", "--cct-public-fg", "--cct-private-fg", "--cct-gold", "--cct-star",
                                   "--cct-danger", "--cct-danger-2", "--cct-error", "--cct-dissolve", "--cct-mention",
                                   "--cct-mention-bg", "--cct-mention-me", "--cct-warn", "--cct-warn-rgb",
                                   "--cct-unread-bg", "--cct-unread-fg", "--cct-font-display", "--cct-glow",
                                   "--cct-glow-strong", "--cct-scan", "--cct-fx"})
    assert not unpinned, ("Classic does not pin these roles, so it would follow the derived (non-Classic) value",
                          unpinned)
