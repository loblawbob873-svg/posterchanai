"""Concord: an .mp3 attached in an encrypted room must arrive as something you can PLAY.

Reported 2026-10-06 on the PosterChanOS desktop: "i uploaded mp3 file in concord just now and
nothing shows". Driven in the real bundled client: the paperclip's file input gets a real MP3
(a committed ffmpeg-made fixture, audio/mpeg), the room is CORD-encrypted so the bytes are sealed and stored as
ciphertext on a Blossom fixture that serves them back, the message is SENT -- and then what a person
sees is measured: a message carrying an <audio> element whose decrypted source actually decodes
(duration > 0), and no "could not attach" / "Could not decrypt" on screen.
"""
import asyncio
import base64
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_concord_attach_survives_a_repaint import EXTRA, ROOM


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


# A Blossom server that stores whatever is PUT and serves it back by URL.
NET = EXTRA + r'''
window.__net = { puts: [], gets: 0, blobs: {} };
(()=>{ const real = window.fetch.bind(window);
  window.fetch = async (u, o) => {
    const url = String(u && u.url || u), m = (o && o.method) || 'GET';
    if(url.includes('/client/blossom-access')) return new Response(JSON.stringify({ ok:true, whitelisted:true, allowed:true }), { status:200, headers:{ 'content-type':'application/json' } });
    if(m === 'PUT' && /\/upload$/.test(url)){
      const body = new Uint8Array(await new Response(o.body).arrayBuffer());
      const id = 'f' + __net.puts.length + 'f'.repeat(63);
      __net.blobs['https://files.test/' + id] = body;
      __net.puts.push({ url, type: (o.headers || {})['Content-Type'], size: body.byteLength });
      return new Response(JSON.stringify({ url: 'https://files.test/' + id, sha256: id, size: body.byteLength }), { status:200, headers:{ 'content-type':'application/json' } });
    }
    if(url.startsWith('https://files.test/')){
      __net.gets++;
      const b = __net.blobs[url.split(/[?#]/)[0]];
      return b ? new Response(b, { status:200, headers:{ 'content-type':'application/octet-stream' } }) : new Response('gone', { status:404 });
    }
    return real(u, o);
  };
})();
'''


def _mp3(tmp_path):
    # A COMMITTED 2s sine (made once with ffmpeg: sine=440:duration=2, libmp3lame 32k) -- the CI runner
    # has no ffmpeg, and a test that skips there fails the desktop build.
    return base64.b64encode((Path(__file__).resolve().parents[1] / 'fixtures' / 'concord' / 'tone-2s.mp3').read_bytes()).decode()


# What a person sees: a player they can find and press. The first version of this test only asked
# for an <audio> that had decoded the song -- and PASSED on the broken code, because the player was
# there, loaded, 2 s long, and 0 px wide: `.cc-encrypted-attachment` is `width:fit-content` with
# `overflow:hidden`, the player's own width was `min(420px,100%)`, and a percentage width inside a
# shrink-to-fit box is cyclic, so the box shrank to nothing and clipped its only child.
# Since 2026-10-09 an audio attachment is the TRACK CARD a post uses (cover, title, artist, play, bars):
# what a person finds and presses is the card and its play button, and the <audio> inside does the
# decoding out of sight. So: the card is drawn at a usable size, its box does not clip it, the play
# button is the thing under the pointer, and the decrypted source behind it really decodes.
VISIBLE = """(()=>{const a=document.querySelector(SEL);if(!a)return null;const card=a.closest('.pc-track');
  const box=card||a,r=box.getBoundingClientRect(),h=box.parentElement.getBoundingClientRect(),
    btn=card&&card.querySelector('.pct-play'),br=btn&&btn.getBoundingClientRect(),
    hit=btn?document.elementFromPoint(br.left+br.width/2,br.top+br.height/2):null;
  return {src:a.src,ready:a.readyState,duration:a.duration,w:r.width,h:r.height,hostW:h.width,hostH:h.height,
    card:!!card,title:card?card.querySelector('.pct-title').textContent:'',hit:!!btn&&!!hit&&btn.contains(hit)};})()"""


def _assert_visible(got, what):
    assert got, what + ': no <audio> in the message at all'
    assert got['card'], (what + ': the attachment is a bare <audio>, not the track card', got)
    assert got['w'] >= 200 and got['h'] >= 24, (what + ': the player is drawn too small to see or press', got)
    assert got['hostW'] >= got['w'] - 1, (what + ': the attachment box clips its player', got)
    assert got['hit'], (what + ': something else is on top of the play button', got)
    assert got['title'] == 'song', (what + ': the track is not named after its file', got)


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('width', [1280, 900, 390])
def test_an_mp3_sent_in_an_encrypted_room_is_a_player_you_can_see(tmp_path, width):
    mp3 = _mp3(tmp_path)
    res = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": 850, "deviceScaleFactor": 1, "mobile": width < 600})
        await desktop.login(b)
        await b.js(ROOM)
        await b.until("!!document.querySelector('#cc-input') && !!document.querySelector('#cc-file')")
        if width < 600:
            # A phone shows the channel list first; open the channel like a person would.
            await b.until("!!document.querySelector('[data-cc-channel]')")
            await b.js("document.querySelector('[data-cc-channel]').click()")
            await b.until("document.querySelector('#cc-input').getBoundingClientRect().width>0")
        # The paperclip's own file input, given a real MP3.
        await b.js("""(()=>{const bytes=Uint8Array.from(atob('%s'),c=>c.charCodeAt(0));
          const dt=new DataTransfer();dt.items.add(new File([bytes],'song.mp3',{type:'audio/mpeg'}));
          const i=document.querySelector('#cc-file');i.files=dt.files;i.dispatchEvent(new Event('change',{bubbles:true}));return true;})()""" % mp3)
        await b.until("(()=>{const el=document.querySelector('#cc-input');return !!el && el.value.includes('files.test/');})()")
        await b.js("document.querySelector('#cc-send').click()")
        await b.until("!!window.__concordTags")
        res['tags'] = await b.js("JSON.stringify(window.__concordTags)")
        # The sealed bytes were fetched back, decrypted and decoded: a real 2-second song.
        await b.until("(()=>{const a=document.querySelector('.cc-message .cc-encrypted-attachment audio');"
                      "return !!a && /^blob:/.test(a.src) && a.readyState>=1 && a.duration>0;})()")
        res['audio'] = await b.js(VISIBLE.replace('SEL', "'.cc-message .cc-encrypted-attachment audio'"))
        res['screen'] = await b.js("document.body.innerText")

    asyncio.run(desktop.with_browser('online', '', check, NET))
    t = res['tags']
    assert 'm audio/mpeg' in t and 'name song.mp3' in t and 'encryption-algorithm aes-gcm' in t, t
    assert 'could not attach' not in res['screen'].lower(), res['screen'][-400:]
    assert 'Could not decrypt' not in res['screen'], res['screen'][-400:]
    _assert_visible(res['audio'], 'encrypted mp3')


# The other way in: the same song picked from Files, which attaches the drive's PLAIN blob.
LISTING = r'''(()=>{const real=window.fetch;
  window.fetch=(u,o)=>String(u).includes('/list/')
    ? Promise.resolve(new Response(JSON.stringify([
        {url:'https://files.test/song.mp3',sha256:'a'.repeat(64),size:4407,type:'audio/mpeg',uploaded:1700000000}]),
        {status:200,headers:{'content-type':'application/json'}}))
    : real(u,o);
  return true;})()'''


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_an_mp3_picked_from_files_is_a_player_you_can_see():
    res = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": 1280, "height": 850, "deviceScaleFactor": 1, "mobile": False})
        await desktop.login(b)
        await b.js(ROOM)
        await b.until("!!document.querySelector('#cc-input')")
        await b.js(LISTING)
        await b.js("document.querySelector('#cc-attach').click()")
        await b.until("!!document.querySelector('#cc-attach-blossom')")
        await b.js("document.querySelector('#cc-attach-blossom').click()")
        await b.until("!!document.querySelector('.bp-pick-card')")
        await b.js("document.querySelector('.bp-pick-card').click()")
        await b.until("(()=>{const el=document.querySelector('#cc-input');return !!el && el.value.includes('files.test/song.mp3');})()")
        await b.js("document.querySelector('#cc-send').click()")
        await b.until("!!window.__concordTags")
        res['tags'] = await b.js("JSON.stringify(window.__concordTags)")
        await b.until("!!document.querySelector('.cc-message .cc-plain-attachment audio')")
        res['audio'] = await b.js(VISIBLE.replace('SEL', "'.cc-message .cc-plain-attachment audio'"))

    asyncio.run(desktop.with_browser('online', '', check, NET))
    assert 'm audio/mpeg' in res['tags'], res['tags']
    _assert_visible(res['audio'], 'mp3 from Files')
