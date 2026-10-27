"""Music's own buttons, pressed in the SHIPPED bundle on a stub five-track library whose bytes are a real WAV:

  * ▶ on a track plays it; ⏭ moves to the next track and ⏮ back;
  * Equalizer opens and closes its panel;
  * ＋ New makes a playlist; ✎ renames it; 🗑 (confirmed) deletes it -- the songs stay;
  * a track's ＋ offers the playlists, "＋ New playlist" makes one holding that track, Close closes.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_cord_direct_invites_full_app import click
from tests.client.test_desktop_music_widget_plays_full_app import LIB as _WIDGET_LIB

# The widget test's stand-in for the music-share module only has to feed the player bytes; the Music SCREEN also
# asks that module about its views and chips, so the stand-in answers every other call with nothing (the real module
# is either absent or whole).
LIB = _WIDGET_LIB.replace("return {plain, meta:()=>null};",
                          "return new Proxy({plain, meta:()=>null},{get:(t,k)=>k in t?t[k]:(typeof k==='string'?()=>'':undefined)});")
assert LIB != _WIDGET_LIB


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


PLAIN = ("localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),"
         "osMode:false}));")
# The media server holds the five tracks' bytes, so none is listed as missing.
HELD = r"""(()=>{const f=window.fetch;window.fetch=(u,o)=>{if(/\/list\/[0-9a-f]{64}/.test(String(u)))
  return Promise.resolve(new Response(JSON.stringify([1,2,3,4,5].map(i=>({sha256:String(i).repeat(64),size:1000}))),{status:200,headers:{'Content-Type':'application/json'}}));
  return f(u,o);};})();"""
NOW = "((__PC.music().now()||{}).title||'')"
NAMES = "PCPlaylists.all().map(p=>p.name)"
TITLE = "((document.querySelector('#ma-plbar .ma-pltitle')||{}).textContent||'')"


async def menu(b, label):
    """The open playlist's ⋯, then one of its items -- pressed, not called."""
    await click(b, "#ma-plmore")
    await b.until("!!document.querySelector('.menu-pop')")
    await b.js("[...document.querySelectorAll('.menu-pop button')].find(x=>x.textContent.trim()===%r).click(); 1" % label)


async def prompt(b, text):
    await b.until("!!document.querySelector('.uiprompt-in')")
    await b.js(f"(()=>{{const i=document.querySelector('.uiprompt-in');i.value={text!r};document.querySelector('.uiconfirm-btns [data-uc=\"1\"]').click();}})(); 1")


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_play_next_previous_equalizer_and_playlists():
    async def check(b):
        await desktop.login(b)
        await b.js("window.__publishOK=true; __seedLib(); __PC.filesIdx().saveLocal(); __PC.switchView('music'); 1")
        await b.until("document.querySelectorAll('.track .track-play').length>=5")
        first = await b.js("__PC.filesIdx().meta(document.querySelector('.track .track-play').dataset.sha).name")
        await click(b, ".track .track-play")
        await b.until(f"{NOW}==={first!r}")
        await b.until("!!document.querySelector('#ma-next')")
        await click(b, "#ma-next")
        await b.until(f"{NOW}!=='' && {NOW}!=={first!r}")
        await click(b, "#ma-prev")
        await b.until(f"{NOW}==={first!r}")

        await click(b, "#ma-eq-toggle")
        await b.until("document.querySelector('#ma-eq-toggle').getAttribute('aria-expanded')==='true'")
        await click(b, "#ma-eq-toggle")
        await b.until("document.querySelector('#ma-eq-toggle').getAttribute('aria-expanded')==='false'")

        # Playlists: new, rename, delete -- from the Playlists tab and the open playlist's one ⋯ (2026-10-27).
        await click(b, "#ma-tab-playlists")
        await b.until("!!document.querySelector('#ma-plnew')")
        await b.js("document.querySelector('#ma-plnew').click(); 1")
        await prompt(b, "Road trip")
        await b.until(f"{TITLE}==='Road trip'")
        await menu(b, "Rename this playlist")
        await prompt(b, "Night drive")
        await b.until(f"{TITLE}==='Night drive' && {NAMES}.includes('Night drive') && !{NAMES}.includes('Road trip')")
        await menu(b, "Delete this playlist")
        await b.until("!!document.querySelector('.uiconfirm-btns [data-uc=\"1\"]')")
        await b.js("document.querySelector('.uiconfirm-btns [data-uc=\"1\"]').click(); 1")
        await b.until(f"!{NAMES}.includes('Night drive')")
        await click(b, "#ma-tab-songs")
        await b.until("document.querySelectorAll('.track .track-play').length>=5")
        assert await b.js("document.querySelectorAll('.track .track-play').length") >= 5, "deleting a playlist removed songs"

        # A track's ＋: new playlist holding it; Close.
        sha = await b.js("document.querySelector('.track .track-add').dataset.sha")
        await b.js("document.querySelector('.track .track-add').click(); 1")
        await b.until("!!document.querySelector('#ma-pladdnew')")
        await b.js("document.querySelector('#ma-pladdnew').click(); 1")
        await prompt(b, "Favourites")
        await b.until(f"{NAMES}.includes('Favourites')")
        n = await b.js("PCPlaylists.all().find(p=>p.name==='Favourites').tracks.length")
        assert n == 1, ("the new playlist should hold the one track", n)
        await b.js(f"document.querySelector('.track .track-add[data-sha=\"{sha}\"]').click(); 1")
        await b.until("!!document.querySelector('#ma-pladdclose')")
        await b.js("document.querySelector('#ma-pladdclose').click(); 1")
        await b.until("!document.querySelector('#ma-pladdclose')")
        assert not await b.js("__errors"), await b.js("__errors")
    asyncio.run(desktop.with_browser("online", "", check, PLAIN + LIB + HELD))
