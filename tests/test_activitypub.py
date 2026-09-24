"""The ActivityPub server (app/services/activitypub) -- stored as Nostr events, compatible with the
Pleroma bridge in both directions.

Runs the SHIPPED code; the boundaries are fixtures: the relay (publish/query and the operator's
documents), remote servers (actor/key fetches and deliveries), and the database (in-memory SQLite
with the bridge's real tables). What is checked, by section:

  1. HTTP Signatures -- a signed request verifies, and every tampering fails.
  2. Translation -- Nostr → Note (escaping, mentions, hashtags, media, CW, reply) and Note → Nostr.
  3. One identity -- a fediverse account always gets the same puppet key.
  4. The inbox -- the relevance gate, cross-path dedup, public-only, replies to our posts, likes,
     follows (with a signed Accept), deletions only by the author.
  5. The outbox -- what a member's events turn into and who they go to; mirrors are never re-sent.
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
BOB = "b2" * 32            # another member
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
    actors._nick_cache.clear()
    actors._profile_cache.clear()
    actors._known_cache.clear()
    outbox._seen.clear()
    outbox._retries.clear()
    outbox._down.clear()
    outbox._gone_cache.update(at=0.0, set=frozenset())
    inbox._pending_by_host.clear()
    inbox._new_followers.clear()
    # Process-wide caches of PERMANENT facts (a pinned id, a claimed handle): right for a process,
    # wrong across tests that each start a fresh relay.
    actors._pin_cache.clear()
    actors._pin_owner_cache.clear()
    actors._nick_owner_cache.clear()
    outbox._done.clear()
    outbox._retries_loaded = False
    state._moved_cache.update(at=0.0, map={})
    inbox._locks.clear()
    from app.services.activitypub import relays as _relays
    _relays.forget_cache()
    _relays._accepted_cache['list'] = []

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

    actor_docs = {REMOTE: carol_actor()}     # what each actor's OWN server answers (remote.actor)

    async def remote_actor(uri, refresh=False, alias=False):
        uri = uri.split("#")[0]
        if uri in actor_docs:
            return json.loads(json.dumps(actor_docs[uri]))
        if alias and uri == "https://mastodon.example/@carol":     # the profile URL serves the same actor
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

    monkeypatch.setattr(settings_store, "is_hydrated", lambda: True)
    return {"settings": settings, "docs": docs, "relay": relay, "sent": sent, "Session": Session,
            "objects": objects, "actors": actor_docs}


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
    # Not public: it goes to the DM path, which finds it addressed to nobody here -- and either way
    # nothing becomes a public note.
    assert run(inbox.process(_create(to=[REMOTE + "/followers"]), REMOTE)).startswith("ignored")
    assert not [e for e in world["relay"].values() if e["kind"] == 1]


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




def test_a_bridge_puppet_keyed_on_a_profile_url_is_followed_by_its_real_id(world):
    """The Pleroma bridge keys puppets on the account's PROFILE URL (`/@carol`). The Follow -- and so
    the Accept and the posts that come after it -- must use the actor's canonical id (`/users/carol`),
    or every follow of an existing bridge puppet would silently fail."""
    s = world["Session"]()
    s.add(FediPuppet(actor_uri="https://mastodon.example/@carol", acct="carol@mastodon.example",
                     pubkey_hex="c4" * 32, nip05_name="c"))
    s.commit()
    jobs = run(outbox.plan(member_post("", kind=3, tags=[["p", "c4" * 32]]), ALICE))
    assert [(a["type"], a["object"]) for _, a in jobs] == [("Follow", REMOTE)]
    assert REMOTE in run(state.following(ALICE))


def test_every_local_user_is_an_actor_except_blocked_ones(world):
    assert set(actors.all_actors()) == {ALICE, BOB}
    world["settings"]["nostr_relay_blocked_pubkeys"] = BOB
    assert actors.all_actors() == [ALICE]
    assert run(actors.member_by_name("bob")) == ""


# ============================================================================ 6. the Pleroma bridge







# ============================================================================ 7. HTTP

@pytest.fixture
def client(world, monkeypatch):
    from app.routers import activitypub as routes
    for table in (routes._hits, routes._refreshed, routes._outbox_counts, routes._imported):
        table.clear()
    routes._count_runs.clear()
    routes._importing.clear()
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
    like = {"id": "https://mastodon.example/likes/1", "type": "Like", "actor": "https://mastodon.example/users/mallory",
            "object": f"{BASE}/ap/objects/{'a' * 64}"}
    assert _post_signed(client, like).status_code == 401                       # signed by carol, claims mallory
    assert client.scheduled == []
    # A Create signed by somebody other than its actor is a FORWARDED post (Mastodon relays replies):
    # accepted only as a POINTER -- what is scheduled is a fetch of the object from its own server,
    # never the activity as the forwarder sent it.
    other = _create()
    other["actor"] = "https://mastodon.example/users/mallory"
    assert _post_signed(client, other).status_code == 202
    assert client.scheduled == [({"type": inbox.FORWARDED, "object": other["object"]["id"]}, REMOTE)]


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
    assert run(outbox.tick()) == 0
    assert world["docs"]["pcai:ap:cursor"]["since"] == 1_700_000_000, "the cursor moved past an event never sent"


# ============================================================================ 10. importing who you follow

def _pleroma_account(n, host="mastodon.example"):
    return {"id": str(n), "acct": f"user{n}@{host}", "username": f"user{n}", "display_name": f"User {n}",
            "uri": f"https://{host}/users/user{n}", "url": f"https://{host}/@user{n}", "avatar": ""}


def _actor_doc(n, host="mastodon.example", name=None):
    uri = f"https://{host}/users/user{n}"
    return {"id": uri, "type": "Person", "preferredUsername": f"user{n}", "name": name or f"User {n}",
            "inbox": uri + "/inbox", "publicKey": {"id": uri + "#main-key", "owner": uri, "publicKeyPem": CAROL_KEY[1]}}


def test_the_import_gives_each_account_its_bridge_identity(world):
    from app.services.activitypub import importer
    world["actors"]["https://mastodon.example/users/user1"] = _actor_doc(1)
    accounts = [_pleroma_account(1), _pleroma_account(2, "blocked.example"),
                {"id": "9", "acct": "alice@" + DOMAIN, "uri": f"{BASE}/ap/users/alice", "username": "alice"},
                _pleroma_account(1)]
    s = world["Session"]()
    people = run(importer.puppets_for(s, accounts, "https://pleroma.example"))
    assert [p["acct"] for p in people] == ["user1@mastodon.example"], people
    assert people[0]["pubkey"] == ident.puppet_for(convert.account_from_actor(_actor_doc(1)))["pubkey_hex"]


def test_an_import_answer_cannot_rewrite_somebody_elses_identity(world):
    """The list is a Mastodon-API answer from a server the member TYPED -- every field in it is that
    server's to invent. Taken as sent, one answer claiming `uri: <Carol's real actor>` with its own
    name and picture republished Carol's Nostr profile as the attacker wished (and a handle alone
    could claim her key). Each account is only an address: its identity is read from its own server."""
    from app.services.activitypub import importer
    carol_pk = ident.puppet_for(convert.account_from_actor(carol_actor()))["pubkey_hex"]
    lie = {"id": "1", "uri": REMOTE, "acct": "evil@evil.example", "username": "evil",
           "display_name": "Carol (official)", "avatar": "https://evil.example/x.png", "note": "send sats"}
    unknown = {"id": "2", "uri": "https://evil.example/users/ghost", "acct": "carol@mastodon.example",
               "username": "carol", "display_name": "Carol"}
    s = world["Session"]()
    people = run(importer.puppets_for(s, [lie, unknown], "https://evil.example"))
    assert people == [{"pubkey": carol_pk, "acct": "carol@mastodon.example"}], people
    row = s.query(FediPuppet).filter(FediPuppet.pubkey_hex == carol_pk).one()
    assert row.display_name == "Carol" and "evil" not in (row.avatar_url or ""), "the answer rewrote her profile"
    profile = [e for e in world["relay"].values() if e["kind"] == 0 and e["pubkey"] == carol_pk]
    assert profile and "official" not in profile[-1]["content"] and "sats" not in profile[-1]["content"]
    assert not s.query(FediPuppet).filter(FediPuppet.actor_uri.like("%evil.example%")).all()


# ============================================================================ 11. out of the box, and the blocklist

def test_everything_is_on_out_of_the_box(world):
    for key in ("activitypub_enabled", "activitypub_everyone", "activitypub_dms"):
        world["settings"].pop(key, None)
    assert config.enabled() and config.everyone() and config.dms()
    world["settings"]["activitypub_dms"] = ""               # saved blank by an older form: still on
    assert config.dms()
    world["settings"]["activitypub_dms"] = "false"
    assert not config.dms()
    world["settings"].pop("activitypub_domain")
    assert not config.base_url(), "a node with no domain must stay silent even when on"


def test_the_blocklist_reads_the_way_people_write_it():
    from app.services import fedi_blocklist as fb
    hosts, accounts = fb.parse("嘟文.com\n@bad.example, https://worse.example/about\n*.wild.example\n"
                               "szymon@nowicki.io   # one person, not the instance\n")
    for h in ("xn--j5r817a.com", "嘟文.com", "sub.xn--j5r817a.com", "bad.example", "worse.example", "x.wild.example"):
        assert fb.host_blocked(h, hosts), h
    assert not fb.host_blocked("nowicki.io", hosts) and not fb.host_blocked("fine.example", hosts)
    assert fb.account_blocked("Szymon@nowicki.io", accounts, hosts)
    assert not fb.account_blocked("someone.else@nowicki.io", accounts, hosts)




def test_a_blocked_account_line_stops_it_at_the_inbox(world):
    world["settings"]["fedi_bridge_blocked_domains"] = "carol@mastodon.example"
    assert run(inbox.process(_create(), REMOTE)) == "ignored: blocked account"


# ============================================================================ 12. every Nostr user

NOSTR_SK = bytes.fromhex("11" * 32)


def _nostr_user(world, sk=NOSTR_SK):
    from app.services.nostr import nostr_service
    pk = nostr_service.derive_pubkey(sk)
    world["relay"]["k" + pk[:10]] = {"id": "k" + pk[:10], "pubkey": pk, "kind": 0, "created_at": 5, "tags": [],
                                     "content": json.dumps({"name": "Dana"})}
    return pk, nostr_service.npub_of(pk)


def test_any_nostr_user_with_a_profile_is_reachable_by_npub(client, world):
    pk, npub = _nostr_user(world)
    r = client.get(f"/.well-known/webfinger?resource=acct:{npub}@{DOMAIN}")
    assert r.status_code == 200 and r.json()["links"][0]["href"] == f"{BASE}/ap/users/{npub}"
    doc = client.get(f"/ap/users/{npub}", headers={"Accept": "application/activity+json"}).json()
    assert doc["name"] == "Dana" and doc["preferredUsername"] == f"dana_{pk[:4]}"
    from app.services.nostr import nostr_service
    stranger = nostr_service.npub_of("99" * 32)                           # nothing published, ever
    assert client.get(f"/.well-known/webfinger?resource=acct:{stranger}@{DOMAIN}").status_code == 404
    world["settings"]["activitypub_everyone"] = "false"
    actors._known_cache.clear()
    assert client.get(f"/ap/users/{npub}").status_code == 404


def test_new_keys_for_strangers_are_rate_limited(world, monkeypatch):
    monkeypatch.setattr(state, "_MINTS_PER_MINUTE", 2)
    monkeypatch.setattr(state.httpsig, "new_keypair", lambda: ("PRIV", "PUB"))
    state._mints.clear()

    async def mint(n):
        return await state.keypair(f"{n:064x}", local=False)
    run(mint(1)); run(mint(2))
    with pytest.raises(state.MintLimited):
        run(mint(3))
    assert run(state.keypair(f"{4:064x}", local=True))["pub"] == "PUB"      # local users are never limited


def test_a_fediverse_reply_to_a_nostr_user_reaches_their_own_relays(world, monkeypatch):
    from app.services.activitypub import nostrside
    pk, npub = _nostr_user(world)
    delivered = []

    async def deliver(to, events, *, dm, force=False):
        delivered.append((to, [e["kind"] for e in events], dm))
        return 1
    monkeypatch.setattr(nostrside, "deliver", deliver)
    act = _create(extra={"tag": [{"type": "Mention", "href": f"{BASE}/ap/users/{npub}"}]})
    assert run(inbox.process(act, REMOTE)) == "stored"
    note = next(e for e in world["relay"].values() if e["kind"] == 1)
    assert ["p", pk] in note["tags"]
    assert delivered and delivered[0][0] == pk and delivered[0][1][-1] == 1 and delivered[0][2] is False


def test_a_nostr_users_reply_to_a_fediverse_note_goes_out(world, monkeypatch):
    from app.services import nostr_store
    pk, npub = _nostr_user(world)
    s = world["Session"]()
    puppet = ident.puppet_for(convert.account_from_actor(carol_actor()))
    s.add(FediPuppet(actor_uri=REMOTE, acct="carol@mastodon.example", pubkey_hex=puppet["pubkey_hex"], nip05_name="c"))
    s.add(FediBridgeDelivered(platform="activitypub", instance_url="https://mastodon.example", note_id="n",
                              note_uri="https://mastodon.example/notes/7", nostr_event_id="7" * 64,
                              nostr_pubkey=puppet["pubkey_hex"]))
    s.commit()
    from app.services.activitypub import dm
    dm._puppets["at"] = 0.0
    reply = member_post("hi carol", author=pk, created=1_700_000_050,
                        tags=[["e", "7" * 64, "", "reply"], ["p", puppet["pubkey_hex"]]])
    unrelated = member_post("just a post", author=pk, created=1_700_000_051)

    async def ws_query(port, filters, strict=False, **kw):
        f = filters[0]
        if "authors" in f:
            return []
        return [e for e in (reply, unrelated) if e["created_at"] >= f["since"]]
    monkeypatch.setattr(nostr_store, "_ws_query", ws_query)
    monkeypatch.setattr(state.httpsig, "new_keypair", lambda: KEY)
    world["docs"]["pcai:ap:cursor"] = {"since": 1_700_000_000}
    world["docs"]["pcai:ap:cursor:everyone"] = {"since": 1_700_000_000}
    run(outbox.tick())
    creates = [x for x in world["sent"] if x["activity"]["type"] == "Create"]
    assert len(creates) == 1, "only what concerns the fediverse leaves: %r" % creates
    assert creates[0]["activity"]["actor"] == f"{BASE}/ap/users/{npub}"
    assert creates[0]["activity"]["object"]["inReplyTo"] == "https://mastodon.example/notes/7"


# ============================================================================ 13. direct messages

ALICE_SK = bytes.fromhex("22" * 32)


def _real_alice(world):
    from app.services.nostr import nostr_service
    pk = nostr_service.derive_pubkey(ALICE_SK)
    world["settings"]["nostr_relay_nip05_names"] = f"alice {pk}"
    actors._names_cache["raw"] = None
    world["docs"]["pcai:ap:key:" + pk] = {"priv": KEY[0], "pub": KEY[1]}
    return pk


def _direct(to_pk_actor, note_id="https://mastodon.example/dm/1", text="<p>psst</p>"):
    note = {"id": note_id, "type": "Note", "attributedTo": REMOTE, "content": text, "to": [to_pk_actor], "cc": []}
    return {"id": note_id + "/a", "type": "Create", "actor": REMOTE, "object": note}


def test_a_fediverse_dm_arrives_encrypted_unless_the_sender_is_muted(world):
    """Anyone not blocked may DM, as on Nostr. (Requiring a follow silently dropped the first DM
    anybody sent -- "I tried to DM myself from detroitriotcity but never got it".)"""
    from app.services.nostr import nip17
    pk = _real_alice(world)
    carol_puppet = ident.puppet_for(convert.account_from_actor(carol_actor()))["pubkey_hex"]
    world["relay"]["m" * 64] = {"id": "m" * 64, "pubkey": pk, "kind": 10000, "created_at": 9,
                                "tags": [["p", carol_puppet]], "content": ""}
    muted = _direct(f"{BASE}/ap/users/alice", note_id="https://mastodon.example/dm/0")
    assert run(inbox.process(muted, REMOTE)) == "ignored: the recipient muted the sender"
    assert not [e for e in world["relay"].values() if e["kind"] == 1059]
    del world["relay"]["m" * 64]
    act = _direct(f"{BASE}/ap/users/alice")
    assert run(inbox.process(act, REMOTE)) == "delivered to 1"
    (wrap,) = [e for e in world["relay"].values() if e["kind"] == 1059]
    sender, text, rumor = nip17.unwrap(ALICE_SK, wrap)
    assert sender == carol_puppet and text == "psst"
    assert not [e for e in world["relay"].values() if e["kind"] == 1], "a DM became a public note"
    assert run(inbox.process(act, REMOTE)) == "already delivered"


def test_a_nostr_dm_to_a_fediverse_account_is_delivered_and_opens_the_conversation(world):
    from app.services.activitypub import dm
    from app.services.nostr import nip17
    pk = _real_alice(world)
    carol_puppet = ident.puppet_for(convert.account_from_actor(carol_actor()))
    s = world["Session"]()
    s.add(FediPuppet(actor_uri=REMOTE, acct="carol@mastodon.example", pubkey_hex=carol_puppet["pubkey_hex"], nip05_name="c"))
    s.commit()
    dm._puppets["at"] = 0.0
    wrap = nip17.wrap(ALICE_SK, carol_puppet["pubkey_hex"], "hello from nostr")
    assert run(dm.handle_wrap(wrap)) == "sent"
    (sent,) = world["sent"]
    note = sent["activity"]["object"]
    assert note["to"] == [REMOTE] and note["cc"] == [] and "hello from nostr" in note["content"]
    assert sent["activity"]["actor"] == f"{BASE}/ap/users/alice"
    assert run(state.in_conversation(pk, REMOTE)), "carol cannot write back"
    assert run(dm.handle_wrap(wrap)) == "already sent"
    # ...and now carol's reply gets through without alice following her.
    assert run(inbox.process(_direct(f"{BASE}/ap/users/alice", "https://mastodon.example/dm/2"), REMOTE)) == "delivered to 1"




def test_puppets_say_where_they_receive_dms_and_the_relay_takes_it(world):
    """Without a kind-10050 no Nostr client knows where to send a DM to a fediverse account."""
    s = world["Session"]()
    world["settings"]["nostr_relay_nip05_domain"] = DOMAIN
    run(ident.ensure_puppet(s, 1, {"uri": "https://m.example/users/z", "acct": "z@m.example", "display_name": "Z"}))
    lists = [e for e in world["relay"].values() if e["kind"] == 10050]
    assert lists and ["relay", f"wss://{DOMAIN}/relay"] in lists[0]["tags"]
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "app/services/nostr_relay/server.py").read_text()
    import re as _re
    m = _re.search(r"elif _is_puppet and kind in \(([\d, ]+)\):", src)
    assert m, "the relay's puppet branch is gone"
    kinds = {int(k) for k in m.group(1).replace(" ", "").split(",") if k}
    # a DM-relay list, a fediverse poll, a vote on a member's poll, and a Flag as a NIP-56 report
    assert {0, 1, 5, 6, 7, 10050, 1068, 1018, 1984} <= kinds




# ============================================================================ 14. the second review's findings

def test_a_fediverse_puppet_or_another_bridges_mirror_is_never_served_as_ours(world, client):
    pk, npub = _nostr_user(world)
    s = world["Session"]()
    s.add(FediPuppet(actor_uri="https://m.example/users/x", acct="x@m.example", pubkey_hex=pk, nip05_name="x"))
    s.commit()
    actors._known_cache.clear()
    assert not run(actors.exposed(pk)), "a puppet was served as our own account"
    assert client.get(f"/ap/users/{npub}").status_code == 404
    other_sk = bytes.fromhex("33" * 32)
    pk2, npub2 = _nostr_user(world, other_sk)
    world["relay"]["k" + pk2[:10]]["tags"] = [["proxy", "https://mastodon.social/users/y", "activitypub"]]
    actors._profile_cache.clear()
    assert not run(actors.exposed(pk2)), "another bridge's mirror account was served as ours"


def test_relay_lists_cannot_point_this_server_at_its_own_network(world):
    from app.services.activitypub import nostrside
    got = run(nostrside._clean(["wss://10.0.0.5", "wss://192.168.0.1", "wss://relay.lan", "wss://[::1]",
                                "wss://127.0.0.1", "wss://8.8.8.8:8443", "ws://8.8.8.8", "wss://8.8.8.8"]))
    assert got == ["wss://8.8.8.8"], got


def test_one_recipient_gets_a_bounded_number_of_pushes(world, monkeypatch):
    from app.services.activitypub import nostrside
    monkeypatch.setattr(nostrside, "PUSHES_PER_HOUR", 2)
    nostrside._pushes.clear()
    assert nostrside._push_ok("z" * 64) and nostrside._push_ok("z" * 64)
    assert not nostrside._push_ok("z" * 64)


def test_an_unreadable_follower_list_stops_the_everyone_pass_where_it_is(world, monkeypatch):
    from app.services import nostr_store
    evs = [member_post("x", author="d" * 64, created=1_700_000_010)]

    async def ws_query(port, filters, strict=False, **kw):
        return [] if "authors" in filters[0] else evs
    monkeypatch.setattr(nostr_store, "_ws_query", ws_query)
    real = nostr_store.list_docs

    async def failing(port, prefix, **kw):
        if prefix == "pcai:ap:follower:":
            raise RuntimeError("relay unreachable")
        return await real(port, prefix, **kw)
    monkeypatch.setattr(nostr_store, "list_docs", failing)
    state._nonlocal_cache["at"] = 0.0
    world["docs"]["pcai:ap:cursor"] = {"since": 1_700_000_000}
    world["docs"]["pcai:ap:cursor:everyone"] = {"since": 1_700_000_000}
    with pytest.raises(RuntimeError):
        run(outbox.tick())
    assert world["docs"]["pcai:ap:cursor:everyone"]["since"] == 1_700_000_000


def test_a_protected_event_is_never_republished(world):
    _with_follower(world)
    assert run(outbox.plan(member_post("private-ish", tags=[["-"]]), ALICE)) == []


def test_deliveries_and_follows_never_hit_the_anonymous_key_limit(world, monkeypatch):
    monkeypatch.setattr(state, "_MINTS_PER_MINUTE", 0)
    monkeypatch.setattr(state.httpsig, "new_keypair", lambda: ("P", "Q"))
    assert run(state.keypair("e" * 64))["pub"] == "Q"               # a delivery: never limited
    with pytest.raises(state.MintLimited):
        run(state.keypair("f" * 64, local=False))                   # an anonymous GET of a stranger


def test_a_pasted_profile_link_blocks_that_account_not_the_instance():
    from app.services import fedi_blocklist as fb
    hosts, accounts = fb.parse("https://mastodon.social/@bob\n")
    assert not fb.host_blocked("mastodon.social", hosts)
    assert fb.account_blocked("bob@mastodon.social", accounts, hosts)


def test_a_dm_to_a_blocked_fediverse_account_is_not_sent(world):
    from app.services.activitypub import dm
    from app.services.nostr import nip17
    _real_alice(world)
    world["settings"]["fedi_bridge_blocked_domains"] = "carol@mastodon.example"
    carol_puppet = ident.puppet_for(convert.account_from_actor(carol_actor()))
    s = world["Session"]()
    s.add(FediPuppet(actor_uri=REMOTE, acct="carol@mastodon.example", pubkey_hex=carol_puppet["pubkey_hex"], nip05_name="c"))
    s.commit()
    dm._puppets["at"] = 0.0
    assert run(dm.handle_wrap(nip17.wrap(ALICE_SK, carol_puppet["pubkey_hex"], "x"))) == "recipient blocked"
    assert world["sent"] == []


# ============================================================================ 12. follows made before it was on

def _k3_relay(world, monkeypatch, *, fail=False):
    from app.services import nostr_store

    async def ws_query(port, filters, timeout=6.0, **kw):
        if fail:
            raise RuntimeError("relay unreachable")
        f = filters[0]
        cand = [e for e in world["relay"].values() if e["kind"] in f.get("kinds", [])
                and e["pubkey"] in f.get("authors", [])]
        return sorted(cand, key=lambda e: -e["created_at"])[: f.get("limit", 500)]
    monkeypatch.setattr(nostr_store, "_ws_query", ws_query)
    outbox._caught_up.clear()


def _carol_puppet(world):
    s = world["Session"]()
    puppet = ident.puppet_for(convert.account_from_actor(carol_actor()))
    s.add(FediPuppet(actor_uri=REMOTE, acct="carol@mastodon.example", pubkey_hex=puppet["pubkey_hex"], nip05_name="c"))
    s.commit()
    return puppet["pubkey_hex"]


def test_a_contact_list_from_before_the_server_was_on_is_followed(world, monkeypatch):
    """The delivery pass only sees contact lists published after its cursor, and its first run sets
    the cursor to now -- so a member who already followed fediverse accounts had a fediverse account
    that followed nobody. The catch-up turns the CURRENT list into Follows, once."""
    carol = _carol_puppet(world)
    k3 = member_post("", kind=3, tags=[["p", carol], ["p", BOB]], created=1_690_000_000)
    world["relay"][k3["id"]] = k3
    _k3_relay(world, monkeypatch)
    run(outbox.catch_up_follows(limit=10))
    assert [(s["activity"]["type"], s["activity"]["object"]) for s in world["sent"]] == [("Follow", REMOTE)]
    assert REMOTE in run(state.following(ALICE))
    assert world["docs"]["pcai:ap:k3:" + ALICE]["since"] == 1_690_000_000
    # A restart (the in-process memory gone) does not send it again: the marker holds.
    outbox._caught_up.clear()
    world["sent"].clear()
    run(outbox.catch_up_follows(limit=10))
    assert world["sent"] == []


def test_the_catch_up_decides_nothing_on_a_relay_it_cannot_read(world, monkeypatch):
    carol = _carol_puppet(world)
    k3 = member_post("", kind=3, tags=[["p", carol]], created=1_690_000_000)
    world["relay"][k3["id"]] = k3
    _k3_relay(world, monkeypatch, fail=True)
    run(outbox.catch_up_follows(limit=10))
    assert world["sent"] == []
    assert "pcai:ap:k3:" + ALICE not in world["docs"], "an unread list was recorded as handled"
    _k3_relay(world, monkeypatch)                       # readable again: it happens then
    run(outbox.catch_up_follows(limit=10))
    assert [s["activity"]["type"] for s in world["sent"]] == ["Follow"]


def test_the_delivery_pass_hands_a_contact_list_to_the_catch_up_and_keeps_going(world, monkeypatch):
    """A contact list of hundreds of fediverse accounts is hundreds of actor fetches. Inside the tick's
    time limit it timed out, the cursor never moved, and every post behind it waited ("I didn't see
    anything on DRC"). The tick now passes the list to the catch-up and delivers what follows it."""
    carol = _carol_puppet(world)
    _with_follower(world)
    world["docs"]["pcai:ap:cursor"] = {"since": 1_700_000_000}
    world["docs"]["pcai:ap:cursor:everyone"] = {"since": 1_700_000_000}
    k3 = member_post("", kind=3, tags=[["p", carol]], created=1_700_000_050)
    post = member_post("after the follow list", created=1_700_000_060)
    world["relay"][k3["id"]] = k3
    world["relay"][post["id"]] = post

    async def since(members, since, kinds=None):
        return [e for e in world["relay"].values() if e["created_at"] > since and e["kind"] in (kinds or outbox.KINDS)
                and (members is None or e["pubkey"] in members)]
    monkeypatch.setattr(outbox, "_since", since)
    world["settings"]["activitypub_everyone"] = "false"
    _k3_relay(world, monkeypatch)
    run(outbox.tick())
    assert [s["activity"]["type"] for s in world["sent"]] == ["Create"], "the post waited behind the follow list"
    assert world["docs"]["pcai:ap:cursor"]["since"] == 1_700_000_060
    world["sent"].clear()
    run(outbox.catch_up_follows(limit=10))
    assert [s["activity"]["type"] for s in world["sent"]] == ["Follow"]
    world["sent"].clear()
    outbox._caught_up.clear()
    run(outbox.catch_up_follows(limit=10))
    assert world["sent"] == [], "a handled list was followed again"


# ============================================================================ 13. importing from a public follow list

def _public_server(monkeypatch, *, following, hidden=False, count=None):
    from app.services.activitypub import importer
    import httpx
    asked = []

    def handler(request):
        asked.append(request)
        assert "authorization" not in request.headers, "a public import sent credentials"
        if request.url.path == "/api/v1/accounts/lookup":
            if request.url.params.get("acct") != "me":
                return httpx.Response(404, json={"error": "not found"})
            return httpx.Response(200, json={"id": "42", "following_count": count if count is not None else len(following)})
        if request.url.path == "/api/v1/accounts/42/following":
            if hidden:
                return httpx.Response(403, json={"error": "hidden"})
            return httpx.Response(200, json=following)
        return httpx.Response(404)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: real_client(**{**kw, "transport": httpx.MockTransport(handler)}))

    async def ok(url):
        return None
    monkeypatch.setattr(remote, "_check", ok)
    return asked


def test_a_public_follow_list_imports_by_address_with_no_login(world, monkeypatch):
    from app.services.activitypub import importer
    _public_server(monkeypatch, following=[_pleroma_account(1), _pleroma_account(2)])
    accounts, base = run(importer.public_following("@me@old.example"))
    assert base == "https://old.example"
    assert [a["id"] for a in accounts] == ["1", "2"]


def test_a_public_import_says_why_it_cannot(world, monkeypatch):
    from app.services.activitypub import importer
    _public_server(monkeypatch, following=[], hidden=True, count=12)
    with pytest.raises(ValueError, match="private"):
        run(importer.public_following("me@old.example"))
    with pytest.raises(ValueError, match="not found"):
        run(importer.public_following("nobody@old.example"))
    with pytest.raises(ValueError, match="name@server"):
        run(importer.public_following("just-a-name"))


def test_a_public_import_never_reaches_a_private_address(world, monkeypatch):
    from app.services.activitypub import importer
    for handle in ("me@127.0.0.1", "me@10.0.0.5:8080"):
        with pytest.raises(ValueError, match="Cannot read"):
            run(importer.public_following(handle))


# ============================================================================ 14. split DNS: neighbours on this network

def test_a_server_on_this_network_is_reachable_only_when_an_admin_named_it(world, monkeypatch):
    """detroitriotcity.com resolves to the router's LAN address from here (split DNS), so the SSRF
    guard refused it -- the import, and every key fetch and delivery with it. Trusted: the Pleroma
    bridge's instance and the admin's list. Never a name that merely resolves nearby (router.lan)."""
    from app.services import rss_service
    monkeypatch.setattr(rss_service, "is_safe_host", lambda url: False)     # everything resolves privately
    real_check = remote._check
    world["settings"]["activitypub_lan_hosts"] = "https://drc.example/\nhttps://other.example/path  # ours too\nthird.example:8443"
    for ok in ("https://drc.example/users/a", "https://other.example/inbox", "https://third.example/x"):
        run(real_check(ok))
    with pytest.raises(remote.FetchError, match="private address"):
        run(real_check("https://evil.example/"))
    with pytest.raises(remote.FetchError):
        run(real_check("https://router.lan/admin"))


def test_importing_from_this_server_says_to_type_the_old_account(world):
    from app.services.activitypub import importer
    with pytest.raises(ValueError, match="OLD account"):
        run(importer.public_following(f"@alice@{DOMAIN}"))


# ============================================================================ 15. mentions are links, not text

def test_mentions_in_a_fediverse_post_become_profile_links(world):
    """A fediverse note carries its mentions as plain text plus a Mention list. Stored as-is the names
    were dead text and only our own users were tagged ("usernames not clickable in fediverse posts").
    Ours link their real key; anybody else links their puppet, the way the Pleroma bridge does."""
    from app.services.nostr import bech32
    world["docs"][f"pcai:ap:following:{ALICE}:x"] = {"actor": REMOTE, "inbox": "i", "state": "accepted"}
    world["actors"]["https://other.example/users/dave"] = {
        "id": "https://other.example/users/dave", "type": "Person", "preferredUsername": "dave",
        "inbox": "https://other.example/users/dave/inbox"}
    note = _create(content="<p>@alice @dave@other.example hi there</p>", extra={"tag": [
        {"type": "Mention", "href": f"{BASE}/ap/users/alice", "name": f"@alice@{DOMAIN}"},
        {"type": "Mention", "href": "https://other.example/users/dave", "name": "@dave@other.example"}]})
    assert run(inbox.process(note, REMOTE)) == "stored"
    ev = next(e for e in world["relay"].values() if e["kind"] == 1)
    alice_ref = "nostr:" + bech32.encode("npub", bytes.fromhex(ALICE))
    assert alice_ref in ev["content"], ev["content"]
    assert "@dave" not in ev["content"] and ev["content"].count("nostr:npub1") == 2, ev["content"]
    ps = [t[1] for t in ev["tags"] if t[0] == "p"]
    assert ALICE in ps and len(ps) == 2 and len(set(ps)) == 2, ps
    assert ev["content"].endswith("hi there")


def test_a_reply_that_mentions_its_parent_author_tags_them_once(world):
    """The parent's author is p-tagged for the thread AND named in the Mention list; they were tagged
    twice (the report's post carried the same p tag two times)."""
    mine = member_post("my post")
    world["relay"][mine["id"]] = mine
    act = _create(content="<p>@alice I see you</p>", extra={
        "inReplyTo": convert.object_url(BASE, mine["id"]),
        "tag": [{"type": "Mention", "href": f"{BASE}/ap/users/alice", "name": f"@alice@{DOMAIN}"}]})
    assert run(inbox.process(act, REMOTE)) == "stored"
    reply = next(e for e in world["relay"].values() if e["kind"] == 1 and e["id"] != mine["id"])
    assert [t for t in reply["tags"] if t[0] == "p"] == [["p", ALICE]], reply["tags"]
    assert reply["content"].startswith("nostr:npub1") and reply["content"].endswith("I see you")


def test_the_retired_pleromas_favicon_address_shows_our_icon(client):
    """Servers that met poster.place when it ran Pleroma stored that instance's favicon URL and keep
    using it; the file is gone, so our posts showed no instance icon there."""
    r = client.get("/media/cb/93/65/cb9365f4ea06831500dde507896aa018db5da207979abdc44add6cc00a67e2e9.webp")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content[:4] == b"\x89PNG"
    assert client.get("/media/cb/93/65/other.webp").status_code == 404, "only that one address is served"


# ============================================================================ 16. a block made from the client

def test_a_fediverse_account_blocked_by_key_is_refused_at_the_inbox(world):
    """Blocking somebody from the client blocks their KEY on the relay -- for a fediverse account, its
    puppet's. Posts that arrive over ActivityPub are written by the server itself, so the inbox has to
    ask too: "I blocked them but I still see new posts"."""
    world["docs"][f"pcai:ap:following:{ALICE}:x"] = {"actor": REMOTE, "inbox": "i", "state": "accepted"}
    assert run(inbox.process(_create(), REMOTE)) == "stored"          # followed: stored
    puppet = ident.puppet_for(convert.account_from_actor(carol_actor()))
    world["settings"]["nostr_relay_blocked_pubkeys"] = puppet["npub"]
    assert run(inbox.process(_create("https://mastodon.example/notes/2"), REMOTE)) == "ignored: blocked account"
    # ...and nothing more is DELIVERED to them either.
    world["docs"][f"pcai:ap:follower:{ALICE}:1"] = {"actor": REMOTE, "inbox": "https://mastodon.example/inbox"}
    assert run(outbox.plan(member_post("to my followers"), ALICE)) == []


# ============================================================================ 17. readable handles for Nostr users

def test_a_nostr_user_shows_a_readable_handle_not_an_npub(client, world):
    """`@npub1…@poster.place` on every fediverse post "looks terrible". The handle an account SHOWS is
    made once from its profile name plus a short key suffix; its address stays /ap/users/<npub> and
    WebFinger answers by either name with the same subject, which is what Mastodon checks."""
    pk, npub = _nostr_user(world)
    nick = f"dana_{pk[:4]}"
    doc = client.get(f"/ap/users/{npub}", headers={"Accept": "application/activity+json"}).json()
    assert doc["id"] == f"{BASE}/ap/users/{npub}" and doc["preferredUsername"] == nick
    for asked in (nick, npub):
        r = client.get(f"/.well-known/webfinger?resource=acct:{asked}@{DOMAIN}").json()
        assert r["subject"] == f"acct:{nick}@{DOMAIN}", asked
        assert r["links"][0]["href"] == f"{BASE}/ap/users/{npub}", asked
    # ...and a mention of them in an outgoing post reads @dana_xxxx, not a key.
    who = run(outbox.resolve_pubkey(pk))
    assert who["name"] == f"@{nick}@{DOMAIN}" and who["href"] == f"{BASE}/ap/users/{npub}"


def test_a_readable_handle_never_changes_and_never_collides(world):
    pk, npub = _nostr_user(world)
    first = run(actors.readable_handle(pk))
    # A profile rename does not move it (follows and mentions name the handle).
    world["relay"]["k" + pk[:10]]["content"] = json.dumps({"name": "Somebody Else"})
    world["relay"]["k" + pk[:10]]["created_at"] = 99
    actors._nick_cache.clear()
    actors._profile_cache.clear()
    assert run(actors.readable_handle(pk)) == first
    # Somebody else already holding "dana_<same prefix>" gets a longer suffix, never the same name.
    other = "%s%s" % (pk[:4], "0" * 60)
    world["relay"]["kx"] = {"id": "kx", "pubkey": other, "kind": 0, "created_at": 5, "tags": [],
                            "content": json.dumps({"name": "Dana"})}
    got = run(actors.readable_handle(other))
    assert got != first and got.startswith("dana_" + other[:6])
    assert run(actors.member_by_name(got)) == other and run(actors.member_by_name(first)) == pk


def test_no_handle_is_minted_on_a_failed_read(world, monkeypatch):
    pk, npub = _nostr_user(world)

    async def broken(*a, **k):
        raise RuntimeError("relay down")
    from app.services import nostr_store
    monkeypatch.setattr(nostr_store, "get_doc", broken)
    assert run(actors.readable_handle(pk)) == npub
    assert not [k for k in world["docs"] if k.startswith("pcai:ap:nick")]


# ============================================================================ 18. custom emoji and emoji reactions

EMO = "https://poster.place/emoji/blobcat.png"


def _mirrored_carol_note(world):
    s = world["Session"]()
    puppet = ident.puppet_for(convert.account_from_actor(carol_actor()))
    s.add(FediPuppet(actor_uri=REMOTE, acct="carol@mastodon.example", pubkey_hex=puppet["pubkey_hex"], nip05_name="c"))
    s.add(FediBridgeDelivered(platform="activitypub", instance_url="https://mastodon.example", note_id="n",
                              note_uri="https://mastodon.example/notes/7", nostr_event_id="7" * 64,
                              nostr_pubkey=puppet["pubkey_hex"]))
    s.commit()
    return puppet["pubkey_hex"]


def test_a_post_with_custom_emoji_carries_their_images(world):
    """Without an Emoji tag the fediverse prints ":blobcat:" as text ("custom emojis are not
    displaying on the fediverse")."""
    _with_follower(world)
    ev = member_post("hi :blobcat: and :unknown:", tags=[["emoji", "blobcat", EMO], ["emoji", "unused", EMO]])
    note = run(outbox.plan(ev, ALICE))[0][1]["object"]
    emo = [t for t in note["tag"] if t["type"] == "Emoji"]
    assert emo == [{"id": EMO, "type": "Emoji", "name": ":blobcat:",
                    "icon": {"type": "Image", "mediaType": "image/png", "url": EMO}}]
    assert ":blobcat:" in note["content"]


def test_emoji_reactions_go_out_as_reactions_and_a_dislike_stays_home(world):
    carol = _mirrored_carol_note(world)
    tags = [["e", "7" * 64], ["p", carol], ["k", "1"]]
    plain = run(outbox.plan(member_post("+", kind=7, tags=tags), ALICE))[0][1]
    assert plain["type"] == "Like" and "content" not in plain
    fire = run(outbox.plan(member_post("🔥", kind=7, tags=tags, created=1_700_000_001), ALICE))[0][1]
    assert fire["content"] == "🔥" and fire["_misskey_reaction"] == "🔥"
    custom = run(outbox.plan(member_post(":blobcat:", kind=7, tags=tags + [["emoji", "blobcat", EMO]],
                                         created=1_700_000_002), ALICE))[0][1]
    assert custom["content"] == ":blobcat:" and custom["tag"][0]["icon"]["url"] == EMO
    assert run(outbox.plan(member_post("-", kind=7, tags=tags, created=1_700_000_003), ALICE)) == []


def test_incoming_emoji_reactions_keep_their_emoji(world):
    mine = member_post("react to me")
    world["relay"][mine["id"]] = mine
    target = convert.object_url(BASE, mine["id"])
    custom = {"id": "https://mastodon.example/react/1", "type": "EmojiReact", "actor": REMOTE, "object": target,
              "content": ":blobcat:", "tag": [{"type": "Emoji", "name": ":blobcat:",
                                               "icon": {"type": "Image", "url": EMO}}]}
    assert run(inbox.process(custom, REMOTE)) == "like stored"
    misskey = {"id": "https://mastodon.example/react/2", "type": "Like", "actor": REMOTE, "object": target,
               "_misskey_reaction": "🎉"}
    assert run(inbox.process(misskey, REMOTE)) == "like stored"
    got = {e["content"]: e for e in world["relay"].values() if e["kind"] == 7}
    assert ["emoji", "blobcat", EMO] in got[":blobcat:"]["tags"], "a custom reaction arrived as bare text"
    assert "🎉" in got


# ============================================================================ 19. the outbox

def test_the_outbox_serves_recent_posts_so_a_profile_is_not_empty(client, world, monkeypatch):
    """Akkoma and Mastodon fill a remote profile from its outbox. It answered a count of 0, so a
    profile opened on another server showed nothing ("profile not visible on detroitriotcity")."""
    from app.services import nostr_store

    async def ws_query(port, filters, **kw):
        f = filters[0]
        rows = [e for e in world["relay"].values() if e["kind"] in f.get("kinds", [])
                and e["pubkey"] in f.get("authors", [e["pubkey"]])
                and e["created_at"] <= f.get("until", 1 << 62)]
        return sorted(rows, key=lambda e: -e["created_at"])[: f.get("limit", 500)]
    monkeypatch.setattr(nostr_store, "_ws_query", ws_query)
    for i in range(25):
        p = member_post(f"post {i}", created=1_700_000_000 + i)
        world["relay"][p["id"]] = p
    mirror = member_post("not mine", created=1_700_001_000, tags=[["proxy", "https://x.example/1", "activitypub"]])
    world["relay"][mirror["id"]] = mirror
    h = {"Accept": "application/activity+json"}
    coll = client.get("/ap/users/alice/outbox", headers=h).json()
    assert coll["totalItems"] == 25 and coll["first"].endswith("/outbox?page=true")
    page = client.get(coll["first"].replace(BASE, ""), headers=h).json()
    texts = [a["object"]["content"] for a in page["orderedItems"]]
    assert len(texts) == 20 and texts[0] == "<p>post 24</p>" and "not mine" not in " ".join(texts)
    assert all(a["type"] == "Create" and a["object"]["attributedTo"] == f"{BASE}/ap/users/alice"
               for a in page["orderedItems"])
    rest = client.get(page["next"].replace(BASE, ""), headers=h).json()
    assert [a["object"]["content"] for a in rest["orderedItems"]] == [f"<p>post {i}</p>" for i in range(4, -1, -1)]


# ============================================================================ 20. blocks from the fediverse

def test_a_fediverse_block_is_recorded_and_cuts_the_follows(world):
    """A Block of one of ours is what the block bot reports and counts. As on Mastodon it also ends
    the follows between the two, and an Undo -- which names our actor too -- is not an unfollow."""
    run(state.add_follower(ALICE, REMOTE, "https://mastodon.example/inbox"))
    run(state.set_following(ALICE, REMOTE, "https://mastodon.example/inbox", "accepted"))
    block = {"id": "https://mastodon.example/blocks/1", "type": "Block", "actor": REMOTE,
             "object": f"{BASE}/ap/users/alice"}
    assert run(inbox.process(block, REMOTE)) == "block recorded"
    assert [(b["member"], b["actor"], b["acct"]) for b in run(state.blocks())] == [(ALICE, REMOTE, "carol@mastodon.example")]
    assert run(state.followers(ALICE, strict=False)) == []
    assert REMOTE not in run(state.following(ALICE, strict=False))
    undo = {"id": "https://mastodon.example/blocks/1/undo", "type": "Undo", "actor": REMOTE, "object": block}
    assert run(inbox.process(undo, REMOTE)) == "unblocked"
    assert run(state.blocks()) == []
    stranger = dict(block, object="https://elsewhere.example/users/x")
    assert run(inbox.process(stranger, REMOTE)) == "ignored: not one of our actors"


# ============================================================================ 21. the security review (2026-09-24)

REAL_FETCH_OBJECT = remote.fetch_object
REAL_FETCH_JSON = remote.fetch_json
REAL_DELIVER = remote.deliver
REAL_SCHEDULE = inbox.schedule
_ALL_SIGNED = ("(request-target)", "host", "date", "digest")


def test_a_mention_cannot_claim_somebody_elses_identity(world):
    """A Mention tag's `href` and `name` are whatever the sender wrote. Trusted, one note registered
    `victim@mastodon.social` against an address the SENDER controls -- and every later sighting of the
    real person reused that record: their key, their DMs, the right to delete their posts. A mentioned
    account now gets an identity only from its own server's actor document."""
    world["docs"][f"pcai:ap:following:{ALICE}:x"] = {"actor": REMOTE, "inbox": "i", "state": "accepted"}
    world["actors"]["https://evil.example/u/v"] = {"id": "https://evil.example/u/v", "type": "Person",
                                                   "preferredUsername": "v", "inbox": "https://evil.example/u/v/inbox"}
    note = _create(content="<p>@victim@mastodon.social hi</p>", extra={"tag": [
        {"type": "Mention", "href": "https://evil.example/u/v", "name": "@victim@mastodon.social"},
        {"type": "Mention", "href": "https://evil.example/u/nobody", "name": "@other@mastodon.social"}]})
    assert run(inbox.process(note, REMOTE)) == "stored"
    s = world["Session"]()
    assert not s.query(FediPuppet).filter(FediPuppet.acct.like("%mastodon.social")).all(), \
        "a Mention tag registered a handle on another server against the sender's address"
    assert [r.acct for r in s.query(FediPuppet).filter(FediPuppet.actor_uri == "https://evil.example/u/v")] \
        == ["v@evil.example"], "the identity must be the actor's own"
    # And the real person, seen later, gets THEIR OWN key.
    real = {"uri": "https://mastodon.social/users/victim", "url": "https://mastodon.social/users/victim",
            "acct": "victim@mastodon.social", "username": "victim"}
    p = run(ident.ensure_puppet(s, 1, real, "mastodon.social"))
    assert p["pubkey_hex"] == ident.puppet_for(real)["pubkey_hex"]


def test_a_handle_is_reused_only_for_the_same_persons_other_address(world):
    """`ensure_puppet` reuses a record by handle for ONE case: Mastodon's two spellings of one person
    on one server (/@alice beside /users/alice). A handle alone never proves it."""
    s = world["Session"]()
    s.add(FediPuppet(actor_uri="https://evil.example/u/v", acct="victim@mastodon.social",
                     pubkey_hex="ee" * 32, nip05_name="x"))
    s.add(FediPuppet(actor_uri="https://m.example/@ann", acct="ann@m.example", pubkey_hex="aa" * 32, nip05_name="ann"))
    s.add(FediPuppet(actor_uri="https://lemmy.example/c/foo", acct="foo@lemmy.example", pubkey_hex="cc" * 32,
                     nip05_name="foo"))
    s.commit()
    victim = {"uri": "https://mastodon.social/users/victim", "acct": "victim@mastodon.social", "username": "victim"}
    assert run(ident.ensure_puppet(s, 1, victim, "", profile_refresh=False))["pubkey_hex"] == \
        ident.puppet_for(victim)["pubkey_hex"], "a handle claimed on another server took the real person's key"
    lemmy_user = {"uri": "https://lemmy.example/u/foo", "acct": "foo@lemmy.example", "username": "foo"}
    assert run(ident.ensure_puppet(s, 1, lemmy_user, "", profile_refresh=False))["pubkey_hex"] == \
        ident.puppet_for(lemmy_user)["pubkey_hex"], "a Lemmy user took the community of the same name's key"
    ann = {"uri": "https://m.example/users/ann", "acct": "ann@m.example", "username": "ann"}
    assert run(ident.ensure_puppet(s, 1, ann, "", profile_refresh=False))["pubkey_hex"] == \
        ident.puppet_for({"uri": "https://m.example/@ann", "acct": "ann@m.example"})["pubkey_hex"], \
        "the same person's two addresses on one server must stay one identity"


def _sign_names(names, *, body=b'{"type":"Create"}', algorithm="rsa-sha256"):
    """A GENUINE signature over exactly `names` -- so only the header-set rule can refuse it."""
    import base64
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    headers = {"host": DOMAIN, "date": httpsig.http_date(), "digest": httpsig.digest_of(body)}
    text = httpsig._signing_string("POST", "/ap/inbox", headers, list(names))
    key = serialization.load_pem_private_key(KEY[0].encode(), password=None)
    sig = base64.b64encode(key.sign(text.encode(), padding.PKCS1v15(), hashes.SHA256())).decode()
    return headers, {"keyId": "k", "algorithm": algorithm, "headers": list(names), "signature": sig}


def test_a_signature_must_cover_the_target_host_date_and_body():
    body = b'{"type":"Create"}'
    for algorithm in ("rsa-sha256", "hs2019"):                         # the control: both spellings verify
        h, p = _sign_names(_ALL_SIGNED, algorithm=algorithm)
        httpsig.verify("POST", "/ap/inbox", h, body, KEY[1], p)
    for missing in _ALL_SIGNED:
        h, p = _sign_names([n for n in _ALL_SIGNED if n != missing])
        with pytest.raises(httpsig.SignatureError):
            httpsig.verify("POST", "/ap/inbox", h, body, KEY[1], p)
    h, p = _sign_names(_ALL_SIGNED, algorithm="rsa-sha512")
    with pytest.raises(httpsig.SignatureError):
        httpsig.verify("POST", "/ap/inbox", h, body, KEY[1], p)


def test_the_inbox_bounds_what_it_will_take(client, monkeypatch):
    from app.routers import activitypub as routes
    assert client.post("/ap/inbox", content=b"x" * (routes.MAX_BODY + 1)).status_code == 413
    assert client.post("/ap/inbox", content=b"not json").status_code == 400
    assert client.post("/ap/inbox", content=b"[1]").status_code == 400
    monkeypatch.setattr(routes, "PER_HOST_PER_MINUTE", 1)
    routes._hits.clear()
    assert _post_signed(client, _create()).status_code == 202
    assert _post_signed(client, _create()).status_code == 429
    assert len(client.scheduled) == 1
    monkeypatch.setattr(routes, "PER_HOST_PER_MINUTE", 300)
    routes._hits.clear()
    monkeypatch.setattr(inbox, "schedule", lambda act, signer: False)
    r = _post_signed(client, _create())
    assert r.status_code == 503 and r.headers["retry-after"] == "120"


def test_nobody_can_spend_a_real_servers_budget_by_naming_it(client, monkeypatch):
    """The per-server budget was charged to the host in the UNVERIFIED keyId, before the signature
    was checked -- so junk naming mastodon.example made every genuine delivery from it 429. It is
    charged to who SIGNED now; unverified junk is charged to the connection."""
    from app.routers import activitypub as routes
    monkeypatch.setattr(routes, "PER_HOST_PER_MINUTE", 3)
    junk = {"Signature": f'keyId="{REMOTE}#main-key",algorithm="rsa-sha256",headers="(request-target) host date digest",'
                         'signature="AAAA"', "Content-Type": "application/activity+json"}
    for _ in range(5):
        client.post("/ap/inbox", content=json.dumps(_create()).encode(), headers=junk)
    assert routes._hits.get("mastodon.example", (0, 0))[1] == 0, "junk spent the named server's budget"
    # The connection that sent it has run out -- and the genuine server has not.
    monkeypatch.setattr(routes, "_client_ip", lambda request: "203.0.113.9")
    routes._hits.clear()
    for _ in range(3):
        client.post("/ap/inbox", content=json.dumps(_create()).encode(), headers=junk)
    assert client.post("/ap/inbox", content=json.dumps(_create()).encode(), headers=junk).status_code == 429
    monkeypatch.setattr(routes, "_client_ip", lambda request: "198.51.100.7")
    assert _post_signed(client, _create()).status_code == 202


def test_a_refused_signature_says_nothing_about_what_we_fetched(client, monkeypatch):
    """The error text of a fetch made on the sender's say-so ("HTTP 404 from …", a connect error)
    turned the 401 into a port scanner."""
    async def pk(key_id, refresh=False):
        raise remote.FetchError("HTTP 404 from 10.0.0.7:8080 -- ConnectError")
    monkeypatch.setattr(remote, "public_key", pk)
    r = _post_signed(client, _create())
    assert r.status_code == 401 and "10.0.0.7" not in r.text and "404" not in r.text


def test_an_unverifiable_delete_is_acknowledged_and_never_acted_on(client, monkeypatch):
    gone = "https://gone.example/users/x"                              # its key can no longer be fetched
    confirmed = []

    async def confirm(actor):
        confirmed.append(actor)
    monkeypatch.setattr(inbox, "confirm_gone", confirm)
    delete = {"id": gone + "#delete", "type": "Delete", "actor": gone, "object": gone}
    assert _post_signed(client, delete, key_id=gone + "#main-key").status_code == 202
    assert client.scheduled == [], "an unverified Delete was processed"
    import time as _t
    for _ in range(50):                      # the confirmation is a background task
        if confirmed:
            break
        _t.sleep(0.02)
    assert confirmed == [gone], "a self-Delete must be CONFIRMED with the account's own server"
    assert _post_signed(client, dict(_create(), actor=gone), key_id=gone + "#main-key").status_code == 401
    forged = {"id": REMOTE + "#delete", "type": "Delete", "actor": REMOTE, "object": "https://mastodon.example/notes/1"}
    assert _post_signed(client, forged, key=KEY).status_code == 401
    assert client.scheduled == []


def test_a_deleted_account_is_only_believed_from_its_own_server(world, monkeypatch):
    answers = {"https://gone.example/users/x": remote.FetchError("HTTP 410 from gone.example"),
               "https://alive.example/users/y": {"id": "https://alive.example/users/y", "type": "Person"},
               # A server that is merely DOWN has not said the account is gone.
               "https://down.example/users/z": remote.FetchError("HTTP 503 from down.example")}

    async def fetch_json(url, signed=True):
        a = answers[url]
        if isinstance(a, Exception):
            raise a
        return a
    monkeypatch.setattr(remote, "fetch_json", fetch_json)
    run(inbox.confirm_gone("https://gone.example/users/x"))
    run(inbox.confirm_gone("https://alive.example/users/y"))
    run(inbox.confirm_gone("https://down.example/users/z"))
    assert run(state.gone_actors()) == {"https://gone.example/users/x"}


def test_a_gone_follower_is_sent_nothing_more(world):
    _with_follower(world)
    world["docs"][f"pcai:ap:follower:{ALICE}:2"] = {"actor": "https://other.example/users/f",
                                                   "inbox": "https://other.example/inbox"}
    assert run(inbox.process({"id": "https://other.example/users/f#delete", "type": "Delete",
                              "actor": "https://other.example/users/f", "object": "https://other.example/users/f"},
                             "https://other.example/users/f")) == "account deleted"
    outbox._gone_cache["at"] = 0.0
    assert [i for i, _ in run(outbox.plan(member_post("hi"), ALICE))] == ["https://mastodon.example/inbox"]


def test_a_bad_signature_refetches_the_key_at_most_once_per_ten_minutes(client, monkeypatch):
    calls = []

    async def pk(key_id, refresh=False):
        calls.append(refresh)
        return REMOTE, CAROL_KEY[1]
    monkeypatch.setattr(remote, "public_key", pk)
    for _ in range(3):
        assert _post_signed(client, _create(), key=KEY).status_code == 401
    assert calls == [False, True, False, False], "every bad signature made us re-fetch a third party's actor"


def _web(monkeypatch, pages, private=()):
    import httpx
    from app.services import rss_service
    monkeypatch.setattr(rss_service, "is_safe_host", lambda url: remote.host_of(url) not in private)
    asked = []

    def handler(request):
        asked.append(str(request.url))
        make = pages.get(str(request.url))
        return make() if make else httpx.Response(404)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(remote, "client", lambda **kw: real_client(
        transport=httpx.MockTransport(handler), follow_redirects=False, trust_env=False,
        **{k: v for k, v in kw.items() if k != "transport"}))
    return asked


def test_every_fetch_is_guarded_on_every_hop(world, monkeypatch):
    import httpx

    def J(doc):
        return lambda: httpx.Response(200, json=doc)
    pages = {
        "https://a.example/hop": lambda: httpx.Response(302, headers={"location": "https://b.example/doc"}),
        "https://b.example/doc": J({"id": "https://b.example/doc"}),
        "https://a.example/moved": lambda: httpx.Response(301, headers={"location": "/doc"}),
        "https://a.example/doc": J({"id": "https://a.example/doc"}),
        "https://a.example/big": lambda: httpx.Response(200, content=b'{"x":"' + b"a" * 5000 + b'"}'),
        "https://a.example/list": J([1, 2]),
        "https://a.example/liar": J({"id": "https://a.example/somebody-else"}),
    }
    asked = _web(monkeypatch, pages, private={"inside.example"})
    monkeypatch.setattr(remote, "MAX_BYTES", 1000)
    monkeypatch.setattr(remote, "fetch_object", REAL_FETCH_OBJECT)
    assert run(remote.fetch_json("https://a.example/moved")) == {"id": "https://a.example/doc"}
    for bad, why in (("https://a.example/hop", "another host"), ("https://a.example/big", "too large"),
                     ("https://a.example/list", "JSON object"), ("http://a.example/doc", "https"),
                     ("https://127.0.0.1/x", "https"), ("https://blocked.example/users/x", "blocked"),
                     (f"{BASE}/ap/users/alice", "this node"), ("https://inside.example/x", "private address"),
                     ("https://a.example:2375/x", "port")):
        with pytest.raises(remote.FetchError, match=why):
            run(remote.fetch_json(bad))
    with pytest.raises(remote.FetchError, match="not the object"):
        run(remote.fetch_object("https://a.example/liar"))
    for host in ("b.example", "blocked.example", "inside.example", DOMAIN, "127.0.0.1", "http://a.", ":2375"):
        assert not any(host in u for u in asked), (host, asked)
    n = len(asked)
    # A BLOCKED inbox is refused for good (403: retrying cannot change it); an unreachable one is
    # "no answer" (0), which the queue retries.
    assert run(REAL_DELIVER("https://blocked.example/inbox", {"type": "Create"}, key_id="k", private_pem=KEY[0])) == 403
    for inbox_url in ("https://inside.example/inbox", "http://a.example/inbox"):
        assert run(REAL_DELIVER(inbox_url, {"type": "Create"}, key_id="k", private_pem=KEY[0])) == 0
    assert len(asked) == n, "a delivery reached a guarded address"


def test_a_connection_goes_to_the_address_that_was_checked(world, monkeypatch):
    """DNS rebinding: the name was checked, then resolved AGAIN to connect -- a name answering
    public-then-private reached the private one. The transport resolves once, judges every answer,
    and connects to that IP with the name kept for TLS and Host."""
    import httpx
    import socket
    answers = {"rebind.example": "10.0.0.5", "fine.example": "93.184.216.34"}
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda host, port, **kw: [(2, 1, 6, "", (answers[host], port))])
    seen = []

    async def handle(self, request):
        seen.append((request.url.host, request.extensions.get("sni_hostname"), request.headers.get("host")))
        return httpx.Response(200, json={})
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle)

    async def go(url):
        async with remote.client() as c:
            return await c.get(url)
    with pytest.raises(httpx.ConnectError, match="private"):
        run(go("https://rebind.example/x"))
    assert run(go("https://fine.example/x")).status_code == 200
    assert seen == [("93.184.216.34", "fine.example", "fine.example")]
    world["settings"]["activitypub_lan_hosts"] = "rebind.example"   # a neighbour the admin named
    assert run(go("https://rebind.example/x")).status_code == 200


def test_one_server_cannot_hold_every_inbox_slot(world, monkeypatch):
    started = []

    async def slow(activity, signer):
        started.append(signer)
        await asyncio.sleep(3600)
    monkeypatch.setattr(inbox, "process", slow)
    monkeypatch.setattr(inbox, "MAX_PENDING_PER_HOST", 2)

    async def go():
        a = [REAL_SCHEDULE(_create(), REMOTE) for _ in range(3)]
        b = REAL_SCHEDULE(_create(), "https://other.example/users/x")
        await asyncio.sleep(0)
        for t in list(inbox._tasks):
            t.cancel()
        await asyncio.sleep(0)
        return a, b
    a, b = run(go())
    assert a == [True, True, False] and b is True
    assert inbox._pending_by_host == {}, "a finished task must give its slot back"


def test_one_server_can_add_only_so_many_new_followers_an_hour(world, monkeypatch):
    monkeypatch.setattr(inbox, "NEW_FOLLOWERS_PER_HOST_PER_HOUR", 1)
    follow = {"id": "https://mastodon.example/f/1", "type": "Follow", "actor": REMOTE, "object": f"{BASE}/ap/users/alice"}
    assert run(inbox.process(follow, REMOTE)).startswith("follower added")
    assert run(inbox.process(dict(follow, id="https://mastodon.example/f/2"), REMOTE)).startswith("follower added"), \
        "a follower we already have is never refused"
    world["actors"]["https://mastodon.example/users/dan"] = dict(
        carol_actor(), id="https://mastodon.example/users/dan", inbox="https://mastodon.example/users/dan/inbox")
    dan = dict(follow, id="https://mastodon.example/f/3", actor="https://mastodon.example/users/dan")
    assert run(inbox.process(dan, "https://mastodon.example/users/dan")) == \
        "ignored: too many new followers from that server this hour"


def test_a_given_out_handle_cannot_be_registered_by_somebody_else(world, monkeypatch):
    """The NIP-05 registry is written by public signup and was checked FIRST, so anyone could register
    `alice_4b56` and take over the fediverse address of the Nostr user already known by it."""
    world["settings"]["activitypub_everyone"] = "true"
    owner = "4b56" + "0" * 60
    run(state.claim_nick(owner, "dana_4b56"))

    async def exposed(pk):
        return True
    monkeypatch.setattr(actors, "exposed", exposed)
    world["settings"]["nostr_relay_nip05_names"] = f"alice {ALICE}\ndana_4b56 {BOB}"
    actors._names_cache["raw"] = None
    assert run(actors.member_by_name("dana_4b56")) == owner
    assert run(actors.name_is_someone_elses_handle("dana_4b56", BOB))
    assert not run(actors.name_is_someone_elses_handle("dana_4b56", owner))
    assert run(actors.name_is_someone_elses_handle("npub1abc"))
    assert not run(actors.name_is_someone_elses_handle("john_smith"))
    # And a handle is never MINTED over a local user's name.
    world["settings"]["nostr_relay_nip05_names"] = f"alice {ALICE}\nnostr_c3c3 {BOB}"
    actors._names_cache["raw"] = None

    async def profile(pk):
        return {}
    monkeypatch.setattr(actors, "profile", profile)
    assert run(actors.readable_handle("c3" * 32)) == "nostr_c3c3c3"


def test_remote_emoji_and_avatars_must_be_https():
    from app.services.fedi_normalize import _emoji_url_map
    assert _emoji_url_map([{"shortcode": "a", "url": "https://x.example/a.png"},
                           {"shortcode": "b", "url": "http://192.168.0.1/track.png"},
                           {"shortcode": "c", "url": "javascript:alert(1)"}]) == {"a": "https://x.example/a.png"}
    p = ident.puppet_for({"uri": "https://x.example/users/a", "acct": "a@x.example", "avatar": "http://10.0.0.1/p.png"})
    assert p["avatar_url"] == ""


def test_a_blocked_account_is_not_delivered_to_by_mention(world):
    carol = _mirrored_carol_note(world)
    world["settings"]["fedi_bridge_blocked_domains"] = "carol@mastodon.example"
    jobs = run(outbox.plan(member_post("hi nostr:" + ident.nostr_service.npub_of(carol), tags=[["p", carol]]), ALICE))
    assert "https://mastodon.example/inbox" not in [i for i, _ in jobs]


def test_the_community_api_is_for_the_bots_and_admins_only(monkeypatch):
    from app.routers import community
    from app.services import community_stats
    from app.utils import auth_utils
    from app.database import get_db

    async def blocks():
        return [{"at": 1}, {"at": 5}]
    monkeypatch.setattr(community_stats, "blocks", blocks)
    settings = {"bots_posterchanai_api_key": "bots-key"}
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: settings.get(k, d))
    users = {"member-key": SimpleUser(False), "admin-key": SimpleUser(True)}
    monkeypatch.setattr(auth_utils, "query_api_key_with_retry",
                        lambda db, key: (key, key) if key in users else (None, None))
    monkeypatch.setattr(auth_utils, "get_user_from_api_key", lambda db, uid: users.get(uid))

    class Q:
        def query(self, *a):
            return self

        def all(self):
            return []
    app = FastAPI()
    app.include_router(community.router)
    app.dependency_overrides[get_db] = lambda: Q()
    with TestClient(app) as c:
        assert c.get("/api/community/blocks").status_code == 401
        assert c.get("/api/community/blocks", headers={"X-API-Key": "member-key"}).status_code == 403
        assert c.get("/api/community/blocks?since=3", headers={"X-API-Key": "bots-key"}).json() == {"blocks": [{"at": 5}]}
        assert c.get("/api/community/blocks", headers={"X-API-Key": "admin-key"}).status_code == 200

        async def down(*a, **k):
            raise RuntimeError("relay down")
        for name in ("blocks", "leaderboard", "activity"):
            monkeypatch.setattr(community_stats, name, down)
        for path in ("/api/community/blocks", "/api/community/block-leaderboard", "/api/community/activity"):
            assert c.get(path, headers={"X-API-Key": "bots-key"}).status_code == 503, path


class SimpleUser:
    def __init__(self, admin):
        self.is_admin = admin
        self.id = 1


def test_the_app_endpoints_need_a_login(client, monkeypatch):
    asked = []

    async def wf(handle):
        asked.append(handle)
        raise remote.FetchError("x")
    monkeypatch.setattr(remote, "webfinger", wf)
    assert client.get("/api/activitypub/lookup?acct=carol@mastodon.example").status_code == 401
    assert client.post("/api/activitypub/import-following", json={"account": "me@old.example"}).status_code == 401
    assert client.get("/api/admin/activitypub/status").status_code == 401
    assert asked == [], "an anonymous caller made this server fetch"


# ============================================================================ 22. the interop review (2026-09-24)

def test_a_gotosocial_key_is_accepted(world, monkeypatch):
    """GoToSocial's keyId has no fragment and answers with a stub of the ACTOR (whose id is the
    actor's). Requiring the document's id to BE the keyId refused every GoToSocial delivery."""
    gts = "https://gts.example/users/g"
    doc = {"id": gts, "type": "Person", "preferredUsername": "g", "inbox": gts + "/inbox",
           "publicKey": {"id": gts + "/main-key", "owner": gts, "publicKeyPem": CAROL_KEY[1]}}
    world["actors"][gts] = doc

    async def fetch_json(url, signed=True):
        if url == gts + "/main-key":
            return {"id": gts, "type": "Person", "publicKey": doc["publicKey"]}
        raise remote.FetchError("HTTP 404")
    monkeypatch.setattr(remote, "fetch_json", fetch_json)
    assert run(REAL_PUBLIC_KEY(gts + "/main-key")) == (gts, CAROL_KEY[1])
    # The stub is never believed on its own: an owner elsewhere, or one that does not publish the key.
    world["actors"][gts] = dict(doc, publicKey={"id": gts + "/other-key", "owner": gts, "publicKeyPem": KEY[1]})
    with pytest.raises(remote.FetchError):
        run(REAL_PUBLIC_KEY(gts + "/main-key"))


def test_a_refollow_is_a_new_follow(world):
    """Pleroma and Akkoma drop an activity whose id they already hold, so re-following somebody reused
    the first Follow's id and was ignored -- "pending" for ever."""
    carol = _carol_puppet(world)
    first = run(outbox.plan(member_post("", kind=3, tags=[["p", carol]], created=1_700_000_001), ALICE))
    (inbox_url, follow), = first
    assert follow["type"] == "Follow" and follow["id"] == run(state.following(ALICE))[REMOTE]["id"]
    assert run(inbox.process({"type": "Accept", "actor": REMOTE, "object": follow}, REMOTE)) == "follow accepted"
    assert run(state.following(ALICE))[REMOTE]["id"] == follow["id"], "the Accept lost the Follow's id"
    (_, undo), = run(outbox.plan(member_post("", kind=3, tags=[], created=1_700_000_002), ALICE))
    assert undo["type"] == "Undo" and undo["object"]["id"] == follow["id"], "the Undo must name the Follow sent"
    import time as _t
    _t.sleep(0.002)
    (_, again), = run(outbox.plan(member_post("", kind=3, tags=[["p", carol]], created=1_700_000_003), ALICE))
    assert again["type"] == "Follow" and again["id"] != follow["id"]


def test_media_goes_out_as_typed_attachments_with_their_metadata(world):
    _with_follower(world)
    ev = member_post("look https://cdn.example/p.jpg and https://cdn.example/blob", tags=[
        ["imeta", "url https://cdn.example/p.jpg", "m image/jpeg", "dim 640x480", "blurhash LEHV6nWB2yk8", "alt a cat"],
        ["imeta", "url https://cdn.example/blob", "m video/mp4"],
        ["imeta", "url https://cdn.example/mystery"]])
    note = run(outbox.plan(ev, ALICE))[0][1]["object"]
    assert note["attachment"] == [
        {"type": "Image", "mediaType": "image/jpeg", "url": "https://cdn.example/p.jpg", "name": "a cat",
         "width": 640, "height": 480, "blurhash": "LEHV6nWB2yk8"},
        {"type": "Video", "mediaType": "video/mp4", "url": "https://cdn.example/blob", "name": None}]
    ev2 = member_post("see https://cdn.example/mystery", tags=[["imeta", "url https://cdn.example/mystery"]],
                      created=1_700_000_005)
    note2 = run(outbox.plan(ev2, ALICE))[0][1]["object"]
    assert note2["attachment"] == [] and "cdn.example/mystery" in note2["content"], \
        "an attachment nobody can type must stay a link, not vanish"


def test_a_quote_post_quotes_on_the_fediverse(world):
    from app.services.nostr import bech32
    _with_follower(world)
    _mirrored_carol_note(world)
    ref = bech32.encode("note", bytes.fromhex("7" * 64))
    ev = member_post(f"so true nostr:{ref}", tags=[["q", "7" * 64]])
    note = run(outbox.plan(ev, ALICE))[0][1]["object"]
    q = "https://mastodon.example/notes/7"
    assert note["quoteUrl"] == note["_misskey_quote"] == note["quoteUri"] == q
    assert f'href="{q}"' in note["content"] and ref not in note["content"].replace(f"/{ref}", "")
    # A post that is only on Nostr is a link to this node's page for it, never raw bech32 text.
    lone = bech32.encode("note", bytes.fromhex("9" * 64))
    note2 = run(outbox.plan(member_post(f"see nostr:{lone}", created=1_700_000_009), ALICE))[0][1]["object"]
    assert f'href="{BASE}/{lone}"' in note2["content"] and "quoteUrl" not in note2


def test_an_incoming_quote_is_linked_and_tagged(world):
    world["docs"][f"pcai:ap:following:{ALICE}:x"] = {"actor": REMOTE, "inbox": "i", "state": "accepted"}
    mine = member_post("original")
    world["relay"][mine["id"]] = mine
    act = _create(content="<p>this</p>", extra={"quoteUrl": convert.object_url(BASE, mine["id"])})
    assert run(inbox.process(act, REMOTE)) == "stored"
    ev = next(e for e in world["relay"].values() if e["kind"] == 1 and e["id"] != mine["id"])
    assert ["q", mine["id"]] in ev["tags"] and convert.object_url(BASE, mine["id"]) in ev["content"]


def test_deleting_a_reply_reaches_the_account_it_was_sent_to(world):
    carol = _mirrored_carol_note(world)
    reply = member_post("oops", tags=[["e", "7" * 64, "", "reply"], ["p", carol]])
    world["relay"][reply["id"]] = reply
    assert [i for i, _ in run(outbox.plan(reply, ALICE))] == ["https://mastodon.example/inbox"]
    delete = member_post("", kind=5, tags=[["e", reply["id"]]], created=1_700_000_100)   # no `k` tag
    jobs = run(outbox.plan(delete, ALICE))
    assert [(i, a["type"]) for i, a in jobs] == [("https://mastodon.example/inbox", "Delete")]


def test_deleting_a_reaction_without_a_k_tag_undoes_the_like(world):
    carol = _mirrored_carol_note(world)
    like = member_post("🔥", kind=7, tags=[["e", "7" * 64], ["p", carol]], created=1_700_000_011)
    assert [i for i, _ in run(outbox.plan(like, ALICE))] == ["https://mastodon.example/inbox"]
    (job,) = run(outbox.plan(member_post("", kind=5, tags=[["e", like["id"]]], created=1_700_000_012), ALICE))
    assert job[0] == "https://mastodon.example/inbox" and job[1]["type"] == "Undo" \
        and job[1]["object"]["type"] == "Like", "a reaction's deletion was sent as a Delete of a Note"


def test_boosts_deletions_undos_and_profile_edits_reach_the_fediverse(world):
    other = "https://other.example/inbox"
    world["docs"][f"pcai:ap:follower:{ALICE}:2"] = {"actor": "https://other.example/users/f", "inbox": other}
    carol = _mirrored_carol_note(world)
    carol_inbox, note7 = "https://mastodon.example/inbox", "https://mastodon.example/notes/7"
    boost = member_post("", kind=6, tags=[["e", "7" * 64], ["p", carol]], created=1_700_000_010)
    jobs = run(outbox.plan(boost, ALICE))
    assert sorted(i for i, _ in jobs) == [carol_inbox, other]
    assert all(a["type"] == "Announce" and a["object"] == note7 for _, a in jobs)
    unboost = member_post("", kind=5, tags=[["e", boost["id"]], ["k", "6"]], created=1_700_000_013)
    assert sorted((i, a["type"], a["object"]["type"]) for i, a in run(outbox.plan(unboost, ALICE))) \
        == [(carol_inbox, "Undo", "Announce"), (other, "Undo", "Announce")], "the boosted author never heard"
    gone = "ab" * 32
    (job,) = run(outbox.plan(member_post("", kind=5, tags=[["e", gone], ["k", "1"]], created=1_700_000_014), ALICE))
    assert job[0] == other and job[1]["type"] == "Delete"
    assert job[1]["object"] == {"id": convert.object_url(BASE, gone), "type": "Tombstone"}
    (job,) = run(outbox.plan(member_post(json.dumps({"name": "Alice"}), kind=0, created=1_700_000_015), ALICE))
    assert job[0] == other and job[1]["type"] == "Update" and job[1]["object"]["id"] == f"{BASE}/ap/users/alice"


def test_a_comment_on_an_article_or_a_web_page_stays_on_nostr(world):
    _with_follower(world)
    ev = member_post("nice article", kind=1111, tags=[["A", "30023:" + ALICE + ":x"], ["a", "30023:" + ALICE + ":x"],
                                                       ["K", "30023"], ["k", "30023"]])
    assert run(outbox.plan(ev, ALICE)) == []
    ev2 = member_post("nice page", kind=1111, tags=[["I", "https://x.example"], ["i", "https://x.example"]])
    assert run(outbox.plan(ev2, ALICE)) == []


def test_a_deep_reply_keeps_its_threads_root(world):
    world["docs"][f"pcai:ap:following:{ALICE}:x"] = {"actor": REMOTE, "inbox": "i", "state": "accepted"}
    root = member_post("root")
    mid = member_post("mid", tags=[["e", root["id"], "", "root"], ["e", root["id"], "", "reply"]], created=1_700_000_050)
    for e in (root, mid):
        world["relay"][e["id"]] = e
    act = _create(content="<p>deep</p>", extra={"inReplyTo": convert.object_url(BASE, mid["id"])})
    assert run(inbox.process(act, REMOTE)) == "stored"
    ev = next(e for e in world["relay"].values() if e.get("content") == "deep")
    assert ["e", root["id"], "", "root"] in ev["tags"] and ["e", mid["id"], "", "reply"] in ev["tags"]


def test_a_reply_to_something_unknown_brings_its_parent(world):
    world["docs"][f"pcai:ap:following:{ALICE}:x"] = {"actor": REMOTE, "inbox": "i", "state": "accepted"}
    parent = {"id": "https://mastodon.example/notes/p", "type": "Note", "attributedTo": REMOTE,
              "content": "<p>parent</p>", "to": [config.PUBLIC], "cc": []}
    world["objects"][parent["id"]] = parent
    act = _create(note_id="https://mastodon.example/notes/c", content="<p>child</p>",
                  extra={"inReplyTo": parent["id"]})
    assert run(inbox.process(act, REMOTE)) == "stored"
    p = next(e for e in world["relay"].values() if e.get("content") == "parent")
    c = next(e for e in world["relay"].values() if e.get("content") == "child")
    assert ["e", p["id"], "", "reply"] in c["tags"]
    # Its parent cannot be fetched: the reply still says what thread it belongs to.
    act2 = _create(note_id="https://mastodon.example/notes/d", content="<p>orphan</p>",
                   extra={"inReplyTo": "https://mastodon.example/notes/missing"})
    assert run(inbox.process(act2, REMOTE)) == "stored"
    o = next(e for e in world["relay"].values() if e.get("content") == "orphan")
    assert ["r", "https://mastodon.example/notes/missing"] in o["tags"]


def test_a_community_relaying_a_post_brings_the_post(world):
    """FEP-1b12: a Lemmy community relays its members' posts as an Announce of the whole Create; read
    as the note itself it had no author, so following a community brought in nothing."""
    group = "https://lemmy.example/c/news"
    world["actors"][group] = {"id": group, "type": "Group", "preferredUsername": "news", "inbox": group + "/inbox"}
    world["docs"][f"pcai:ap:following:{ALICE}:g"] = {"actor": group, "inbox": group + "/inbox", "state": "accepted"}
    author = "https://lemmy.example/u/ann"
    world["actors"][author] = {"id": author, "type": "Person", "preferredUsername": "ann", "inbox": author + "/inbox"}
    page = {"id": "https://lemmy.example/post/1", "type": "Page", "attributedTo": author, "name": "Big news",
            "content": "<p>details</p>", "to": [group, config.PUBLIC], "cc": []}
    world["objects"][page["id"]] = page
    ann = {"id": "https://lemmy.example/activities/announce/1", "type": "Announce", "actor": group,
           "to": [config.PUBLIC], "object": {"id": "https://lemmy.example/activities/create/1", "type": "Create",
                                             "actor": author, "object": page}}
    assert run(inbox.process(ann, group)) == "boost stored"
    note = next(e for e in world["relay"].values() if e["kind"] == 1)
    assert note["content"].startswith("Big news\n\ndetails"), "the post's title was lost"
    relayed_like = dict(ann, id="https://lemmy.example/activities/announce/2",
                        object={"id": "https://lemmy.example/l/1", "type": "Like", "actor": author, "object": page["id"]})
    assert run(inbox.process(relayed_like, group)) == "ignored: relayed Like"


def test_an_edit_replaces_the_stored_copy_and_only_the_author_can_edit(world):
    world["docs"][f"pcai:ap:following:{ALICE}:x"] = {"actor": REMOTE, "inbox": "i", "state": "accepted"}
    assert run(inbox.process(_create(content="<p>tpyo</p>"), REMOTE)) == "stored"
    old = next(e for e in world["relay"].values() if e["kind"] == 1)
    deleted = []

    async def delete_note(port, actor_uri, eid, broadcast=False):
        deleted.append(eid)
        return True
    import app.services.fedi_bridge_identity as fbi
    fbi_delete = fbi.delete_note
    fbi.delete_note = delete_note
    try:
        edited = dict(_create(content="<p>typo</p>")["object"])
        assert run(inbox.process({"type": "Update", "actor": REMOTE, "object": edited}, REMOTE)) == "edited"
        new = next(e for e in world["relay"].values() if e["kind"] == 1 and e["content"] == "typo")
        assert deleted == [old["id"]] and inbox._delivered(edited["id"]).nostr_event_id == new["id"]
        mallory = "https://mastodon.example/users/mallory"
        forged = dict(edited, attributedTo=mallory, content="<p>pwned</p>")
        assert run(inbox.process({"type": "Update", "actor": mallory, "object": forged}, mallory)).startswith("ignored")
    finally:
        fbi.delete_note = fbi_delete


def test_a_forwarded_reply_is_fetched_from_its_own_server(world):
    world["docs"][f"pcai:ap:following:{ALICE}:x"] = {"actor": REMOTE, "inbox": "i", "state": "accepted"}
    note = _create(note_id="https://mastodon.example/notes/fw")["object"]
    world["objects"][note["id"]] = note
    assert run(inbox.process({"type": inbox.FORWARDED, "object": note["id"]}, "https://other.example/users/x")) == "stored"
    with pytest.raises(remote.FetchError):          # nothing on its own server: nothing stored
        run(inbox.process({"type": inbox.FORWARDED, "object": "https://mastodon.example/notes/none"},
                          "https://other.example/users/x"))
    assert len([e for e in world["relay"].values() if e["kind"] == 1]) == 1


def test_a_dead_server_stops_costing_every_post_a_timeout(world, monkeypatch):
    calls = []

    async def deliver(inbox_url, activity, *, key_id, private_pem):
        calls.append(inbox_url)
        return 0
    monkeypatch.setattr(remote, "deliver", deliver)
    for _ in range(outbox._DOWN_AFTER):
        run(outbox._send("https://dead.example/inbox", {"type": "Create"}, ALICE))
    n = len(calls)
    run(outbox._send("https://dead.example/inbox", {"type": "Create"}, ALICE))
    assert len(calls) == n, "a resting server was contacted again"
    assert outbox._retries, "what was meant for it waits in the retry queue"
    outbox._down.clear()

    async def ok(inbox_url, activity, *, key_id, private_pem):
        calls.append(inbox_url)
        return 202
    monkeypatch.setattr(remote, "deliver", ok)
    run(outbox._send("https://dead.example/inbox", {"type": "Create"}, ALICE))
    assert "dead.example" not in outbox._down


def test_articles_polls_and_media_arrive_whole(world):
    world["docs"][f"pcai:ap:following:{ALICE}:x"] = {"actor": REMOTE, "inbox": "i", "state": "accepted"}
    art = _create(note_id="https://mastodon.example/a/1", content="<p>body</p>",
                  extra={"type": "Article", "name": "The Title", "summary": "an abstract"})["object"]
    text, tags = convert.note_content(art, local_actors={})
    assert text.startswith("The Title\n\nbody") and not any(t[0] == "content-warning" for t in tags)
    poll = {"id": "https://mastodon.example/q/1", "type": "Question", "content": "<p>Tea?</p>",
            "oneOf": [{"type": "Note", "name": "yes"}, {"type": "Note", "name": "no"}]}
    assert convert.note_content(poll, local_actors={})[0] == "Tea?\n\n◯ yes\n◯ no"
    pic = {"id": "x", "type": "Note", "content": "<p>pic</p>", "attachment": [
        {"type": "Document", "mediaType": "image/png", "url": "https://m.example/p.png", "name": "a dog",
         "width": 10, "height": 20, "blurhash": "LKO2?U%2Tw=w"}]}
    _, tags = convert.note_content(pic, local_actors={})
    assert ["imeta", "url https://m.example/p.png", "m image/png", "dim 10x20", "blurhash LKO2?U%2Tw=w",
            "alt a dog"] in tags


def test_discovery_answers_what_other_servers_ask(client, world):
    hm = client.get("/.well-known/host-meta")
    assert hm.status_code == 200 and "/.well-known/webfinger?resource={uri}" in hm.text
    links = client.get("/.well-known/nodeinfo").json()["links"]
    assert {l["href"] for l in links} == {f"{BASE}/nodeinfo/2.1", f"{BASE}/nodeinfo/2.0"}
    assert client.get("/nodeinfo/2.0").json()["version"] == "2.0"
    feat = client.get("/ap/users/alice/featured", headers={"Accept": "application/activity+json"}).json()
    assert feat["type"] == "OrderedCollection" and feat["totalItems"] == 0
    doc = client.get("/ap/users/alice", headers={"Accept": "application/activity+json"}).json()
    assert doc["featured"] == f"{BASE}/ap/users/alice/featured" and doc["indexable"] is True
    assert "toot" in doc["@context"][2]


def test_an_actor_shows_its_profile_fields():
    doc = convert.person(base=BASE, name="alice", public_key_pem="k", profile={
        "website": "https://alice.example", "lud16": "alice@getalby.com", "nip05": "alice@poster.test",
        "about": "hi"})
    names = [a["name"] for a in doc["attachment"]]
    assert names == ["Website", "Lightning", "NIP-05"]
    assert 'href="https://alice.example"' in doc["attachment"][0]["value"]
    bad = convert.person(base=BASE, name="a", public_key_pem="k",
                         profile={"website": "javascript:alert(1)", "lud16": "<script>@x"})
    assert "attachment" not in bad


def test_a_deleted_post_is_gone_not_unknown(client, world, monkeypatch):
    from app.services import nostr_store
    gone = "d" * 64
    dele = member_post("", kind=5, tags=[["e", gone]])

    async def ws_query(port, filters, **kw):
        f = filters[0]
        return [dele] if f.get("kinds") == [5] and f.get("#e") == [gone] else []
    monkeypatch.setattr(nostr_store, "_ws_query", ws_query)
    r = client.get(f"/ap/objects/{gone}", headers={"Accept": "application/activity+json"})
    assert r.status_code == 410 and r.json()["type"] == "Tombstone"
    assert client.get(f"/ap/objects/{'e' * 64}").status_code == 404


def test_the_retired_pleromas_actor_address_leads_to_the_actor():
    from starlette.requests import Request
    from app.main import nostr_user_page
    scope = {"type": "http", "method": "GET", "path": "/users/alice", "query_string": b"",
             "headers": [(b"accept", b"application/activity+json")]}
    r = run(nostr_user_page("alice", Request(scope)))
    assert r.status_code == 301 and r.headers["location"] == "/ap/users/alice"


def test_the_outbox_pages_every_post_once_and_counts_what_it_serves(client, world, monkeypatch):
    from app.services import nostr_store
    h = {"Accept": "application/activity+json"}
    evs = [member_post(f"p{i}", created=1_700_000_000 + (i // 3)) for i in range(45)]    # three per second
    evs += [member_post("protected", tags=[["-"]], created=1_700_000_100)]
    for e in evs:
        world["relay"][e["id"]] = e

    async def ws_query(port, filters, **kw):
        f = filters[0]
        rows = [e for e in world["relay"].values() if e["kind"] in f.get("kinds", [])
                and e["pubkey"] in f.get("authors", []) and e["created_at"] <= f.get("until", 1 << 62)]
        return sorted(rows, key=lambda e: (-e["created_at"], e["id"]))[: f.get("limit", 500)]
    monkeypatch.setattr(nostr_store, "_ws_query", ws_query)
    coll = client.get("/ap/users/alice/outbox", headers=h).json()
    assert coll["totalItems"] == 45, "a protected post was counted"
    seen, url, pages = [], coll["first"], 0
    while url and pages < 10:
        pages += 1
        page = client.get(url.replace(BASE, ""), headers=h).json()
        seen += [a["object"]["content"] for a in page["orderedItems"]]
        url = page.get("next") if page["orderedItems"] else None
    assert sorted(seen) == sorted(f"<p>p{i}</p>" for i in range(45)), "a page boundary skipped or repeated posts"


def test_a_dm_names_its_recipient_by_handle(world):
    from app.services.activitypub import dm
    from app.services.nostr import nip17
    pk = _real_alice(world)
    carol = _carol_puppet(world)
    world["relay"]["k" + pk[:10]] = {"id": "k" + pk[:10], "pubkey": pk, "kind": 0, "created_at": 5, "tags": [],
                                     "content": json.dumps({"name": "Alice"})}
    dm._puppets["at"] = 0.0
    assert run(dm.handle_wrap(nip17.wrap(ALICE_SK, carol, "hello"))) == "sent"
    tag = world["sent"][-1]["activity"]["object"]["tag"][0]
    assert tag == {"type": "Mention", "href": REMOTE, "name": "@carol@mastodon.example"}


def test_an_import_runs_once_a_minute_per_member(client, monkeypatch):
    from app.routers import activitypub as routes
    from app.routers.auth import get_current_user
    calls = []

    async def fake_import(handle):
        calls.append(handle)
        return {"following": 0, "people": []}
    monkeypatch.setattr(routes, "_import", fake_import)
    client.app.dependency_overrides[get_current_user] = lambda: SimpleUser(False)
    try:
        assert client.post("/api/activitypub/import-following", json={"account": "me@old.example"}).status_code == 200
        assert client.post("/api/activitypub/import-following", json={"account": "me@old.example"}).status_code == 429
    finally:
        client.app.dependency_overrides.clear()
    assert calls == ["me@old.example"]


def test_counting_outboxes_has_a_node_wide_budget(client, world, monkeypatch):
    """Anybody can ask for any npub's outbox, and each uncounted one was a 2000-event read."""
    from app.routers import activitypub as routes
    from app.services import nostr_store
    reads = []

    async def ws_query(port, filters, **kw):
        reads.append(1)
        return []
    monkeypatch.setattr(nostr_store, "_ws_query", ws_query)
    monkeypatch.setattr(routes, "COUNTS_PER_MINUTE", 1)
    h = {"Accept": "application/activity+json"}
    assert client.get("/ap/users/alice/outbox", headers=h).json()["totalItems"] == 0
    second = client.get("/ap/users/bob/outbox", headers=h)
    assert second.status_code == 200 and "totalItems" not in second.json() and second.json()["first"]
    assert len(reads) == 1


# ============================================================================ 20. the third review + polls and the standard features
#
# Every test below was checked to FAIL with its rule removed (see the commit message for the list).

def _relay_filter(world, monkeypatch):
    """`nostr_store._ws_query` answered from the world relay, honouring the filter fields the shipped
    queries use (ids, kinds, authors, #e, since, until, limit)."""
    from app.services import nostr_store

    def match(e, f):
        if f.get("ids") and e["id"] not in f["ids"]:
            return False
        if f.get("kinds") and e["kind"] not in f["kinds"]:
            return False
        if f.get("authors") and e["pubkey"] not in f["authors"]:
            return False
        if f.get("#e") and not any(len(t) > 1 and t[0] == "e" and t[1] in f["#e"] for t in e["tags"]):
            return False
        if "since" in f and e["created_at"] < f["since"]:
            return False
        if "until" in f and e["created_at"] > f["until"]:
            return False
        return True

    async def ws_query(port, filters, strict=False, **kw):
        out = [e for e in world["relay"].values() if any(match(e, f) for f in filters)]
        out.sort(key=lambda e: -e["created_at"])
        return out[:max(f.get("limit", 500) for f in filters)]
    monkeypatch.setattr(nostr_store, "_ws_query", ws_query)

    async def query_one(port, filt, timeout=8.0):
        hit = await ws_query(port, [filt])
        return True, (hit[0] if hit else None)
    monkeypatch.setattr(ident, "query_one", query_one)


# ---- the inbox's answers

def test_a_blocked_server_is_answered_202_before_its_key_is_fetched(client, monkeypatch):
    """Its key CANNOT be fetched (every fetch from a blocked host is refused), so verifying first made
    every delivery from it a 401 -- retried for days -- and the 'accepted and dropped' 202 meant for
    it was unreachable. (The older test stubbed the key fetch to succeed, which is why it passed.)"""
    fetched = []

    async def pk(key_id, refresh=False):
        fetched.append(key_id)
        raise remote.FetchError("blocked.example is blocked or is this node")
    monkeypatch.setattr(remote, "public_key", pk)
    act = _create()
    act["actor"] = "https://blocked.example/users/x"
    assert _post_signed(client, act, key_id="https://blocked.example/users/x#main-key").status_code == 202
    assert fetched == [] and client.scheduled == []


def test_a_post_delete_whose_key_fetch_failed_is_retried_not_swallowed(client, monkeypatch):
    """Only an account deleting ITSELF may be accepted unverified (its key is gone with it). A post's
    Delete answered 202 on a failed fetch (a 429, a timeout) was never re-sent, and the post stayed."""
    async def pk(key_id, refresh=False):
        raise remote.FetchError("HTTP 429 from mastodon.example")
    monkeypatch.setattr(remote, "public_key", pk)
    delete_post = {"id": REMOTE + "#delete/1", "type": "Delete", "actor": REMOTE,
                   "object": "https://mastodon.example/notes/1"}
    assert _post_signed(client, delete_post).status_code == 401
    delete_self = {"id": REMOTE + "#delete/2", "type": "Delete", "actor": REMOTE, "object": REMOTE}
    assert _post_signed(client, delete_self).status_code == 202


def test_a_forwarded_delete_is_accepted_and_confirmed_with_the_posts_server(client, world, monkeypatch):
    act = {"id": "https://mastodon.example/users/mallory#delete/9", "type": "Delete",
           "actor": "https://other.example/users/dave", "object": "https://other.example/notes/5"}
    assert _post_signed(client, act).status_code == 202          # signed by carol's server, for dave
    assert client.scheduled == [({"type": inbox.FORWARDED_DELETE, "object": "https://other.example/notes/5"}, REMOTE)]


def test_forwarded_delete_removes_the_copy_only_when_its_own_server_says_gone(world, monkeypatch):
    world["docs"][f"pcai:ap:following:{ALICE}:x"] = {"actor": REMOTE, "inbox": "i", "state": "accepted"}
    assert run(inbox.process(_create(), REMOTE)) == "stored"
    deleted = []

    async def delete_note(port, actor, eid, broadcast=False):
        deleted.append((actor, eid))
        return True
    monkeypatch.setattr(ident, "delete_note", delete_note)

    async def still_there(url, signed=True):
        return {"id": url, "type": "Note"}
    monkeypatch.setattr(remote, "fetch_json", still_there)
    assert run(inbox.process({"type": inbox.FORWARDED_DELETE, "object": "https://mastodon.example/notes/1"}, REMOTE)) \
        == "ignored: still there on its own server"

    async def gone(url, signed=True):
        raise remote.FetchError("HTTP 410 from mastodon.example")
    monkeypatch.setattr(remote, "fetch_json", gone)
    assert run(inbox.process({"type": inbox.FORWARDED_DELETE, "object": "https://mastodon.example/notes/1"}, REMOTE)) \
        == "deleted (confirmed with its server)"
    assert deleted and deleted[0][0] == REMOTE


def test_an_unreachable_relay_is_a_503_never_a_500(client, world, monkeypatch):
    """The ten seconds after a restart, before the relay accepts: strict reads raise, and a 500 on the
    actor or the inbox tells the asking server something is broken rather than 'ask again'."""
    async def refused(*a, **k):
        raise ConnectionRefusedError(111, "Connection refused")
    from app.services import nostr_store
    monkeypatch.setattr(nostr_store, "get_doc", refused)
    state._key_cache.clear()
    r = client.get("/ap/users/alice", headers={"Accept": "application/activity+json"})
    assert r.status_code == 503 and r.headers.get("retry-after")
    assert client.get(f"/.well-known/webfinger?resource=acct:alice@{DOMAIN}").status_code == 503


# ---- an actor's id never moves

def test_an_actor_id_is_pinned_when_the_account_gets_a_name_later(client, world):
    """A Nostr user followed as their npub who later claims a name here used to become a NEW account
    everywhere: deliveries signed as /ap/users/<name>, which nobody followed."""
    pk, npub = _nostr_user(world)
    doc = client.get(f"/ap/users/{npub}", headers={"Accept": "application/activity+json"}).json()
    assert doc["id"] == f"{BASE}/ap/users/{npub}"
    world["settings"]["nostr_relay_nip05_names"] += f"\ndana {pk}"
    actors._names_cache["raw"] = None
    assert run(actors.actor_id(pk)) == f"{BASE}/ap/users/{npub}"
    actors._pin_cache.clear()                                   # a fresh process reads the pin back
    assert run(actors.actor_id(pk)) == f"{BASE}/ap/users/{npub}"
    again = client.get("/ap/users/dana", headers={"Accept": "application/activity+json"}).json()
    assert again["id"] == f"{BASE}/ap/users/{npub}" and again["preferredUsername"] == "dana"
    wf = client.get(f"/.well-known/webfinger?resource=acct:dana@{DOMAIN}").json()
    assert wf["links"][0]["href"] == f"{BASE}/ap/users/{npub}"


def test_a_name_given_to_somebody_else_later_never_takes_the_first_ones_address(world):
    assert run(actors.actor_id(ALICE)) == f"{BASE}/ap/users/alice"
    other = "d4" * 32
    world["settings"]["nostr_relay_nip05_names"] = f"alice {other}\nbob {BOB}"
    actors._names_cache["raw"] = None
    from app.services.nostr import nostr_service
    assert run(actors.actor_id(other)) == f"{BASE}/ap/users/{nostr_service.npub_of(other)}"
    assert run(actors.member_of_path("alice")) == ""                   # alice is no longer a member...
    world["settings"]["nostr_relay_nip05_names"] = f"alice {other}\nbob {BOB}\nalice2 {ALICE}"
    actors._names_cache["raw"] = None
    assert run(actors.member_of_path("alice")) == ALICE                # ...and the address stays hers


# ---- the outbox

def test_a_quote_marked_mention_is_not_a_parent(world):
    _with_follower(world)
    ev = member_post("look at this nostr:note1xyz", tags=[["e", "9" * 64, "", "mention"]])
    assert outbox.parent_of(ev) == ""
    jobs = run(outbox.plan(ev, ALICE))
    assert jobs and "inReplyTo" not in jobs[0][1]["object"]


def test_an_unlike_names_what_was_liked(world, monkeypatch):
    """Mastodon and Misskey find the favourite to remove through the Undo's inner `object`; sent as ""
    the unlike reached them and removed nothing."""
    world["actors"][REMOTE] = carol_actor()
    remote_note = _mirror(world)
    like = member_post("+", kind=7, tags=[["e", remote_note], ["p", "x" * 64]])
    jobs = run(outbox.plan(like, ALICE))
    assert jobs and jobs[0][1]["type"] == "Like"
    unlike = member_post("", kind=5, tags=[["e", like["id"]]])
    undo = run(outbox.plan(unlike, ALICE))
    assert undo and undo[0][1]["type"] == "Undo"
    assert undo[0][1]["object"]["object"] == "https://mastodon.example/notes/77"


def _mirror(world, uri="https://mastodon.example/notes/77"):
    """A fediverse note stored here as carol's puppet: its Nostr event id."""
    s = world["Session"]()
    p = run(ident.ensure_puppet(s, 1, convert.account_from_actor(carol_actor()), "mastodon.example"))
    eid = "7" * 64
    world["relay"][eid] = {"id": eid, "pubkey": p["pubkey_hex"], "kind": 1, "created_at": 1_700_000_000,
                           "tags": [["proxy", uri, "activitypub"]], "content": "remote words"}
    inbox._record(uri, eid, p["pubkey_hex"], "carol@mastodon.example")
    return eid


def test_a_member_cannot_send_a_delete_of_another_members_post(world):
    """The relay refuses such a kind-5 for its own copy, but the outbox would still have sent a Delete
    of it signed by the deleter -- and Pleroma/Akkoma honour a Delete from the object's own domain."""
    _with_follower(world)
    world["docs"][f"pcai:ap:follower:{BOB}:1"] = {"actor": REMOTE, "inbox": "https://mastodon.example/inbox"}
    post = member_post("alice's words")
    world["relay"][post["id"]] = post
    assert run(outbox.plan(member_post("", kind=5, tags=[["e", post["id"]]], author=BOB), BOB)) == []
    own = run(outbox.plan(member_post("", kind=5, tags=[["e", post["id"]]]), ALICE))
    assert own and own[0][1]["type"] == "Delete"
    # The post already gone from the relay (its author deleted it): our record of who SENT it decides.
    reply = member_post("alice's reply to the fediverse")
    world["docs"][outbox._SENT + reply["id"]] = {"inbox": "https://m.example/inbox",
                                                 "inboxes": ["https://m.example/inbox"], "kind": 1, "by": ALICE}
    assert run(outbox.plan(member_post("", kind=5, tags=[["e", reply["id"]]], author=BOB), BOB)) == []


def test_a_deletion_in_everyone_mode_goes_out_by_our_record_of_sending(world, monkeypatch):
    """The relay removes the author's own event as it stores the kind-5, so the filter that looked the
    deleted event up found nothing and the Delete never left: the reply stayed on the fediverse."""
    pk, _npub = _nostr_user(world)
    world["docs"][outbox._SENT + "e" * 64] = {"inbox": "https://mastodon.example/inbox",
                                              "inboxes": ["https://mastodon.example/inbox"], "kind": 1, "by": pk}
    _relay_filter(world, monkeypatch)
    page = run(outbox._everyone_filter())
    deletion = member_post("", kind=5, tags=[["e", "e" * 64]], author=pk)
    world["relay"][deletion["id"]] = deletion
    assert deletion["id"] in run(page([deletion]))


def test_an_event_that_arrives_late_is_still_sent_and_only_once(world, monkeypatch):
    """The cursor is a created_at -- the author's say-so. One signed offline and replayed later is
    older than the cursor by the time it is here, and was skipped for good."""
    import time as _t
    _with_follower(world)
    _relay_filter(world, monkeypatch)
    now = int(_t.time())
    world["docs"]["pcai:ap:cursor"] = {"since": now - 10}
    late = member_post("written offline", created=now - 3600)
    world["relay"][late["id"]] = late
    run(outbox.tick())
    assert [a for a in world["sent"] if a["activity"]["type"] == "Create"], "the late post was never sent"
    n = len(world["sent"])
    outbox._seen.clear()                                        # a worker restart
    outbox._done.clear()
    run(outbox.tick())
    assert len(world["sent"]) == n, "a restart sent it again"


def test_a_future_dated_event_does_not_move_the_cursor_past_the_clock(world, monkeypatch):
    import time as _t
    _with_follower(world)
    _relay_filter(world, monkeypatch)
    now = int(_t.time())
    world["docs"]["pcai:ap:cursor"] = {"since": now - 10}
    ahead = member_post("from a fast clock", created=now + 900)
    world["relay"][ahead["id"]] = ahead
    run(outbox.tick())
    assert world["docs"]["pcai:ap:cursor"]["since"] <= int(_t.time())


def test_an_outage_never_unfollows_somebody_still_in_the_contact_list(world, monkeypatch):
    """A puppet from the old bridge is keyed on /@carol while the follow is recorded as /users/carol;
    with carol's server briefly down, an unrelated contact-list edit unfollowed her."""
    s = world["Session"]()
    p = run(ident.ensure_puppet(s, 1, {"uri": "https://mastodon.example/@carol", "acct": "carol@mastodon.example",
                                       "display_name": "Carol"}, "mastodon.example"))
    world["docs"][f"pcai:ap:following:{ALICE}:c"] = {"actor": REMOTE, "inbox": "https://mastodon.example/inbox",
                                                    "state": "accepted"}

    async def down(uri, refresh=False, alias=False):
        raise remote.FetchError("mastodon.example took too long to answer")
    monkeypatch.setattr(remote, "actor", down)
    k3 = member_post("", kind=3, tags=[["p", p["pubkey_hex"]]])
    assert run(outbox._follows(k3, ALICE, f"{BASE}/ap/users/alice")) == []
    assert not world["docs"][f"pcai:ap:following:{ALICE}:c"].get("gone")


def test_a_retry_survives_a_worker_restart(world, monkeypatch):
    calls = []

    async def deliver(inbox_url, activity, *, key_id, private_pem):
        calls.append(inbox_url)
        return 503 if len(calls) == 1 else 202
    monkeypatch.setattr(remote, "deliver", deliver)
    run(outbox._send("https://mastodon.example/inbox", {"id": "x", "type": "Create"}, ALICE))
    assert [k for k, v in world["docs"].items() if k.startswith("pcai:ap:retry:") and not v.get("done")]
    outbox._retries.clear()                                     # the worker restarts
    outbox._retries_loaded = False
    run(outbox._load_retries())
    outbox._retries[:] = [(0.0,) + r[1:] for r in outbox._retries]   # due now
    run(outbox._flush_retries())
    assert calls == ["https://mastodon.example/inbox"] * 2
    assert not [k for k, v in world["docs"].items() if k.startswith("pcai:ap:retry:") and not v.get("done")]


# ---- the inbox

def test_an_edit_that_fails_keeps_the_old_copy_deletable(world, monkeypatch):
    world["docs"][f"pcai:ap:following:{ALICE}:x"] = {"actor": REMOTE, "inbox": "i", "state": "accepted"}
    assert run(inbox.process(_create(), REMOTE)) == "stored"

    async def boom(*a, **k):
        raise RuntimeError("timed out")
    monkeypatch.setattr(inbox, "store_note", boom)
    edited = dict(_create()["object"], content="<p>edited</p>")
    with pytest.raises(RuntimeError):
        run(inbox._edit(edited, REMOTE))
    assert inbox._delivered("https://mastodon.example/notes/1"), "the old copy lost its mapping"


def test_an_account_marked_gone_that_follows_again_is_delivered_to(world):
    run(state.mark_gone(REMOTE))
    outbox._gone_cache.update(at=0.0, set=frozenset())
    assert REMOTE in run(outbox._gone())
    follow = {"id": REMOTE + "#follows/1", "type": "Follow", "actor": REMOTE, "object": f"{BASE}/ap/users/alice"}
    assert run(inbox.process(follow, REMOTE)).startswith("follower added")
    assert REMOTE not in run(outbox._gone())
    _with_follower(world)
    assert run(outbox.plan(member_post("back again"), ALICE))


def test_an_undo_naming_only_the_follow_id_unfollows(world):
    follow = {"id": REMOTE + "#follows/7", "type": "Follow", "actor": REMOTE, "object": f"{BASE}/ap/users/alice"}
    run(inbox.process(follow, REMOTE))
    assert run(state.is_follower(ALICE, REMOTE))
    other = "https://mastodon.example/users/mallory"
    assert run(inbox._undo({"id": REMOTE + "#follows/7"}, other)) != "follower removed"
    assert run(inbox.process({"type": "Undo", "id": REMOTE + "#undo/7", "actor": REMOTE,
                              "object": REMOTE + "#follows/7"}, REMOTE)) == "follower removed"
    assert not run(state.is_follower(ALICE, REMOTE))


def test_a_move_moves_the_follow_only_when_the_new_account_confirms_it(world):
    new = "https://new.example/users/carol"
    run(state.set_following(ALICE, REMOTE, "i", "accepted"))
    world["actors"][new] = {"id": new, "type": "Person", "preferredUsername": "carol", "inbox": new + "/inbox"}
    move = {"id": REMOTE + "#move", "type": "Move", "actor": REMOTE, "object": REMOTE, "target": new}
    assert run(inbox.process(move, REMOTE)) == "ignored: the new account does not confirm the move"
    world["actors"][new]["alsoKnownAs"] = [REMOTE]
    assert run(inbox.process(move, REMOTE)).startswith("moved (1")
    following = run(state.following(ALICE))
    assert new in following and REMOTE not in following
    assert any(a["activity"]["type"] == "Follow" and a["activity"]["object"] == new for a in world["sent"])
    assert run(state.moves()) == {REMOTE: new}


def test_a_report_about_a_member_becomes_a_nip56_report(world):
    post = member_post("reported words")
    world["relay"][post["id"]] = post
    flag = {"id": "https://mastodon.example/flags/1", "type": "Flag", "actor": REMOTE, "content": "spam",
            "object": [f"{BASE}/ap/users/alice", convert.object_url(BASE, post["id"])]}
    assert run(inbox.process(flag, REMOTE)) == "report stored"
    rep = [e for e in world["relay"].values() if e["kind"] == 1984]
    assert rep and ["p", ALICE, "other"] in rep[0]["tags"] and ["e", post["id"], "other"] in rep[0]["tags"]
    assert run(inbox.process(dict(flag, object=["https://elsewhere.example/users/x"]), REMOTE)) \
        == "ignored: a report about nobody here"


def test_a_malformed_emoji_or_icon_does_not_cost_the_post(world):
    """`icon` may be a string or a list in ActivityStreams; `.get("url")` on it raised and dropped the
    note (and made every Like and Announce from that actor fail)."""
    world["docs"][f"pcai:ap:following:{ALICE}:x"] = {"actor": REMOTE, "inbox": "i", "state": "accepted"}
    world["actors"][REMOTE]["icon"] = ["https://mastodon.example/c.png"]
    world["actors"][REMOTE]["tag"] = [{"type": "Emoji", "name": ":a:", "icon": "https://mastodon.example/a.png"}]
    act = _create(content="<p>hi :a: :b:</p>")
    act["object"]["tag"] = [{"type": "Emoji", "name": ":a:", "icon": ["https://mastodon.example/a.png"]},
                            {"type": "Emoji", "name": ":b:", "icon": {"url": {"href": "https://mastodon.example/b.png"}}}]
    assert run(inbox.process(act, REMOTE)) == "stored"
    ev = [e for e in world["relay"].values() if e["kind"] == 1][-1]
    assert ["emoji", "a", "https://mastodon.example/a.png"] in ev["tags"]
    assert ["emoji", "b", "https://mastodon.example/b.png"] in ev["tags"]


# ---- polls

def _member_poll(world, multi=False, end=0):
    tags = [["option", "opt1", "Red"], ["option", "opt2", "Blue"],
            ["polltype", "multiplechoice" if multi else "singlechoice"]]
    if end:
        tags.append(["endsAt", str(end)])
    poll = member_post("Which colour?", kind=1068, tags=tags)
    world["relay"][poll["id"]] = poll
    return poll


def _vote_note(poll, name, n=1, who=REMOTE):
    return {"id": f"{who}#votes/{n}", "type": "Note", "attributedTo": who, "name": name,
            "inReplyTo": convert.object_url(BASE, poll["id"]), "to": [f"{BASE}/ap/users/alice"]}


def test_a_member_poll_goes_out_as_a_question(world):
    _with_follower(world)
    poll = _member_poll(world, end=1_900_000_000)
    jobs = run(outbox.plan(poll, ALICE))
    q = jobs[0][1]["object"]
    assert q["type"] == "Question" and [o["name"] for o in q["oneOf"]] == ["Red", "Blue"]
    assert q["endTime"] == convert.iso(1_900_000_000) and q["votersCount"] == 0
    multi = _member_poll(world, multi=True)
    assert "anyOf" in run(outbox.plan(multi, ALICE))[0][1]["object"]


def test_a_fediverse_vote_is_counted_under_a_key_that_is_not_the_voters(world, monkeypatch):
    _relay_filter(world, monkeypatch)
    poll = _member_poll(world)
    create = {"type": "Create", "id": REMOTE + "#votes/1/activity", "actor": REMOTE, "object": _vote_note(poll, "Blue")}
    assert run(inbox.process(create, REMOTE)) == "vote stored"
    votes = [e for e in world["relay"].values() if e["kind"] == 1018]
    assert len(votes) == 1 and ["response", "opt2"] in votes[0]["tags"] and ["e", poll["id"]] in votes[0]["tags"]
    carol_puppet = ident.puppet_for(convert.account_from_actor(carol_actor()))["pubkey_hex"]
    assert votes[0]["pubkey"] != carol_puppet, "a fediverse vote is anonymous there, and must stay so here"
    assert run(inbox.process(dict(create, object=_vote_note(poll, "Red", 2)), REMOTE)) == "ignored: already voted"
    assert run(inbox.process(dict(create, object=_vote_note(poll, "Green", 3)), REMOTE)) == "ignored: no such option"


def test_a_multiple_choice_vote_is_one_vote_with_every_option(world, monkeypatch):
    _relay_filter(world, monkeypatch)
    poll = _member_poll(world, multi=True)
    for n, name in enumerate(("Red", "Blue"), 1):
        create = {"type": "Create", "id": f"{REMOTE}#votes/{n}/activity", "actor": REMOTE,
                  "object": _vote_note(poll, name, n)}
        assert run(inbox.process(create, REMOTE)) == "vote stored"
    counts, voters = outbox.count_votes(poll, [e for e in world["relay"].values() if e["kind"] == 1018])
    assert counts == {"opt1": 1, "opt2": 1} and voters == 1


def test_a_polls_counts_go_out_when_they_change(world, monkeypatch):
    _relay_filter(world, monkeypatch)
    _with_follower(world)
    import time as _t
    poll = _member_poll(world)
    poll["created_at"] = int(_t.time()) - 60
    world["relay"].pop(next(k for k, v in world["relay"].items() if v is poll))
    world["relay"][poll["id"]] = poll
    world["docs"]["pcai:ap:pollvoters:" + poll["id"]] = {"inboxes": ["https://voter.example/inbox"]}
    assert run(outbox.poll_updates([ALICE])) == 0                   # nothing to say yet
    vote = member_post("", kind=1018, tags=[["e", poll["id"]], ["response", "opt1"]], author=BOB)
    world["relay"][vote["id"]] = vote
    assert run(outbox.poll_updates([ALICE])) == 1
    upd = [a for a in world["sent"] if a["activity"]["type"] == "Update"]
    assert {a["inbox"] for a in upd} == {"https://mastodon.example/inbox", "https://voter.example/inbox"}
    q = upd[0]["activity"]["object"]
    assert q["type"] == "Question" and q["oneOf"][0]["replies"]["totalItems"] == 1 and q["votersCount"] == 1
    assert run(outbox.poll_updates([ALICE])) == 0                   # unchanged: nothing re-sent


def test_a_fediverse_poll_arrives_as_a_poll_nostr_users_can_vote_in(world, monkeypatch):
    _relay_filter(world, monkeypatch)
    world["docs"][f"pcai:ap:following:{ALICE}:x"] = {"actor": REMOTE, "inbox": "i", "state": "accepted"}
    act = _create(note_id="https://mastodon.example/polls/1", content="<p>Tea or coffee?</p>")
    act["object"].update(type="Question", endTime="2030-01-01T00:00:00Z",
                         oneOf=[{"type": "Note", "name": "Tea"}, {"type": "Note", "name": "Coffee"}])
    assert run(inbox.process(act, REMOTE)) == "stored"
    poll = [e for e in world["relay"].values() if e["kind"] == 1068][0]
    assert ["option", "opt1", "Tea"] in poll["tags"] and ["polltype", "singlechoice"] in poll["tags"]
    assert "Coffee" not in poll["content"]
    # Mastodon sends an Update of the Question on every vote: counts only, so the poll (and every
    # Nostr vote on it) is kept.
    upd = dict(act["object"], oneOf=[{"type": "Note", "name": "Tea", "replies": {"totalItems": 5}},
                                     {"type": "Note", "name": "Coffee"}])
    assert run(inbox._edit(upd, REMOTE)) == "ignored: poll counts only"
    # A member's vote goes back to the poll's server as the standard answer.
    vote = member_post("", kind=1018, tags=[["e", poll["id"]], ["response", "opt2"]])
    jobs = run(outbox.plan(vote, ALICE))
    assert jobs and jobs[0][0] == "https://mastodon.example/inbox"
    ans = jobs[0][1]["object"]
    assert ans["name"] == "Coffee" and ans["inReplyTo"] == "https://mastodon.example/polls/1" and ans["to"] == [REMOTE]


def test_pinned_posts_are_the_featured_collection(client, world, monkeypatch):
    _relay_filter(world, monkeypatch)
    post = member_post("pinned words")
    world["relay"][post["id"]] = post
    pins = member_post("", kind=10001, tags=[["e", post["id"]], ["e", "9" * 64]])
    world["relay"][pins["id"]] = pins
    doc = client.get("/ap/users/alice/featured", headers={"Accept": "application/activity+json"}).json()
    assert doc["totalItems"] == 1 and doc["orderedItems"][0]["id"] == convert.object_url(BASE, post["id"])
    assert client.get("/ap/actor/outbox").status_code == 200


# ---- the edges

def test_the_fetch_guard_refuses_cgnat_addresses(monkeypatch):
    import socket
    from app.services import rss_service
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("100.100.1.1", 443))])
    assert rss_service.is_safe_host("https://sneaky.example/") is False


def test_a_blocklist_reads_stars_punctuation_and_usernames_with_at_signs():
    from app.services import fedi_blocklist
    hosts, accounts = fedi_blocklist.parse("*bad.example\nworse.example;\n\"quoted.example\"\nbob@evil.com")
    assert {"bad.example", "worse.example", "quoted.example"} <= hosts
    assert fedi_blocklist.account_blocked("bob@evil.com", accounts)
    # the host is after the LAST @, so a crafted username cannot move the account to another host
    assert fedi_blocklist.account_blocked("bob@x@evil.com", frozenset(), frozenset({"evil.com"}))
    assert convert.username_of({"preferredUsername": "bob@x"}) == ""
    assert inbox.acct_of_actor({"id": "https://evil.com/users/1", "preferredUsername": "bob@x"}) == ""


def test_a_lemmy_person_and_community_of_the_same_name_get_different_nip05_names():
    person = ident.puppet_for({"uri": "https://lemmy.example/u/foo", "acct": "foo@lemmy.example"})
    group = ident.puppet_for({"uri": "https://lemmy.example/c/foo", "acct": "foo@lemmy.example"})
    assert person["pubkey_hex"] != group["pubkey_hex"] and person["nip05_name"] != group["nip05_name"]


def test_a_registry_name_that_is_somebody_elses_handle_is_not_shown_as_ones_own(world):
    other, dan = "e5" * 32, "f6" * 32
    world["docs"]["pcai:ap:nickof:dan_2024"] = {"pk": other}
    world["settings"]["nostr_relay_nip05_names"] += f"\ndan_2024 {dan}"
    actors._names_cache["raw"] = None
    assert run(actors.readable_handle(dan)) not in ("dan_2024", "")


def test_a_lookup_of_a_blocked_account_mints_nothing(client, world):
    world["settings"]["fedi_bridge_blocked_domains"] = "carol@mastodon.example"
    from app.routers import activitypub as routes
    from app.auth import get_current_user
    client.app.dependency_overrides[get_current_user] = lambda: type("U", (), {"id": 1})()
    r = client.get(f"/api/activitypub/lookup?acct={REMOTE}")
    assert r.status_code == 404
    s = world["Session"]()
    assert s.query(FediPuppet).count() == 0


def test_indexing_consent_is_only_asserted_for_local_users(client, world):
    pk, npub = _nostr_user(world)
    doc = client.get(f"/ap/users/{npub}", headers={"Accept": "application/activity+json"}).json()
    assert doc["indexable"] is False and doc["discoverable"] is False
    alice = client.get("/ap/users/alice", headers={"Accept": "application/activity+json"}).json()
    assert alice["indexable"] is True


def test_an_outgoing_dm_is_retried_when_the_recipients_server_is_down(world, monkeypatch):
    """Sent once and forgotten, a Nostr → fediverse DM written while the other server hiccupped was
    lost with nothing on either side to say so."""
    from app.services.activitypub import dm
    calls = []

    async def deliver(inbox_url, activity, *, key_id, private_pem):
        calls.append(inbox_url)
        return 502 if len(calls) == 1 else 202
    monkeypatch.setattr(remote, "deliver", deliver)
    real_sleep = asyncio.sleep

    async def fast(_s):
        await real_sleep(0)
    monkeypatch.setattr(dm.asyncio, "sleep", fast)

    async def go():
        dm._retry_later("w1:abc", "https://mastodon.example/inbox", {"id": "x", "type": "Create"}, ALICE, REMOTE, "w1")
        await asyncio.gather(*list(dm._retry_tasks))
    run(go())
    assert calls == ["https://mastodon.example/inbox"] * 2 and dm._done("w1:abc")


def test_an_import_stops_reading_an_answer_that_is_too_large(world, monkeypatch):
    from app.services.activitypub import importer
    _public_server(monkeypatch, following=[_pleroma_account(n) for n in range(50)])
    monkeypatch.setattr(importer, "MAX_PAGE_BYTES", 200)
    with pytest.raises(ValueError, match="too large"):
        run(importer.public_following("me@old.example"))


# ============================================================================ 22. follow requests, both ways

def _puppet_contact_lists(world):
    carol = ident.puppet_for(convert.account_from_actor(carol_actor()))["pubkey_hex"]
    return [e for e in world["relay"].values() if e["kind"] == 3 and e["pubkey"] == carol]


def test_a_fediverse_follow_tells_the_person_followed_the_nostr_way(world):
    """"we need to make sure following requests work both ways on the bridge, especially with
    notifications when they follow you". A fediverse Follow was accepted and recorded and the person
    followed was never told. Now carol's puppet publishes a contact list naming who she follows here --
    the event every Nostr client turns into "carol followed you"."""
    follow = {"id": REMOTE + "#follows/1", "type": "Follow", "actor": REMOTE, "object": f"{BASE}/ap/users/alice"}
    assert run(inbox.process(follow, REMOTE)).startswith("follower added")
    lists = _puppet_contact_lists(world)
    assert len(lists) == 1 and ["p", ALICE] in lists[0]["tags"]
    # the same Follow again (servers retry) must not notify twice
    run(inbox.process(follow, REMOTE))
    assert len(_puppet_contact_lists(world)) == 1
    # following a second member names both
    run(inbox.process(dict(follow, id=REMOTE + "#follows/2", object=f"{BASE}/ap/users/bob"), REMOTE))
    latest = max(_puppet_contact_lists(world), key=lambda e: (e["created_at"], e["id"]))
    assert {t[1] for t in latest["tags"] if t[0] == "p"} == {ALICE, BOB}


def test_a_fediverse_unfollow_takes_the_person_out_of_the_contact_list(world):
    follow = {"id": REMOTE + "#follows/1", "type": "Follow", "actor": REMOTE, "object": f"{BASE}/ap/users/alice"}
    run(inbox.process(follow, REMOTE))
    n = len(_puppet_contact_lists(world))
    assert run(inbox.process({"type": "Undo", "id": REMOTE + "#undo/1", "actor": REMOTE,
                              "object": follow}, REMOTE)) == "follower removed"
    lists = _puppet_contact_lists(world)
    assert len(lists) == n + 1
    latest = max(lists, key=lambda e: (e["created_at"], e["id"]))
    assert ["p", ALICE] not in latest["tags"]


def test_an_unfollow_by_id_alone_also_updates_the_contact_list(world):
    follow = {"id": REMOTE + "#follows/9", "type": "Follow", "actor": REMOTE, "object": f"{BASE}/ap/users/alice"}
    run(inbox.process(follow, REMOTE))
    run(inbox.process({"type": "Undo", "id": REMOTE + "#undo/9", "actor": REMOTE, "object": REMOTE + "#follows/9"}, REMOTE))
    latest = max(_puppet_contact_lists(world), key=lambda e: (e["created_at"], e["id"]))
    assert ["p", ALICE] not in latest["tags"]


def test_a_nostr_follow_request_to_the_fediverse_round_trips(world):
    """Nostr → fediverse: adding a fediverse account to the contact list sends a Follow, which stays
    PENDING until that server answers; Accept opens the door to its posts, Reject closes it."""
    s = world["Session"]()
    carol = run(ident.ensure_puppet(s, 1, convert.account_from_actor(carol_actor()), "mastodon.example"))
    k3 = member_post("", kind=3, tags=[["p", carol["pubkey_hex"]]])
    jobs = run(outbox._follows(k3, ALICE, f"{BASE}/ap/users/alice"))
    assert [a["type"] for _, a in jobs] == ["Follow"] and jobs[0][1]["object"] == REMOTE
    assert run(state.following(ALICE))[REMOTE]["state"] == "pending"
    fid = jobs[0][1]["id"]
    assert run(inbox.process({"type": "Accept", "id": REMOTE + "#accept/1", "actor": REMOTE,
                              "object": {"id": fid, "type": "Follow", "actor": f"{BASE}/ap/users/alice",
                                         "object": REMOTE}}, REMOTE)) == "follow accepted"
    assert run(state.following(ALICE))[REMOTE]["state"] == "accepted"
    state.forget_followed_cache()
    assert REMOTE in run(state.followed_actors())          # her posts now get in
    assert run(inbox.process({"type": "Reject", "id": REMOTE + "#reject/1", "actor": REMOTE,
                              "object": fid}, REMOTE)) == "follow rejected"
    assert REMOTE not in run(state.following(ALICE))


# ============================================================================ 21. relays

RELAY_INBOX = "https://relay.example/inbox"
RELAY_ACTOR = "https://relay.example/actor"


def _relays_on(world, *, scope="local", accepted=True):
    from app.services.activitypub import relays
    world["settings"]["activitypub_relays"] = RELAY_INBOX + "  # the big one"
    world["settings"]["activitypub_relay_scope"] = scope
    relays.forget_cache()
    if accepted:
        world["docs"][relays._PREFIX + relays._h(RELAY_INBOX)] = {
            "inbox": RELAY_INBOX, "state": "accepted", "follow_id": f"{BASE}/ap/actor#relay-follow/x/1",
            "actor": RELAY_ACTOR}
    return relays


def test_the_relay_list_is_https_unique_and_never_this_node_or_a_blocked_host(world):
    from app.services.activitypub import relays
    world["settings"]["activitypub_relays"] = "\n".join([
        "https://relay.example/inbox", "https://relay.example/inbox", "http://plain.example/inbox",
        f"https://{DOMAIN}/ap/inbox", "https://blocked.example/inbox", "not a url", "",
        "https://two.example/inbox # a comment"])
    assert relays.configured() == ["https://relay.example/inbox", "https://two.example/inbox"]


def test_a_listed_relay_is_followed_once_by_the_instance_actor_and_unfollowed_when_removed(world):
    relays = _relays_on(world, accepted=False)
    assert run(relays.reconcile()) == 1
    follow = world["sent"][-1]
    assert follow["inbox"] == RELAY_INBOX and follow["key_id"] == f"{BASE}/ap/actor#main-key"
    act = follow["activity"]
    assert act["type"] == "Follow" and act["actor"] == f"{BASE}/ap/actor" and act["object"] == config.PUBLIC
    assert run(relays.reconcile()) == 0, "a pending Follow was re-sent on the next tick"
    # nothing is sent to a relay that has not accepted
    _with_follower(world)
    assert RELAY_INBOX not in [i for i, _ in run(outbox.plan(member_post("hello"), ALICE))]
    world["settings"]["activitypub_relays"] = ""
    assert run(relays.reconcile()) == 1
    undo = world["sent"][-1]["activity"]
    assert undo["type"] == "Undo" and undo["object"]["id"] == act["id"] and undo["object"]["type"] == "Follow"


def test_only_the_relays_own_host_can_accept_our_follow(world):
    relays = _relays_on(world, accepted=False)
    run(relays.reconcile())
    fid = world["sent"][-1]["activity"]["id"]
    assert relays.is_relay_follow(fid)
    assert run(inbox.process({"type": "Accept", "id": "https://evil.example/a", "actor": "https://evil.example/actor",
                              "object": fid}, "https://evil.example/actor")) == "ignored: answered by somebody other than the relay"
    assert run(relays.accepted_inboxes(max_age=0)) == []
    assert run(inbox.process({"type": "Accept", "id": "https://relay.example/a", "actor": RELAY_ACTOR,
                              "object": f"{BASE}/ap/actor#relay-follow/nope/1"}, RELAY_ACTOR)) == "ignored: not a relay follow we sent"
    assert run(inbox.process({"type": "Accept", "id": "https://relay.example/a", "actor": RELAY_ACTOR,
                              "object": {"id": fid, "type": "Follow"}}, RELAY_ACTOR)) == "relay accepted"
    assert run(relays.accepted_inboxes(max_age=0)) == [RELAY_INBOX]


def test_a_member_post_goes_to_an_accepted_relay_and_so_does_its_deletion(world):
    _relays_on(world)
    _with_follower(world)
    post = member_post("for everyone")
    assert RELAY_INBOX in [i for i, _ in run(outbox.plan(post, ALICE))]
    delete = run(outbox.plan(member_post("", kind=5, tags=[["e", post["id"]]]), ALICE))
    assert (RELAY_INBOX, "Delete") in [(i, a["type"]) for i, a in delete]


def test_replies_and_boosts_never_go_to_a_relay(world):
    _relays_on(world)
    _with_follower(world)
    remote_note = _mirror(world)
    reply = member_post("a reply", tags=[["e", remote_note, "", "reply"]])
    assert RELAY_INBOX not in [i for i, _ in run(outbox.plan(reply, ALICE))]
    boost = member_post("", kind=6, tags=[["e", remote_note]])
    assert RELAY_INBOX not in [i for i, _ in run(outbox.plan(boost, ALICE))]


def test_the_scope_decides_whether_a_nostr_user_without_a_name_reaches_the_relays(world, monkeypatch):
    pk, _npub = _nostr_user(world)
    _relay_filter(world, monkeypatch)
    post = member_post("from a plain npub", author=pk)
    world["relay"][post["id"]] = post
    _relays_on(world, scope="local")
    assert RELAY_INBOX not in [i for i, _ in run(outbox.plan(post, pk))]
    assert post["id"] not in run(run(outbox._everyone_filter())([post]))
    _relays_on(world, scope="everyone")
    assert RELAY_INBOX in [i for i, _ in run(outbox.plan(post, pk))]
    assert post["id"] in run(run(outbox._everyone_filter())([post]))


def test_what_a_relay_relays_is_acknowledged_without_verifying_it(client, world, monkeypatch):
    """A subscribed relay pushes every post of every other member instance. Verified one by one that
    is a key fetch per post and a 429 past the host budget, which a relay reads as a dead subscriber."""
    _relays_on(world)
    fetched = []

    async def pk(key_id, refresh=False):
        fetched.append(key_id)
        return RELAY_ACTOR, CAROL_KEY[1]
    monkeypatch.setattr(remote, "public_key", pk)
    announce = {"id": "https://relay.example/a/1", "type": "Announce", "actor": RELAY_ACTOR,
                "object": "https://elsewhere.example/notes/1"}
    assert _post_signed(client, announce, key_id=RELAY_ACTOR + "#main-key").status_code == 202
    assert fetched == [] and client.scheduled == []
    # its ANSWER to us is still verified
    accept = {"id": "https://relay.example/a/2", "type": "Accept", "actor": RELAY_ACTOR,
              "object": f"{BASE}/ap/actor#relay-follow/x/1"}
    assert _post_signed(client, accept, key_id=RELAY_ACTOR + "#main-key").status_code == 202
    assert fetched and client.scheduled


def test_a_listed_relay_that_follows_back_is_accepted_and_a_stranger_is_not(world):
    world["actors"][RELAY_ACTOR] = {"id": RELAY_ACTOR, "type": "Application", "preferredUsername": "relay",
                                    "inbox": RELAY_INBOX}
    follow = {"id": "https://relay.example/f/1", "type": "Follow", "actor": RELAY_ACTOR, "object": f"{BASE}/ap/actor"}
    world["settings"]["activitypub_relays"] = ""
    assert run(inbox.process(follow, RELAY_ACTOR)).startswith("ignored")
    _relays_on(world, accepted=False)
    assert run(inbox.process(follow, RELAY_ACTOR)).startswith("relay follow accepted")
    sent = world["sent"][-1]
    assert sent["inbox"] == RELAY_INBOX and sent["activity"]["type"] == "Accept"
    assert sent["activity"]["actor"] == f"{BASE}/ap/actor"


def test_a_reply_whose_e_tag_carries_the_parents_pubkey_is_still_a_reply(world):
    """Measured on poster.place, nevent1qqsf7qtu…: a reply tagged ["e", id, relay, <parent pubkey>] went
    out as a TOP-LEVEL post. The 4th place is a marker only when it IS one (reply/root/mention)."""
    parent = "0000f626c48e07d686d01c2bfc31fbd594eb60fc1208b2251bf6b29f7436a1ad"
    ev = member_post("check out this account lol", tags=[
        ["e", parent, "wss://poster.place/relay", "2495d53db24bd7e229cfafba318f0f282c2646dabb610f5b070b53ee6d86d9ec"],
        ["p", "2495d53db24bd7e229cfafba318f0f282c2646dabb610f5b070b53ee6d86d9ec"]])
    assert outbox.parent_of(ev) == parent
    # and a quote still is not a parent
    assert outbox.parent_of(member_post("quote", tags=[["e", parent, "", "mention"]])) == ""


def test_a_bare_npub_in_the_text_is_a_mention_on_the_fediverse(world):
    """Measured on detroitriotcity.com: a post whose text began with a BARE `npub1v28hlk…` (no `nostr:`)
    arrived as 63 characters of text -- "I tagged an account but it showed as an npub". A bare bech32
    reference is a reference; one inside a URL is still just the URL."""
    from app.services.nostr import nostr_service
    npub = nostr_service.npub_of(BOB)
    ev = member_post(f"{npub} try https://poster.place/{npub} for the coolest nostr experience")
    assert BOB in outbox._referenced_pubkeys(ev)
    note = convert.note_from_event(ev, base=BASE, actor=f"{BASE}/ap/users/alice", followers="f",
                                   mentions={BOB: {"href": f"{BASE}/ap/users/bob", "name": f"@bob@{DOMAIN}"}})
    assert 'class="u-url mention">@<span>bob</span>' in note["content"], note["content"]
    assert note["content"].count("@<span>bob</span>") == 1, "the npub inside the URL became a mention too"
    assert f"https://poster.place/{npub}" in note["content"]
    # plans a Mention of bob, like the nostr: form does
    _with_follower(world)
    jobs = run(outbox.plan(ev, ALICE))
    assert any(t.get("type") == "Mention" and t.get("href") == f"{BASE}/ap/users/bob" for t in jobs[0][1]["object"]["tag"])
    # a word that merely starts with "npub1" is not a reference
    assert not list(convert._NOSTR_REF_RE.finditer("see npub1abc for details"))



def test_a_pleroma_relay_on_an_instances_own_host_does_not_silence_that_instance(client, world, monkeypatch):
    """A Pleroma/Akkoma relay is `https://<instance>/relay`. Dropping relay traffic by HOST silenced
    every post, deletion, block and DM from that instance's people. Only the relay's own ACTOR is
    acknowledged without work."""
    from app.services.activitypub import relays
    world["settings"]["activitypub_relays"] = "https://mastodon.example/inbox"
    world["docs"][relays._PREFIX + relays._h("https://mastodon.example/inbox")] = {
        "inbox": "https://mastodon.example/inbox", "state": "accepted", "follow_id": "x",
        "actor": "https://mastodon.example/relay"}
    relays.forget_cache()
    r = _post_signed(client, _create())                  # carol, on the relay's host, signing as herself
    assert r.status_code == 202 and client.scheduled, "a person on the relay's instance was silenced"
    client.scheduled.clear()

    async def pk(key_id, refresh=False):
        return "https://mastodon.example/relay", CAROL_KEY[1]
    monkeypatch.setattr(remote, "public_key", pk)
    announce = {"id": "https://mastodon.example/relay/a/1", "type": "Announce", "actor": "https://mastodon.example/relay",
                "object": "https://elsewhere.example/notes/1"}
    assert _post_signed(client, announce, key_id="https://mastodon.example/relay#main-key").status_code == 202
    assert client.scheduled == [], "what the relay relayed was processed"


def test_a_rejected_relay_is_asked_again_later_and_a_late_accept_is_honoured(world):
    relays = _relays_on(world, accepted=False)
    run(relays.reconcile())
    first = world["sent"][-1]["activity"]["id"]
    run(inbox.process({"type": "Reject", "id": "https://relay.example/r", "actor": RELAY_ACTOR, "object": first}, RELAY_ACTOR))
    doc = world["docs"][relays._PREFIX + relays._h(RELAY_INBOX)]
    assert doc["state"] == "rejected"
    assert run(relays.reconcile()) == 0                     # not straight away
    doc["at"] = 0                                            # a day later
    assert run(relays.reconcile()) == 1
    second = world["sent"][-1]["activity"]["id"]
    assert second != first
    # the relay answers the EARLIER Follow late: still honoured
    assert run(inbox.process({"type": "Accept", "id": "https://relay.example/a", "actor": RELAY_ACTOR,
                              "object": first}, RELAY_ACTOR)) == "relay accepted"
