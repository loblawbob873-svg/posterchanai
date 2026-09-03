"""Target-blank links must not become undecorated PosterChanOS windows.

Only ``?pcwin=`` URLs participate in the desktop window manager.  A generic same-origin link used
to pass Electron's window-open handler and create a raw child: a small square surface absent from
PCOS bookkeeping and, on the frameless shell, effectively impossible to close.
"""
from pathlib import Path


MAIN = (Path(__file__).resolve().parents[1] / "desktop/main.js").read_text(encoding="utf-8")


def _handler() -> str:
    start = MAIN.index("created.webContents.setWindowOpenHandler")
    end = MAIN.index("created.webContents.on('will-redirect'", start)
    return MAIN[start:end]


def test_only_explicit_pcos_windows_are_allowed_for_ordinary_urls():
    body = _handler()
    assert "isOurs(url) && /[?&]pcwin=/" in body
    assert "if (isOurs(url) || /^blob:|^data:/.test(url))" not in body
    assert "if (/^https?:/i.test(url)) shell.openExternal(url)" in body
    assert "return { action: 'deny' };\n  });" in body


def test_in_process_media_preview_has_a_real_closeable_frame():
    body = _handler()
    media = body[body.index("if (/^blob:|^data:/.test(url)"):]
    media = media[:media.index("/* `isOurs`", 1)]
    assert "action: 'allow'" in media
    assert "frame: true" in media
    assert "minWidth: 360" in media and "minHeight: 240" in media


def test_generic_same_origin_allow_rule_cannot_return():
    body = _handler()
    pcwin_end = body.index("if (/^blob:|^data:/.test(url)")
    tail = body[pcwin_end:]
    assert "isOurs(url)) return { action: 'allow'" not in tail
