"""The file picker looks and navigates like the file manager, in the SHIPPED client, on a phone and a desktop.

Asked 2026-10-26: "our file picker needs to be consistent across the app, same like what you see in the file manager nav
bar". Opened from the post composer (📎 -> 📁 Files) over the eight-track library fixture, every press real
(music_art_fixture.press):
  * the header carries a search box and the Details / Tiles switch, and it is the SAME setting as Files: Details is the
    default, a file is ONE row (icon, name, size), switching to Tiles here is what Files then shows;
  * the search narrows the list as you type, and the status line says how many items are shown;
  * the tree draws its folders with the file manager's icons (no emoji), and offers Synced Folders to a caller that can
    take a synced file -- pressing it opens the synced-folder chooser -- while a caller that cannot (the Meme Builder)
    is not offered it;
  * nothing sideways, and on a phone the rows and the switch are finger-sized.
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


async def _open_picker(b, w, h):
    await V._screen(b, w, h)
    await desktop.login(b)
    await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}; ClientSettings.set('filesView', null); __seedLib(); 1")
    await b.js("__PC.switchView('home'); __PC.compose({}); 1")
    await b.until("!!document.querySelector('#cmp-attach')")
    await A.press(b, "#cmp-attach")
    await b.until("!!document.querySelector('.menu-pop')")
    await b.js("[...document.querySelectorAll('.menu-pop button')].find(e=>/Files/.test(e.textContent)).id='pick-files'; 1")
    await A.press(b, "#pick-files")
    await b.until("!!document.querySelector('.bp-file-picker #bp-folders [data-folder]')")
    await V._until(b, "document.querySelectorAll('#bp-grid [data-sha]').length>0", 20, "the picker's files")


GEOM = r"""(()=>{const g=document.querySelector('#bp-grid'), gr=g.getBoundingClientRect();
  const rows=[...g.querySelectorAll('[data-sha]')].filter(e=>e.checkVisibility()).map(e=>{const r=e.getBoundingClientRect();return {t:Math.round(r.top),h:r.height,w:r.width}});
  const icons=[...document.querySelectorAll('#bp-folders .folder-chip')].map(c=>({svg:!!c.querySelector('svg'), txt:c.textContent.trim()}));
  return {rows, gridW:gr.width, details:document.querySelector('.bp-file-picker').classList.contains('bp-details'),
    find:!!document.querySelector('.bp-file-picker .bp-find')?.checkVisibility(),
    view:[...document.querySelectorAll('[data-bp-view]')].map(b=>({v:b.dataset.bpView,on:b.getAttribute('aria-pressed')==='true',
      h:b.getBoundingClientRect().height,w:b.getBoundingClientRect().width})),
    status:(document.querySelector('.bp-status')||{}).textContent||'', icons,
    synced:!!document.querySelector('#bp-synced'), sideways:document.documentElement.scrollWidth>innerWidth+1}})()"""


@CHROME
@pytest.mark.parametrize("w,h", [(390, 844), (1280, 860)])
def test_the_picker_is_the_file_managers_list(w, h):
    got = {}

    async def check(b):
        await _open_picker(b, w, h)
        got["start"] = await b.js(GEOM)
        if w <= 820:                       # a phone: the folder tree is behind Locations
            await A.press(b, ".bp-locations")
        await A.press(b, "#bp-folders [data-folder='Music']")
        await V._until(b, "[...document.querySelectorAll('#bp-grid [data-sha]')].every(e=>__PC.filesIdx().folderOf(e.dataset.sha)==='Music')", 15, "Music")
        got["music"] = await b.js(GEOM)
        await b.js("(()=>{const f=document.querySelector('.bp-find');f.focus();f.value='night';f.dispatchEvent(new Event('input',{bubbles:true}));})()")
        await V._until(b, "document.querySelectorAll('#bp-grid [data-sha]').length===1", 10, "the search narrowing the list")
        got["searched"] = await b.js(GEOM)
        await A.press(b, "[data-bp-view='tiles']")
        await V._until(b, "!document.querySelector('.bp-file-picker').classList.contains('bp-details')", 10, "Tiles")
        got["setting"] = await b.js("ClientSettings.get('filesView')")
        got["errors"] = await b.js("(window.__errors||[]).slice(-5)")

    asyncio.run(desktop.with_browser("online", "", check, L.PLAIN + L.LIB))
    shape, phone = (w, h), w <= 820
    s = got["start"]
    assert s["details"] and s["find"], (shape, "no search box, or not Details by default", s)
    assert [v["v"] for v in s["view"] if v["on"]] == ["details"], (shape, "the view switch does not say Details", s["view"])
    assert s["rows"] and len({r["t"] for r in s["rows"]}) == len(s["rows"]), (shape, "files sit side by side, not one row each", s["rows"][:4])
    assert all(r["h"] <= (60 if phone else 46) and r["w"] >= s["gridW"] * 0.85 for r in s["rows"]), (shape, "a file is not a row", s["rows"][:3])
    assert all(i["svg"] for i in s["icons"]) and not any(i["txt"][:1] in "📁🗂" for i in s["icons"]), (shape, "emoji folder icons", s["icons"])
    assert s["synced"], (shape, "the post composer's picker does not offer Synced Folders")
    assert s["status"].endswith("items") or "item" in s["status"], (shape, "no status line", s["status"])
    assert got["music"]["status"].startswith("8 items"), (shape, got["music"]["status"])
    assert got["searched"]["status"].startswith("1 item"), (shape, got["searched"]["status"])
    assert got["setting"] == "tiles", (shape, "switching here did not switch the file manager's view", got["setting"])
    if phone:
        assert all(v["h"] >= 43.5 for v in s["view"]), (shape, "the view switch is not finger-sized", s["view"])
    assert not s["sideways"], shape
    assert not got["errors"], (shape, got["errors"])


@CHROME
def test_synced_folders_opens_the_synced_chooser_and_is_offered_only_where_it_can_be_taken():
    got = {}

    async def check(b):
        await _open_picker(b, 1280, 860)
        await A.press(b, "#bp-synced")
        await V._until(b, "!!document.querySelector('.sp-picker')", 15, "the synced-folder chooser")
        got["chooser"] = await b.js("document.querySelector('.sp-picker .sp-title').textContent")
        await b.js("document.querySelector('.sp-picker .sp-close').click(); 1")
        got["back"] = await b.js("!!document.querySelector('.bp-file-picker')")
        # the Meme Builder publishes what it is handed and takes no File: no Synced Folders there
        await b.js("document.querySelector('.bp-close').click(); 1")
        await b.js("__PC.blossomPicker(null, ()=>{}, {allowEncrypted:true}); 1")
        await b.until("!!document.querySelector('.bp-file-picker #bp-folders')")
        got["onpick_synced"] = await b.js("!!document.querySelector('#bp-synced')")

    asyncio.run(desktop.with_browser("online", "", check, L.PLAIN + L.LIB))
    assert "Synced" in got["chooser"], got
    assert got["back"], "cancelling the synced chooser closed the picker too"
    assert got["onpick_synced"] is False, "a caller that cannot take a synced file is offered Synced Folders"
