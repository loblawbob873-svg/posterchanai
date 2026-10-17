"""A REPORT FILED HERE REACHES THE REPORTED ACCOUNT'S SERVER (ActivityPub `Flag`), AND OURS READS THEIRS.

"we need a way to file reports on users posts. make sure works on fedi/nostr" -- and then "we're a
fediverse server, remember". A member's NIP-56 report (kind 1984) about a fediverse account bridged here
used to stay on Nostr: the moderators who can actually act on that account -- its own server's -- never
heard of it. Runs the SHIPPED outbox/inbox/HTTP-signature code; the boundaries are the fixtures of
tests/test_activitypub.py (relay, remote servers, the puppet registry), plus ONE real wire: the outbound
Flag is sent through the real `remote.deliver` (signing included) into a mock transport, and that exact
request -- body and signature headers -- is POSTed into OUR OWN `/ap/inbox` acting as the other server.

Shapes the receiving servers need (checked against their handlers, 2026-10-11):
  * Mastodon (ActivityPub::Activity::Flag): `object` is an ARRAY of URIs -- the account, then its
    statuses; `content` is the comment; the activity `id` must be on the ACTOR'S host.
  * Akkoma/Pleroma (Transmogrifier Flag): `object` reduced with Enum over ap_id STRINGS (a bare string
    object crashes the reduce), the first one that is a local user is the account; `content` is the
    comment; the actor must be fetchable (its key verifies the HTTP signature).
"""
import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.models import FediBridgeDelivered, FediPuppet
from app.services.activitypub import actors, convert, inbox, outbox, remote, state
from app.services import fedi_bridge_identity as ident
from tests.test_activitypub import (ALICE, BASE, KEY, REMOTE, member_post, run,
                                    _mirrored_carol_note)
from tests.test_activitypub import world  # noqa: F401  (fixture)

REAL_DELIVER = remote.deliver
CAROL_INBOX = "https://mastodon.example/inbox"
CAROL_NOTE = "https://mastodon.example/notes/7"
MIRROR_ID = "7" * 64


def _report(tags, content="Selling fake followers", created=1_700_000_500):
    return member_post(content, kind=1984, tags=tags, created=created)


def _flags(jobs):
    return [(i, a) for i, a in jobs if a.get("type") == "Flag"]


# ------------------------------------------------------------------------------------ the shape

def test_a_report_of_a_fediverse_post_becomes_a_flag_to_its_server(world):
    carol = _mirrored_carol_note(world)
    rep = _report([["e", MIRROR_ID, "spam"], ["p", carol, "spam"]])
    jobs = run(outbox.plan(rep, ALICE))
    assert [i for i, _ in jobs] == [CAROL_INBOX], jobs          # the account's SHARED inbox, nobody else
    (_, flag), = jobs
    assert flag["type"] == "Flag"
    assert flag["actor"] == f"{BASE}/ap/users/alice"
    # Mastodon and Akkoma both read `object` as a LIST of URIs: the account first, then its statuses.
    assert flag["object"] == [REMOTE, CAROL_NOTE]
    assert all(isinstance(o, str) for o in flag["object"]), "Akkoma reduces over ap_id strings"
    assert flag["content"] == "[spam] Selling fake followers"
    # Mastodon keeps the report's uri only when the id is on the actor's own host.
    assert remote.host_of(flag["id"]) == remote.host_of(flag["actor"])
    assert rep["id"] in flag["id"]
    assert flag["to"] == [REMOTE]


def test_a_report_of_an_account_alone_flags_just_the_account(world):
    carol = _mirrored_carol_note(world)
    (_, flag), = run(outbox.plan(_report([["p", carol, "impersonation"]], content=""), ALICE))
    assert flag["object"] == [REMOTE] and flag["content"] == "[impersonation]"


def test_reporting_a_native_nostr_user_or_a_member_sends_no_flag(world):
    carol = _mirrored_carol_note(world)
    assert _flags(run(outbox.plan(_report([["p", carol, "spam"]], created=1_700_000_400), ALICE))), "control"
    native = "c3" * 32
    post = member_post("nostr words", author=native)
    world["relay"][post["id"]] = post
    assert _flags(run(outbox.plan(_report([["e", post["id"], "spam"], ["p", native, "spam"]]), ALICE))) == []
    bob_post = member_post("bob's words", author="b2" * 32)
    world["relay"][bob_post["id"]] = bob_post
    assert _flags(run(outbox.plan(_report([["e", bob_post["id"], "spam"], ["p", "b2" * 32, "spam"]],
                                          created=1_700_000_501), ALICE))) == []


def test_a_blocked_server_gets_no_flag(world):
    carol = _mirrored_carol_note(world)
    # The control first: the same report, unblocked, IS flagged -- so the empty answer below is the block.
    assert _flags(run(outbox.plan(_report([["p", carol, "spam"]], created=1_700_000_400), ALICE)))
    world["settings"]["fedi_bridge_blocked_domains"] = "mastodon.example"
    assert run(outbox.plan(_report([["e", MIRROR_ID, "spam"], ["p", carol, "spam"]]), ALICE)) == []


def test_a_report_is_flagged_once_and_cannot_be_deleted_into_a_delete(world, monkeypatch):
    """The delivery pass sends it once; a second pass (and a restart that forgot the pass's own memory)
    sends nothing, because the ledger records it. A kind-5 of the report sends nothing either -- a Flag
    cannot be withdrawn, and a Delete naming the report's id as a Note would be nonsense."""
    carol = _mirrored_carol_note(world)
    world["docs"]["pcai:ap:cursor"] = {"since": 1_700_000_000}
    world["docs"]["pcai:ap:cursor:everyone"] = {"since": 1_700_000_000}
    world["settings"]["activitypub_everyone"] = "false"
    rep = _report([["e", MIRROR_ID, "spam"], ["p", carol, "spam"]])
    world["relay"][rep["id"]] = rep

    async def since(members, since, kinds=None, **kw):
        return [e for e in world["relay"].values() if e["created_at"] > since and e["kind"] in (kinds or outbox.KINDS)
                and (members is None or e["pubkey"] in members)]
    monkeypatch.setattr(outbox, "_since", since)
    monkeypatch.setattr(actors, "all_actors", lambda: [ALICE])
    run(outbox.tick())
    flags = [s for s in world["sent"] if s["activity"]["type"] == "Flag"]
    assert len(flags) == 1 and flags[0]["inbox"] == CAROL_INBOX
    assert flags[0]["key_id"] == f"{BASE}/ap/users/alice#main-key", "signed as the member who reported"
    run(outbox.tick())
    # A restart: the pass's in-memory and cursor memory both gone -- the ledger still says "sent".
    outbox._seen.clear()
    outbox._done.clear()
    outbox._last_full.clear()
    world["docs"]["pcai:ap:cursor"] = {"since": 1_700_000_000}
    run(outbox.tick())
    assert len([s for s in world["sent"] if s["activity"]["type"] == "Flag"]) == 1, "the report was flagged twice"
    undo = member_post("", kind=5, tags=[["e", rep["id"]], ["k", "1984"]], created=1_700_000_600)
    assert run(outbox.plan(undo, ALICE)) == []


# ------------------------------------------------------------------------------------ the round trip

DAVE_HOST = "other.test"
DAVE_URI = f"https://{DAVE_HOST}/ap/users/dave"
DAVE_INBOX = f"https://{DAVE_HOST}/ap/inbox"
DAVE = "d4" * 32


def _dave_actor():
    return {"id": DAVE_URI, "type": "Person", "preferredUsername": "dave", "name": "Dave",
            "inbox": DAVE_URI + "/inbox", "endpoints": {"sharedInbox": DAVE_INBOX},
            "publicKey": {"id": DAVE_URI + "#main-key", "owner": DAVE_URI, "publicKeyPem": KEY[1]}}


def _inbox_app(monkeypatch):
    from app.routers import activitypub as routes
    for table in (routes._hits, routes._refreshed, routes._outbox_counts, routes._imported):
        table.clear()
    scheduled = []
    monkeypatch.setattr(inbox, "schedule", lambda act, signer: scheduled.append((act, signer)) or True)
    app = FastAPI()
    app.include_router(routes.router)
    return app, scheduled


def _become(world, domain, names):
    """Switch the in-memory node to another server: its own domain and NIP-05 registry."""
    world["settings"]["activitypub_domain"] = domain
    world["settings"]["nostr_relay_nip05_names"] = names
    actors._names_cache["raw"] = None
    actors._profile_cache.clear()
    actors._known_cache.clear()
    remote._actors.clear()


def test_our_flag_on_the_wire_is_read_by_our_own_inbox_as_the_same_report(world, monkeypatch):
    # ---- node A (poster.test): alice reports dave's post, which arrived here from other.test.
    dave_post = member_post("dave sells followers", author=DAVE, created=1_699_999_000)
    world["actors"][DAVE_URI] = _dave_actor()
    puppet = ident.puppet_for(convert.account_from_actor(_dave_actor()))["pubkey_hex"]
    s = world["Session"]()
    s.add(FediPuppet(actor_uri=DAVE_URI, acct=f"dave@{DAVE_HOST}", pubkey_hex=puppet, nip05_name="d"))
    s.add(FediBridgeDelivered(platform="activitypub", instance_url=f"https://{DAVE_HOST}", note_id="n",
                              note_uri=convert.object_url(f"https://{DAVE_HOST}", dave_post["id"]),
                              nostr_event_id=MIRROR_ID, nostr_pubkey=puppet))
    s.commit()
    rep = _report([["e", MIRROR_ID, "spam"], ["p", puppet, "spam"]])
    alice_actor = run(actors.person(ALICE))
    (inbox_url, flag), = run(outbox.plan(rep, ALICE))
    assert inbox_url == DAVE_INBOX

    # The REAL delivery: signed exactly as it would leave this server; the transport records the request.
    wire = []

    def handler(request):
        wire.append(request)
        return httpx.Response(202)

    async def no_dns(url):
        return None
    monkeypatch.setattr(remote, "_check", no_dns)
    monkeypatch.setattr(remote, "client", lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    key_id, priv = run(actors.signing(ALICE, run(state.keypair(ALICE))))
    assert run(REAL_DELIVER(inbox_url, flag, key_id=key_id, private_pem=priv)) == 202
    (req,), body = wire, wire[0].content

    # ---- node B (other.test): dave is OUR member; alice is the remote reporter.
    _become(world, DAVE_HOST, f"dave {DAVE}")
    world["relay"][dave_post["id"]] = dave_post
    world["actors"][alice_actor["id"]] = alice_actor

    async def public_key(kid, refresh=False):
        if kid == key_id:
            return alice_actor["id"], alice_actor["publicKey"]["publicKeyPem"]
        raise remote.FetchError("unknown key")
    monkeypatch.setattr(remote, "public_key", public_key)
    app, scheduled = _inbox_app(monkeypatch)
    headers = {k: v for k, v in req.headers.items() if k.lower() not in ("content-length", "host", "transfer-encoding")}
    with TestClient(app, base_url=f"https://{DAVE_HOST}") as c:
        r = c.post(req.url.path, content=body, headers=headers)
    assert r.status_code == 202, r.text                         # our own verifier accepts our own signature
    (act, signer), = scheduled
    assert signer == alice_actor["id"]
    assert run(inbox.process(act, signer)) == "report stored"
    stored = [e for e in world["relay"].values() if e["kind"] == 1984 and e["pubkey"] != ALICE]
    assert len(stored) == 1
    ev = stored[0]
    alice_puppet = ident.puppet_for(convert.account_from_actor(alice_actor))["pubkey_hex"]
    assert ev["pubkey"] == alice_puppet, "the report is signed by the REPORTER's puppet"
    assert ["p", DAVE, "spam"] in ev["tags"] and ["e", dave_post["id"], "spam"] in ev["tags"], ev["tags"]
    assert ev["content"] == "Selling fake followers"


# ------------------------------------------------------------------------------------ what others send

def _instance_actor(uri, username):
    return {"id": uri, "type": "Application", "preferredUsername": username, "name": username,
            "inbox": uri + "/inbox", "publicKey": {"id": uri + "#main-key", "owner": uri, "publicKeyPem": KEY[1]}}


def test_a_flag_shaped_like_mastodons_becomes_a_report(world):
    """Mastodon forwards a report as its INSTANCE actor (an Application), object = [account, statuses...]
    as URI strings, content = the comment, and no category in the activity."""
    post = member_post("reported words")
    world["relay"][post["id"]] = post
    actor = "https://mastodon.example/actor"
    world["actors"][actor] = _instance_actor(actor, "mastodon.example")
    flag = {"@context": "https://www.w3.org/ns/activitystreams", "id": "https://mastodon.example/2c4f0a",
            "type": "Flag", "actor": actor, "content": "Harassing people in replies",
            "object": [f"{BASE}/ap/users/alice", convert.object_url(BASE, post["id"])]}
    assert run(inbox.process(flag, actor)) == "report stored"
    ev, = [e for e in world["relay"].values() if e["kind"] == 1984]
    assert ["p", ALICE, "other"] in ev["tags"] and ["e", post["id"], "other"] in ev["tags"]
    assert ev["content"] == "Harassing people in replies"


def test_a_flag_shaped_like_akkomas_becomes_a_report(world):
    """Akkoma/Pleroma embed each reported status as an OBJECT (not a bare URI) and add context/state/cc."""
    post = member_post("reported words")
    world["relay"][post["id"]] = post
    actor = "https://akkoma.example/relay"
    world["actors"][actor] = _instance_actor(actor, "relay")
    note_uri = convert.object_url(BASE, post["id"])
    flag = {"@context": ["https://www.w3.org/ns/activitystreams", "https://akkoma.example/schemas/litepub-0.1.jsonld"],
            "id": "https://akkoma.example/activities/9f1e", "type": "Flag", "actor": actor,
            "content": "[spam] bot account", "context": "https://akkoma.example/contexts/1", "state": "open",
            "cc": [f"{BASE}/ap/users/alice"],
            "object": [f"{BASE}/ap/users/alice",
                       {"type": "Note", "id": note_uri, "content": "reported words", "actor": f"{BASE}/ap/users/alice",
                        "published": "2023-11-14T22:13:20Z"}]}
    assert run(inbox.process(flag, actor)) == "report stored"
    ev, = [e for e in world["relay"].values() if e["kind"] == 1984]
    assert ["p", ALICE, "spam"] in ev["tags"] and ["e", post["id"], "spam"] in ev["tags"], ev["tags"]
    assert ev["content"] == "bot account"
