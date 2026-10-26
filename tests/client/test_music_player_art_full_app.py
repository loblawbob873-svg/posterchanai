"""The music player looks like a music player: the track's own COVER, title and artist on every surface, and every
control still drives the audio -- in the SHIPPED bundled client, pressed with real mouse events.

"improve the Music player (mobile, desktop, OS) to look like regular music players ... maybe album art?" The
player reads the file's own tags (audiotags.js, now including a WAV's `id3 ` chunk) from the decrypted track and
shows them in the Music app (a now-playing card that opens into a full-screen Now playing), the floating widget,
the desktop's Now-playing panel, the lock screen (navigator.mediaSession) and the APK's native media session.
Tracks with no tags draw a GENERATED cover, never a broken image.

What each test guards, and how it fails on the code before this change:
  * test_cover_artist_and_placeholder_follow_the_track -- the art, the artist and the mediaSession metadata follow a
    track CHANGE; the old cover's object URL is revoked; a skip while a load is in flight never paints the
    previous track's (or the abandoned track's) cover. Before: no cover, no artist, mediaSession said "PosterChan".
  * test_every_control_drives_the_player[390|1280] -- every transport control of the NEW layout, pressed where it is
    drawn (a control covered by the bottom nav or a sibling fails the press itself), changes the audio/queue.
  * test_the_floating_widget_drives_the_player[390|1280] -- the same for the web/PWA floating widget, incl. the
    playlist picker, search, a track row, minimise/expand/close and the mini pill's art.
  * test_phone_layouts_fit[...] -- 320x640 (the CI emulator), 360, 390, landscape: nothing sideways, nothing off
    screen or under the bottom nav, every control >= 44px, the cover square, icons centred.
  * test_desktop_window_and_now_playing_widget -- the windowed desktop: the Music window's controls and the desk's
    Now-playing panel showing the cover and the artist.
  * test_the_apk_hides_the_widget_and_tells_android_the_artist -- MusicControls stubbed as the APK has it: the
    floating widget stays hidden, and the native session is given the real artist and album.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client import music_art_fixture as F
from tests.client.test_music_opens_in_front_full_app import _widget

CHROME = pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
S1, S2, S3 = F.SHA[1], F.SHA[2], F.SHA[3]


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


async def _phone(b, w, h):
    await b.call("Emulation.setDeviceMetricsOverride", {"width": w, "height": h, "deviceScaleFactor": 1, "mobile": w <= 820})
    if w <= 820:
        await b.call("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5})


async def _music(b):
    await desktop.login(b)
    await b.js("window.__publishOK=true; __seedLib(); __PC.filesIdx().saveLocal(); __PC.switchView('music'); 1")
    await b.until("document.querySelectorAll('.track .track-play').length>=5")


async def _play_row(b, sha):
    await F.press(b, f".track .track-play[data-sha='{sha}']")
    await F.until_state(b, lambda s: s["cur"] == sha and s["playing"], what=f"{sha[:4]} playing")


async def _settled(b):
    """Before a drag: the track has its length (the bar is not seekable without one) and play() has finished --
    its closing _render rebuilds the widget, which abandons a drag in flight by design (see MusicPlayer._render)."""
    await b.until("(()=>{const n=__PC.music().now();return !!(n&&n.d>0)&&!__PC.MusicPlayer._loading})()")
    await asyncio.sleep(.6)


def _run(check, init, w=1440, h=1000):
    asyncio.run(desktop.with_browser("online", "", check, init))


# ── metadata and art follow the track ────────────────────────────────────────────────────────────────────────────
@CHROME
def test_cover_artist_and_placeholder_follow_the_track():
    got = {}

    async def check(b):
        await _phone(b, 390, 844)
        await _music(b)
        await _play_row(b, S1)
        s = await F.until_state(b, lambda s: s["appArt"]["kind"] == "cover" and s["appArt"]["loaded"], what="track 1's cover")
        got["one"] = s
        # Two skips while track 2's bytes are held back: the first lands mid-load, the second abandons that load.
        await b.js(f"window.__delay[{json.dumps(S2)}]=1500; window.__painted=[];"
                   "new MutationObserver(ms=>{for(const m of ms)for(const n of m.addedNodes){const i=n.querySelector?(n.matches('img')?n:n.querySelector('img')):null;"
                   "if(i)__painted.push(i.getAttribute('src'));}}).observe(document.getElementById('ma-art'),{childList:true,subtree:true}); 1")
        await F.press(b, "#ma-next")
        got["mid"] = await F.state(b)              # the instant after the press: track 2 is loading
        await F.press(b, "#ma-next")
        await F.until_state(b, lambda s: s["cur"] == S3 and s["playing"], what="track 3 playing")
        await asyncio.sleep(2.2)                   # track 2's held-back load lands after this
        got["three"] = await F.state(b)
        got["painted"] = await b.js("window.__painted")
        got["held"] = await b.js("(()=>{const c=__PC.MusicPlayer._cover;return c?c.sha:null})()")
        got["glyph"] = await b.js(GLYPH)                 # the generated cover, card size
        await F.press(b, "#ma-art")
        await F.until_state(b, lambda s: s["full"], what="Now playing")
        await asyncio.sleep(.3)
        got["glyphFull"] = await b.js(GLYPH)             # ...and full size
        await F.press(b, "#ma-collapse")
        # Back: track 2, now loaded, shows ITS cover and artist.
        await F.press(b, "#ma-prev")
        got["two"] = await F.until_state(b, lambda s: s["cur"] == S2 and s["appArt"]["kind"] == "cover" and s["appArt"]["loaded"],
                                         what="track 2's cover after ⏮")
        # A skip DURING a cover read: track 1's read is held back, ⏭ moves on to track 2 (read at once), and track 1's
        # late answer must not take track 2's cover away -- nor leave track 1's picture held for nobody.
        await _settled(b)
        await b.js("window.__tagDelayOnce=1500; 1")
        await F.press(b, "#ma-prev")
        await F.until_state(b, lambda s: s["cur"] == S1, what="back on track 1, its cover still being read")
        await F.press(b, "#ma-next")
        await F.until_state(b, lambda s: s["cur"] == S2 and s["appArt"]["loaded"], what="track 2's cover")
        await asyncio.sleep(2.0)                   # track 1's held-back read answers in here
        got["late"] = await F.state(b)
        got["lateHeld"] = await b.js("(()=>{const c=__PC.MusicPlayer._cover;return c?c.sha:null})()")
        assert not await b.js("__errors"), await b.js("__errors")

    _run(check, F.PLAIN + F.LIB + F.HELD)
    one, mid, three, two = got["one"], got["mid"], got["three"], got["two"]
    # Track 1: its own title, artist and cover everywhere the player speaks.
    assert one["appTitle"] == "Neon Rain" and one["appArtist"] == "Kaito Vega · Night City", one
    assert one["title"] == "Neon Rain" and one["artist"] == "Kaito Vega" and one["album"] == "Night City", one
    assert one["art"] and one["appArt"]["src"] == one["art"], one
    assert one["md"] and one["md"]["title"] == "Neon Rain" and one["md"]["artist"] == "Kaito Vega", one
    assert one["md"]["album"] == "Night City" and one["md"]["art"][0] == one["art"], one
    # The skip: the old cover is gone at once -- revoked, and not drawn over the next song while it loads.
    assert mid["cur"] == S2 and mid["appArt"]["kind"] == "placeholder", mid
    assert one["art"] in mid["revoked"], ("the previous cover's object URL was not revoked", mid)
    assert mid["appArtist"] == "" and mid["md"]["artist"] != "Kaito Vega", mid
    # The abandoned load never painted anything, and the untagged track 3 shows the generated cover.
    assert got["painted"] == [], ("a cover was painted for a track that was skipped mid-load", got["painted"])
    assert got["held"] is None, ("the skipped track's cover is still held (an object URL nobody shows)", got["held"])
    assert three["appArt"]["kind"] == "placeholder" and three["appTitle"] == "Stub Song 3", three
    assert three["artist"] == "" and three["art"] == "" and three["md"]["title"] == "Stub Song 3", three
    # The generated cover is a cover: a gradient with a note CENTRED on it at a real size -- the glyph was once
    # 22x60px pinned to the left edge in the card (percent sizes resolved against the grid row, not the cover).
    for k in ("glyph", "glyphFull"):
        g = got[k]
        assert g["bg"] and "gradient" in g["bg"], (k, "the placeholder is a flat block", g)
        assert .3 <= g["w"] <= .5 and abs(g["w"] - g["h"]) < .02, (k, "the note is not a square ~40% of the cover", g)
        assert abs(g["dx"]) < .03 and abs(g["dy"]) < .03, (k, "the note is not centred on the cover", g)
    # Track 2: its own cover (a different picture), title and artist.
    assert two["appTitle"] == "Chrome Hearts" and two["artist"] == "Mira Sol" and two["art"] and two["art"] != one["art"], two
    assert two["md"]["art"][0] == two["art"] and two["md"]["artist"] == "Mira Sol", two
    late = got["late"]
    assert got["lateHeld"] == S2, ("a late read for a track that was skipped took over the held cover", got["lateHeld"])
    assert late["cur"] == S2 and late["appArt"]["kind"] == "cover" and late["appArt"]["loaded"], late
    assert late["art"] not in late["revoked"], ("the playing track's cover was revoked by a late read", late)


@CHROME
def test_a_cover_arriving_mid_seek_does_not_cancel_it():
    """Found while writing these tests: a cover finishing its read calls _render, which abandons any drag in flight
    -- so a seek made while the tags were still loading sprang back and did nothing (1 drag in 3, measured)."""
    got = {}

    async def check(b):
        await _phone(b, 390, 844)
        await _music(b)
        await _play_row(b, S1)
        await F.until_state(b, lambda s: s["appArt"]["loaded"], what="cover")
        r = await b.js("(()=>{const r=document.querySelector('#ma-seek').getBoundingClientRect();return [r.left,r.top+r.height/2,r.width]})()")
        x0, y, wd = r[0] + r[2] * .2, r[1], r[2]
        await b.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x0, "y": y, "button": "left", "clickCount": 1})
        await b.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": r[0] + wd * .5, "y": y, "button": "left", "buttons": 1})
        # The cover is read again WHILE the finger is down (as it is on every return to a track).
        await b.js("(async()=>{const MP=__PC.MusicPlayer;MP._tags.delete(MP.cur);MP._dropCover(null);await MP._readTags(MP.cur,'');})()")
        await asyncio.sleep(.4)
        await b.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": r[0] + wd * .7, "y": y, "button": "left", "buttons": 1})
        await b.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": r[0] + wd * .7, "y": y, "button": "left", "clickCount": 1})
        got["s"] = await F.until_state(b, lambda s: s["appArt"]["loaded"], what="cover back after the drag")

    _run(check, F.PLAIN + F.LIB + F.HELD)
    assert 18 <= got["s"]["t"] <= 24, ("the seek was cancelled by the cover arriving", got["s"]["t"])


GLYPH = r"""(()=>{const a=document.querySelector('#ma-art .pc-art'), i=a&&a.querySelector('svg.ic'); if(!a||!i) return {bg:'',w:0,h:0,dx:1,dy:1};
  const A=a.getBoundingClientRect(), G=i.getBoundingClientRect();
  return {bg:getComputedStyle(a).backgroundImage, w:G.width/A.width, h:G.height/A.height,
          dx:((G.left+G.width/2)-(A.left+A.width/2))/A.width, dy:((G.top+G.height/2)-(A.top+A.height/2))/A.height};})()"""

# Which fraction of the spectrum strip's columns carry a bar taller than a quarter of the strip (and of its right quarter).
SPREAD = r"""(()=>{const cv=document.getElementById('ma-viz'); const W=cv.width,H=cv.height; if(!W||!H) return {lit:0,right:0,W,H};
  const d=cv.getContext('2d').getImageData(0,0,W,H).data, tall=[];
  for(let x=0;x<W;x++){ let top=H; for(let y=0;y<H;y++){ if(d[(y*W+x)*4+3]>60){ top=y; break; } } tall.push(H-top>H*.25); }
  const frac=a=>a.filter(Boolean).length/Math.max(1,a.length);
  // bars have gaps between them: count a column lit if any column within 4px is
  const near=tall.map((_,x)=>tall.slice(Math.max(0,x-4),x+5).some(Boolean));
  return {lit:frac(near), right:frac(near.slice(Math.floor(W*.75))), W, H};})()"""

# How much of Now playing's card the cover takes (a desktop view must not be a small cover in an empty card).
FILL = r"""(()=>{const a=document.querySelector('#ma-art .pc-art').getBoundingClientRect(), c=document.querySelector('.ma-now').getBoundingClientRect(),
  t=document.getElementById('ma-title'); return {w:a.width/c.width, h:a.height/c.height, font:parseFloat(getComputedStyle(t).fontSize), cardW:c.width,
  display:getComputedStyle(document.querySelector('.ma-now')).display};})()"""


# ── every control, pressed where it is drawn ─────────────────────────────────────────────────────────────────────
@CHROME
@pytest.mark.parametrize("width", [390, 1280])
def test_every_control_drives_the_player(width):
    async def check(b):
        await _phone(b, width, 844 if width < 800 else 800)
        await _music(b)
        await _play_row(b, S1)                                             # a track row
        assert await F.audit(b, ".ma-now") == [], await F.audit(b, ".ma-now")
        await _settled(b)
        await F.press(b, "#ma-play")                                       # pause
        await F.until_state(b, lambda s: not s["playing"], what="paused")
        await b.until("document.querySelector('#ma-play use').getAttribute('href')==='#i-play'")
        await F.press(b, "#ma-play")                                       # resume
        await F.until_state(b, lambda s: s["playing"], what="resumed")
        await F.press(b, "#ma-next")
        await F.until_state(b, lambda s: s["cur"] != S1 and s["playing"], what="next")
        await F.press(b, "#ma-prev")
        await F.until_state(b, lambda s: s["cur"] == S1, what="previous")
        # The spectrum draws across the WHOLE strip for broadband audio (log bands up to 16 kHz), not a clump on the left.
        await asyncio.sleep(1.0)
        spread = await b.js(SPREAD)
        assert spread["lit"] >= .6 and spread["right"] >= .3, ("the spectrum is a clump, not a band across the strip", spread)
        await F.press(b, "#ma-shuffle")
        await F.until_state(b, lambda s: s["shuffle"], what="shuffle on")
        assert await b.js("document.getElementById('ma-shuffle').getAttribute('aria-pressed')") == "true"
        await F.press(b, "#ma-shuffle")
        await F.until_state(b, lambda s: not s["shuffle"], what="shuffle off")
        await _settled(b)                                                  # a seek needs the track's length
        await F.drag(b, "#ma-seek", .2, .7)                                # seek by dragging
        await F.until_state(b, lambda s: 18 <= s["t"] <= 24, what="seeked to ~70% of 30 s")
        # Now playing: opened from the cover, every control still works in the big layout, closed by its chevron.
        await F.press(b, "#ma-art")
        await F.until_state(b, lambda s: s["full"], what="Now playing open")
        assert await F.audit(b, ".music-app") == [], await F.audit(b, ".music-app")
        if width >= 1280:
            fill = await b.js(FILL)
            assert fill["w"] >= .42 and fill["h"] >= .55, ("desktop Now playing is a small cover in an empty card", fill)
            assert fill["font"] >= 30, ("desktop Now playing's title did not scale up with the view", fill)
        assert await b.js("!document.getElementById('ma-lib').checkVisibility()"), "the library is still drawn under Now playing"
        await F.press(b, "#ma-next")
        await F.until_state(b, lambda s: s["cur"] != S1 and s["playing"], what="next in Now playing")
        await F.press(b, "#ma-play")
        await F.until_state(b, lambda s: not s["playing"], what="paused in Now playing")
        await _settled(b)
        await F.drag(b, "#ma-seek", .1, .5)
        await F.until_state(b, lambda s: 12 <= s["t"] <= 18, what="seeked in Now playing")
        await F.press(b, "#ma-collapse")
        await F.until_state(b, lambda s: not s["full"], what="back to the library")
        assert await b.js("document.getElementById('ma-lib').checkVisibility()")
        # Search narrows the library; a row from the result plays.
        await F.press(b, "#ma-q")
        await b.call("Input.insertText", {"text": "Song 3"})
        await b.until("document.querySelectorAll('.track .track-play').length===1")
        await F.press(b, f".track .track-play[data-sha='{S3}']")
        await F.until_state(b, lambda s: s["cur"] == S3 and s["playing"], what="search result played")
        assert not await b.js("__errors"), await b.js("__errors")

    _run(check, F.PLAIN + F.LIB + F.HELD)


@CHROME
@pytest.mark.parametrize("width", [390, 1280])
def test_the_floating_widget_drives_the_player(width):
    async def check(b):
        await _phone(b, width, 844 if width < 800 else 800)
        await _music(b)
        assert await b.js(f"(async()=>!!(await PCPlaylists.create('Road trip',[{json.dumps(S2)},{json.dumps(S1)}])))()")
        await _play_row(b, S1)
        await F.until_state(b, lambda s: s["appArt"]["loaded"], what="cover")
        # Leave Music: the floating widget takes over, expanded, with the cover and the artist.
        await b.js("__PC.switchView('notifications'); __PC.MusicPlayer.setMin(false); 1")
        await F.until_state(b, lambda s: not s["widgetHidden"] and s["widgetArt"] and s["widgetArt"]["loaded"], what="widget with cover")
        s = await F.state(b)
        assert s["widgetArt"]["kind"] == "cover" and s["widgetArtist"] == "Kaito Vega · Night City", s
        assert await F.audit(b, "#music-player", nav=False) == [], await F.audit(b, "#music-player", nav=False)
        await F.press(b, "#music-player .mp-play")
        await F.until_state(b, lambda s: not s["playing"], what="widget pause")
        # The widget is rebuilt when the 'pause' event lands, a moment after `paused` flips; wait for it to say so.
        await b.until("document.querySelector('#music-player .mp-play').getAttribute('aria-label')==='Play'")
        await F.press(b, "#music-player .mp-play")
        await F.until_state(b, lambda s: s["playing"], what="widget play")
        await b.until("document.querySelector('#music-player .mp-play').getAttribute('aria-label')==='Pause'")
        await F.press(b, "#music-player .mp-next")
        await F.until_state(b, lambda s: s["cur"] != S1 and s["playing"], what="widget next")
        await _settled(b)
        await F.press(b, "#music-player .mp-prev")
        await F.until_state(b, lambda s: s["cur"] == S1, what="widget previous")
        await _settled(b)                    # play() ends by rebuilding the widget; a press then lands on the old one
        await F.press(b, "#music-player .mp-shuffle")
        await F.until_state(b, lambda s: s["shuffle"], what="widget shuffle")
        await F.press(b, "#music-player .mp-shuffle")
        await F.until_state(b, lambda s: not s["shuffle"], what="widget shuffle off")
        await _settled(b)
        await F.drag(b, "#music-player .mp-seek", .2, .8)
        await F.until_state(b, lambda s: 21 <= s["t"] <= 27, what="widget seek")
        # The playlist picker: choosing "Road trip" makes the queue that playlist, in its order.
        await b.until("!!document.querySelector('#music-player .mp-pl option:not([value=\"\"])')")
        await b.js("(()=>{const s=document.querySelector('#music-player .mp-pl');s.value=[...s.options].find(o=>o.value).value;"
                   "s.dispatchEvent(new Event('change',{bubbles:true}));})()")
        assert await b.js("__PC.MusicPlayer.queue.slice()") == [S2, S1]
        await b.js("(()=>{const s=document.querySelector('#music-player .mp-pl');s.value='';s.dispatchEvent(new Event('change',{bubbles:true}));})()")
        # Search, then a row.
        await F.press(b, "#music-player .mp-search")
        await b.call("Input.insertText", {"text": "Song 3"})
        await b.until("document.querySelectorAll('#music-player .mp-list .mp-track').length===1")
        await F.press(b, "#music-player .mp-list .mp-track")
        await F.until_state(b, lambda s: s["cur"] == S3 and s["playing"], what="widget row played")
        # Minimise: the pill carries the generated cover of the untagged track; expand; close.
        await F.press(b, "#music-player .mp-min")
        await F.until_state(b, lambda s: s["miniArt"] is not None, what="mini pill")
        assert (await F.state(b))["miniArt"]["kind"] == "placeholder"
        assert await F.audit(b, "#music-player", nav=False) == [], await F.audit(b, "#music-player", nav=False)
        await b.until("document.querySelector('#music-player .mp-play').getAttribute('aria-label')==='Pause'")
        await F.press(b, "#music-player .mp-play")
        await F.until_state(b, lambda s: not s["playing"], what="mini pause")
        await b.until("document.querySelector('#music-player .mp-play').getAttribute('aria-label')==='Play'")
        await F.press(b, "#music-player .mp-exp")
        await F.until_state(b, lambda s: s["widgetArt"] is not None, what="expanded again")
        # Close WHILE PLAYING: that is the case whose late 'pause' event used to bring the panel straight back.
        await F.press(b, "#music-player .mp-play")
        await F.until_state(b, lambda s: s["playing"], what="playing before close")
        await b.until("document.querySelector('#music-player .mp-play').getAttribute('aria-label')==='Pause'")
        await F.press(b, "#music-player .mp-close")
        await F.until_state(b, lambda s: s["widgetHidden"] and not s["playing"], what="closed")
        await asyncio.sleep(1.0)       # the 'pause' event lands after close() returns; closed must STAY closed
        assert (await F.state(b))["widgetHidden"], "the widget came back after ✕ Close"
        assert not await b.js("__errors"), await b.js("__errors")

    _run(check, F.PLAIN + F.LIB + F.HELD)


# ── phone layouts ────────────────────────────────────────────────────────────────────────────────────────────────
@CHROME
@pytest.mark.parametrize("size", [(320, 640), (360, 780), (390, 844), (844, 390), (740, 360), (360, 600), (360, 720), (360, 540)])
def test_phone_layouts_fit(size):
    w, h = size
    got = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": w, "height": h, "deviceScaleFactor": 2, "mobile": True})
        await b.call("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5})
        await _music(b)
        await _play_row(b, S1)
        await F.until_state(b, lambda s: s["appArt"]["loaded"], what="cover")
        # Measured where a person arrives: the top of the Music screen (reaching a row above scrolled the page).
        await b.js("document.getElementById('feed').scrollTop=0; scrollTo(0,0); 1")
        await asyncio.sleep(.2)
        got["card"] = await F.audit(b, ".ma-now")
        got["cardSizes"] = await b.js(SIZES)
        # The equalizer opens INSIDE the card, which then needs far more height: the card must grow, never be
        # squeezed by the column so that its own contents (or the chips below) draw over each other.
        await F.press(b, "#ma-eq-toggle")
        await b.until("document.getElementById('ma-eq-toggle').getAttribute('aria-expanded')==='true'")
        got["eqOverlap"] = await b.js(OVERLAP)
        # Taller than a short screen now, so it is scrolled to -- and must then be clear of the bottom nav.
        await b.js("document.getElementById('ma-eq').scrollIntoView({block:'end'}); 1")
        await asyncio.sleep(.2)
        got["eq"] = await F.audit(b, "#ma-eq")
        await F.press(b, "#ma-eq-toggle")
        await b.until("document.getElementById('ma-eq-toggle').getAttribute('aria-expanded')==='false'")
        await F.press(b, "#ma-art")
        await F.until_state(b, lambda s: s["full"], what="Now playing")
        await asyncio.sleep(.3)
        got["full"] = await F.audit(b, ".music-app")
        got["fullSizes"] = await b.js(SIZES)

    _run(check, F.PLAIN + F.LIB + F.HELD)
    assert got["card"] == [], got
    assert got["eq"] == [] and got["eqOverlap"] == [], got
    assert got["full"] == [], got
    for k in ("cardSizes", "fullSizes"):
        z = got[k]
        assert z["small"] == [], (k, "controls smaller than a thumb", z)
        assert abs(z["art"][0] - z["art"][1]) <= 1 and z["art"][0] >= 48, (k, "the cover is not a square", z)
    full = got["fullSizes"]
    assert full["art"][0] >= 120, ("Now playing's cover is a thumbnail", full)
    assert full["playBottom"] <= full["limit"], ("the play button is below the visible screen", full)
    assert full["artOverlaps"] == [], ("Now playing's cover draws over", full)


SIZES = r"""(()=>{const vis=e=>e.checkVisibility();
  const small=[...document.querySelectorAll('.ma-now button')].filter(vis).map(b=>b.getBoundingClientRect())
    .filter(r=>r.width<44||r.height<40).map(r=>[Math.round(r.width),Math.round(r.height)]);
  const a=document.querySelector('#ma-art .pc-art').getBoundingClientRect(), p=document.getElementById('ma-play').getBoundingClientRect();
  const nav=document.querySelector('.mobilenav'); const nr=nav&&getComputedStyle(nav).display!=='none'?nav.getBoundingClientRect():null;
  const meet=(x,y)=>x.left<y.right-1&&y.left<x.right-1&&x.top<y.bottom-1&&y.top<x.bottom-1;
  const artOverlaps=['ma-title','ma-seek','ma-play','ma-prev','ma-next','ma-collapse','ma-viz'].filter(id=>meet(a,document.getElementById(id).getBoundingClientRect()));
  return {small, art:[Math.round(a.width),Math.round(a.height)], playBottom:Math.round(p.bottom), artOverlaps,
          limit:Math.round(nr&&nr.height?nr.top:innerHeight)};})()"""


# Siblings in the Music screen's column that draw over each other (a squeezed card overflowing onto the chips).
OVERLAP = r"""(()=>{const app=document.querySelector('.music-app'), out=[];
  const kids=[...app.children].filter(e=>e.checkVisibility()).map(e=>[e.id||e.className,e.getBoundingClientRect()]);
  for(let i=1;i<kids.length;i++){const [n0,a]=kids[i-1],[n1,b]=kids[i]; if(b.top<a.bottom-1) out.push(n0+' / '+n1);}
  const now=document.querySelector('.ma-now'); if(now.scrollHeight>now.clientHeight+1 && getComputedStyle(now).overflowY==='hidden')
    out.push('card clips its own contents '+now.scrollHeight+'>'+now.clientHeight);
  return out;})()"""


# ── the windowed desktop ─────────────────────────────────────────────────────────────────────────────────────────
@CHROME
def test_desktop_window_and_now_playing_widget():
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.until("!!document.querySelector('#os-desk')")
        await _widget(b)
        await b.js("window.__publishOK=true; __seedLib(); __PC.filesIdx().saveLocal(); __PC.switchView('music'); 1")
        await b.until("!!document.querySelector('.osw .music-app') && document.querySelectorAll('.track .track-play').length>=5")
        await _play_row(b, S1)
        await F.until_state(b, lambda s: s["appArt"]["loaded"], what="cover in the Music window")
        got["audit"] = await F.audit(b, ".osw .ma-now", nav=False)
        await F.press(b, "#ma-next")
        await F.until_state(b, lambda s: s["cur"] != S1, what="next in the window")
        await F.press(b, "#ma-prev")
        await F.until_state(b, lambda s: s["cur"] == S1 and s["art"], what="back to track 1")
        await F.press(b, "#ma-art")
        await F.until_state(b, lambda s: s["full"], what="Now playing in the window")
        got["fullAudit"] = await F.audit(b, ".osw .music-app", nav=False)
        got["fill"] = await b.js(FILL)
        await F.press(b, "#ma-play")
        await F.until_state(b, lambda s: not s["playing"], what="paused in the window")
        await F.press(b, "#ma-collapse")
        # The desk's Now-playing panel: the record wears the cover, and the artist is under the title.
        sel = '.os-wgt[data-type="music"]'
        await b.until(f"!!document.querySelector('{sel} .wgt-mart img')")
        got["wgt"] = await b.js(f"""(()=>{{const w=document.querySelector('{sel}'),i=w.querySelector('.wgt-mart img');
          return {{title:w.querySelector('.wgt-mtitle').textContent, artist:w.querySelector('.wgt-martist').textContent,
                   src:i.getAttribute('src'), loaded:i.complete&&i.naturalWidth>0, now:__PC.music().now().art}}}})()""")
        await F.press(b, "#ma-next")
        await F.until_state(b, lambda s: s["cur"] != S1, what="next")
        await b.until(f"!document.querySelector('{sel} .wgt-mart img') || document.querySelector('{sel} .wgt-mart').dataset.src!=={json.dumps(got['wgt']['src'])}")
        got["wgtAfter"] = await b.js(f"document.querySelector('{sel} .wgt-martist').textContent")
        # An untagged track: the disc wears the player's generated cover, not the empty record.
        await _settled(b)
        await F.press(b, "#ma-next")
        await F.until_state(b, lambda s: s["cur"] == S3, what="the untagged track")
        await b.until(f"document.querySelector('{sel} .wgt-mart').classList.contains('ph')")
        got["wgtPh"] = await b.js(f"getComputedStyle(document.querySelector('{sel} .wgt-mart')).backgroundImage")
        assert not await b.js("__errors"), await b.js("__errors")

    _run(check, F.LIB + F.HELD)
    assert got["audit"] == [] and got["fullAudit"] == [], got
    assert got["fill"]["display"] == "grid" and got["fill"]["w"] >= .38, \
        ("Now playing in a wide desktop window is not the side-by-side layout, or its cover is small", got["fill"])
    w = got["wgt"]
    assert w["title"] == "Neon Rain" and w["artist"] == "Kaito Vega · Night City", w
    assert w["src"] == w["now"] and w["loaded"], w
    # The generated cover stacks four gradients; the empty record it replaces is a single radial one.
    assert got["wgtPh"].count("gradient(") >= 4, ("the desk widget shows the empty record for a track", got["wgtPh"])


# ── the APK ──────────────────────────────────────────────────────────────────────────────────────────────────────
NATIVE = r"""
window.__nativeUpdates=[];
window.Capacitor={isNativePlatform:()=>true,getPlatform:()=> 'android',Plugins:{
 HomeScreen:{consumeLaunchView:async()=>({view:''}), addListener:()=>({remove(){}}), formFactor:async()=>({form:'phone'}),
   setSystemBarsHidden:async o=>o, systemBars:async()=>({hidden:false})},
 MusicControls:{addListener:()=>({remove(){}}), setOptions:async()=>({}), ack:async()=>({}),
   update:async o=>{ window.__nativeUpdates.push(o); return {}; },
   stop:async()=>({}), consumeLaunchAction:async()=>({action:''}), playBlocked:async()=>({}), status:async()=>({})}
}};"""


@CHROME
def test_the_apk_hides_the_widget_and_tells_android_the_artist():
    got = {}

    async def check(b):
        await _phone(b, 390, 844)
        await _music(b)
        await _play_row(b, S1)
        await b.until("__nativeUpdates.some(u=>u.artist==='Kaito Vega')")
        got["updates"] = await b.js("__nativeUpdates.slice(-3)")
        await b.js("__PC.switchView('notifications'); 1")
        await asyncio.sleep(1.5)
        got["state"] = await F.state(b)

    _run(check, F.PLAIN + NATIVE + F.LIB + F.HELD)
    last = got["updates"][-1]
    assert last["title"] == "Neon Rain" and last["artist"] == "Kaito Vega" and last["album"] == "Night City", got
    assert got["state"]["widgetHidden"], ("the floating widget drew over the APK", got["state"])
    assert got["state"]["playing"], got["state"]
