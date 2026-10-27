"""The Music app lists the library the way a music client does -- Songs, Albums, Artists -- in the SHIPPED bundle.

Asked for 2026-10-26: "modern music clients can list by album, song, artist", album art pulled online on request,
"good test cases", and "make sure it looks good on mobile". Every press here is a REAL mouse event at the centre of
the control after proving nothing covers it (music_art_fixture.press), over the library in music_library_fixture.py.

  * the tag scan: reads every track once, says "Reading tags: k of 8" counting up, writes `tg` into each record;
  * the views at 390 and 1280: Albums grid (order, covers from the files' own pictures), an album page in disc/track
    order with Play / Shuffle making THAT album the queue, Artists and an artist page, Back to where you came from
    (and Android's Back), search per tab, the Songs sort, the tab remembered after a reload;
  * phone geometry at 360x780, 390x844, 844x390 (held sideways) and 320x640: 2 columns of square covers (3 sideways),
    one-line titles, every control >= 44px, the tab bar on screen and clear of the bottom nav, nothing sideways;
  * a 1000-song library builds one page of covers, not 100, and fills covers only as they scroll into view;
  * the scan resumes after a reload without re-reading, survives a track that will not read, pauses while the page
    is hidden, and never holds up a song somebody presses play on;
  * the online covers, ON by default (2026-10-26: "should be default"): OFF means not one request and not one upload;
    ON asks once per album without a picture, stores the answer in the tracks' records and never asks again after a
    reload; "no cover" is remembered; a 503 is not. (Covers reaching the account's OTHER devices:
    test_music_cover_sync_full_app.py.)
"""
import asyncio
import json
import os
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client import music_art_fixture as A
from tests.client import music_library_fixture as L

CHROME = pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
SH = L.SHAS
NEON = [SH["c"], SH["a"], SH["d"], SH["b"]]           # disc 1 tracks 1,2,3 -- then disc 2 track 1
SHOTS = os.environ.get("PC_MUSIC_SHOTS")


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


def _run(check, init):
    asyncio.run(desktop.with_browser("online", "", check, init))


async def _screen(b, w, h):
    await b.call("Emulation.setDeviceMetricsOverride", {"width": w, "height": h, "deviceScaleFactor": 2 if w <= 900 else 1,
                                                        "mobile": w <= 900})
    if w <= 900:
        await b.call("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5})


INDEXED = ("(()=>{const L=window.PCMusicLibrary, f=__PC.filesIdx()._norm().files;"
           "return !!L && !L.st.running && Object.values(f).filter(m=>m.folder==='Music').every(m=>m.tg&&m.tg.v>=1);})()")


async def _music(b, indexed_first=True, login=True, covers=None):
    """`covers`: None leaves the online lookup at its default (ON); True/False sets it before Music opens."""
    if login:
        await desktop.login(b)
    if covers is not None:
        await b.js(f"ClientSettings.set('musicCoverLookup', {json.dumps(covers)}); 1")
    await b.js("window.__publishOK=true; __seedLib(); __PC.switchView('music'); 1")
    await b.until("!!document.querySelector('#ma-browse')")
    if indexed_first:
        await _until(b, INDEXED, 40, "the tag scan to finish")


async def _until(b, expr, timeout=20.0, what=""):
    end = asyncio.get_running_loop().time() + timeout
    last = None
    while asyncio.get_running_loop().time() < end:
        try:
            last = await b.js(expr)
            if last:
                return last
        except AssertionError:
            pass
        await asyncio.sleep(.15)
    raise AssertionError({"waiting for": what or expr, "last": last,
                          "state": await b.js("({errors:__errors.slice(-5),scan:window.PCMusicLibrary&&{...PCMusicLibrary.st,failed:[...PCMusicLibrary.st.failed]},"
                                              "lib:(document.getElementById('ma-lib')||{}).className})")})


# The tab buttons, spelled out: these ARE the controls pressed (scripts/control_coverage.py reads literal ids).
TABS = {"songs": "#ma-tab-songs", "albums": "#ma-tab-albums", "artists": "#ma-tab-artists"}


async def _tab(b, name):
    await A.press(b, TABS[name])
    await _until(b, f"document.getElementById('ma-tab-{name}').getAttribute('aria-selected')==='true'", 15, f"{name} tab")


async def _shot(b, name):
    if not SHOTS:
        return
    import base64
    Path(SHOTS).mkdir(parents=True, exist_ok=True)
    data = (await b.call("Page.captureScreenshot", {"format": "png"}))["data"]
    Path(SHOTS, name + ".png").write_bytes(base64.b64decode(data))


CARDS = "[...document.querySelectorAll('#ma-albums .ma-card')].map(c=>({t:c.querySelector('.ma-card-t').textContent,s:c.querySelector('.ma-card-s').textContent,img:!!c.querySelector('img.pc-art-img')}))"
ROWS = "[...document.querySelectorAll('#ma-lib .track')].map(r=>r.dataset.sha)"
PLAYER = "({cur:__PC.MusicPlayer.cur||'', queue:__PC.MusicPlayer.queue.slice(), shuffle:!!__PC.MusicPlayer.shuffle, playing:!!(__PC.music().now()||{}).playing})"


# ── the scan ────────────────────────────────────────────────────────────────────────────────────────────────────
@CHROME
def test_the_scan_reads_each_track_once_and_says_how_far_it_has_got():
    got = {"seen": []}

    async def check(b):
        await desktop.login(b)
        await b.js("for(const s of Object.keys(window.__plainDelay||{})) delete __plainDelay[s]; 1")
        await b.js("window.__publishOK=true; __seedLib({indexed:false}); "
                   + ";".join(f"__plainDelay['{s}']=250" for s in SH.values()) + "; __PC.switchView('music'); 1")
        end = asyncio.get_running_loop().time() + 40
        while asyncio.get_running_loop().time() < end:
            t = await b.js("(()=>{const e=document.getElementById('ma-scan');return e&&!e.classList.contains('hidden')?document.getElementById('ma-scan-t').textContent:''})()")
            if t and (not got["seen"] or got["seen"][-1] != t):
                got["seen"].append(t)
                if len(got["seen"]) == 3:
                    await _shot(b, "phone-scan-progress")
            if await b.js(INDEXED):
                break
            await asyncio.sleep(.05)
        got["hiddenAfter"] = await b.js("document.getElementById('ma-scan').classList.contains('hidden')")
        got["reads"] = await b.js("__plainCalls.slice()")
        got["tg"] = await b.js(f"__PC.filesIdx().meta('{SH['a']}').tg")
        got["titles"] = await b.js("[...document.querySelectorAll('#ma-lib .track .track-name')].map(e=>e.textContent)")
        assert not await b.js("__errors.slice()"), await b.js("__errors.slice()")

    _run(check, L.PLAIN + L.LIB)
    import re
    nums = [int(re.search(r"Reading tags: (\d+) of 8", t).group(1)) for t in got["seen"] if "Reading tags" in t]
    assert nums and nums == sorted(nums) and len(set(nums)) >= 3 and max(nums) >= 7, ("progress did not count up", got["seen"])
    assert got["hiddenAfter"], "the scan's status line stayed up after it finished"
    assert sorted(got["reads"]) == sorted(SH.values()), ("every track is read exactly once", got["reads"])
    # `ca` (the album's synced cover) may already be there too -- the share pass follows the scan; that is
    # test_music_cover_sync_full_app.py's subject, so it is left out of the scan's own fields here.
    got["tg"].pop("ca", None)
    assert got["tg"] == {"v": 1, "t": "Night Drive", "a": "Kaito Vega", "al": "Neon Skyline", "n": 2, "d": 1, "y": "2021", "p": 1}, got["tg"]
    assert "Night Drive" in got["titles"] and "mystery_track" in got["titles"], got["titles"]


# ── the views, pressed, at a phone and a desktop width ──────────────────────────────────────────────────────────
@CHROME
@pytest.mark.parametrize("w,h", [(390, 844), (1280, 900)])
def test_albums_artists_and_their_pages(w, h):
    got = {}

    async def check(b):
        await _screen(b, w, h)
        await _music(b, covers=False)       # the files' own pictures: the online lookup is a separate test
        await _tab(b, "albums")
        await _until(b, "document.querySelectorAll('#ma-albums .ma-card img.pc-art-img').length>=2", 10, "the embedded covers")
        got["cards"] = await b.js(CARDS)
        await _shot(b, f"{'phone' if w < 600 else 'desktop'}-albums")
        # An album: disc, then track order -- and Play makes THAT album the queue.
        await A.press(b, "#ma-albums .ma-card[data-i='1']")
        await _until(b, "!!document.querySelector('#ma-page[data-kind=album]')", 15, "the album page")
        got["albumRows"] = await b.js(ROWS)
        got["albumHead"] = await b.js("({t:document.querySelector('.ma-page-t').textContent,s:document.querySelector('.ma-page-s').textContent,x:document.querySelector('.ma-page-x').textContent,nums:[...document.querySelectorAll('#ma-pagelist .track-no')].map(e=>e.textContent)})")
        got["androidBack"] = await b.js("PCMusicFull.open()")
        await _shot(b, f"{'phone' if w < 600 else 'desktop'}-album-page")
        await A.press(b, "#ma-alb-play")
        await _until(b, f"(()=>{{const p={PLAYER};return p.cur==='{NEON[0]}'&&p.playing}})()", 15, "the album playing from its first track")
        got["afterPlay"] = await b.js(PLAYER)
        await A.press(b, "#ma-next")
        await _until(b, f"__PC.MusicPlayer.cur==='{NEON[1]}'", 10, "next = the album's second track")
        await A.press(b, "#ma-alb-shuffle")
        await _until(b, "__PC.MusicPlayer.shuffle===true", 15, "shuffle")
        got["afterShuffle"] = await b.js(PLAYER)
        await A.press(b, "#ma-back")
        await _until(b, "!!document.querySelector('#ma-albums') && !document.getElementById('ma-page')", 15, "back to Albums")
        # Artists, an artist's page, one of their albums -- and Back walks out the way it came.
        await _tab(b, "artists")
        got["artists"] = await b.js("[...document.querySelectorAll('#ma-artists .ma-artist-n')].map(e=>e.textContent)")
        await _shot(b, f"{'phone' if w < 600 else 'desktop'}-artists")
        kaito = got["artists"].index("Kaito Vega")
        await A.press(b, f"#ma-artists .ma-arow[data-i='{kaito}']")
        await _until(b, "!!document.querySelector('#ma-page[data-kind=artist]')", 15, "the artist page")
        got["artistAlbums"] = await b.js("[...document.querySelectorAll('#ma-albums .ma-card-t')].map(e=>e.textContent)")
        got["artistRows"] = await b.js(ROWS)
        await _shot(b, f"{'phone' if w < 600 else 'desktop'}-artist-page")
        await A.press(b, "#ma-albums .ma-card[data-i='0']")
        await _until(b, "!!document.querySelector('#ma-page[data-kind=album]')", 15, "the album from the artist page")
        got["backLabel"] = await b.js("document.querySelector('#ma-back span').textContent")
        # Android's Back, as app.js drives it: PCMusicFull.open() then close().
        await b.js("PCMusicFull.open() && PCMusicFull.close(); 1")
        await _until(b, "!!document.querySelector('#ma-page[data-kind=artist]')", 15, "Back to the artist")
        await A.press(b, "#ma-back")
        await _until(b, "!!document.querySelector('#ma-artists') && !document.getElementById('ma-page')", 15, "Back to Artists")
        got["openAfter"] = await b.js("PCMusicFull.open()")
        # One search box per tab.
        await b.js("(()=>{const q=document.getElementById('ma-q');q.value='ghost';q.dispatchEvent(new Event('input'));})(); 1")
        got["artistSearch"] = await b.js("[...document.querySelectorAll('#ma-artists .ma-artist-n')].map(e=>e.textContent)")
        await _tab(b, "albums")
        await b.js("(()=>{const q=document.getElementById('ma-q');q.value='city';q.dispatchEvent(new Event('input'));})(); 1")
        got["albumSearch"] = await b.js("[...document.querySelectorAll('#ma-albums .ma-card-t')].map(e=>e.textContent)")
        await _tab(b, "songs")
        got["songSearch"] = await b.js(ROWS)
        await b.js("(()=>{const q=document.getElementById('ma-q');q.value='';q.dispatchEvent(new Event('input'));})(); 1")
        # The Songs sort.
        await A.press(b, "#ma-sort")
        await b.js("(()=>{const s=document.getElementById('ma-sort');s.value='title';s.dispatchEvent(new Event('change'));})(); 1")
        got["byTitle"] = await b.js("[...document.querySelectorAll('#ma-lib .track .track-name')].map(e=>e.textContent)")
        await _shot(b, f"{'phone' if w < 600 else 'desktop'}-songs-sorted")
        # The tab is this device's: Albums, a reload, and Music opens on Albums.
        await _tab(b, "albums")
        await b.call("Page.reload", {})
        await b.until("!!window.__PC && !!__PC.me && !!__PC.me()")
        await _music(b, indexed_first=False, login=False)
        got["afterReload"] = await b.js("({tab:document.querySelector('.ma-tab.on').dataset.tab,grid:!!document.getElementById('ma-albums'),sort:document.getElementById('ma-sort').value})")
        got["errors"] = await b.js("__errors.slice()")

    _run(check, L.PLAIN + L.LIB)
    cards = got["cards"]
    assert [c["t"] for c in cards] == ["No Cover Club", "Neon Skyline", "City Lights", "Unknown album"], cards
    assert [c["s"] for c in cards[:3]] == ["Ghost Band · 2018", "Kaito Vega · 2021", "Various Artists · 2019"], cards
    assert [c["img"] for c in cards] == [False, True, True, False], ("covers come from the files' own pictures only", cards)
    assert got["albumRows"] == NEON, ("the album is not in disc/track order", got["albumRows"])
    assert got["albumHead"]["t"] == "Neon Skyline" and got["albumHead"]["s"] == "Kaito Vega" and "4 songs" in got["albumHead"]["x"], got["albumHead"]
    assert got["albumHead"]["nums"] == ["1", "2", "3", "1"], got["albumHead"]
    assert got["androidBack"] is True, "Android's Back would leave Music instead of the album page"
    assert got["afterPlay"]["queue"] == NEON and not got["afterPlay"]["shuffle"], got["afterPlay"]
    assert got["afterShuffle"]["shuffle"] and sorted(got["afterShuffle"]["queue"]) == sorted(NEON) and got["afterShuffle"]["cur"] in NEON, got["afterShuffle"]
    assert got["artists"] == ["Ghost Band", "Kaito Vega", "Various Artists", "Unknown artist"], got["artists"]
    assert got["artistAlbums"] == ["Neon Skyline"] and got["artistRows"] == NEON, (got["artistAlbums"], got["artistRows"])
    assert got["backLabel"] == "Artist", got["backLabel"]
    assert got["openAfter"] is False, "Back left something open"
    assert got["artistSearch"] == ["Ghost Band"], got["artistSearch"]
    assert got["albumSearch"] == ["City Lights"], got["albumSearch"]
    assert sorted(got["songSearch"]) == sorted([SH["e"], SH["f"]]), ("a song is found by its album", got["songSearch"])
    assert got["byTitle"][:4] == ["Afterglow", "Bonus Cut", "Ghost Song", "Intro"], got["byTitle"]
    assert got["afterReload"] == {"tab": "albums", "grid": True, "sort": "title"}, got["afterReload"]
    assert not got["errors"], got["errors"]


# ── phone geometry of every new screen ──────────────────────────────────────────────────────────────────────────
GEOM_FN = r"""((MIN) => {
  const vw=innerWidth, vh=innerHeight, out={problems:[]}, P=out.problems;
  const vis=e=>e&&e.checkVisibility&&e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
  const nav=document.querySelector('.mobilenav'), nr=nav&&getComputedStyle(nav).display!=='none'?nav.getBoundingClientRect():null;
  const navTop=nr&&nr.height?nr.top:vh;
  if(document.documentElement.scrollWidth>vw+1) P.push('page scrolls sideways '+document.documentElement.scrollWidth+'>'+vw);
  const feed=document.getElementById('feed'); if(feed && feed.scrollWidth>feed.clientWidth+1) P.push('feed scrolls sideways');
  // Thumb targets: every visible control of the new views, 44px both ways (cards are far bigger).
  document.querySelectorAll('.ma-tab,#ma-sort,#ma-libopts,#ma-back,.ma-page-acts .btn,.ma-arow,.ma-card,.ma-more').forEach(e=>{
    if(!vis(e)) return; const r=e.getBoundingClientRect(); if(!r.width) return;
    if(r.height<MIN-.5||r.width<MIN-.5) P.push('small target '+(e.id||e.className)+' '+Math.round(r.width)+'x'+Math.round(r.height));
    if(r.left<-1||r.right>vw+1) P.push('runs off the side: '+(e.id||e.className));
  });
  // The tab bar is on screen and clear of the bottom nav, at the top of the screen as a person arrives.
  const tabs=document.querySelector('.ma-tabs'); if(tabs){ const r=tabs.getBoundingClientRect();
    if(r.top<0||r.bottom>navTop+0.5) P.push('tab bar not on screen above the nav '+[r.top,r.bottom,navTop].map(Math.round)); }
  // Covers: square, never stretched; titles one line, ellipsized.
  const cards=[...document.querySelectorAll('.ma-card')].filter(vis);
  cards.forEach(c=>{ const a=c.querySelector('.ma-card-art').getBoundingClientRect(); if(Math.abs(a.width-a.height)>1) P.push('cover not square '+Math.round(a.width)+'x'+Math.round(a.height));
    const im=c.querySelector('img'); if(im){ const r=im.getBoundingClientRect(); if(Math.abs(r.width-r.height)>1) P.push('cover image stretched'); }
    const t=c.querySelector('.ma-card-t'), cs=getComputedStyle(t);
    if(cs.whiteSpace!=='nowrap'||cs.textOverflow!=='ellipsis') P.push('card title can wrap');
    if(t.getBoundingClientRect().height>parseFloat(cs.lineHeight||'20')*1.6+2) P.push('card title taller than a line'); });
  // Play / Shuffle on a page split the width between them (a thumb's targets, not two text-sized pills).
  const acts=document.querySelector('.ma-page-acts'); if(acts && vis(acts)){ const aw=acts.getBoundingClientRect().width;
    acts.querySelectorAll('.btn').forEach(x=>{ const r=x.getBoundingClientRect(); if(r.width<aw*0.4) P.push('page action narrower than half the row '+Math.round(r.width)+'/'+Math.round(aw)); }); }
  // Columns in the first row of the grid.
  const g=document.getElementById('ma-albums');
  if(g){ const first=[...g.children].filter(vis); const top=first.length?first[0].getBoundingClientRect().top:0;
    out.cols=first.filter(e=>Math.abs(e.getBoundingClientRect().top-top)<2).length; }
  // The scroll area never draws under the bottom nav: the library's box ends above it (or the page scrolls).
  const lib=document.getElementById('ma-lib'); if(lib && vis(lib)){ const r=lib.getBoundingClientRect(), cs=getComputedStyle(lib);
    if(cs.overflowY!=='visible' && r.bottom>navTop+1) P.push('the library scrolls under the bottom nav '+Math.round(r.bottom)+'>'+Math.round(navTop)); }
  return out;
})"""
# A finger's 44px on a phone; a mouse-driven desktop window (page zoom, fine pointer) WCAG's 24px.
GEOM = "(" + GEOM_FN + ")(44)"
GEOM_DESK = "(" + GEOM_FN + ")(24)"


@CHROME
@pytest.mark.parametrize("w,h", [(360, 780), (390, 844), (844, 390), (320, 640)])
def test_every_new_screen_fits_a_phone(w, h):
    got = {}

    async def check(b):
        await _screen(b, w, h)
        await _music(b)
        tag = f"{w}x{h}"
        for name in ("albums", "artists", "songs"):
            await _tab(b, name)
            await b.js("document.getElementById('feed').scrollTop=0; (document.getElementById('ma-lib')||{}).scrollTop=0; scrollTo(0,0); 1")
            await asyncio.sleep(.25)
            got[name] = await b.js(GEOM)
            await _shot(b, f"phone{tag}-{name}")
        # Scrolled down, as a person browses a real library: the screen is the library, the tab bar pinned at its top.
        await b.js("__seedBig(400); 1")
        await _tab(b, "artists")
        await _tab(b, "albums")
        await b.js("document.getElementById('feed').scrollTop=1500; 1")
        await asyncio.sleep(.3)
        got["scrolled"] = await b.js("""(()=>{const f=document.getElementById('feed').getBoundingClientRect(),t=document.querySelector('.ma-tabs').getBoundingClientRect();
          const nav=document.querySelector('.mobilenav'),nr=nav&&getComputedStyle(nav).display!=='none'?nav.getBoundingClientRect():null,bottom=nr&&nr.height?nr.top:innerHeight;
          const cards=[...document.querySelectorAll('#ma-albums .ma-card-art')].map(e=>e.getBoundingClientRect());
          const visibleArt=cards.filter(r=>r.top>=t.bottom-1&&r.bottom<=bottom+1).length;
          // Nothing of the grid may show between the top of the screen and the pinned bar: a strip of covers peeking
          // above the tabs is the scroll container's padding left uncovered.
          const peek=[];for(let y=Math.ceil(f.top)+1;y<t.top-1;y+=2) for(const x of [innerWidth*.25,innerWidth*.5,innerWidth*.75]){
            const e=document.elementFromPoint(x,y); if(e&&e.closest('#ma-albums')) peek.push(Math.round(y-f.top));}
          return {tabsTop:Math.round(t.top-f.top), tabsOnScreen:t.top>=f.top-1&&t.bottom<=bottom, visibleArt, cards:cards.length,
                  peek:[...new Set(peek)]}})()""")
        await _shot(b, f"phone{tag}-albums-scrolled")
        await _tab(b, "albums")
        await A.press(b, "#ma-albums .ma-card[data-i='1']")
        await _until(b, "!!document.querySelector('#ma-page[data-kind=album]')", 15, "album page")
        await asyncio.sleep(.2)
        got["album"] = await b.js(GEOM)
        await _shot(b, f"phone{tag}-album-page")
        await A.press(b, "#ma-back")
        await _tab(b, "artists")
        await A.press(b, "#ma-artists .ma-arow[data-i='1']")
        await _until(b, "!!document.querySelector('#ma-page[data-kind=artist]')", 15, "artist page")
        await asyncio.sleep(.2)
        got["artist"] = await b.js(GEOM)
        await _shot(b, f"phone{tag}-artist-page")
        await A.press(b, "#ma-back")
        await A.press(b, "#ma-libopts")
        await _until(b, "!!document.getElementById('ma-cover-lookup')", 15, "the library options")
        got["opts"] = await b.js("""(()=>{const l=document.querySelector('.ma-opt').getBoundingClientRect(),c=document.getElementById('ma-opt-close').getBoundingClientRect();
          return {fits:l.left>=-1&&l.right<=innerWidth+1, h:Math.round(l.height), close:[Math.round(c.width),Math.round(c.height)],
                  sideways:document.documentElement.scrollWidth>innerWidth+1}})()""")
        await _shot(b, f"phone{tag}-library-options")

    _run(check, L.PLAIN + L.LIB)
    for k in ("albums", "artists", "songs", "album", "artist"):
        assert got[k]["problems"] == [], (w, h, k, got[k]["problems"])
    assert got["albums"]["cols"] == (3 if w >= 560 else 2), ("album grid columns", w, got["albums"])
    sc = got["scrolled"]
    # Pinned: the tabs sit just under the top of the screen (the bar's own padding), not scrolled away above it.
    assert sc["tabsOnScreen"] and 0 <= sc["tabsTop"] <= 20, ("the tab bar is not pinned at the top while browsing", sc)
    assert sc["peek"] == [], ("the albums show through above the pinned tab bar", sc)
    # The screen under the bar is covers: a full row on a short screen, two rows on a tall one.
    cols = 3 if w >= 560 else 2
    assert sc["visibleArt"] >= (cols if h < 700 else 2 * cols), ("browsing a phone shows a strip of the library, not the screen", sc)
    o = got["opts"]
    assert o["fits"] and not o["sideways"] and o["h"] >= 44 and o["close"][1] >= 43.5, o


# ── a big library is built lazily ───────────────────────────────────────────────────────────────────────────────
@CHROME
def test_a_thousand_song_library_builds_one_page_and_fills_covers_as_they_scroll_in():
    got = {}

    async def check(b):
        await _screen(b, 390, 844)
        await desktop.login(b)
        await b.js("window.__publishOK=true; __seedBig(1000); __PC.switchView('music'); 1")
        await b.until("!!document.querySelector('#ma-browse')")
        t0 = await b.js("performance.now()")
        await _tab(b, "albums")
        got["ms"] = await b.js(f"performance.now()-{t0}")
        got["cards"] = await b.js("document.querySelectorAll('#ma-albums .ma-card').length")
        got["more"] = await b.js("(document.getElementById('ma-more')||{}).textContent||''")
        await asyncio.sleep(.5)
        got["filled"] = await b.js("document.querySelectorAll('#ma-albums [data-filled]').length")
        await A.press(b, "#ma-more")
        got["cards2"] = await b.js("document.querySelectorAll('#ma-albums .ma-card').length")
        await _tab(b, "songs")
        got["rows"] = await b.js("document.querySelectorAll('#ma-lib .track').length")
        await _tab(b, "artists")
        got["artistRows"] = await b.js("document.querySelectorAll('#ma-artists .ma-arow').length")
        got["nodes"] = await b.js("document.getElementById('ma-lib').querySelectorAll('*').length")

    _run(check, L.PLAIN + L.LIB)
    assert got["cards"] == 60 and "40 left" in got["more"], got
    assert got["filled"] < 30, ("covers were asked for far below the fold", got["filled"])
    assert got["cards2"] == 100, got
    assert got["rows"] <= 120, got
    assert got["artistRows"] == 25, got
    assert got["nodes"] < 1500, got


# ── the scan: resume, a bad track, a hidden page, a song pressed meanwhile ───────────────────────────────────────
@CHROME
def test_the_scan_resumes_after_a_reload_without_reading_anything_twice():
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.js("window.__publishOK=true; __seedLib({indexed:false}); "
                   + ";".join(f"__plainDelay['{s}']=500" for s in SH.values()) + "; __PC.switchView('music'); 1")
        await _until(b, "Object.values(__PC.filesIdx()._norm().files).filter(m=>m.tg).length>=3", 30, "three tracks indexed")
        got["before"] = await b.js("Object.entries(__PC.filesIdx()._norm().files).filter(([k,m])=>m.tg).map(([k])=>k)")
        await b.call("Page.reload", {})
        await b.until("!!window.__PC && !!__PC.me && !!__PC.me()")
        await _music(b, indexed_first=False, login=False)
        await _until(b, INDEXED, 40, "the rest indexed")
        got["reread"] = await b.js("__plainCalls.slice()")

    _run(check, L.PLAIN + L.LIB)
    assert len(got["before"]) >= 3
    again = set(got["reread"]) & set(got["before"])
    assert not again, ("a track indexed before the reload was decrypted again", again)
    assert set(got["reread"]) | set(got["before"]) == set(SH.values())


@CHROME
def test_a_track_that_will_not_read_does_not_stop_the_scan():
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.js(f"window.__publishOK=true; __plainFail['{SH['d']}']=true; __seedLib({{indexed:false}}); __PC.switchView('music'); 1")
        await _until(b, f"(()=>{{const f=__PC.filesIdx()._norm().files;return !PCMusicLibrary.st.running&&Object.entries(f).filter(([k,m])=>m.folder==='Music'&&k!=='{SH['d']}').every(([k,m])=>m.tg)}})()",
                     40, "every other track indexed")
        got["d"] = await b.js(f"__PC.filesIdx().meta('{SH['d']}').tg||null")
        got["albums"] = await b.js("PCMusicGroups.albums(__PC.music ? Object.entries(__PC.filesIdx()._norm().files).map(([sha,m])=>({sha,m})) : []).map(a=>[a.album,a.tracks.length])")

    _run(check, L.PLAIN + L.LIB)
    assert got["d"] is None, got
    assert ["Unknown album", 2] in got["albums"], ("the unreadable track must still be listed (as unknown)", got["albums"])


@CHROME
def test_the_scan_waits_while_the_page_is_hidden_and_never_holds_up_a_song():
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.js("window.__publishOK=true; window.__plainSerial=true; __seedLib({indexed:false}); "
                   + ";".join(f"__plainDelay['{s}']=400" for s in SH.values()) + "; __PC.switchView('music'); 1")
        await _until(b, "Object.values(__PC.filesIdx()._norm().files).filter(m=>m.tg).length>=2", 30, "two indexed")
        await b.js("Object.defineProperty(document,'hidden',{configurable:true,get:()=>true}); document.dispatchEvent(new Event('visibilitychange')); 1")
        await asyncio.sleep(.5)
        n0 = await b.js("Object.values(__PC.filesIdx()._norm().files).filter(m=>m.tg).length")
        await asyncio.sleep(2.0)
        got["hidden"] = (n0, await b.js("Object.values(__PC.filesIdx()._norm().files).filter(m=>m.tg).length"))
        await b.js("Object.defineProperty(document,'hidden',{configurable:true,get:()=>false}); document.dispatchEvent(new Event('visibilitychange')); 1")
        # A song pressed while the scan runs plays at once: the scan steps aside while the player loads.
        row = SH["9"]
        t0 = await b.js("performance.now()")
        await A.press(b, f".track .track-play[data-sha='{row}']")
        await _until(b, f"__PC.MusicPlayer.cur==='{row}' && !!(__PC.music().now()||{{}}).playing", 10, "the pressed song playing")
        got["ms"] = await b.js(f"performance.now()-{t0}")
        await _until(b, INDEXED, 40, "the scan to finish after")

    _run(check, L.PLAIN + L.LIB)
    assert got["hidden"][1] - got["hidden"][0] <= 1, ("the scan kept decrypting while the page was hidden", got["hidden"])
    assert got["ms"] < 2500, ("a song waited behind the scan", got["ms"])


# ── the opt-in online covers ────────────────────────────────────────────────────────────────────────────────────
async def _turn_on_covers(b):
    await A.press(b, "#ma-libopts")
    await _until(b, "!!document.getElementById('ma-cover-lookup')", 15, "the library options")
    got = await b.js("document.getElementById('ma-cover-lookup').checked")
    await A.press(b, "#ma-cover-lookup")
    await _until(b, "ClientSettings.get('musicCoverLookup')===true", 15, "the switch on")
    await A.press(b, "#ma-opt-close")
    return got


@CHROME
def test_on_by_default_asks_for_the_albums_with_no_picture():
    """Asked for 2026-10-26 ("should be default"): a person who never opens the ⋯ sheet gets the covers."""
    got = {}

    async def check(b):
        await _music(b)
        await _tab(b, "albums")
        await _until(b, "__coverReqs.length>0", 15, "a cover lookup with the switch never touched")
        got["reqs"] = await b.js("__coverReqs.slice()")
        got["checked"] = await b.js("(async()=>{document.getElementById('ma-libopts').click();"
                                    "for(let i=0;i<50&&!document.getElementById('ma-cover-lookup');i++)await new Promise(r=>setTimeout(r,100));"
                                    "return document.getElementById('ma-cover-lookup').checked})()")

    _run(check, L.PLAIN + L.LIB)
    assert got["reqs"], got
    assert got["checked"] is True, ("the switch does not show the lookup is on", got)


@CHROME
def test_switched_off_means_not_one_request():
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.js("ClientSettings.set('musicCoverLookup', false); 1")
        await b.js("window.__publishOK=true; __seedLib(); __PC.switchView('music'); 1")
        await b.until("!!document.querySelector('#ma-browse')")
        await _until(b, INDEXED, 40, "the tag scan to finish")
        await _tab(b, "albums")
        await _until(b, "document.querySelectorAll('#ma-albums .ma-card img.pc-art-img').length>=2", 10, "the files' own covers")
        await asyncio.sleep(2.5)
        got["reqs"] = await b.js("__coverReqs.slice()")
        # OFF is "nothing about my covers leaves this device": the files' own pictures are not stored for syncing either.
        got["uploads"] = await b.js("__uploads.length")
        got["ca"] = await b.js("Object.values(__PC.filesIdx()._norm().files).filter(m=>m.tg&&m.tg.ca).length")

    _run(check, L.PLAIN + L.LIB)
    assert got["reqs"] == [], ("the person turned the lookup off and it still asked", got)
    assert got["uploads"] == 0 and got["ca"] == 0, ("the person turned the lookup off and a cover was still uploaded", got)


@CHROME
def test_on_asks_once_per_album_without_a_picture_and_never_again():
    got = {}

    async def check(b):
        await _screen(b, 390, 844)
        await _music(b, covers=False)       # off first, so the press below is what turns it on
        await _tab(b, "albums")
        got["wasOn"] = await _turn_on_covers(b)
        await _shot(b, "phone-cover-switch-on")
        await _until(b, "__coverReqs.length>=2 && !PCMusicLibrary.st.running", 15, "the lookup and the image")
        await _until(b, "!!document.querySelector('#ma-albums .ma-card[data-i=\"0\"] img.pc-art-img')", 10, "the looked-up cover drawn")
        got["reqs"] = await b.js("__coverReqs.slice()")
        got["oc"] = await b.js(f"__PC.filesIdx().meta('{SH['0']}').tg.oc")
        got["ocOthers"] = await b.js(f"[__PC.filesIdx().meta('{SH['a']}').tg.oc||null, __PC.filesIdx().meta('{SH['9']}').tg.oc||null]")
        await _shot(b, "phone-albums-with-online-cover")
        await b.call("Page.reload", {})
        await b.until("!!window.__PC && !!__PC.me && !!__PC.me()")
        await _music(b, indexed_first=False, login=False)
        await _until(b, INDEXED + " && !PCMusicLibrary.st.running", 20, "settled after reload")
        await asyncio.sleep(2.5)
        got["reqsAfter"] = await b.js("__coverReqs.slice()")
        await _tab(b, "albums")
        await _until(b, "!!document.querySelector('#ma-albums .ma-card[data-i=\"0\"] img.pc-art-img')", 10, "the cover from this device after a reload")

    _run(check, L.PLAIN + L.LIB)
    assert got["wasOn"] is False
    reqs = got["reqs"]
    assert len(reqs) == 2, ("one lookup and one image -- per album without a picture, nothing for the rest", reqs)
    assert reqs[0]["method"] == "POST" and reqs[0]["url"].endswith("/api/music/cover"), reqs[0]
    assert json.loads(reqs[0]["body"]) == {"artist": "Ghost Band", "album": "No Cover Club"}, reqs[0]
    assert "Ghost" not in reqs[0]["url"] and "Club" not in reqs[0]["url"], ("the names went into a URL", reqs[0])
    assert reqs[1]["url"].endswith("/api/music/cover/" + L.COVER_ID), reqs[1]
    assert got["oc"] == L.COVER_ID and got["ocOthers"] == [None, None], got
    assert got["reqsAfter"] == [], ("asked again after a reload", got["reqsAfter"])


@CHROME
@pytest.mark.parametrize("answer,stored,asks_again", [("none", "-", False), ("503", None, True)])
def test_no_cover_is_remembered_and_could_not_ask_is_not(answer, stored, asks_again):
    got = {}

    async def check(b):
        await _music(b, covers=False)
        await b.js(f"window.__coverAnswer={json.dumps(answer)}; 1")
        await _turn_on_covers(b)
        await _until(b, "__coverReqs.length>=1 && !PCMusicLibrary.st.running", 15, "the lookup")
        got["oc"] = await b.js(f"__PC.filesIdx().meta('{SH['0']}').tg.oc||null")
        await b.call("Page.reload", {})
        await b.until("!!window.__PC && !!__PC.me && !!__PC.me()")
        await b.js("window.__coverAnswer='ok'; 1")
        await _music(b, indexed_first=False, login=False)
        await asyncio.sleep(3)
        got["after"] = await b.js("__coverReqs.filter(r=>r.method==='POST').length")

    _run(check, L.PLAIN + L.LIB)
    assert got["oc"] == stored, (answer, got)
    assert (got["after"] >= 1) == asks_again, (answer, "asked again" if got["after"] else "never asked again", got)


# ── the windowed desktop (PosterChanOS) ─────────────────────────────────────────────────────────────────────────
@CHROME
def test_the_music_window_browses_albums_and_artists():
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.until("!!document.querySelector('#os-desk')")
        await b.js("window.__publishOK=true; __seedLib(); __PC.switchView('music'); 1")
        await b.until("!!document.querySelector('.osw .music-app #ma-browse')")
        await _until(b, INDEXED, 40, "the scan")
        await _tab(b, "albums")
        await _until(b, "document.querySelectorAll('.osw #ma-albums .ma-card').length===4", 10, "albums in the window")
        got["geom"] = await b.js(GEOM_DESK)
        got["cols"] = await b.js("(()=>{const g=document.getElementById('ma-albums'),k=[...g.children],t=k[0].getBoundingClientRect().top;return k.filter(e=>Math.abs(e.getBoundingClientRect().top-t)<2).length})()")
        await _shot(b, "desktop-window-albums")
        await A.press(b, ".osw #ma-albums .ma-card[data-i='1']")
        await _until(b, "!!document.querySelector('.osw #ma-page[data-kind=album]')", 15, "album page in the window")
        await A.press(b, ".osw #ma-alb-play")
        await _until(b, f"__PC.MusicPlayer.cur==='{NEON[0]}'", 15, "playing from the window")
        await _shot(b, "desktop-window-album-page")
        await A.press(b, ".osw #ma-back")
        await _tab(b, "artists")
        got["artists"] = await b.js("[...document.querySelectorAll('.osw #ma-artists .ma-artist-n')].map(e=>e.textContent)")
        got["errors"] = await b.js("__errors.slice()")

    _run(check, L.LIB)
    assert got["geom"]["problems"] == [], got["geom"]
    assert got["cols"] >= 3, ("a desktop window shows more than a phone's two columns", got["cols"])
    assert got["artists"] == ["Ghost Band", "Kaito Vega", "Various Artists", "Unknown artist"], got["artists"]
    assert not got["errors"], got["errors"]


# ── the phone layout: the library gets the screen, the player docks (2026-10-26: "made less room to see your music") ──
ROWS_IN_VIEW = r"""(()=>{const bar=document.querySelector('.ma-now'), nav=document.querySelector('.mobilenav');
  const br=bar&&bar.checkVisibility()?bar.getBoundingClientRect():null, nr=nav&&getComputedStyle(nav).display!=='none'?nav.getBoundingClientRect():null;
  const floor=Math.min(br?br.top:innerHeight, nr?nr.top:innerHeight);
  return [...document.querySelectorAll('#ma-lib .track')].filter(t=>{const r=t.getBoundingClientRect();return r.top>=0&&r.bottom<=floor+1;}).length;})()"""


@CHROME
def test_a_phone_lists_songs_as_rows_and_the_row_plays():
    got = {}

    async def check(b):
        await _screen(b, 390, 844)
        await _music(b)
        await _tab(b, "songs")
        await b.js("document.getElementById('feed').scrollTop=0; 1")
        await asyncio.sleep(.3)
        got["idle"] = await b.js("({bar:document.querySelector('.ma-now').checkVisibility(), rows:" + ROWS_IN_VIEW + "})")
        acts = ".track-keep,.track-add,.track-dl,.track-ren,.track-del"
        got["actsBefore"] = await b.js(f"[...document.querySelectorAll('#ma-lib .track:first-of-type :is({acts})')].filter(e=>e.checkVisibility()).length")
        # A tap on the ROW (its title, not its play button) plays the song.
        first = await b.js("document.querySelector('#ma-lib .track').dataset.sha")
        await A.press(b, f"#ma-lib .track[data-sha='{first}'] .track-name")
        await _until(b, f"__PC.MusicPlayer.cur==={json.dumps(first)}", 10, "the tapped row playing")
        await asyncio.sleep(.6)
        got["playing"] = await b.js("({bar:document.querySelector('.ma-now').checkVisibility(), rows:" + ROWS_IN_VIEW + "})")
        # The row is play, the song and ⋯ -- nothing else; ⋯ is a MENU of that song's actions, and picking one runs
        # it (Add to a playlist opens the playlist picker for THAT song).
        got["rowButtons"] = await b.js(f"[...document.querySelectorAll('#ma-lib .track[data-sha=\"{first}\"] button')].filter(e=>e.checkVisibility()).map(e=>e.className.split(' ')[0])")
        second = await b.js("[...document.querySelectorAll('#ma-lib .track')][1].dataset.sha")
        await A.press(b, f"#ma-lib .track[data-sha='{second}'] .track-more")
        await _until(b, "!!document.querySelector('.menu-pop')", 10, "the song's menu")
        got["menu"] = await b.js("[...document.querySelectorAll('.menu-pop button')].map(e=>e.textContent.trim())")
        await b.js("[...document.querySelectorAll('.menu-pop button')].find(e=>e.textContent.trim()==='Add to a playlist').id='pick-add'; 1")
        await A.press(b, "#pick-add")
        await _until(b, "!document.querySelector('.menu-pop') && !!document.querySelector('.modal, [role=dialog]')", 15, "the playlist picker")
        await b.js("__PC.closeModal && __PC.closeModal(); 1")
        assert not await b.js("__errors.slice()")

    _run(check, L.PLAIN + L.LIB)
    assert got["idle"]["bar"] is False, ("nothing is loaded and the player still takes the screen", got)
    assert got["idle"]["rows"] >= 6, ("fewer than six songs fit a 390x844 phone", got)
    assert got["actsBefore"] == 0, ("every row still shows its five action buttons", got)
    assert got["playing"]["bar"] is True and got["playing"]["rows"] >= 5, ("playing, the docked player took the list's room", got)
    assert got["rowButtons"] == ["track-play", "track-more"], ("a song row shows more than play and ⋯", got)
    assert got["menu"] == ["Keep on this device", "Add to a playlist", "Save a copy", "Rename", "Delete"], ("⋯ is not the song's actions", got)


@CHROME
def test_add_and_the_library_actions_are_reachable_on_a_phone():
    got = {}

    async def check(b):
        await _screen(b, 390, 844)
        await _music(b)
        await _tab(b, "songs")
        # The library's own Refresh, pressed through the options sheet -- the same button and handler, not a copy.
        await b.js("(()=>{const r=document.getElementById('mus-refresh'), o=r.onclick; r.onclick=function(){window.__refreshed=(window.__refreshed||0)+1; return o&&o.apply(this,arguments)}; })(); 1")
        await A.press(b, "#ma-libopts")
        await _until(b, "!!document.querySelector('#ma-opt-acts [data-act=mus-refresh]')", 15, "Refresh in the library options")
        await A.press(b, "#ma-opt-acts [data-act=mus-refresh]")
        await _until(b, "window.__refreshed===1", 15, "the library's Refresh")
        # ＋ is Add music.
        await A.press(b, "#ma-libadd")
        await _until(b, "__PC.VIEW!=='music'", 15, "Add music opening the Music folder")
        got["view"] = await b.js("__PC.VIEW")

    _run(check, L.PLAIN + L.LIB)
    assert got["view"] in ("blossom", "files"), got


@CHROME
def test_the_desktop_keeps_its_player_card():
    async def check(b):
        await _screen(b, 1280, 800)
        await _music(b)
        await _tab(b, "songs")
        got = await b.js("""({pos:getComputedStyle(document.querySelector('.ma-now')).position,
            card:document.querySelector('.ma-now').checkVisibility(), add:document.getElementById('ma-libadd').checkVisibility(),
            more:[...document.querySelectorAll('#ma-lib .track-more')].some(e=>e.checkVisibility())})""")
        # The card stays; a song row is play, the song and ⋯ here too ("all of the buttons can be in a hamburger menu").
        got["inline"] = await b.js("[...document.querySelectorAll('#ma-lib .track:not(.gone) :is(.track-keep,.track-add,.track-dl,.track-ren,.track-del)')].filter(e=>e.checkVisibility()).length")
        assert got["pos"] != "fixed" and got["card"] and not got["add"] and got["more"] and got["inline"] == 0, got
    _run(check, L.PLAIN + L.LIB)


# The device's offline-copy list (IndexedDB 'pcmusic') answers LATE: held until the test releases it.
HOLD_OFFLINE = r"""
(() => { const real = indexedDB.open.bind(indexedDB); window.__idbHeld = [];
  indexedDB.open = function(name, v){ if(name !== 'pcmusic' || window.__idbGo) return real(name, v);
    const fake = {}; window.__idbHeld.push(() => { const r = real(name, v);
      r.onupgradeneeded = e => { fake.result = r.result; fake.onupgradeneeded && fake.onupgradeneeded(e); };
      r.onsuccess = e => { fake.result = r.result; fake.onsuccess && fake.onsuccess(e); };
      r.onerror = e => { fake.error = r.error; fake.onerror && fake.onerror(e); }; });
    return fake; };
  window.__releaseOffline = () => { window.__idbGo = 1; window.__idbHeld.splice(0).forEach(f => f()); };
})();
"""


@CHROME
def test_a_late_offline_check_does_not_draw_the_songs_back_over_albums():
    """The gate (deploy 88): Albums selected, the song list on screen. The first paint of Songs asks which tracks are
    kept on this device and repaints itself when that answers; the repaint only checked that the element was still on
    the page -- and #ma-lib is the same element the Albums grid draws into. Here that answer is held until AFTER the
    switch to Albums."""
    got = {}

    async def check(b):
        await _screen(b, 390, 844)
        await desktop.login(b)
        await b.js("try{localStorage.setItem('pc_music_tab','songs')}catch(_){}; window.__publishOK=true; __seedLib(); __PC.switchView('music'); 1")
        await _until(b, "document.querySelectorAll('#ma-lib .track').length>=3", 20, "the song list (offline check still held)")
        assert await b.js("window.__idbHeld.length") >= 1, "the offline check was not held -- this proves nothing"
        await _tab(b, "albums")
        await _until(b, "!!document.getElementById('ma-albums')", 10, "the albums grid")
        # Any song row drawn into the library while Albums is the tab -- even for a moment -- is the bug.
        await b.js("""window.__songsOverAlbums = 0; new MutationObserver(() => {
            if(document.querySelector('.ma-tab.on')?.dataset.tab === 'albums' && document.querySelector('#ma-lib > .track'))
              window.__songsOverAlbums++; }).observe(document.getElementById('ma-lib'), {childList: true}); 1""")
        await b.js("__releaseOffline(); 1")
        await asyncio.sleep(1.5)                           # the held answer lands, and any repaint it triggers
        got["after"] = await b.js("({albums:!!document.getElementById('ma-albums'), songs:document.querySelectorAll('#ma-lib > .track').length,"
                                  " tab:document.querySelector('.ma-tab.on').dataset.tab, flashed:window.__songsOverAlbums})")

    _run(check, L.PLAIN + L.LIB + HOLD_OFFLINE)
    assert got["after"]["flashed"] == 0 and got["after"]["tab"] == "albums" and got["after"]["albums"] and got["after"]["songs"] == 0, \
        ("a late offline check drew the song list back over the Albums the person had opened", got)
