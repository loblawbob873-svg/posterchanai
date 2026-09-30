"""Texts shows the contact's own picture, as Contacts does.

Reported: "Contact pictures are not showing in Texts. Contacts is showing the pictures fine". Texts drew
two initials for every conversation, over the same people whose photos Contacts showed one screen
away. Driven in the REAL bundled Texts renderer at phone and desktop width, with the address book in
the device's contacts cache (what Contacts itself paints from first): a conversation with a pictured
contact shows that picture -- loaded, round, the avatar's size -- in the list and the conversation
header; a contact with no photo and an unknown number keep their initials.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_sms_image_paste_full_app import open_texts


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


PLAIN = "localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"
# A 2x2 red PNG, the way a phone writes a vCard 3.0 photo (folded base64, ENCODING=b).
PNG = "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAFklEQVR4nGP8z8DAwMDAxMDAwMDAAAANHQEDasKb6QAAAABJRU5ErkJggg=="

# The address book as the device holds it before Texts ever opens (the key test_desktop_offline_full_app
# logs in with: 32 bytes of 0x01).
OWNER = "1b84c5567b126440995d3ed5aaba0565d71e1834604819ff9c17f5e9d5dd078f"


def _card(uid, fn, tel, photo):
    lines = ["BEGIN:VCARD", "VERSION:3.0", "UID:" + uid, "FN:" + fn, "TEL;TYPE=CELL:" + tel]
    if photo:
        lines.append("PHOTO;ENCODING=b;TYPE=PNG:" + photo)
    return "\r\n".join(lines + ["END:VCARD"])


CACHE = {"books": [{"id": "b1", "name": "Contacts"}],
         "cards": {"b1": [{"uid": "alice", "ics": _card("alice", "Alice Photo", "+1 555 010 0100", PNG)},
                          {"uid": "bob", "ics": _card("bob", "Bob Plain", "+1 555 010 0177", "")}]}}
# The node's address book, answered by /api/contacts/* the way a real node answers it (the cache is
# replaced by what the server says, so the server has to hold the cards).
BOOT = PLAIN + r"""
(()=>{const CACHE=%s;const f=window.fetch;
  window.fetch=function(url,opts){const u=String(url&&url.url||url);
    const ok=o=>Promise.resolve(new Response(JSON.stringify(o),{status:200,headers:{'Content-Type':'application/json'}}));
    if(u.includes('/api/contacts/books'))return ok({books:CACHE.books});
    if(u.includes('/api/contacts/cards'))return ok({cards:CACHE.cards.b1});
    return f.apply(this,arguments);};})();
""" % json.dumps(CACHE)

SEED = r"""
(()=>{
  const S=PCSms._state(), now=Date.now();
  S.msgs.clear();
  const add=(doc,addr,body,ago)=>S.msgs.set(doc,{doc,address:addr,body,date:now-ago,incoming:true,parts:[],_at:1});
  add('ph-a','+15550100100','from alice',3000);
  add('ph-b','+15550100177','from bob',2000);
  add('ph-c','+15550199999','from a stranger',1000);
  S.open='';S.q='';
  PCSms.refreshNames();
})()
"""

ROWS = r"""[...document.querySelectorAll('.sms-thread')].map(b=>{const av=b.querySelector('.sms-av'),img=av.querySelector('img');
  const r=av.getBoundingClientRect();
  return {who:b.querySelector('.sms-who').textContent,img:!!img,loaded:!!(img&&img.complete&&img.naturalWidth>0),
          text:av.textContent.trim(),w:Math.round(r.width),h:Math.round(r.height),round:getComputedStyle(av).borderRadius,
          iw:img?Math.round(img.getBoundingClientRect().width):0}})"""


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('phone', [True, False])
def test_texts_shows_the_contacts_pictures(phone):
    got = {}

    async def check(b):
        w, h = (390, 844) if phone else (1280, 900)
        await b.call('Emulation.setDeviceMetricsOverride', {'width': w, 'height': h, 'deviceScaleFactor': 1, 'mobile': phone})
        await open_texts(b)
        assert await b.js("__PC.ME.pubkey") == OWNER
        await b.js("document.querySelector('#sms-back')&&document.querySelector('#sms-back').click()")
        await b.js(SEED)
        await b.until("document.querySelectorAll('.sms-thread').length===3")
        await b.until("[...document.querySelectorAll('.sms-av img')].every(i=>i.complete&&i.naturalWidth>0)&&document.querySelectorAll('.sms-av img').length>0")
        got['rows'] = await b.js(ROWS)
        # The conversation header carries the same picture.
        await b.js("[...document.querySelectorAll('.sms-thread')].find(b=>b.textContent.includes('from alice')).click()")
        await b.until("!!document.querySelector('.sms-head .sms-av-head img')")
        await b.until("document.querySelector('.sms-head .sms-av-head img').naturalWidth>0")
        hb = await b.js("(()=>{const r=document.querySelector('.sms-head .sms-av-head').getBoundingClientRect();return {l:r.left,r:r.right,vw:innerWidth}})()")
        got['head'] = hb
    asyncio.run(desktop.with_browser('online', '', check, BOOT))
    rows = {r['who']: r for r in got['rows']}
    alice = rows['Alice Photo']
    assert alice['img'] and alice['loaded'] and alice['text'] == '', alice
    bob = rows['Bob Plain']
    # Exactly the size of the initials beside it (the list's own avatar size at this width), filled.
    assert alice['w'] == alice['h'] == bob['w'] == bob['h'] and alice['iw'] == alice['w'] >= 24, (alice, bob)
    assert alice['round'] not in ('', '0px'), alice
    assert not rows['Bob Plain']['img'] and rows['Bob Plain']['text'] == 'BP', rows['Bob Plain']
    stranger = next(r for r in got['rows'] if r['who'] not in ('Alice Photo', 'Bob Plain'))
    assert not stranger['img'] and stranger['text'], stranger
    assert got['head']['l'] >= 0 and got['head']['r'] <= got['head']['vw'] + .5, got['head']
