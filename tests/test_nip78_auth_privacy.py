import asyncio
import time
from pathlib import Path

from app.services.nostr.event import build_event
from app.services.nostr_relay.server import RelayServer, SubscriptionManager, _matches
from app.services import nostr_store


class Store:
    def __init__(self, events=()):
        self.events = list(events)
        self.stored = []

    async def query(self, filters, **_kw):
        return [event for event in self.events if _matches(filters, event)]

    async def count_filtered(self, filters):
        return len(self.events)

    async def add_event(self, event, **_kw):
        self.stored.append(event)
        return True


class Gate:
    def is_member(self, _pubkey): return True
    def is_operator(self, _pubkey): return False
    def is_puppet_event(self, _event): return False


def server(events=()):
    srv = RelayServer(Store(events), Gate(), {"wot_enabled": False})
    srv.sent = []
    srv._send = lambda conn, obj: srv.sent.append((conn, obj))
    return srv


def signed(sk, kind, tags, content="ciphertext"):
    return build_event(sk, kind, content, tags)


def test_nip42_auth_requires_fresh_matching_challenge_relay_and_signature():
    sk = bytes.fromhex("11" * 32)
    conn = object()
    srv = server()
    srv._auth_challenges[conn] = "challenge"
    srv._auth_pubkeys[conn] = set()
    srv._relay_urls[conn] = "wss://relay.example/relay"

    good = signed(sk, 22242, [["relay", "wss://relay.example/relay/"], ["challenge", "challenge"]], "")
    srv._on_auth(conn, good)
    assert srv.sent[-1][1] == ["OK", good["id"], True, ""]
    assert good["pubkey"] in srv._auth_pubkeys[conn]

    for bad in (
        signed(sk, 22242, [["relay", "wss://other.example/relay"], ["challenge", "challenge"]], ""),
        signed(sk, 22242, [["relay", "wss://relay.example/relay"], ["challenge", "wrong"]], ""),
        build_event(sk, 22242, "", [["relay", "wss://relay.example/relay"], ["challenge", "challenge"]],
                    created_at=int(time.time()) - 601),
        signed(sk, 1, [["relay", "wss://relay.example/relay"], ["challenge", "challenge"]], ""),
    ):
        srv._on_auth(conn, bad)
        assert srv.sent[-1][1][0:3] == ["OK", bad["id"], False]


def test_nip78_write_requires_same_author_connection_auth():
    owner_sk, other_sk = bytes.fromhex("12" * 32), bytes.fromhex("13" * 32)
    event = signed(owner_sk, 30078, [["d", "app:settings"]])
    conn = object()
    srv = server()
    srv._auth_pubkeys[conn] = {signed(other_sk, 1, [])["pubkey"]}

    asyncio.run(srv._on_event(conn, event))
    assert srv.store.stored == []
    assert srv.sent[-1][1] == ["OK", event["id"], False,
                               "auth-required: authenticate as the NIP-78 event author"]

    srv.sent.clear()
    srv._auth_pubkeys[conn].add(event["pubkey"])
    asyncio.run(srv._on_event(conn, event))
    assert srv.store.stored == [event]
    assert srv.sent[0][1] == ["OK", event["id"], True, ""]


def test_auth_event_can_never_be_published_or_fanned_out():
    sk = bytes.fromhex("18" * 32)
    event = signed(sk, 22242, [["relay", "wss://relay.example"], ["challenge", "x"]], "")
    conn = object()
    srv = server()
    asyncio.run(srv._on_event(conn, event))
    assert srv.store.stored == []
    assert srv.sent[-1][1][0:3] == ["OK", event["id"], False]
    assert "only valid in an AUTH" in srv.sent[-1][1][3]


def test_nip78_read_requires_owner_bound_filter_and_never_leaks_through_broad_query():
    owner_sk, other_sk = bytes.fromhex("14" * 32), bytes.fromhex("15" * 32)
    private = signed(owner_sk, 30078, [["d", "app:secret"]])
    other_private = signed(other_sk, 78, [["d", "app:log"]])
    public = signed(other_sk, 1, [], "public")
    conn = object()
    srv = server([private, other_private, public])
    owner = private["pubkey"]
    srv._auth_pubkeys[conn] = {owner}

    asyncio.run(srv._on_req(conn, "bad", [{"kinds": [30078]}]))
    assert srv.sent[-1][1][0:3] == ["CLOSED", "bad", "auth-required: NIP-78 reads require AUTH and matching authors"]

    srv.sent.clear()
    asyncio.run(srv._on_req(conn, "mine", [{"kinds": [30078], "authors": [owner]}]))
    delivered = [m[1][2] for m in srv.sent if m[1][0] == "EVENT"]
    assert delivered == [private]

    srv.sent.clear()
    asyncio.run(srv._on_req(conn, "broad", [{}]))
    delivered = [m[1][2] for m in srv.sent if m[1][0] == "EVENT"]
    assert {event["id"] for event in delivered} == {private["id"], public["id"]}
    assert other_private not in delivered


def test_live_fanout_applies_same_owner_rule_even_to_broad_subscriptions():
    sk = bytes.fromhex("16" * 32)
    private = signed(sk, 30078, [["d", "app:secret"]])
    owner, stranger = object(), object()
    sm = SubscriptionManager()
    sm.add(owner, "all", [{}])
    sm.add(stranger, "all", [{}])
    sent = []
    sm.fanout(private, lambda conn, msg: sent.append((conn, msg)),
              lambda conn, ev: conn is owner)
    assert [conn for conn, _ in sent] == [owner]


def test_nip11_advertises_both_auth_and_private_app_data_support():
    import json
    doc = json.loads(server().nip11_doc("relay.example"))
    assert 42 in doc["supported_nips"]
    assert 78 in doc["supported_nips"]


def test_negentropy_cannot_bypass_private_read_authorization():
    conn = object()
    srv = server()
    srv._auth_challenges[conn] = "challenge"
    srv._auth_pubkeys[conn] = set()
    asyncio.run(srv._on_neg_open(conn, "neg", {"kinds": [30078], "authors": ["a" * 64]}, "00"))
    assert srv.sent[-1][1][0:2] == ["NEG-ERR", "neg"]
    assert srv.sent[-1][1][2].startswith("auth-required:")


def test_public_multiplayer_protocol_no_longer_uses_private_nip78_kind():
    root = Path(__file__).resolve().parents[1]
    paths = [
        *(root / "static/js/client").glob("*.js"),
        *(root / "botframework").glob("*Listener.py"),
    ]
    game_names = ("blackjack", "chess", "connect4", "hangman", "holdem", "ttt")
    game_paths = [p for p in paths if any(name in p.name.lower() for name in game_names)]
    assert game_paths, "game protocol scan found no files"
    offenders = [str(p.relative_to(root)) for p in game_paths if "30078" in p.read_text(errors="ignore")]
    assert offenders == [], "public cross-author game data still abuses private NIP-78: " + ", ".join(offenders)
    for path in game_paths:
        assert "30388" in path.read_text(errors="ignore"), f"{path.name} did not migrate to the dedicated game kind"


def test_server_side_datastore_authenticates_and_replays_private_write():
    sk = bytes.fromhex("17" * 32)
    event = signed(sk, 30078, [["d", "pcai:test"]])

    class Socket:
        def __init__(self):
            self.sent = []
            self.replies = [
                ["AUTH", "challenge"],
                ["OK", event["id"], False, "auth-required: owner"],
                None,  # replaced below with the signed AUTH event's id
                ["OK", event["id"], True, ""],
            ]

        async def send(self, raw):
            import json
            self.sent.append(json.loads(raw))
            if self.sent[-1][0] == "AUTH":
                self.replies[self.replies.index(None)] = ["OK", self.sent[-1][1]["id"], True, ""]

        async def recv(self):
            import json
            return json.dumps(self.replies.pop(0))

    ws = Socket()
    ok, message = asyncio.run(nostr_store._publish_once(3052, event, 1, ws, sk))
    assert ok and message == ""
    auth = next(frame[1] for frame in ws.sent if frame[0] == "AUTH")
    assert auth["kind"] == 22242 and auth["pubkey"] == event["pubkey"]
    assert ["challenge", "challenge"] in auth["tags"]
    assert sum(frame[0] == "EVENT" for frame in ws.sent) == 2
