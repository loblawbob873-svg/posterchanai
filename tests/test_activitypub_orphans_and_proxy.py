"""Federation that behaves like Mastodon/Pleroma when the other server is having a bad day.

Reported: https://poster.place/nevent1qqs08frfs… — "i don't see the conversation". A reply from
shitposter.world to a post on parcero.casa, stored while parcero.casa was down: the inbox tried the
parent once, failed, kept only an `r` link, and NOTHING asked again (44 such failures in 3 hours in
the log). Now the orphan is queued, retried with backoff, and re-threaded when the parent arrives; the
client says where the thread is meanwhile.

And "since we own the fediverse software, make federation like we do for nostr, tor1, tor2, fall back
to direct — with our proxy": ActivityPub now leaves through the proxy's fallback listener, keeping the
private-address guard on BOTH sides of it (the listener's direct leg is what a hostile keyId would
otherwise turn into a request into our own network).
"""
import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from tests.test_activitypub import ALICE, REMOTE, _create, run, world  # noqa: F401  (fixture)
from app.models import FediBridgeDelivered
from app.services import settings_store
from app.services.activitypub import config, inbox, remote

ROOT = Path(__file__).resolve().parents[1]
PARENT = "https://mastodon.example/notes/1"
CHILD = "https://mastodon.example/notes/2"


def _follow(world):
    world["docs"][f"pcai:ap:following:{ALICE}:x"] = {"actor": REMOTE, "inbox": "i", "state": "accepted"}


def _orphan_docs(world):
    return {k: v for k, v in world["docs"].items() if k.startswith("pcai:ap:orphan:") and not v.get("done")}


def _stored(world, uri):
    row = world["Session"]().query(FediBridgeDelivered).filter(FediBridgeDelivered.note_uri == uri).one()
    return world["relay"][row.nostr_event_id]


# ------------------------------------------------------------------------------------ orphan replies

def test_a_reply_whose_parent_is_unreachable_is_queued_and_rethreaded_when_it_arrives(world):
    _follow(world)
    assert run(inbox.process(_create(note_id=CHILD, content="<p>so on point</p>", extra={"inReplyTo": PARENT}), REMOTE)) == "stored"
    first = _stored(world, CHILD)
    assert ["r", PARENT] in first["tags"] and not any(t[0] == "e" for t in first["tags"])
    (orphan,) = _orphan_docs(world).values()
    assert orphan["uri"] == CHILD and orphan["reply_to"] == PARENT and orphan["tries"] == 0

    # Still down at the first retry: it waits longer, it does not give up.
    out = run(inbox.relink_orphans(now=orphan["next"]))
    assert out == {"relinked": 0, "waiting": 1, "given_up": 0}
    (orphan2,) = _orphan_docs(world).values()
    assert orphan2["tries"] == 1 and orphan2["next"] > orphan["next"]

    # The parent's server is back.
    world["objects"][PARENT] = {"id": PARENT, "type": "Note", "attributedTo": REMOTE, "content": "<p>fedi people are</p>",
                                "published": "2026-09-23T09:00:00Z", "to": [config.PUBLIC], "cc": []}
    out = run(inbox.relink_orphans(now=orphan2["next"]))
    assert out["relinked"] == 1
    parent_ev = _stored(world, PARENT)
    threaded = _stored(world, CHILD)
    assert threaded["id"] != first["id"], "the reply was not re-issued"
    assert ["e", parent_ev["id"], "", "reply"] in threaded["tags"], "the re-issued reply is still outside its thread"
    assert threaded["content"] == first["content"]
    # The orphaned copy is deleted (kind 5 by the same puppet), and only AFTER the new one exists.
    assert any(e["kind"] == 5 and ["e", first["id"]] in [t[:2] for t in e["tags"]] and e["pubkey"] == first["pubkey"]
               for e in world["relay"].values()), "the orphaned copy was left behind as a duplicate"
    assert not _orphan_docs(world)


def test_an_orphan_is_given_up_after_a_week_and_a_deleted_reply_is_forgotten(world):
    _follow(world)
    run(inbox.process(_create(note_id=CHILD, extra={"inReplyTo": PARENT}), REMOTE))
    (orphan,) = _orphan_docs(world).values()
    assert run(inbox.relink_orphans(now=orphan["first"] + inbox.ORPHAN_GIVE_UP + 1))["given_up"] == 1
    assert not _orphan_docs(world)
    # A queued reply that was deleted meanwhile is dropped, not resurrected.
    run(inbox.process(_create(note_id=CHILD + "b", extra={"inReplyTo": PARENT}), REMOTE))
    s = world["Session"](); s.query(FediBridgeDelivered).filter(FediBridgeDelivered.note_uri == CHILD + "b").delete(); s.commit()
    (o,) = _orphan_docs(world).values()
    world["objects"][PARENT] = {"id": PARENT, "type": "Note", "attributedTo": REMOTE, "content": "x",
                                "to": [config.PUBLIC], "cc": []}
    assert run(inbox.relink_orphans(now=o["next"]))["relinked"] == 0
    assert not _orphan_docs(world)


def test_an_unreadable_queue_is_never_read_as_empty(world, monkeypatch):
    async def boom():
        raise RuntimeError("relay unreachable")
    monkeypatch.setattr(inbox.state, "orphans", boom)
    assert run(inbox.relink_orphans()) == {"relinked": 0, "waiting": 0, "given_up": 0}


def test_a_reply_whose_parent_is_here_is_not_queued(world):
    _follow(world)
    world["objects"][PARENT] = {"id": PARENT, "type": "Note", "attributedTo": REMOTE, "content": "p",
                                "to": [config.PUBLIC], "cc": []}
    assert run(inbox.process(_create(note_id=CHILD, extra={"inReplyTo": PARENT}), REMOTE)) == "stored"
    assert not _orphan_docs(world)


@pytest.mark.skipif(not shutil.which("node"), reason="node required")
def test_the_client_says_where_the_thread_is():
    js = r"""
const fs=require('fs'),vm=require('vm');const code=fs.readFileSync('static/js/client/cards.js','utf8');
const lift=n=>{const i=code.indexOf('  function '+n+'(');const j=code.indexOf('\n  }\n',i)+4;return code.slice(i,j);};
const escape=s=>String(s).replace(/[&<>"']/g,c=>'&#'+c.charCodeAt(0)+';');
vm.runInThisContext('var enc='+escape.toString()+';var replyParentId=()=>null;'+lift('fediParentLink')+lift('replyContextHtml'));
const fedi={kind:1,tags:[['r','https://parcero.casa/objects/99a7'],['proxy','https://shitposter.world/objects/8c','activitypub']]};
const plain={kind:1,tags:[['r','https://example.com/article']]};
process.stdout.write(JSON.stringify({fedi:replyContextHtml(fedi),plain:replyContextHtml(plain),
  js:replyContextHtml({kind:1,tags:[['r','javascript:alert(1)'],['proxy','x','activitypub']]})}));
"""
    out = json.loads(subprocess.run(["node", "-e", js], cwd=ROOT, capture_output=True, text=True, timeout=30, check=True).stdout)
    assert "reply to a post on parcero.casa" in out["fedi"] and 'href="https://parcero.casa/objects/99a7"' in out["fedi"]
    assert out["plain"] == "", "an ordinary link on a Nostr note was read as a reply"
    assert out["js"] == "", "a non-https link became an anchor"


# ------------------------------------------------------------------------------------ via the proxy

class _Fake(httpx.AsyncBaseTransport):
    def __init__(self, fail=None):
        self.calls, self.fail = [], fail

    async def handle_async_request(self, request):
        self.calls.append((request.url.host, dict(request.extensions.get("timeout") or {})))
        if self.fail:
            raise self.fail
        return httpx.Response(200, json={"ok": True})


def _transport(monkeypatch, proxy_fail=None, lan=()):
    t = remote._ProxyFirstTransport("http://127.0.0.1:8119")
    t._proxy, t._direct = _Fake(proxy_fail), _Fake()

    async def judge(host, port):
        if host.startswith("evil"):
            raise httpx.ConnectError(f"{host} resolves to a private address")
    monkeypatch.setattr(remote, "_judge", judge)
    monkeypatch.setattr(config, "lan_trusted", lambda h: h in lan)
    return t


async def _get(t, url):
    async with httpx.AsyncClient(transport=t, timeout=httpx.Timeout(12.0, connect=6.0)) as c:
        return await c.get(url)


def test_federation_goes_out_through_the_proxy(monkeypatch):
    t = _transport(monkeypatch)
    assert asyncio.run(_get(t, "https://mastodon.example/users/carol")).status_code == 200
    assert [h for h, _ in t._proxy.calls] == ["mastodon.example"] and not t._direct.calls
    assert t._proxy.calls[0][1]["connect"] >= 25, "a Tor circuit gets the connect time a direct dial gets"


def test_no_proxy_running_falls_back_to_the_checked_direct_connection(monkeypatch):
    t = _transport(monkeypatch, proxy_fail=httpx.ConnectError("connection refused"))
    assert asyncio.run(_get(t, "https://mastodon.example/x")).status_code == 200
    assert t._direct.calls, "a node without the proxy stopped federating"


def test_a_private_address_is_refused_before_the_proxy_is_asked(monkeypatch):
    t = _transport(monkeypatch)
    with pytest.raises(httpx.ConnectError):
        asyncio.run(_get(t, "https://evil.example/actor"))
    assert not t._proxy.calls and not t._direct.calls


def test_a_lan_neighbour_goes_direct(monkeypatch):
    t = _transport(monkeypatch, lan=("pleroma.lan",))
    asyncio.run(_get(t, "https://pleroma.lan/x"))
    assert t._direct.calls and not t._proxy.calls, "Tor cannot reach a LAN"


def test_the_switch_and_the_port(monkeypatch):
    values = {"proxy_fallback_port": "8119"}
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: values.get(k, d))
    monkeypatch.setattr(settings_store, "get_int", lambda k, d=0: int(values.get(k, d)))
    assert remote._proxy_url() == "http://127.0.0.1:8119", "blank must read as ON"
    values["activitypub_via_proxy"] = "false"
    assert remote._proxy_url() == ""
    t = remote._ProxyFirstTransport(remote._proxy_url())
    assert t._proxy is None


def test_the_proxys_direct_leg_refuses_private_addresses():
    from app.services.http_proxy_service import _public_address
    for host in ("127.0.0.1", "localhost", "10.0.0.5", "192.168.0.85", "169.254.169.254", "100.64.1.1"):
        with pytest.raises(Exception):
            asyncio.run(_public_address(host, 443))
    assert asyncio.run(_public_address("8.8.8.8", 443)) == "8.8.8.8"
