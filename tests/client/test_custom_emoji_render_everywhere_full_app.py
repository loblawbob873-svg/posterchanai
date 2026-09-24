"""A fediverse account's custom emoji render in its post AND on its profile.

Reported with a real reply from pl.kitsunemimi.club: "this event did not show any custom emojis". The
post itself carries a correct NIP-30 tag; the author's bio -- `:fluffytail::fox_love::fluffytail:`,
with its own tags on the kind-0 -- was rendered with linkify() alone and showed as shortcode text.
Uses that post and that profile, verbatim, through the shipped bundle.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop

PK = "774ed0fd2d23a054781ede0068c6da8c400431d3b95ef30a94af920325ffe047"
NOTE = {"id": "97a53342935ebbf61fb1d989700fcdba649ede971cfa4d87e96406cb2b296fbd", "pubkey": PK, "kind": 1,
        "created_at": 1790260724, "sig": "", "content": "thanks :foxnoears:",
        "tags": [["emoji", "foxnoears", "https://pl.kitsunemimi.club/emoji/custom-emoji/foxnoears.png"],
                 ["proxy", "https://pl.kitsunemimi.club/objects/0cd2d5ed", "activitypub"]]}
PROFILE = {"id": "e" * 64, "pubkey": PK, "kind": 0, "created_at": 1790260000, "sig": "",
           "content": json.dumps({"name": "bronze", "about": ":fluffytail::fox_love::fluffytail:\n\nbridged"}),
           "tags": [["emoji", "fluffytail", "https://pl.kitsunemimi.club/emoji/custom-emoji/fluffytail.png"],
                    ["emoji", "fox_love", "https://pl.kitsunemimi.club/emoji/custom/fox_love.png"]]}


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_the_post_and_the_bio_draw_their_emoji():
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.until("!!window.__PC")
        await b.js("Store.saveEvent(" + json.dumps(PROFILE) + ");Store.saveProfile(" + json.dumps(PROFILE) + ");Store.saveEvent(" + json.dumps(NOTE) + ");true")
        await b.js(f"__PC.openThread('{NOTE['id']}');true")
        await b.until("[...document.querySelectorAll('img.emoji-inline')].some(i=>/foxnoears/.test(i.src))")
        got['post'] = True
        await b.js(f"(()=>{{const n=document.querySelector('.name[data-prof=\"{PK}\"]');n.click();return !!n}})()")
        await b.until("!!document.querySelector('.prof .about')")
        await asyncio.sleep(.5)
        got['bio'] = await b.js("(()=>{const a=document.querySelector('.prof .about');"
                                "return {imgs:[...a.querySelectorAll('img.emoji-inline')].map(i=>i.alt),text:a.textContent}})()")

    asyncio.run(desktop.with_browser('online', '', check, ''))
    assert got.get('post'), got
    assert got['bio']['imgs'] == [':fluffytail:', ':fox_love:', ':fluffytail:'], got
    assert ':fluffytail:' not in got['bio']['text'], "the bio still shows shortcode text: %r" % got


DUCK = "db72120fbd902e9437b0ba5296cb7c16ff440f7c8e5fed2cd1fb207e4fc6868a"
DUCK_NAME = ":catnoears: Big Duck :mrquackers2:"
DUCK_TAGS = [["emoji", "catnoears", "https://pl.kitsunemimi.club/emoji/catnoears.png"],
             ["emoji", "mrquackers2", "https://pl.kitsunemimi.club/emoji/mrquackers2.png"]]


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_mentioned_name_gets_its_emoji_when_they_arrive_after_it():
    """"it was the emojis in the name in the reply": the reply mentions somebody whose NAME is custom
    emoji. When the name was known but its NIP-30 map was not yet (a profile cached before its tags
    were), the mention was drawn as shortcode text and marked done -- so the refetch that brought the
    map in had nothing left to patch."""
    got = {}
    reply = dict(NOTE, id="f" * 64, content=f"nostr:{{npub}} thanks :foxnoears:", tags=NOTE["tags"] + [["p", DUCK]])

    async def check(b):
        await desktop.login(b)
        await b.until("!!window.__PC")
        npub = await b.js(f"NostrTools.nip19.npubEncode('{DUCK}')")
        ev = dict(reply, content=reply["content"].replace("{npub}", npub))
        bare = {"id": "d" * 64, "pubkey": DUCK, "kind": 0, "created_at": 1790000000, "sig": "", "tags": [],
                "content": json.dumps({"name": DUCK_NAME})}
        # The name first, its emoji map not yet -- then the thread is drawn.
        await b.js("Store.saveProfile(" + json.dumps(bare) + ");Store.saveEvent(" + json.dumps(ev) + ");true")
        await b.js(f"__PC.openThread('{ev['id']}');true")
        await b.until("[...document.querySelectorAll('a.mention')].some(a=>a.textContent.includes('Big Duck'))")
        got['before'] = await b.js("[...document.querySelectorAll('a.mention')].find(a=>a.textContent.includes('Big Duck')).querySelectorAll('img.emoji-inline').length")
        # The same kind-0 arrives again WITH its tags (the refetch emojiName asks for), and decorate runs.
        tagged = dict(bare, tags=DUCK_TAGS)
        await b.js("Store.saveProfile(" + json.dumps(tagged) + ");__PC.decorateProfiles();true")
        await asyncio.sleep(.3)
        got['after'] = await b.js("[...document.querySelectorAll('a.mention')].find(a=>a.textContent.includes('Big Duck')||a.querySelector('img'))"
                                  "?.querySelectorAll('img.emoji-inline').length")

    asyncio.run(desktop.with_browser('online', '', check, ''))
    assert got['before'] == 0, got
    assert got['after'] == 2, "the mention kept its :shortcode: text after the emoji arrived: %r" % got
