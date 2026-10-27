"""Music: Playlists are a TAB, an open playlist has ONE menu, Search has its own line, Shuffle means the whole library.

Asked 2026-10-27, in four messages: "Can't Shared by me, Shared with me, Rename this playlist, Delete this playlist,
Share, be combined into a small button that has all that in a menu? be much cleaner"; "Search looks out of place
being on the same line as Songs, Albums, Artists"; "Playlists should be in a tab like Songs, Artists, Albums, would
save a bunch of space and make UI cleaner"; "Shuffle: make sure it shuffles your entire library instead of only a
short list of songs". The SHIPPED client over the eight-track library fixture, every press a real mouse event at the
control's centre (music_art_fixture.press), at a phone and a desktop size.
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


LAYOUT = r"""(()=>{const R=e=>e&&e.getBoundingClientRect();
  const tabs=[...document.querySelectorAll('#ma-browse .ma-tab')].filter(t=>t.checkVisibility());
  const q=document.getElementById('ma-q'), qr=R(q), tr=R(document.querySelector('#ma-browse .ma-tabs'));
  return {tabs:tabs.map(t=>t.textContent.trim()), qInBar:!!(q&&q.closest('#ma-browse')),
    qBelow:!!(qr&&tr&&qr.top>=tr.bottom-1), qWide:qr?qr.width:0, bar:(document.getElementById('ma-plbar')||{}).innerHTML||'',
    sideways:document.documentElement.scrollWidth>innerWidth+1}})()"""
MENU = "[...document.querySelectorAll('.menu-pop button')].map(b=>b.textContent.trim())"


@CHROME
@pytest.mark.parametrize("w,h", [(390, 844), (1280, 860)])
def test_playlists_tab_one_menu_search_line_and_whole_library_shuffle(w, h):
    got = {}

    async def check(b):
        await V._screen(b, w, h)
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}; localStorage.setItem('pc_music_tab','songs'); __seedLib(); 1")
        await b.js("__PC.switchView('music'); 1")
        await V._until(b, "document.querySelectorAll('#ma-lib .track').length>=8 && !!window.PCPlaylists", 20, "the library")
        got["layout"] = await b.js(LAYOUT)
        await b.js("(async()=>{await PCPlaylists.load();const ids=[...document.querySelectorAll('#ma-lib .track[data-sha]')].slice(0,3).map(t=>t.dataset.sha);"
                   "await PCPlaylists.create('Gym mix', ids);})(); 1")
        await V._until(b, "PCPlaylists.all().length===1", 15, "the playlist")
        await A.press(b, "#ma-tab-playlists")
        await V._until(b, "[...document.querySelectorAll('#ma-lib .ma-plrow')].some(r=>r.textContent.includes('Gym mix'))", 15, "the Playlists tab")
        got["rows"] = await b.js("[...document.querySelectorAll('#ma-lib .ma-plrow')].map(r=>r.textContent.replace(/\\s+/g,' ').trim())")
        got["rowsH"] = await b.js("[...document.querySelectorAll('#ma-lib .ma-plrow')].map(r=>r.getBoundingClientRect().height)")
        pid = await b.js("PCPlaylists.all()[0].id")
        await A.press(b, "#ma-lib .ma-plrow[data-pl='%s']" % pid)
        await V._until(b, "document.querySelectorAll('#ma-lib > .track').length===3 && !!document.querySelector('#ma-plbar .ma-pltitle')", 15, "the open playlist")
        got["title"] = await b.js("document.querySelector('#ma-plbar .ma-pltitle').textContent")
        got["headBtns"] = await b.js("[...document.querySelectorAll('#ma-plbar button')].filter(x=>x.checkVisibility()).map(x=>x.id)")
        await A.press(b, "#ma-plmore")
        await b.until("!!document.querySelector('.menu-pop')")
        got["menu"] = await b.js(MENU)
        await b.js("document.querySelectorAll('.menu-pop,.pop-backdrop').forEach(p=>p.remove()); 1")
        await A.press(b, "#ma-plback")
        await V._until(b, "!!document.querySelector('#ma-lib .ma-plrow') && !document.querySelector('#ma-plbar .ma-pltitle')", 15, "back to Playlists")
        got["back_on"] = await b.js("document.querySelector('#ma-tab-playlists').getAttribute('aria-selected')")
        # Shuffle after a ONE-song queue (an album, a search): the switch must shuffle the whole library.
        await A.press(b, "#ma-tab-songs")
        await V._until(b, "document.querySelectorAll('#ma-lib .track').length>=8", 15, "Songs")
        # Something is playing (on a phone the now-playing card, and its Shuffle, only shows then), from a one-song queue.
        await b.js("(()=>{const sha=document.querySelector('#ma-lib .track[data-sha]').dataset.sha;"
                   "__PC.MusicPlayer.queue=[sha]; __PC.MusicPlayer.play(sha, {force:true});})(); 1")
        await V._until(b, "!document.querySelector('.music-app.ma-idle') && !!__PC.MusicPlayer.cur", 20, "a song playing")
        await b.js("__PC.MusicPlayer.shuffle=false; __PC.MusicPlayer.queue=[__PC.MusicPlayer.cur]; 1")
        if w <= 820:   # a phone's mini-bar has no Shuffle: it is on Now playing (tap the cover)
            await A.press(b, "#ma-art")
            await V._until(b, "!!document.querySelector('.music-app.ma-full')", 15, "Now playing")
        await b.js("document.getElementById('ma-shuffle').scrollIntoView({block:'center'}); 1")
        await A.press(b, "#ma-shuffle")
        got["queue"] = await b.js("__PC.MusicPlayer.queue.length")
        got["shuffle"] = await b.js("__PC.MusicPlayer.shuffle")
        got["errors"] = await b.js("(window.__errors||[]).slice(-5)")

    asyncio.run(desktop.with_browser("online", "", check, L.PLAIN + L.LIB))
    shape = (w, h)
    lay = got["layout"]
    assert lay["tabs"] == ["Songs", "Albums", "Artists", "Playlists"], (shape, lay["tabs"])
    assert not lay["qInBar"] and lay["qBelow"], (shape, "Search is still on the tab row", lay)
    assert lay["bar"].strip() == "", (shape, "the playlist chip bar is still above the tabs", lay["bar"][:200])
    assert not lay["sideways"], shape
    rows = got["rows"]
    assert rows[0].startswith("Gym mix") and any("Shared with me" in r for r in rows) and any("Shared by me" in r for r in rows) \
        and any("New playlist" in r for r in rows), (shape, rows)
    if w <= 820:
        assert all(hh >= 43.5 for hh in got["rowsH"]), (shape, "a playlist row is not finger-sized", got["rowsH"])
    assert got["title"] == "Gym mix", (shape, got["title"])
    assert got["headBtns"] == ["ma-plback", "ma-plmore"], (shape, "the open playlist's header is not back + one menu", got["headBtns"])
    assert got["menu"] == ["Rename this playlist", "Delete this playlist", "Share", "📥 Shared with me", "📤 Shared by me",
                           "＋ New playlist"], (shape, got["menu"])
    assert got["back_on"] == "true", (shape, "Back did not return to the Playlists tab")
    assert got["shuffle"] is True and got["queue"] == 8, (shape, "Shuffle did not take the whole library", got["queue"])
    assert not got["errors"], (shape, got["errors"])
