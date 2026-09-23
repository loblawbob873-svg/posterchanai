"""The ActivityPub server (app/services/activitypub) -- stored as Nostr events, compatible with the
Pleroma bridge in both directions.

Runs the SHIPPED code; the boundaries are fixtures: the relay (publish/query and the operator's
documents), remote servers (actor/key fetches and deliveries), and the database (in-memory SQLite
with the bridge's real tables). What is checked, by section:

  1. HTTP Signatures -- a signed request verifies, and every tampering fails.
  2. Translation -- Nostr → Note (escaping, mentions, hashtags, media, CW, reply) and Note → Nostr.
  3. One identity -- a fediverse account gets the SAME puppet key whether it arrives through
     ActivityPub or through the Pleroma timeline bridge.
  4. The inbox -- the relevance gate, cross-path dedup, public-only, replies to our posts, likes,
     follows (with a signed Accept), deletions only by the author.
  5. The outbox -- what a member's events turn into and who they go to; mirrors are never re-sent;
     a member on a linked Pleroma account is not sent twice.
  6. The Pleroma bridge -- does not mirror our own users back, and threads a reply under their post.
  7. HTTP -- WebFinger, the actor, objects, and the inbox's signature checks; 404 while off.
  8. Admin -- the two settings hydrate.
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base, FediBridgeDelivered, FediPuppet
from app.services import settings_store
from app.services.activitypub import actors, config, convert, httpsig, inbox, outbox, remote, state
from app.services import fedi_bridge_identity as ident

DOMAIN = "poster.test"
BASE = f"https://{DOMAIN}"
ALICE = "a1" * 32          # a member, name "alice"
BOB = "b2" * 32            # a member on a linked Pleroma account
REMOTE = "https://mastodon.example/users/carol"
KEY = httpsig.new_keypair()
CAROL_KEY = httpsig.new_keypair()
# The REAL functions, captured before any fixture stubs them (section 9 tests them directly).
REAL_PUBLIC_KEY = remote.public_key
REAL_ACTOR = remote.actor


def carol_actor():
    return {"id": REMOTE, "type": "Person", "preferredUsername": "carol", "name": "Carol",
            "inbox": REMOTE + "/inbox", "endpoints": {"sharedInbox": "https://mastodon.example/inbox"},
            "summary": "<p>hi</p>", "icon": {"type": "Image", "url": "https://mastodon.example/c.png"},
            "publicKey": {"id": REMOTE + "#main-key", "owner": REMOTE, "publicKeyPem": CAROL_KEY[1]}}


@pytest.fixture
def world(monkeypatch):
    """Settings, relay documents, published events, remote servers and the DB, all in memory."""
    settings = {"activitypub_enabled": "true", "activitypub_domain": DOMAIN,
                "nostr_relay_nip05_names": f"alice {ALICE}\nbob {BOB}",
                "nostr_relay_blocked_relays": "blocked.example", "fedi_bridge_broadcast": "false"}
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: settings.get(k, d))
    monkeypatch.setattr(settings_store, "get_bool",
                        lambda k, d=False: str(settings.get(k, d)).lower() in ("1", "true", "yes", "on"))
    monkeypatch.setattr(settings_store, "_port", lambda db=None: 1)
    monkeypatch.setattr(settings_store, "_operator_seckey", lambda db=None: b"\x01" * 32)
    monkeypatch.setattr(ident, "_secret", lambda: b"bridge-secret-for-tests")
    actors._names_cache["raw"] = None
    state._key_cache.clear()
    state.forget_followed_cache()
    state._followed_cache["map"] = {}
    remote._actors.clear()
    ident._PUPPET_CACHE.clear()           # process-wide in the bridge; each test has a fresh database
    outbox._seen.clear()
    outbox._retries.clear()

    docs = {}

    async def get_doc(port, d, **kw):
        return json.loads(json.dumps(docs[d])) if d in docs else None

    async def put_doc(port, sk, d, value, **kw):
        docs[d] = json.loads(json.dumps(value))
        return True

    async def list_docs(port, prefix, **kw):
        return {k: v for k, v in docs.items() if k.startswith(prefix)}

    from app.services import nostr_store
    monkeypatch.setattr(nostr_store, "get_doc", get_doc)
    monkeypatch.setattr(nostr_store, "put_doc", put_doc)
    monkeypatch.setattr(nostr_store, "list_docs", list_docs)
    docs["pcai:ap:key:" + ALICE] = {"priv": KEY[0], "pub": KEY[1]}
    docs["pcai:ap:key:instance"] = {"priv": KEY[0], "pub": KEY[1]}

    relay = {}          # event id → event

    async def publish(port, ev, timeout=8.0):
        relay[ev["id"]] = ev
        return True, ""

    async def query_one(port, filt, timeout=8.0):
        if filt.get("ids"):
            return True, relay.get(filt["ids"][0])
        cand = [e for e in relay.values() if e["kind"] in filt.get("kinds", [e["kind"]])
                and e["pubkey"] in filt.get("authors", [e["pubkey"]])]
        return True, (max(cand, key=lambda e: e["created_at"]) if cand else None)

    monkeypatch.setattr(ident, "publish", publish)
    monkeypatch.setattr(ident, "query_one", query_one)

    sent = []

    async def deliver(inbox_url, activity, *, key_id, private_pem):
        sent.append({"inbox": inbox_url, "activity": activity, "key_id": key_id})
        return 202

    async def remote_actor(uri, refresh=False):
        if uri.split("#")[0] == REMOTE:
            return carol_actor()
        raise remote.FetchError("unknown actor " + uri)

    async def public_key(key_id, refresh=False):
        if key_id == REMOTE + "#main-key":
            return REMOTE, CAROL_KEY[1]
        raise remote.FetchError("unknown key")

    objects = {}        # what each object's OWN server would answer (remote.fetch_object)

    async def fetch_object(uri):
        if uri in objects:
            return json.loads(json.dumps(objects[uri]))
        raise remote.FetchError("not found on its server")

    monkeypatch.setattr(remote, "deliver", deliver)
    monkeypatch.setattr(remote, "actor", remote_actor)
    monkeypatch.setattr(remote, "public_key", public_key)
    monkeypatch.setattr(remote, "fetch_object", fetch_object)

    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine, tables=[FediBridgeDelivered.__table__, FediPuppet.__table__])
    Session = sessionmaker(bind=engine)
    import app.database
    monkeypatch.setattr(app.database, "SessionLocal", Session)

    linked = set()
    import app.services.fedi_nostr_writeback_service as wb
    monkeypatch.setattr(wb, "_bridge_allowed_pubkeys", lambda: frozenset(linked))
    return {"settings": settings, "docs": docs, "relay": relay, "sent": sent, "Session": Session,
            "linked": linked, "objects": objects}


def run(coro):
    return asyncio.run(coro)


def member_post(content="hello", created=1_700_000_000, tags=None, author=ALICE, kind=1):
    """An event as the relay would hand it over (id computed the NIP-01 way; no signature needed --
    nothing in the outbox re-verifies what the relay already accepted)."""
    import hashlib
    tags = tags or []
    eid = hashlib.sha256(json.dumps([0, author, created, kind, tags, content], separators=(",", ":"),
                                    ensure_ascii=False).encode()).hexdigest()
    return {"id": eid, "pubkey": author, "created_at": created, "kind": kind, "tags": tags,
            "content": content, "sig": ""}


# ============================================================================ 1. signatures

def _signed(body=b'{"type":"Create"}', url=f"{BASE}/ap/inbox", key=KEY, key_id=f"{BASE}/ap/actor#main-key", now=None):
    h = httpsig.sign("POST", url, key_id=key_id, private_pem=key[0], body=body, now=now)
    return {k.lower(): v for k, v in h.items()}, httpsig.parse(h["Signature"])


def test_a_signed_request_verifies():
    headers, params = _signed()
    httpsig.verify("POST", "/ap/inbox", headers, b'{"type":"Create"}', KEY[1], params)


@pytest.mark.parametrize("tamper", ["body", "path", "host", "key", "old", "future", "no-digest"])
def test_every_tampering_fails(tamper):
    body = b'{"type":"Create"}'
    now = None
    if tamper == "old":
        now = datetime.now(timezone.utc) - timedelta(hours=13)
    if tamper == "future":
        now = datetime.now(timezone.utc) + timedelta(hours=2)
    headers, params = _signed(body, now=now)
    path, pem = "/ap/inbox", KEY[1]
    if tamper == "body":
        body = b'{"type":"Delete"}'
    if tamper == "path":
        path = "/ap/users/alice/inbox"
    if tamper == "host":
        headers["host"] = "evil.example"
    if tamper == "key":
        pem = CAROL_KEY[1]
    if tamper == "no-digest":
        params["headers"] = [h for h in params["headers"] if h != "digest"]
    with pytest.raises(httpsig.SignatureError):
        httpsig.verify("POST", path, headers, body, pem, params)


# ============================================================================ 2. translation

def test_a_member_post_becomes_a_safe_public_note():
    ev = member_post("hi <script>alert(1)</script> #PosterChan https://img.example/a.png\n\nsecond line",
                     tags=[["content-warning", "spoilers"]])
    note = convert.note_from_event(ev, base=BASE, actor=f"{BASE}/ap/users/alice", followers=f"{BASE}/ap/users/alice/followers",
                                   mentions={}, in_reply_to="https://mastodon.example/notes/9",
                                   reply_to_actor=REMOTE)
    assert "<script>" not in note["content"] and "&lt;script&gt;" in note["content"]
    assert note["to"] == [config.PUBLIC] and REMOTE in note["cc"]
    assert note["inReplyTo"] == "https://mastodon.example/notes/9"
    assert note["attachment"][0]["url"] == "https://img.example/a.png"
    assert "img.example" not in note["content"], "the attached image is also printed as a link"
    assert {"type": "Hashtag", "href": f"{BASE}/tags/posterchan", "name": "#posterchan"} in note["tag"]
    assert note["sensitive"] is True and note["summary"] == "spoilers"
    assert note["content"].count("<p>") == 2
    assert note["url"].startswith(f"{BASE}/note1")


def test_an_incoming_note_becomes_nostr_text_and_tags():
    note = {"id": "https://mastodon.example/notes/1", "type": "Note", "attributedTo": REMOTE,
            "content": "<p>hey <a href='x'>@alice</a><br>line two</p>", "sensitive": True, "summary": "cw",
            "to": [config.PUBLIC], "tag": [{"type": "Mention", "href": f"{BASE}/ap/users/alice"},
                                           {"type": "Hashtag", "name": "#Cats"}],
            "attachment": [{"type": "Document", "url": "https://files.example/cat.jpg"}]}
    text, tags = convert.note_content(note, local_actors={f"{BASE}/ap/users/alice": ALICE})
    assert text.startswith("hey @alice\nline two") and text.endswith("https://files.example/cat.jpg")
    assert ["p", ALICE] in tags and ["t", "cats"] in tags and ["content-warning", "cw"] in tags


def test_only_public_posts_are_stored():
    assert convert.is_public({"to": [config.PUBLIC]})
    assert convert.is_public({"to": [REMOTE + "/followers"], "cc": ["as:Public"]})       # unlisted
    assert not convert.is_public({"to": [REMOTE + "/followers"]})                          # followers-only
    assert not convert.is_public({"to": [f"{BASE}/ap/users/alice"]})                       # direct


# ============================================================================ 3. one identity

def test_the_same_account_gets_the_same_puppet_both_ways(world):
    """A Mastodon API account (what the Pleroma bridge reads) and the ActivityPub actor document
    (what the inbox reads) for one person derive ONE key -- so their posts from either path are one
    Nostr identity, and the bridge's dedup and write-back apply to both."""
    api_account = {"uri": REMOTE, "url": "https://mastodon.example/@carol", "acct": "carol@mastodon.example",
                   "display_name": "Carol", "avatar": "https://mastodon.example/c.png", "note": "<p>hi</p>"}
    via_bridge = ident.puppet_for(api_account)
    via_ap = ident.puppet_for(convert.account_from_actor(carol_actor()))
    assert via_bridge["pubkey_hex"] == via_ap["pubkey_hex"]
    assert via_bridge["nip05_name"] == via_ap["nip05_name"]


# ============================================================================ 4. the inbox

def _create(note_id="https://mastodon.example/notes/1", content="<p>hello</p>", to=None, extra=None):
    note = {"id": note_id, "type": "Note", "attributedTo": REMOTE, "content": content,
            "published": "2026-09-23T10:00:00Z", "to": to or [config.PUBLIC], "cc": [], **(extra or {})}
    return {"id": note_id + "/activity", "type": "Create", "actor": REMOTE, "object": note}


def test_a_stranger_cannot_fill_the_relay(world):
    assert run(inbox.process(_create(), REMOTE)).startswith("ignored: nobody here follows")
    assert world["relay"] == {}


def test_a_followed_accounts_post_is_stored_as_its_puppet(world):
    world["docs"][f"pcai:ap:following:{ALICE}:x"] = {"actor": REMOTE, "inbox": "i", "state": "accepted"}
    assert run(inbox.process(_create(), REMOTE)) == "stored"
    # The author's puppet profile is published the first time too -- the same kind-0 the bridge makes.
    assert sorted(e["kind"] for e in world["relay"].values()) == [0, 1]
    ev = next(e for e in world["relay"].values() if e["kind"] == 1)
    assert ev["content"] == "hello"
    assert ["proxy", "https://mastodon.example/notes/1", "activitypub"] in ev["tags"]
    assert any(t[0] == "fedibridge" and t[1] == REMOTE for t in ev["tags"])
    assert ["nofederate"] in ev["tags"], "a mirror left the relay with broadcast off"
    assert ev["pubkey"] == ident.puppet_for(convert.account_from_actor(carol_actor()))["pubkey_hex"]
    row = world["Session"]().query(FediBridgeDelivered).one()
    assert (row.platform, row.note_uri, row.nostr_event_id) == ("activitypub", "https://mastodon.example/notes/1", ev["id"])
    assert run(inbox.process(_create(), REMOTE)) == "already stored"


def test_a_note_the_pleroma_bridge_already_mirrored_is_not_stored_twice(world):
    world["docs"][f"pcai:ap:following:{ALICE}:x"] = {"actor": REMOTE, "inbox": "i", "state": "accepted"}
    s = world["Session"]()
    s.add(FediBridgeDelivered(platform="pleroma", instance_url="https://pleroma.example", note_id="A1",
                              note_uri="https://mastodon.example/notes/1", nostr_event_id="e" * 64, nostr_pubkey="f" * 64))
    s.commit()
    assert run(inbox.process(_create(), REMOTE)) == "already stored"
    assert not [e for e in world["relay"].values() if e["kind"] == 1]


def test_followers_only_is_never_stored(world):
    world["docs"][f"pcai:ap:following:{ALICE}:x"] = {"actor": REMOTE, "inbox": "i", "state": "accepted"}
    assert run(inbox.process(_create(to=[REMOTE + "/followers"]), REMOTE)) == "ignored: not public"


def test_a_reply_to_a_member_threads_under_their_post_and_notifies_them(world):
    mine = member_post("my post")
    world["relay"][mine["id"]] = mine
    act = _create(extra={"inReplyTo": convert.object_url(BASE, mine["id"])})
    assert run(inbox.process(act, REMOTE)) == "stored"
    reply = next(e for e in world["relay"].values() if e["kind"] == 1 and e["id"] != mine["id"])
    assert ["e", mine["id"], "", "reply"] in reply["tags"] and ["p", ALICE] in reply["tags"]


def test_a_note_posted_for_someone_on_another_server_is_refused(world):
    act = _create()
    act["object"]["attributedTo"] = "https://elsewhere.example/users/dave"
    world["objects"][act["object"]["id"]] = act["object"]        # even its own server agrees
    assert run(inbox.process(act, REMOTE)).startswith("ignored")


def test_a_follow_is_recorded_and_accepted_as_the_member(world):
    follow = {"id": "https://mastodon.example/f/1", "type": "Follow", "actor": REMOTE, "object": f"{BASE}/ap/users/alice"}
    assert run(inbox.process(follow, REMOTE)).startswith("follower added")
    assert run(state.followers(ALICE)) == [{"actor": REMOTE, "inbox": "https://mastodon.example/inbox",
                                            "at": run(state.followers(ALICE))[0]["at"]}]
    (sent,) = world["sent"]
    assert sent["activity"]["type"] == "Accept" and sent["activity"]["actor"] == f"{BASE}/ap/users/alice"
    assert sent["activity"]["object"]["id"] == "https://mastodon.example/f/1"
    assert sent["key_id"] == f"{BASE}/ap/users/alice#main-key"
    run(inbox.process({"type": "Undo", "actor": REMOTE, "object": follow}, REMOTE))
    assert run(state.followers(ALICE)) == []


def test_a_follow_of_somebody_who_is_not_ours_is_ignored(world):
    follow = {"id": "x", "type": "Follow", "actor": REMOTE, "object": f"{BASE}/ap/users/nobody"}
    assert run(inbox.process(follow, REMOTE)) == "ignored: not one of our actors"


def test_a_like_on_a_member_post_is_a_reaction_to_it(world):
    mine = member_post("likeable")
    world["relay"][mine["id"]] = mine
    act = {"id": "https://mastodon.example/likes/1", "type": "Like", "actor": REMOTE,
           "object": convert.object_url(BASE, mine["id"])}
    assert run(inbox.process(act, REMOTE)) == "like stored"
    like = next(e for e in world["relay"].values() if e["kind"] == 7)
    assert ["e", mine["id"]] in like["tags"] and ["p", ALICE] in like["tags"] and like["content"] == "+"


def test_only_the_author_can_delete_what_they_posted(world, monkeypatch):
    world["docs"][f"pcai:ap:following:{ALICE}:x"] = {"actor": REMOTE, "inbox": "i", "state": "accepted"}
    run(inbox.process(_create(), REMOTE))
    deleted = []

    async def delete_note(port, actor_uri, eid, broadcast=False):
        deleted.append((actor_uri, eid))
        return True
    monkeypatch.setattr(ident, "delete_note", delete_note)
    neighbour = "https://mastodon.example/users/mallory"
    assert run(inbox._delete("https://mastodon.example/notes/1", neighbour)).startswith("ignored")
    assert deleted == []
    assert run(inbox._delete("https://mastodon.example/notes/1", REMOTE)) == "deleted"
    assert deleted and deleted[0][0] == REMOTE


# ============================================================================ 5. the outbox

def _with_follower(world):
    world["docs"][f"pcai:ap:follower:{ALICE}:1"] = {"actor": REMOTE, "inbox": "https://mastodon.example/inbox"}


def test_a_top_level_post_goes_to_the_followers(world):
    _with_follower(world)
    ev = member_post("hello fediverse")
    jobs = run(outbox.plan(ev, ALICE))
    assert [i for i, _ in jobs] == ["https://mastodon.example/inbox"]
    act = jobs[0][1]
    assert act["type"] == "Create" and act["object"]["id"] == convert.object_url(BASE, ev["id"])
    assert act["actor"] == f"{BASE}/ap/users/alice"


def test_a_reply_in_a_nostr_only_thread_stays_on_nostr(world):
    _with_follower(world)
    ev = member_post("reply", tags=[["e", "9" * 64, "", "reply"]])
    assert run(outbox.plan(ev, ALICE)) == []


def test_a_reply_to_a_mirrored_note_answers_the_original(world):
    _with_follower(world)
    s = world["Session"]()
    puppet = ident.puppet_for(convert.account_from_actor(carol_actor()))
    s.add(FediPuppet(actor_uri=REMOTE, acct="carol@mastodon.example", pubkey_hex=puppet["pubkey_hex"], nip05_name="c"))
    s.add(FediBridgeDelivered(platform="activitypub", instance_url="https://mastodon.example", note_id="n",
                              note_uri="https://mastodon.example/notes/7", nostr_event_id="7" * 64,
                              nostr_pubkey=puppet["pubkey_hex"]))
    s.commit()
    ev = member_post("replying", tags=[["e", "7" * 64, "", "reply"], ["p", puppet["pubkey_hex"]]])
    jobs = run(outbox.plan(ev, ALICE))
    note = jobs[0][1]["object"]
    assert note["inReplyTo"] == "https://mastodon.example/notes/7"
    assert REMOTE in note["cc"] and any(t["type"] == "Mention" and t["href"] == REMOTE for t in note["tag"])
    comment = member_post("a NIP-22 comment", kind=1111, tags=[["E", "7" * 64], ["e", "7" * 64], ["k", "1"]])
    assert run(outbox.plan(comment, ALICE))[0][1]["object"]["inReplyTo"] == "https://mastodon.example/notes/7"


def test_a_mirror_is_never_sent_back_out(world):
    _with_follower(world)
    ev = member_post("mirrored", tags=[["proxy", "https://mastodon.example/notes/1", "activitypub"]])
    assert run(outbox.plan(ev, ALICE)) == []


def test_following_a_fediverse_account_sends_a_follow(world):
    s = world["Session"]()
    puppet = ident.puppet_for(convert.account_from_actor(carol_actor()))
    s.add(FediPuppet(actor_uri=REMOTE, acct="carol@mastodon.example", pubkey_hex=puppet["pubkey_hex"], nip05_name="c"))
    s.commit()
    contacts = member_post("", kind=3, tags=[["p", puppet["pubkey_hex"]], ["p", BOB]])
    jobs = run(outbox.plan(contacts, ALICE))
    assert [(i, a["type"], a["object"]) for i, a in jobs] == [("https://mastodon.example/inbox", "Follow", REMOTE)]
    assert REMOTE in run(state.following(ALICE))
    unfollow = member_post("", kind=3, tags=[["p", BOB]], created=1_700_000_100)
    assert [a["type"] for _, a in run(outbox.plan(unfollow, ALICE))] == ["Undo"]


def test_a_member_on_a_linked_pleroma_account_is_not_sent_twice(world):
    world["linked"].add(BOB)
    assert run(outbox._members()) == [ALICE]


def test_every_local_user_is_an_actor_except_blocked_ones(world):
    assert set(actors.all_actors()) == {ALICE, BOB}
    world["settings"]["nostr_relay_blocked_pubkeys"] = BOB
    assert actors.all_actors() == [ALICE]
    assert run(actors.member_by_name("bob")) == ""


# ============================================================================ 6. the Pleroma bridge

def test_the_pleroma_bridge_does_not_mirror_our_own_users_back(world):
    from app.services import fedi_nostr_bridge_service as mirror
    assert mirror._is_own_activitypub_host(DOMAIN)
    world["settings"]["activitypub_enabled"] = "false"
    assert not mirror._is_own_activitypub_host(DOMAIN)


def test_a_fediverse_reply_to_our_user_threads_under_their_real_post(world):
    """The bridge mirrors a reply's ANCESTORS through `_deliver`, and the ancestor of a fediverse
    reply to one of our users is that user's own post, seen from outside as /ap/objects/<id>. It is
    not mirrored as a puppet copy: `_deliver` hands back the real event id to thread under."""
    from app.models import User, FediBridgeSkipped
    from app.services import fedi_nostr_bridge_service as mirror
    engine = world["Session"]().get_bind()
    Base.metadata.create_all(engine, tables=[User.__table__, FediBridgeSkipped.__table__])
    mine = member_post("the original")
    raw = {"id": "S1", "uri": convert.object_url(BASE, mine["id"]), "visibility": "public",
           "content": "<p>the original</p>",
           "account": {"acct": f"alice@{DOMAIN}", "uri": f"{BASE}/ap/users/alice", "username": "alice"}}
    db = world["Session"]()
    got = run(mirror._deliver(db, 1, "pleroma", "https://pleroma.example", "pleroma.example", raw,
                              mirror._norm("pleroma", raw)))
    assert got == mine["id"]
    assert world["relay"] == {}, "our own user was mirrored back as a puppet"


def test_the_writeback_resolves_an_activitypub_note_by_uri(world, monkeypatch):
    import app.services.fedi_nostr_writeback_service as wb
    asked = []

    async def resolve_status(inst, token, uri):
        asked.append(uri)
        return {"id": "LOCAL42"}
    monkeypatch.setattr(wb.pleroma_service, "resolve_status", resolve_status)
    row = FediBridgeDelivered(platform="activitypub", instance_url="https://mastodon.example",
                              note_id="https://mastodon.example/notes/7", note_uri="https://mastodon.example/notes/7",
                              nostr_event_id="7" * 64)

    class U:
        pleroma_instance_url = "https://mastodon.example"
        pleroma_access_token = "t"
    assert run(wb._resolve_target_id(U(), row)) == "LOCAL42" and asked == ["https://mastodon.example/notes/7"]


# ============================================================================ 7. HTTP

@pytest.fixture
def client(world, monkeypatch):
    from app.routers import activitypub as routes
    scheduled = []
    monkeypatch.setattr(inbox, "schedule", lambda act, signer: scheduled.append((act, signer)) or True)
    app = FastAPI()
    app.include_router(routes.router)
    with TestClient(app, base_url=BASE) as c:
        c.scheduled = scheduled
        yield c


def test_webfinger_finds_a_local_user(client):
    r = client.get(f"/.well-known/webfinger?resource=acct:alice@{DOMAIN}")
    assert r.status_code == 200 and r.json()["links"][0]["href"] == f"{BASE}/ap/users/alice"
    assert client.get(f"/.well-known/webfinger?resource=acct:nobody@{DOMAIN}").status_code == 404
    assert client.get("/.well-known/webfinger?resource=acct:alice@elsewhere.example").status_code == 404


def test_everything_is_404_while_it_is_off(client, world):
    world["settings"]["activitypub_enabled"] = "false"
    for path in (f"/.well-known/webfinger?resource=acct:alice@{DOMAIN}", "/ap/users/alice", "/nodeinfo/2.1", "/ap/actor"):
        assert client.get(path).status_code == 404, path
    assert client.post("/ap/inbox", content=b"{}").status_code == 404


def test_the_actor_document_carries_the_members_key_and_profile(client, world):
    world["relay"]["p" * 64] = {"id": "p" * 64, "pubkey": ALICE, "kind": 0, "created_at": 1, "tags": [],
                                "content": json.dumps({"name": "Alice A", "about": "hi", "picture": "https://x.example/a.png"})}
    doc = client.get("/ap/users/alice", headers={"Accept": "application/activity+json"}).json()
    assert doc["id"] == f"{BASE}/ap/users/alice" and doc["name"] == "Alice A"
    assert doc["publicKey"]["publicKeyPem"] == KEY[1] and doc["icon"]["url"] == "https://x.example/a.png"
    browser = client.get("/ap/users/alice", headers={"Accept": "text/html"}, follow_redirects=False)
    assert browser.status_code == 302 and browser.headers["location"] == f"{BASE}/users/alice"


def test_objects_are_only_a_members_own_posts(client, world):
    mine = member_post("public words")
    world["relay"][mine["id"]] = mine
    note = client.get(f"/ap/objects/{mine['id']}", headers={"Accept": "application/activity+json"}).json()
    assert note["attributedTo"] == f"{BASE}/ap/users/alice" and "public words" in note["content"]
    mirror = member_post("not mine", tags=[["proxy", "https://m.example/1", "activitypub"]])
    world["relay"][mirror["id"]] = mirror
    assert client.get(f"/ap/objects/{mirror['id']}").status_code == 404
    stranger = member_post("stranger", author="c3" * 32)
    world["relay"][stranger["id"]] = stranger
    assert client.get(f"/ap/objects/{stranger['id']}").status_code == 404


def _post_signed(client, activity, *, key=CAROL_KEY, key_id=REMOTE + "#main-key", path="/ap/inbox"):
    body = json.dumps(activity).encode()
    h = httpsig.sign("POST", f"{BASE}{path}", key_id=key_id, private_pem=key[0], body=body)
    h["Content-Type"] = "application/activity+json"
    return client.post(path, content=body, headers=h)


def test_the_inbox_takes_a_correctly_signed_activity(client):
    r = _post_signed(client, _create())
    assert r.status_code == 202 and client.scheduled and client.scheduled[0][1] == REMOTE


def test_the_inbox_refuses_unsigned_forged_and_impersonating_requests(client):
    assert client.post("/ap/inbox", content=json.dumps(_create()).encode()).status_code == 401
    assert _post_signed(client, _create(), key=KEY).status_code == 401          # not carol's key
    other = _create()
    other["actor"] = "https://mastodon.example/users/mallory"
    assert _post_signed(client, other).status_code == 401                      # signed by carol, claims mallory
    assert client.scheduled == []


def test_a_blocked_instance_is_accepted_and_dropped(client, monkeypatch):
    blocked = "https://blocked.example/users/x"

    async def pk(key_id, refresh=False):
        return blocked, CAROL_KEY[1]
    monkeypatch.setattr(remote, "public_key", pk)
    act = _create()
    act["actor"] = blocked
    assert _post_signed(client, act, key_id=blocked + "#main-key").status_code == 202
    assert client.scheduled == []


# ============================================================================ 8. admin

def test_the_settings_hydrate_and_have_inputs():
    from pathlib import Path
    from app.schemas import SettingsResponse
    fields = SettingsResponse.model_fields
    assert "activitypub_enabled" in fields and "activitypub_domain" in fields
    assert "activitypub_blocked_domains" not in fields, "a second blocklist -- instance blocking is the relay's"
    tab = (Path(__file__).resolve().parents[1] / "templates/admin/tabs/social.html").read_text()
    for key in ("activitypub_enabled", "activitypub_domain"):
        assert f'id="{key}" name="{key}"' in tab


def test_the_domain_falls_back_to_the_nip05_domain(world):
    world["settings"]["activitypub_domain"] = ""
    world["settings"]["nostr_relay_nip05_domain"] = "@Poster.Test"
    assert config.domain() == "poster.test"
    world["settings"]["activitypub_domain"] = "https://ap.example/"
    assert config.domain() == "ap.example"


# ============================================================================ 9. the security review's findings
# Each of these was a real hole in the first version; each test fails against that version.

def test_a_key_cannot_vouch_for_an_actor_on_another_server(world, monkeypatch):
    """IMPERSONATION: a document at https://evil.example/k claiming to BE Gargron's actor and holding
    the attacker's key. Its self-declared id must not be believed -- by the REAL key lookup."""
    victim = "https://mastodon.social/users/Gargron"
    fake_person = {"type": "Person", "id": victim,
                   "publicKey": {"id": "https://evil.example/k", "publicKeyPem": CAROL_KEY[1]}}

    async def fetch_json(url, signed=True):
        if url.startswith("https://evil.example/"):
            return dict(fake_person)
        raise remote.FetchError("no")
    monkeypatch.setattr(remote, "fetch_json", fetch_json)
    monkeypatch.setattr(remote, "actor", REAL_ACTOR)
    with pytest.raises(remote.FetchError):
        run(REAL_PUBLIC_KEY("https://evil.example/k"))                 # the key document is not the key
    with pytest.raises(remote.FetchError):
        run(REAL_PUBLIC_KEY("https://evil.example/actor#main-key"))    # the actor is not the one asked for
    # And the honest shape still works: an actor at its own address publishing its own key.
    honest = carol_actor()

    async def fetch_honest(url, signed=True):
        if url == REMOTE:
            return dict(honest)
        raise remote.FetchError("no")
    monkeypatch.setattr(remote, "fetch_json", fetch_honest)
    remote._actors.clear()
    assert run(REAL_PUBLIC_KEY(REMOTE + "#main-key")) == (REMOTE, CAROL_KEY[1])


def test_a_boost_cannot_smuggle_a_forged_note(world, monkeypatch):
    """FORGERY: a followed account boosts an embedded note 'by' somebody on another server. What is
    stored must be the note as its own server serves it -- or nothing."""
    world["docs"][f"pcai:ap:following:{ALICE}:x"] = {"actor": REMOTE, "inbox": "i", "state": "accepted"}
    victim = "https://other.example/users/g"
    forged = {"id": "https://other.example/notes/1", "type": "Note", "attributedTo": victim,
              "content": "<p>FORGED</p>", "to": [config.PUBLIC]}
    fetched = []

    async def fetch_object(uri):
        fetched.append(uri)
        raise remote.FetchError("the real server does not have it")
    monkeypatch.setattr(remote, "fetch_object", fetch_object)
    act = {"id": "https://mastodon.example/boosts/1", "type": "Announce", "actor": REMOTE,
           "to": [config.PUBLIC], "object": forged}
    with pytest.raises(remote.FetchError):
        run(inbox.process(act, REMOTE))
    assert fetched == ["https://other.example/notes/1"]
    assert not [e for e in world["relay"].values() if "FORGED" in e.get("content", "")]


def test_a_neighbour_on_the_same_server_cannot_post_as_you(world):
    world["docs"][f"pcai:ap:following:{ALICE}:x"] = {"actor": REMOTE, "inbox": "i", "state": "accepted"}
    act = _create()
    act["object"]["attributedTo"] = "https://mastodon.example/users/somebody-else"
    act["object"]["id"] = "https://mastodon.example/notes/2"
    world["objects"][act["object"]["id"]] = act["object"]
    assert run(inbox.process(act, REMOTE)) == "ignored: posted for somebody else"


def test_an_unrequested_accept_opens_nothing(world):
    follow = {"id": f"{BASE}/ap/users/alice#follows/abc", "type": "Follow",
              "actor": f"{BASE}/ap/users/alice", "object": REMOTE}
    assert run(inbox.process({"type": "Accept", "actor": REMOTE, "object": follow}, REMOTE)) \
        == "ignored: we never asked to follow them"
    assert REMOTE not in run(state.followed_actors())
    run(state.set_following(ALICE, REMOTE, "https://mastodon.example/inbox", "pending"))
    from app.services.activitypub.outbox import _h
    by_id_only = {"type": "Accept", "actor": REMOTE, "object": f"{BASE}/ap/users/alice#follows/{_h(REMOTE)}"}
    assert run(inbox.process(by_id_only, REMOTE)) == "follow accepted"
    state.forget_followed_cache()
    assert REMOTE in run(state.followed_actors())


def test_activity_ids_must_be_the_senders(world):
    mine = member_post("likeable")
    world["relay"][mine["id"]] = mine
    for bad in ("", "https://other.example/likes/1"):
        act = {"id": bad, "type": "Like", "actor": REMOTE, "object": convert.object_url(BASE, mine["id"])}
        assert run(inbox.process(act, REMOTE)) == "ignored: activity id is not the sender's"


def test_a_follower_inbox_must_be_on_the_followers_own_server(world, monkeypatch):
    evil = dict(carol_actor(), inbox="https://victim.example/inbox", endpoints={})

    async def actor_doc(uri, refresh=False):
        return evil
    monkeypatch.setattr(remote, "actor", actor_doc)
    follow = {"id": "https://mastodon.example/f/2", "type": "Follow", "actor": REMOTE, "object": f"{BASE}/ap/users/alice"}
    assert run(inbox.process(follow, REMOTE)).startswith("ignored: follower has no inbox on its own server")
    assert run(state.followers(ALICE)) == []


def test_the_pleroma_bridge_sweep_never_deletes_activitypub_notes(world, monkeypatch):
    """The bridge's deletion sweep reads the same table by instance, and asks the instance about each
    row's `note_id` as a STATUS id. For a row that came in over ActivityPub that is a URI: the
    instance says 404, and the sweep used to delete the note as 'gone on the source'."""
    from app.services import fedi_nostr_bridge_service as mirror
    inst = "https://mastodon.example"
    s = world["Session"]()
    s.add(FediBridgeDelivered(platform="activitypub", instance_url=inst, note_id=inst + "/notes/1",
                              note_uri=inst + "/notes/1", nostr_event_id="1" * 64, nostr_pubkey="2" * 64))
    s.commit()
    asked = []

    async def status_deleted(instance_url, token, status_id):
        asked.append(status_id)
        return True
    monkeypatch.setattr(mirror.pleroma_service, "status_deleted", status_deleted)
    monkeypatch.setattr(settings_store, "put", lambda *a, **k: None)
    run(mirror._check_deletions(s, 1, inst, "token", False))
    assert asked == [], "an ActivityPub note was checked as a status (and would have been deleted)"


def test_the_outbox_pages_back_so_a_backlog_is_not_skipped(world, monkeypatch):
    """The relay answers newest-first with a limit. One query after a quiet spell returned only the
    newest page, and moving the cursor to it skipped everything older."""
    from app.services import nostr_store
    _with_follower(world)
    evs = [member_post(f"post {i}", created=1_700_000_000 + i) for i in range(7)]

    async def ws_query(port, filters, strict=False, **kw):
        f = filters[0]
        hit = [e for e in evs if e["created_at"] >= f["since"] and ("until" not in f or e["created_at"] <= f["until"])]
        return sorted(hit, key=lambda e: -e["created_at"])[:f["limit"]]
    monkeypatch.setattr(nostr_store, "_ws_query", ws_query)
    monkeypatch.setattr(outbox, "_PAGE", 3)
    world["docs"]["pcai:ap:cursor"] = {"since": 1_699_999_999}
    world["linked"].add(BOB)
    assert run(outbox.tick()) == 7
    created = sorted(a["activity"]["object"]["content"] for a in world["sent"] if a["activity"]["type"] == "Create")
    assert len(created) == 7, created
    assert world["docs"]["pcai:ap:cursor"]["since"] == 1_700_000_006


def test_a_failed_translation_does_not_move_the_cursor_past_it(world, monkeypatch):
    from app.services import nostr_store
    evs = [member_post("one", created=1_700_000_001), member_post("two", created=1_700_000_002)]

    async def ws_query(port, filters, strict=False, **kw):
        return sorted(evs, key=lambda e: -e["created_at"])
    monkeypatch.setattr(nostr_store, "_ws_query", ws_query)
    outbox._failures.clear()

    async def boom(member, strict=True):
        raise RuntimeError("relay hiccup")
    monkeypatch.setattr(state, "followers", boom)
    world["docs"]["pcai:ap:cursor"] = {"since": 1_700_000_000}
    world["linked"].add(BOB)
    assert run(outbox.tick()) == 0
    assert world["docs"]["pcai:ap:cursor"]["since"] == 1_700_000_000, "the cursor moved past an event never sent"
