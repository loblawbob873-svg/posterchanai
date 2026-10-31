"""POSTERCHAN IS AN APP OTHER NOSTR APPS CAN NAME: A NIP-89 ANNOUNCEMENT AND A `client` TAG THAT POINTS AT IT.

"don't we use nip-89 to show posterchan" / "I want us to appear on https://radarnostr.vercel.app/" (2026-10-30). Radar
counts the `client` tag on events it sees; other clients turn the tag's 31990 address into the app's name and picture.
Before: every event said `["client","PosterChan AI"]` with no address and no announcement behind it.
"""
import asyncio
import json

from app.services import app_handler, emoji_sets
from app.services.nostr import bip340

SK = bytes.fromhex('11' * 32)
PK = bip340.pubkey_from_seckey(SK).hex()


def _node(monkeypatch, base='https://poster.place', key=True):
    monkeypatch.setattr(app_handler.ap_config, 'base_url', lambda: base)
    monkeypatch.setattr(emoji_sets.ap_config, 'base_url', lambda: base)
    monkeypatch.setattr(emoji_sets, '_operator', lambda: (SK, PK) if key else (None, ''))
    store = {}
    monkeypatch.setattr(app_handler.settings_store, 'get', lambda k, d='': store.get(k, d))
    monkeypatch.setattr(app_handler.settings_store, 'put', lambda k, v: store.__setitem__(k, v))
    sent = []

    async def pub(ev):
        sent.append(ev)
        return True
    monkeypatch.setattr(emoji_sets, '_publish_local', pub)
    return sent


def test_the_client_tag_names_posterchan_and_points_at_the_announcement(monkeypatch):
    _node(monkeypatch)
    assert app_handler.client_tag() == ['client', 'PosterChan', f'31990:{PK}:posterchan', 'wss://poster.place/relay']


def test_with_no_operator_key_the_tag_is_the_name_alone(monkeypatch):
    _node(monkeypatch, key=False)
    assert app_handler.client_tag() == ['client', 'PosterChan']


def test_the_announcement_is_a_valid_31990_and_is_published_once(monkeypatch):
    sent = _node(monkeypatch)
    r = asyncio.run(app_handler.publish())
    assert r['published'] and len(sent) == 1
    ev = sent[0]
    assert ev['kind'] == 31990 and ev['pubkey'] == PK
    assert ['d', 'posterchan'] in ev['tags'] and ['k', '1'] in ev['tags']
    assert ['web', 'https://poster.place/<bech32>', 'nevent'] in ev['tags']
    meta = json.loads(ev['content'])
    assert meta['name'] == 'PosterChan' and meta['picture'].startswith('https://poster.place/')
    # the address in the client tag is exactly this event's coordinate
    assert app_handler.client_tag()[2] == f"{ev['kind']}:{ev['pubkey']}:posterchan"
    assert not asyncio.run(app_handler.publish())['published'], 'an unchanged announcement must not be republished'
    assert len(sent) == 1


def test_the_client_uses_the_instance_tag_and_never_the_old_name():
    src = open('static/js/client/app.js').read()
    assert "tags.push(['client','PosterChan AI'])" not in src
    assert 'tags.push(_clientTag())' in src and 'CFG.client_tag' in src
    assert "'client','PosterChan AI']" not in open('static/js/client/streams.js').read()


def test_config_hands_the_tag_to_the_client():
    assert '"client_tag": app_handler.client_tag()' in open('app/routers/client.py').read()
