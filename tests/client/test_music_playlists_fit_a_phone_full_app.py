"""A PLAYLIST -- your own or one shared with you -- gives a phone screen to its songs, in the SHIPPED client.

Reported 2026-10-26, after the phone redesign of the library: "shared playlists screen is wasted like before" and
"same for regular playlists". The redesign's phone rule made the list header a flex row while an older one still
gave every header button `width:100%`, so a shared playlist drew Shuffle / Refresh / Keep / ⋯ as four full-width
bars and a two-line note ABOVE its songs: the first song started 397px down a 390x844 screen, and three spaced
cards fit under it. Your own playlist's Shuffle took a full row of its own the same way. The tests that existed
measured the Songs tab, or rendered the share header into a box OUTSIDE the music app where the rule never
applied -- which is how a test passed while the real screen did not.

Measured on the real screens, at four phone shapes (upright, small, sideways):
  * the header is the count and the actions on ONE row (a shared playlist: the count on a line above one row of
    buttons), every action a 44px target, nothing past the right edge;
  * the songs start right under the header and are rows, not spaced cards -- all 8 of a playlist and at least 9
    of a 12-song share fit a 390x844 phone;
  * a song row is play, the song and ⋯ ("play button, song name, then hamburger menu with all the other buttons"):
    a shared song's ⋯ holds Keep in my library and Save a copy (the compact row had hidden Save a copy with no ⋯
    to find it under), and Keep picked from it is pressed for real and answers;
  * the note about the sharer's copy is under the songs, not in front of them;
  * Shared with me / Shared by me: the header is one row too.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client import music_art_fixture as A
from tests.client import music_library_fixture as L
from tests.client import test_music_library_views_full_app as V
from tests.client.test_dm_newest_first_full_app import RELAY

CHROME = pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
PHONES = [(390, 844), (360, 780), (320, 640), (844, 390)]


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


# The header, its controls, and the list under it -- in SCREEN px (a calc(44px / var(--zf)) target is 44 on screen).
GEOM = r"""(()=>{const z=1, R=e=>{const r=e.getBoundingClientRect();
    return {t:r.top/z,b:r.bottom/z,l:r.left/z,r:r.right/z,h:r.height/z,w:r.width/z}};
  const lib=document.getElementById('ma-lib'), head=lib&&lib.querySelector(':scope > .music-head');
  const btns=head?[...head.querySelectorAll('.music-head-primary > button')].filter(b=>b.checkVisibility()).map(b=>({id:b.id||b.className,...R(b)})):[];
  const count=head&&head.querySelector('.music-count');
  const rows=[...lib.querySelectorAll(':scope > .track')].map(R);
  const nav=document.querySelector('.mobilenav'), nr=nav&&getComputedStyle(nav).display!=='none'?R(nav):null;
  const bar=document.querySelector('.ma-now'), br=bar&&bar.checkVisibility()?R(bar):null;
  const floor=Math.min(br?br.t:innerHeight/z, nr?nr.t:innerHeight/z);
  const note=lib.querySelector('.msh-note');
  return {vw:innerWidth/z, head:head&&R(head), btns, count:count&&{...R(count),text:count.textContent.trim()},
    rows, inView:rows.filter(r=>r.t>=0&&r.b<=floor+1).length, note:note&&R(note), errors:(window.__errors||[]).slice(-5)};})()"""


def _check_header(g, shape, buttons_min, count_own_line=False):
    head, btns = g["head"], g["btns"]
    assert head and len(btns) >= buttons_min, (shape, "the header or its buttons are missing", g)
    tops = {round(b["t"]) for b in btns}
    assert max(tops) - min(tops) <= 2, (shape, "the header's buttons are stacked, not one row", btns)
    for b in btns:
        assert b["h"] >= 43.5 and b["w"] >= 43.5, (shape, "a header action is smaller than a finger", b)
        assert b["r"] <= g["vw"] + .5 and b["l"] >= -.5, (shape, "a header action is off the screen", b)
    if count_own_line:
        assert head["h"] <= 72, (shape, "a shared playlist's header takes more than a count line and one row", head)
    else:
        assert head["h"] <= 48, (shape, "the header takes more than one row", head)
        assert abs(g["count"]["t"] + g["count"]["h"] / 2 - (btns[0]["t"] + btns[0]["h"] / 2)) <= 6, \
            (shape, "the count is not on the buttons' row", g["count"], btns)
    assert g["count"]["w"] > 20, (shape, "the count is squeezed out", g["count"])


def _check_rows(g, shape):
    rows = g["rows"]
    assert rows and rows[0]["t"] - g["head"]["b"] <= 12, (shape, "the songs do not start under the header", g["head"], rows[:1])
    for a, b in zip(rows, rows[1:]):
        assert b["t"] - a["b"] <= 2, (shape, "the songs are spaced cards, not rows", a, b)
        assert a["h"] <= 64, (shape, "a song row is taller than a row", a)


@CHROME
def test_your_own_playlist_gives_the_screen_to_its_songs():
    got = {}

    async def check(b):
        for w, h in PHONES:
            await V._screen(b, w, h)
            if not got:
                await V._music(b)
                await b.js("(async()=>{const f=__PC.filesIdx()._norm().files;"
                           "const ids=Object.entries(f).filter(([k,m])=>m.folder==='Music').map(([k,m])=>m.sha||k);"
                           "window.__pl=await PCPlaylists.create('Gym mix', ids);})(); 1")
                await V._until(b, "PCPlaylists.all().length===1 && !!document.querySelector('.ma-pl[data-pl=\"'+PCPlaylists.all()[0].id+'\"]')", 15, "the playlist chip")
                await A.press(b, ".ma-pl[data-pl='%s']" % await b.js("PCPlaylists.all()[0].id"))
                await V._until(b, "document.querySelectorAll('#ma-lib > .track').length===8 && !!document.querySelector('#ma-plbar .ma-pl.on[data-pl=\"'+PCPlaylists.all()[0].id+'\"]')", 15, "the playlist's songs")
            await b.js("document.getElementById('feed').scrollTop=0; 1")
            await asyncio.sleep(.4)
            got[(w, h)] = await b.js(GEOM)
            await V._shot(b, f"playlist-{w}x{h}")

    V._run(check, L.PLAIN + L.LIB)
    for shape, g in got.items():
        assert not g["errors"], (shape, g["errors"])
        _check_header(g, shape, 1)
        assert [b["id"] for b in g["btns"]] == ["mus-shuffle"], (shape, "Shuffle is not the header's one action", g["btns"])
        _check_rows(g, shape)
    assert got[(390, 844)]["inView"] == 8, ("an 8-song playlist does not fit a 390x844 phone", got[(390, 844)])


def _share_seed(n):
    """A REAL share: a kind-30078 from "Matthew", NIP-44-sealed to the signed-in account, on the fixture relay."""
    return r"""(()=>{
  const me=new Uint8Array(32).fill(1), mePk=NostrTools.getPublicKey(me), mat=new Uint8Array(32).fill(7), matPk=NostrTools.getPublicKey(mat);
  const now=Math.floor(Date.now()/1000), b64=u=>btoa(String.fromCharCode(...u));
  const track=i=>({s:(i.toString(16).padStart(2,'0')+'a').repeat(30).slice(0,64), k:b64(new Uint8Array(32).fill(i+3)), iv:b64(new Uint8Array(12).fill(i+5)), n:'Song '+i, m:'audio/mpeg', z:4200000});
  const body={v:1,id:'roadtrip1',name:'Road Trip',from:matPk,created:now,updated:now,tracks:[...Array(%d).keys()].map(i=>track(i+1))};
  const ck=NostrTools.nip44.getConversationKey(mat, mePk);
  const ev=NostrTools.finalizeEvent({kind:30078,created_at:now-60,content:NostrTools.nip44.encrypt(JSON.stringify(body),ck),
    tags:[['d','pcai:musicshare:roadtrip1:'+mePk.slice(0,16)],['p',mePk],['l','pcai-musicshare']]},mat);
  const prof=NostrTools.finalizeEvent({kind:0,created_at:now-999,tags:[],content:JSON.stringify({name:'Matthew'})},mat);
  const r=_rel(); r.push(ev,prof); localStorage.setItem('__relayEvents',JSON.stringify(r));
})()""" % n


async def _chip(b, label):
    sel = await b.js("(()=>{const c=[...document.querySelectorAll('#ma-plbar .ma-pl')].find(x=>x.textContent.includes(%s));"
                     "if(!c)return ''; c.id=c.id||('chip-'+Math.random().toString(36).slice(2)); return '#'+c.id})()" % json.dumps(label))
    assert sel, ("no chip", label, await b.js("[...document.querySelectorAll('#ma-plbar .ma-pl')].map(x=>x.textContent.trim())"))
    await A.press(b, sel)


@CHROME
def test_a_shared_playlist_gives_the_screen_to_its_songs_and_keeps_both_row_actions():
    got = {}

    async def check(b):
        await V._screen(b, 390, 844)
        await b.js(_share_seed(12))
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}; __PC.switchView('music'); 1")
        await b.until("!!window.PCMusicShare && !!document.getElementById('ma-plbar')")
        await b.js("(async()=>{ await PCMusicShare.loadIn(); })(); 1")
        await V._until(b, "[...document.querySelectorAll('#ma-plbar .ma-pl')].some(x=>/Shared with me\\s*1/.test(x.textContent))", 20, "the Shared with me chip")
        await _chip(b, "Shared with me")
        await V._until(b, "!!document.querySelector('#ma-lib .msh-offer')", 15, "the offer")
        got["in"] = await b.js(GEOM)
        await V._shot(b, "shared-in")
        await A.press(b, "#ma-lib .msh-offer .msh-yes")
        await V._until(b, "document.querySelectorAll('#ma-lib > .track.msh-track').length===12", 20, "the shared playlist's songs")
        for w, h in PHONES:
            await V._screen(b, w, h)
            await b.js("document.getElementById('feed').scrollTop=0; 1")
            await asyncio.sleep(.4)
            g = await b.js(GEOM)
            # A shared song's row is play, the song and ⋯ -- the ⋯ itself a finger-sized target on the row.
            g["rowButtons"] = await b.js("[...document.querySelector('#ma-lib > .track.msh-track').querySelectorAll('button')].filter(e=>e.checkVisibility()).map(e=>e.className)")
            g["acts"] = await b.js(r"""(()=>{const t=document.querySelector('#ma-lib > .track.msh-track'), tr=t.getBoundingClientRect(), e=t.querySelector('.track-more');
              const r=e.getBoundingClientRect(), h=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);
              return [{s:'.track-more', vis:e.checkVisibility(), w:r.width, h:r.height, inRow:r.top>=tr.top-1&&r.bottom<=tr.bottom+1&&r.right<=tr.right+1, hit:!!(h&&h.closest('.track-more'))}]})()""")
            got[(w, h)] = g
            await V._shot(b, f"shared-{w}x{h}")
            if not g["acts"][0]["vis"]:
                return        # the assertions below say which
        if not got[(390, 844)]["acts"][0]["vis"]:
            return
        await V._screen(b, 390, 844)
        await asyncio.sleep(.3)
        first = await b.js("document.querySelector('#ma-lib > .track.msh-track').dataset.sha")
        await A.press(b, f"#ma-lib > .track.msh-track[data-sha='{first}'] .track-more")
        await V._until(b, "!!document.querySelector('.menu-pop')", 10, "the shared song's menu")
        got["menu"] = await b.js("[...document.querySelectorAll('.menu-pop button')].map(e=>e.textContent.trim())")
        await b.js("[...document.querySelectorAll('.menu-pop button')].find(e=>e.textContent.trim()==='Keep in my library').id='pick-keep'; 1")
        await A.press(b, "#pick-keep")
        # Keeping copies the sharer's bytes (not served by this fixture), so the proof that the press reached Keep is
        # Keep's own answer -- "kept 1 song" or "not kept — <why>" -- rather than nothing at all.
        await V._until(b, "/\\b(kept 1 song|not kept —)/.test(document.body.innerText)", 20, "Keep answering the press")
        await _chip(b, "Shared by me")
        await V._until(b, "!!document.querySelector('#ma-lib > .music-head #msh-refresh')", 15, "Shared by me")
        got["out"] = await b.js(GEOM)

    asyncio.run(desktop.with_browser("online", "", check, extra_init=RELAY))
    for view in [v for v in ("in", "out") if v in got]:
        assert not got[view]["errors"], (view, got[view]["errors"])
        _check_header(got[view], view, 1)
    for shape in [p for p in PHONES if p in got]:
        g = got[shape]
        assert not g["errors"], (shape, g["errors"])
        _check_header(g, shape, 4, count_own_line=True)
        _check_rows(g, shape)
        for a in g["acts"]:
            assert a["vis"] and a["inRow"] and a["hit"] and a["w"] >= 43.5 and a["h"] >= 43.5, \
                (shape, "a shared song's ⋯ is hidden, off its row or covered", a)
        assert g["rowButtons"] == ["track-play", "track-more"], (shape, "a shared song's row shows more than play and ⋯", g["rowButtons"])
        assert g["note"] and g["note"]["t"] >= g["rows"][-1]["b"] - 1, (shape, "the sharer's-copy note is in front of the songs", g["note"])
    assert got.get("menu") == ["Keep in my library", "Save a copy"], ("⋯ is not the shared song's actions", got.get("menu"))
    assert got[(390, 844)]["inView"] >= 9, ("fewer than 9 of a 12-song share fit a 390x844 phone", got[(390, 844)]["inView"])


@CHROME
def test_accepting_a_share_while_shared_with_me_is_still_reading_keeps_the_playlist_on_screen():
    """Shared with me paints what it has, reads the relays, then paints again -- and that second paint went over
    whatever the person had moved to. Accept opens the accepted playlist, so a read still out at that moment put
    Shared with me back: the playlist's chip selected and no songs (measured: 12 rows, then 200 ms later none). The
    relay is slowed here so the read is certainly still out when Accept is pressed."""
    got = {}

    async def check(b):
        await V._screen(b, 390, 844)
        await b.js(_share_seed(12))
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}; __PC.switchView('music'); 1")
        await b.until("!!window.PCMusicShare && !!document.getElementById('ma-plbar')")
        await b.js("(async()=>{ await PCMusicShare.loadIn(); })(); 1")
        await V._until(b, "[...document.querySelectorAll('#ma-plbar .ma-pl')].some(x=>/Shared with me\\s*1/.test(x.textContent))", 20, "the Shared with me chip")
        await b.js("window.__RELAY_MS=2500; 1")          # every relay answer from here on takes 2.5 s
        await _chip(b, "Shared with me")
        await V._until(b, "!!document.querySelector('#ma-lib .msh-offer .msh-yes')", 15, "the offer")
        await A.press(b, "#ma-lib .msh-offer .msh-yes")
        await V._until(b, "document.querySelectorAll('#ma-lib > .track.msh-track').length===12", 20, "the shared playlist's songs")
        await asyncio.sleep(4)                            # the slowed read lands in here
        got["after"] = await b.js("({chip:(document.querySelector('#ma-plbar .ma-pl.on')||{}).textContent||'',"
                                  "rows:document.querySelectorAll('#ma-lib > .track.msh-track').length,"
                                  "offer:!!document.querySelector('#ma-lib .msh-offer'), errors:(window.__errors||[]).slice(-5)})")

    asyncio.run(desktop.with_browser("online", "", check, extra_init=RELAY))
    a = got["after"]
    assert not a["errors"], a
    assert "Road Trip" in a["chip"] and a["rows"] == 12 and not a["offer"], \
        ("a late Shared with me read painted over the accepted playlist", a)
