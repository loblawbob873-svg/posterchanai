"""The post composer's 📎 -> 📁 Files reaches the MUSIC folder, and a track picked there goes into the post as a copy anybody
can play -- in the SHIPPED client.

Reported 2026-10-26: "no way to attach Music folder files in our file picker for social". Music is stored ENCRYPTED, so
the picker left the folder out of every caller that publishes a URL (a link to ciphertext is a link nobody can play).
The composers now opt in with `publishDecrypted`: the picker decrypts the track on this device, uploads an ordinary
copy, and puts THAT link in the post (upload.js _linkFor), saying so. Also asked the same night: "Remove (.xdc) from
the attach dialog on social, Miniapp is enough".

Driven with real presses (music_art_fixture.press) over the eight-track library fixture (music_library_fixture.py),
at a phone and a desktop size:
  * 📎 lists "🎮 Mini app" -- not "Mini app (.xdc)";
  * 📁 Files lists the Music folder, and its tracks;
  * picking a track puts a NEW upload's link in the post -- the decrypted bytes, never the ciphertext address -- and
    nothing is published until the person posts.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client import music_art_fixture as A
from tests.client import music_library_fixture as L
from tests.client import test_music_library_views_full_app as V

CHROME = pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@CHROME
@pytest.mark.parametrize("w,h", [(390, 844), (1280, 860)])
def test_a_music_track_attaches_to_a_post_as_a_playable_copy(w, h):
    got = {}

    async def check(b):
        await V._screen(b, w, h)
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}; window.__publishOK=true; __seedLib(); 1")
        await b.js("window.__toasts=[]; new MutationObserver(ms=>ms.forEach(m=>m.addedNodes.forEach(n=>__toasts.push(n.textContent))))"
                   ".observe(document.getElementById('toast-root'),{childList:true}); 1")
        await b.js("__PC.switchView('home'); __PC.compose({}); 1")
        await b.until("!!document.querySelector('#cmp-attach')")
        await A.press(b, "#cmp-attach")
        await b.until("!!document.querySelector('.menu-pop')")
        got["attach"] = await b.js("[...document.querySelectorAll('.menu-pop button, .menu-pop label')].map(e=>e.textContent.trim())")
        await b.js("[...document.querySelectorAll('.menu-pop button')].find(e=>/Files/.test(e.textContent)).id='pick-files'; 1")
        await A.press(b, "#pick-files")
        await b.until("!!document.querySelector('.bp-file-picker #bp-folders')")
        await V._until(b, "[...document.querySelectorAll('#bp-folders [data-folder]')].some(e=>e.dataset.folder==='Music')", 20, "the Music folder in the picker")
        got["folders"] = await b.js("[...document.querySelectorAll('#bp-folders [data-folder]')].map(e=>e.dataset.folder)")
        if await b.js("!!document.querySelector('.bp-locations') && getComputedStyle(document.querySelector('.bp-locations')).display!=='none'"):
            await A.press(b, ".bp-locations")
        await A.press(b, "#bp-folders [data-folder='Music']")
        await V._until(b, "(()=>{const c=[...document.querySelectorAll('#bp-grid [data-sha]')];"
                          "return c.length>0 && c.every(e=>__PC.filesIdx().folderOf(e.dataset.sha)==='Music')})()", 20,
                       "the grid showing the Music folder's tracks")
        sha = await b.js("document.querySelector('#bp-grid [data-sha]').dataset.sha")
        cipher = await b.js("document.querySelector('#bp-grid [data-sha]').dataset.url")
        got["sha"], got["cipher"] = sha, cipher
        got["before"] = await b.js("document.querySelector('#cmp').value")
        await A.press(b, f"#bp-grid [data-sha='{sha}']")
        if await b.js("!!document.querySelector('.bp-multi-go') && !document.querySelector('.bp-multi-bar').hidden"):
            await A.press(b, ".bp-multi-go")
        await V._until(b, "__toasts.includes('attached') && /https?:\\/\\//.test(document.querySelector('#cmp')?.value||'')",
                       30, "the playable copy's link in the post")
        got["text"] = await b.js("(document.querySelector('#cmp')||{}).value||''")
        got["toasts"] = await b.js("__toasts.slice()")
        got["published"] = await b.js("(window.__published||[]).filter(e=>e.kind===1).length")
        got["errors"] = await b.js("(window.__errors||[]).slice(-5)")

    asyncio.run(desktop.with_browser("online", "", check, L.PLAIN + L.LIB))
    shape = (w, h)
    assert "🎮 Mini app" in got["attach"] and not any("(.xdc)" in t for t in got["attach"]), (shape, got["attach"])
    assert "Music" in got["folders"], (shape, "the Music folder is not in the post picker", got["folders"])
    t = got["toasts"]
    # decrypted on this device, a copy uploaded (the harness answers every upload with its own URL), THEN attached
    assert any(x.startswith("decrypting ") for x in t) and any("playable copy" in x for x in t) and "attached" in t, \
        (shape, "the track was not decrypted and re-published before it was attached", t)
    assert t.index("attached") > max(i for i, x in enumerate(t) if "playable copy" in x), (shape, "attached before the copy", t)
    assert got["cipher"] not in got["text"] and got["sha"] not in got["text"], \
        (shape, "the post holds the ENCRYPTED track's address, which nobody can play", got["text"])
    assert got["before"] == "" and got["text"].startswith("http"), (shape, "the copy's link is not what the post holds", got["text"])
    assert got["published"] == 0, (shape, "something was posted without the person posting it")
    assert not got["errors"], (shape, got["errors"])
