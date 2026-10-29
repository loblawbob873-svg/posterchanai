"""The instance's emoji packs as Nostr emoji sets (NIP-51 kind 30030), so any Nostr client can add them.

"make our custom emojis as packs that work on nostr clients? our custom work on ditto and posterchan and the
fediverse fine but we are missing the nostr side for packs" (2026-10-28). The shipped builder, the shipped naddr
encoder (decoded by the client's own NostrTools under node), and publish_all against a capturing relay with REAL
signatures verified.
"""
import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from app.services import emoji_sets
from app.services.nostr import bip340

ROOT = Path(__file__).resolve().parents[1]
SK = bytes.fromhex("11" * 32)
PK = bip340.pubkey_from_seckey(SK).hex()


def _e(pack, sc, ext=".png"):
    return {"pack": pack, "shortcode": sc, "ext": ext}


def test_a_set_per_pack_with_absolute_urls_and_nip30_shortcodes():
    sets = emoji_sets.build_sets([_e("blobcat", "blobcat-happy"), _e("blobcat", "blobcat_sad", ".gif"),
                                  _e("_", "posterchan")], "https://poster.place/")
    by_d = {s["d"]: s for s in sets}
    assert set(by_d) == {"pcai-emoji-blobcat", "pcai-emoji-default"}, by_d.keys()
    cat = by_d["pcai-emoji-blobcat"]
    emojis = [t for t in cat["tags"] if t[0] == "emoji"]
    # NIP-30: letters, digits and _ only -- the URL keeps the real file name
    assert ["emoji", "blobcat_happy", "https://poster.place/client/emoji/blobcat/blobcat-happy.png"] in emojis
    assert ["emoji", "blobcat_sad", "https://poster.place/client/emoji/blobcat/blobcat_sad.gif"] in emojis
    assert ["d", "pcai-emoji-blobcat"] in cat["tags"] and ["title", "blobcat"] in cat["tags"]
    assert any(t[0] == "image" and t[1].startswith("https://") for t in cat["tags"])
    assert by_d["pcai-emoji-default"]["title"] == "PosterChan"


def test_two_names_that_map_to_one_shortcode_keep_one():
    sets = emoji_sets.build_sets([_e("p", "a-b"), _e("p", "a_b")], "https://x")
    codes = [t[1] for t in sets[0]["tags"] if t[0] == "emoji"]
    assert codes == ["a_b"], codes


def test_a_big_pack_is_split_so_relays_accept_it():
    sets = emoji_sets.build_sets([_e("huge", "e%04d" % i) for i in range(emoji_sets.PER_SET * 2 + 5)], "https://x")
    assert [s["d"] for s in sets] == ["pcai-emoji-huge", "pcai-emoji-huge-2", "pcai-emoji-huge-3"]
    assert all(sum(1 for t in s["tags"] if t[0] == "emoji") <= emoji_sets.PER_SET for s in sets)
    assert sets[0]["title"] == "huge (1/3)"
    ev_bytes = max(len(json.dumps(s["tags"])) for s in sets)
    assert ev_bytes < 60_000, ev_bytes


def test_the_naddr_decodes_in_the_client_to_the_same_coordinate():
    na = emoji_sets.naddr(30030, PK, "pcai-emoji-blobcat", ["wss://poster.place/relay"])
    js = ("globalThis.window=globalThis;require('vm').runInThisContext(require('fs').readFileSync("
          "'static/vendor/nostr/nostr.bundle.js','utf8'));const d=NostrTools.nip19.decode(%s);"
          "console.log(JSON.stringify(d))" % json.dumps(na))
    out = subprocess.run(["node", "-e", js], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    d = json.loads(out.stdout)
    assert d["type"] == "naddr", d
    assert d["data"] == {"identifier": "pcai-emoji-blobcat", "pubkey": PK, "kind": 30030,
                         "relays": ["wss://poster.place/relay"]}, d


class _Env:
    def __init__(self, monkeypatch, entries):
        self.sent, self.store, self.ok = [], {}, True
        monkeypatch.setattr(emoji_sets.ap_config, "base_url", lambda: "https://poster.place")
        monkeypatch.setattr(emoji_sets, "_operator", lambda: (SK, PK))
        monkeypatch.setattr(emoji_sets.emoji_service, "index", lambda *a, **k: list(self.entries))
        monkeypatch.setattr(emoji_sets.settings_store, "get", lambda k, d=None: self.store.get(k, d))
        monkeypatch.setattr(emoji_sets.settings_store, "put", lambda k, v, **kw: self.store.__setitem__(k, v))
        self.entries = entries

        async def pub(ev):
            self.sent.append(ev)
            return self.ok
        monkeypatch.setattr(emoji_sets, "_publish_local", pub)


def _verify(ev):
    from app.services.nostr.event import _canonical
    import hashlib
    eid = hashlib.sha256(_canonical(ev["pubkey"], ev["created_at"], ev["kind"], ev["tags"], ev["content"])).hexdigest()
    return eid == ev["id"] and bip340.verify(bytes.fromhex(ev["id"]), bytes.fromhex(ev["pubkey"]), bytes.fromhex(ev["sig"]))


def test_publish_signs_each_set_and_skips_when_nothing_changed(monkeypatch):
    env = _Env(monkeypatch, [_e("cats", "cat"), _e("dogs", "dog")])
    r = asyncio.run(emoji_sets.publish_all())
    assert r["published"] == 2 and len(env.sent) == 2, r
    assert all(ev["kind"] == 30030 and ev["pubkey"] == PK and _verify(ev) for ev in env.sent)
    env.sent.clear()
    assert asyncio.run(emoji_sets.publish_all())["skipped"] == "unchanged" and env.sent == []


def test_a_removed_pack_is_withdrawn_not_left_pointing_at_dead_images(monkeypatch):
    env = _Env(monkeypatch, [_e("cats", "cat"), _e("dogs", "dog")])
    asyncio.run(emoji_sets.publish_all())
    env.sent.clear()
    env.entries = [_e("cats", "cat")]
    r = asyncio.run(emoji_sets.publish_all())
    assert r["withdrawn"] == 1, r
    empty = [ev for ev in env.sent if ev["kind"] == 30030 and ["d", "pcai-emoji-dogs"] in ev["tags"]]
    assert empty and not any(t[0] == "emoji" for t in empty[0]["tags"]), env.sent
    dels = [ev for ev in env.sent if ev["kind"] == 5]
    assert dels and ["a", "30030:%s:pcai-emoji-dogs" % PK] in dels[0]["tags"], dels


def test_a_publish_the_relay_refused_is_tried_again(monkeypatch):
    env = _Env(monkeypatch, [_e("cats", "cat")])
    env.ok = False
    asyncio.run(emoji_sets.publish_all())
    env.ok, env.sent = True, []
    assert asyncio.run(emoji_sets.publish_all())["published"] == 1, "a refused publish was remembered as done"


def test_no_domain_or_no_operator_key_publishes_nothing(monkeypatch):
    env = _Env(monkeypatch, [_e("cats", "cat")])
    monkeypatch.setattr(emoji_sets.ap_config, "base_url", lambda: "")
    assert asyncio.run(emoji_sets.publish_all())["published"] == 0 and env.sent == []


def test_every_admin_change_republishes():
    src = (ROOT / "app/routers/admin_emoji.py").read_text()
    for route in ("upload_emoji", "rename_emoji", "delete_emoji", "create_pack", "delete_pack"):
        body = src[src.index("def " + route):]
        body = body[:body.index("\n@router") if "\n@router" in body else len(body)]
        assert "emoji_sets.schedule_publish()" in body, route


@pytest.fixture(autouse=True)
def _no_pending():
    yield
    emoji_sets._pending = None
