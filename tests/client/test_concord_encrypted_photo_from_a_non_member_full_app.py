"""Concord: somebody this node does not store files for can still post a photo in an encrypted room.

Reported for the Lounge's owner: "I have tried and failed 3 times to add a photo to the politics chat".
He is not allowed on this node's Blossom, so the client fell back to nostr.build -- and an encrypted
room's attachment is ciphertext, which nostr.build (NIP-96, media only) refuses. Nothing reached this
node's log. Vector (Armada's core) sends encrypted attachments to an ordered list of Blossom servers
that store any bytes; so does this now. Driven in the real client: the paperclip's file input, a
room with CORD encryption, this node answering "not allowed", nostr.build refusing, and the first
Vector default accepting -- then the message is SENT and carries the encrypted imeta for that server.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_concord_attach_survives_a_repaint import EXTRA, ROOM


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


# The network as Josephus met it, recorded: this node says no, nostr.build refuses opaque bytes,
# blossom.ditto.pub stores anything.
NET = EXTRA + r'''
window.__net = { puts: [], nip96: 0 };
(()=>{ const real = window.fetch.bind(window);
  window.fetch = (u, o) => {
    const url = String(u && u.url || u), m = (o && o.method) || 'GET';
    if(url.includes('/client/blossom-access')) return Promise.resolve(new Response(JSON.stringify({ ok:true, whitelisted:false, allowed:false }), { status:200, headers:{ 'content-type':'application/json' } }));
    if(/nostr\.build/.test(url)){ __net.nip96++; return Promise.resolve(new Response('unsupported media type', { status:415 })); }
    if(m === 'PUT' && /\/upload$/.test(url)){
      const base = url.replace(/\/upload$/, '');
      __net.puts.push({ base, type: (o.headers || {})['Content-Type'] });
      if(base === 'https://blossom.ditto.pub') return Promise.resolve(new Response(JSON.stringify({ url: base + '/' + 'e'.repeat(64) }), { status:200, headers:{ 'content-type':'application/json' } }));
      return Promise.resolve(new Response('no', { status:403 }));
    }
    return real(u, o);
  };
})();
'''


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_non_member_posts_a_photo_in_an_encrypted_room():
    res = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": 1280, "height": 850, "deviceScaleFactor": 1, "mobile": False})
        await desktop.login(b)
        await b.js(ROOM)
        await b.until("!!document.querySelector('#cc-input') && !!document.querySelector('#cc-file')")
        # A photo picked from the device.
        await b.js("""(()=>{const png=Uint8Array.from(atob('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=='),c=>c.charCodeAt(0));
          const dt=new DataTransfer();dt.items.add(new File([png],'protest.png',{type:'image/png'}));
          const i=document.querySelector('#cc-file');i.files=dt.files;i.dispatchEvent(new Event('change',{bubbles:true}));return true;})()""")
        await b.until("(()=>{const el=document.querySelector('#cc-input');return !!el && el.value.includes('blossom.ditto.pub');})()")
        res["net"] = await b.js("JSON.parse(JSON.stringify(__net))")
        res["toasts"] = await b.js("[...document.querySelectorAll('.toast,.pc-toast')].map(t=>t.textContent).join(' | ')")
        await b.js("document.querySelector('#cc-send').click()")
        await b.until("!!window.__concordTags")
        res["tags"] = await b.js("JSON.stringify(window.__concordTags)")

    asyncio.run(desktop.with_browser('online', '', check, NET))
    net = res["net"]
    assert net["nip96"] == 0, ("an encrypted attachment was sent to nostr.build", net)
    assert net["puts"] and net["puts"][-1]["base"] == "https://blossom.ditto.pub", ("not stored on a Blossom server that takes any bytes", net)
    assert all(p["type"] == "application/octet-stream" for p in net["puts"]), ("plaintext left the device", net)
    t = res["tags"]
    assert "imeta" in t and "blossom.ditto.pub" in t and "encryption-algorithm aes-gcm" in t, ("the message does not carry the encrypted attachment", t)
    assert "m image/png" in t and "name protest.png" in t, t
