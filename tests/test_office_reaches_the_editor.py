"""OFFICE, FROM THE FOUR WAYS IT ACTUALLY BROKE TODAY.

Reported in one sitting: "Documents does not work at all", "Download -> pdf does nothing but slight
glitch the screen", "the taskbar icon for office stuff is empty, nothing", "'Save probe-doc.pdf
to…' very ugly", "convert to pdf button is stuck", and "why do we not support the features natively
in the app? office has this all built in".

Every one of them was real, and NONE of them was the server: driven from the live desktop over CDP,
`POST /client/office/session` answered 200, the editor URL answered 200, WOPI CheckFileInfo answered
200 with correct JSON, and `/contents` answered 200. The existing office tests all pass and cover
that half well. What they never touched is the half between the server and the person.

  1. POST-MESSAGE ORIGIN. `PostMessageOrigin` was the WOPI request's origin — this instance. In the
     packaged apps the embedding page is `app://posterchan` (desktop) or `capacitor://localhost`
     (Android), measured on the running desktop as `location.origin === 'app://posterchan'`. The
     browser drops every host message addressed elsewhere, so `Action_Save_Resp` never arrived and
     `askEditorToSave` spent its full 8s timeout on every Save, Save As and PDF export. That is also
     the honest answer to "why not use Collabora's own buttons": its Save writes through WOPI into a
     TEMPORARY session directory that is deleted on close, and the user's files live in encrypted
     Blossom under a key this server does not have — so a client step is structural. It should not
     also be a manual one, and it cannot stop being manual while the editor cannot talk to the app.

  2. THE MISSING ICON. The document window asked for `i-doc`, which the sprite has never defined, so
     `<use href="#i-doc">` drew nothing — an empty taskbar button.

  3. THE UNSTYLED SHEET. The save-destination chooser used `.openwith-list` / `.openwith-row`,
     class names the stylesheet defines NOWHERE, beside `_openWithSheet`'s styled `.openwith` /
     `.ow-opt` that every other chooser in the app uses. "very ugly" was literally unstyled markup.

  4. THE PROMISE THAT NEVER SETTLED. `Save as PDF…` disables its button, awaits that sheet and
     re-enables afterwards. Dismissing the sheet by the backdrop or Escape called `closeModal()` and
     resolved nothing, so the await never returned: "convert to pdf button is stuck".
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
from starlette.requests import Request

from app.auth import NATIVE_APP_ORIGINS
from app.routers import office

ROOT = Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")
CSS = (ROOT / "static/css/client.css").read_text(encoding="utf-8")
SPRITE = (ROOT / "static/js/client/sprite.js").read_text(encoding="utf-8")


class Upload:
    def __init__(self, name, data): self.filename, self.data = name, data
    async def read(self, _limit): return self.data


def _request(monkeypatch, tmp_path):
    monkeypatch.setattr(office, "_ROOT", Path(tmp_path))
    monkeypatch.setenv("POSTERCHANAI_OFFICE", "1")

    async def action(_ext, _mode):
        return "http://office:9980/browser/hash/cool.html?"

    monkeypatch.setattr(office, "_action_url", action)
    return Request({"type": "http", "method": "POST", "path": "/", "scheme": "https",
                    "headers": [(b"host", b"local"), (b"x-forwarded-proto", b"https"),
                                (b"x-forwarded-host", b"poster.place")],
                    "server": ("local", 443)})


def _session(request, origin="", name="report.docx"):
    return asyncio.run(office.create_session(request, Upload(name, b"original"), "edit", origin))


# ---------------------------------------------------------------- 1. the post-message origin

@pytest.mark.parametrize("origin", sorted(NATIVE_APP_ORIGINS))
def test_a_packaged_app_is_told_to_post_back_to_itself(origin, tmp_path, monkeypatch):
    """THE BUG, per shell. Measured on the real desktop: location.origin is app://posterchan, and
    an editor addressing https://poster.place is an editor nothing hears."""
    request = _request(monkeypatch, tmp_path)
    session = _session(request, origin)
    info = office.check_file_info(session["id"], request, session["token"])
    assert info["PostMessageOrigin"] == origin


def test_a_browser_is_unchanged(tmp_path, monkeypatch):
    """The web client posts its own origin too, and it is this instance — so nothing about the
    ordinary path may move."""
    request = _request(monkeypatch, tmp_path)
    for claimed in ("", "https://poster.place"):
        session = _session(request, claimed)
        info = office.check_file_info(session["id"], request, session["token"])
        assert info["PostMessageOrigin"] == "https://poster.place"


def test_an_origin_we_do_not_know_is_refused_not_echoed(tmp_path, monkeypatch):
    """This value decides who may receive the contents of a document somebody is editing. It is a
    claim from the client, so it is checked against the list the CORS policy already uses; an
    unknown one falls back rather than failing the session."""
    request = _request(monkeypatch, tmp_path)
    for hostile in ("https://evil.example", "app://posterchan.evil", "javascript:alert(1)", "*"):
        session = _session(request, hostile)
        info = office.check_file_info(session["id"], request, session["token"])
        assert info["PostMessageOrigin"] == "https://poster.place", hostile


def test_a_session_made_before_this_field_existed_still_answers(tmp_path, monkeypatch):
    """Sessions are files on disk and outlive a deploy. One without `origin` must not KeyError its
    way into a 500 on the next CheckFileInfo."""
    request = _request(monkeypatch, tmp_path)
    session = _session(request, "app://posterchan")
    meta = Path(tmp_path) / session["id"] / "meta.json"
    import json
    data = json.loads(meta.read_text()); data.pop("origin", None)
    meta.write_text(json.dumps(data))
    info = office.check_file_info(session["id"], request, session["token"])
    assert info["PostMessageOrigin"] == "https://poster.place"


def test_the_client_actually_sends_it():
    """A server that accepts the field and a client that never sends it is the same bug with more
    code. `location.origin` is the only correct source — the embedding page is what receives."""
    assert "fd.append('origin', location.origin)" in APP_JS


# ---------------------------------------------------------------- 2. the icon that never existed

def _sprite_ids():
    return set(re.findall(r'id="(i-[a-z0-9-]+)"', SPRITE))


def test_the_document_window_asks_for_an_icon_that_exists():
    """`i-doc` was never in the sprite, so the taskbar button drew nothing at all."""
    call = re.search(r"PCOS\.openDoc\('office:'\+session\.id,\s*file\.name,\s*'([^']+)'", APP_JS)
    assert call, "the office window's openDoc call has moved"
    assert call.group(1) in _sprite_ids(), (
        f"the office window asks for #{call.group(1)}, which the sprite does not define — "
        f"that is an empty taskbar icon")


# ---------------------------------------------------------------- 3. the unstyled chooser

def _save_copy_body():
    start = APP_JS.index("function _officeSaveCopy(")
    return APP_JS[start:APP_JS.index("\n  function ", start + 10)]


def test_the_save_destination_sheet_uses_the_shared_chooser():
    """One pattern for "which one of these?", not two — and the bespoke one had no CSS at all."""
    body = _save_copy_body()
    assert 'class="ow-opt"' in body and 'class="openwith"' in body
    # The emitted ATTRIBUTE, not the word: the comment above the code names the old classes, and
    # matching prose is how a test starts failing for the wrong reason.
    assert 'class="openwith-row"' not in body and 'class="openwith-list"' not in body


def test_every_class_that_sheet_draws_is_actually_styled():
    """THE MECHANISM behind "very ugly", stated so it cannot come back in a new name: markup whose
    classes the stylesheet never defines renders as plain browser buttons."""
    body = _save_copy_body()
    for cls in sorted(set(re.findall(r'class="([a-z][a-z0-9 _-]*)"', body))):
        for one in cls.split():
            if one in ("row", "btn"):      # generic utilities, defined far away
                continue
            assert f".{one}" in CSS, f"the save sheet draws .{one}, which the stylesheet never defines"


def test_it_has_a_visible_way_out():
    """A chooser reachable only by the backdrop is a trap on a phone, and this one is reached by
    saving work."""
    assert 'id="os-x"' in _save_copy_body()


# ---------------------------------------------------------------- 4. the promise that hung

def test_dismissing_the_sheet_settles_it_however_it_was_closed():
    """"convert to pdf button is stuck". The button awaits this promise and re-enables afterwards,
    so a dismissal that resolves nothing disables the button for the life of the page. Watching the
    node leave the document covers backdrop, Escape, the ✕ and any closeModal() from elsewhere —
    handling them one at a time is how the backdrop got missed."""
    body = _save_copy_body()
    assert "MutationObserver" in body
    assert "root.isConnected" in body
    assert "let settled = false" in body


def test_the_pdf_button_re_enables_itself():
    """The other half: whatever the promise does, the button must come back."""
    start = APP_JS.index("const pdfBtn = $('#office-pdf',root);")
    handler = APP_JS[start:APP_JS.index("const saveAsBtn", start)]
    assert "b.disabled=false" in handler
    assert "Save as PDF…" in handler


def test_cancelling_is_not_reported_as_a_failure():
    """Backing out of a destination chooser is a choice, not an error — rejecting would put "could
    not save a PDF" on screen for somebody who decided not to."""
    body = _save_copy_body()
    assert "done(null)" in body, "cancelling no longer resolves"
    assert "fail(e)" in body, "a real save failure must still reject"


# ---------------------------------------------------------------- 5. the web gets a view, not a box

def test_the_web_client_opens_the_editor_as_a_view():
    """Reported as "office is loading in a tiny ass window in the webui, classic mode, it should be
    the entire right side like concord".

    A document editor is an application — it is the thing you are doing, for as long as you are
    doing it. `modal()` is one centred box with a backdrop: right for a picker, wrong for a word
    processor. Concord is the shape to copy, because it owns `#feed`, which IS the right side. The
    desktop keeps its real window; the modal stays only as the last resort for a client with no feed
    to take."""
    session = APP_JS[APP_JS.index("async function _officeSession"):]
    session = session[:session.index("function renderOfficeHome")]
    feed_at = session.index("const feed = document.getElementById('feed');")
    modal_at = session.index("modal(bodyHTML, root=>{")
    assert feed_at < modal_at, "the modal is still reached before the view"
    view = session[feed_at:modal_at]
    assert "office-view" in view and "feed.appendChild(host)" in view
    assert "switchView('office')" in view, "the editor mounts into a feed that may be showing another view"
    assert "renderOfficeHome()" in view, (
        "closing the editor leaves the office view blank, which reads as a crash")


@pytest.mark.skipif(not __import__("shutil").which("google-chrome-stable")
                    and not __import__("shutil").which("chromium"), reason="Chrome unavailable")
def test_the_editor_view_gives_the_document_real_height():
    """MEASURED, because this is the failure mode a class name cannot show: `#feed` is a scroll
    container, so an editor that only takes its content's height collapses and the iframe gets NO
    height at all — a blank white panel with no error anywhere."""
    import json as _json, re as _re, shutil as _shutil, subprocess as _sp, tempfile as _tf
    chrome = _shutil.which("google-chrome-stable") or _shutil.which("chromium")
    html = f"""<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
    <style>html,body{{margin:0;height:100%}}{CSS}</style>
    <!-- AUTO height, which is what `#feed` actually is. Given a fixed height the editor fills it
         from `.office-win{{height:100%}}` alone and this test proves nothing — the collapse only
         happens against a parent that has no height of its own. -->
    <div id="feed" style="overflow:auto">
      <div class="office-win office-view">
        <div class="office-head"><h3>doc.odt</h3></div>
        <iframe class="office-frame"></iframe>
        <div class="row office-actions"><button class="btn">Save</button></div>
      </div></div><pre id=o></pre><script>
    const g=s=>{{const r=document.querySelector(s).getBoundingClientRect();
      return {{w:Math.round(r.width),h:Math.round(r.height)}};}};
    o.textContent=JSON.stringify({{view:g('.office-view'),frame:g('.office-frame')}});</script>"""
    with _tf.TemporaryDirectory() as td:
        page = Path(td) / "o.html"
        page.write_text(html, encoding="utf-8")
        done = _sp.run([chrome, "--headless=new", "--no-sandbox", "--disable-gpu",
                        "--window-size=1400,900", "--force-device-scale-factor=1",
                        "--dump-dom", page.as_uri()], text=True, capture_output=True, timeout=90)
    m = _re.search(r'<pre id="o">(.*?)</pre>', done.stdout, _re.S)
    assert m, done.stdout[-800:]
    got = _json.loads(m.group(1).replace("&quot;", '"'))
    assert got["view"]["h"] >= 400, f"the editor view collapsed: {got}"
    assert got["frame"]["h"] >= 300, f"the document itself has almost no height: {got}"
    assert got["frame"]["w"] >= got["view"]["w"] - 4, f"the document does not fill the width: {got}"
