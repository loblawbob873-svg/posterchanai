"""A shared playlist is addressed to somebody, and the relay used to serve it to nobody.

"I shared Music on TV but can't see it on phone or laptop." Measured on this node's relay: both
documents were stored (kind 30078, `l=pcai-musicshare`, `p=<recipient>`, 6.9 KB of NIP-44 for the
live one) and were unreadable by the one account they were written for --

  * `_on_req` demanded an `authors` filter the RECIPIENT cannot supply (their query is
    `{kinds:[30078], "#p":[me], "#l":[...]}`), so an anonymous probe got back exactly
    `CLOSED ... auth-required: NIP-78 reads require AUTH and matching authors`;
  * `_can_serve_event` then gated every 30078 EVENT on `pubkey in authed`, so even a recipient who
    guessed the sharer's key and authenticated correctly would have been served nothing;
  * the client never even signed: relay.js only answers an auth-required refusal when the private
    filter names exactly one AUTHOR, so "shared with me" ended its EOSE empty with no prompt.

Three layers, one symptom, nothing in any log. The rule is now "the author, or somebody the author
addressed it to" -- the same rule the relay already applies to DMs.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

from app.services.nostr.event import build_event
from app.services.nostr_relay.server import RelayServer, _matches

ROOT = Path(__file__).resolve().parents[1]

SHARER = bytes.fromhex("a1" * 32)
RECIPIENT = bytes.fromhex("b2" * 32)
STRANGER = bytes.fromhex("c3" * 32)


class Store:
    def __init__(self, events=()):
        self.events = list(events)

    async def query(self, filters, **_kw):
        return [e for e in self.events if _matches(filters, e)]

    async def count_filtered(self, filters, protect_nip78=False):
        return len([e for e in self.events if _matches(filters, e)])


class Gate:
    def is_member(self, _pk): return True
    def is_operator(self, _pk): return False
    def is_puppet_event(self, _ev): return False


def _server(events=()):
    srv = RelayServer(Store(events), Gate(), {"wot_enabled": False})
    srv.sent = []
    srv._send = lambda conn, obj: srv.sent.append((conn, obj))
    return srv


def _share(to_pubkey):
    """What musicshare.js publishes: one document per recipient, encrypted to them."""
    return build_event(SHARER, 30078, "nip44-ciphertext",
                       [["d", "pcai:musicshare:abc123:" + to_pubkey[:16]],
                        ["p", to_pubkey], ["l", "pcai-musicshare"]])


def _pk(sk):
    return build_event(sk, 1, "", [])["pubkey"]


def _read(srv, conn, filters, authed=()):
    srv._auth_pubkeys[conn] = set(authed)
    srv.sent = []
    import asyncio
    asyncio.run(srv._on_req(conn, "s1", filters))
    events = [m[2] for _c, m in srv.sent if m[0] == "EVENT"]
    closed = [m[2] for _c, m in srv.sent if m[0] == "CLOSED"]
    return events, closed


def test_the_recipient_can_read_a_document_addressed_to_them():
    me, share = _pk(RECIPIENT), None
    share = _share(_pk(RECIPIENT))
    srv = _server([share])
    filters = [{"kinds": [30078], "#p": [me], "#l": ["pcai-musicshare"]}]

    events, closed = _read(srv, object(), filters, authed=[me])
    assert not closed, closed
    assert [e["id"] for e in events] == [share["id"]], "the share never reached the person it names"


def test_the_sharer_still_reads_their_own_shares():
    share = _share(_pk(RECIPIENT))
    srv = _server([share])
    events, closed = _read(srv, object(), [{"kinds": [30078], "authors": [_pk(SHARER)]}],
                           authed=[_pk(SHARER)])
    assert not closed and [e["id"] for e in events] == [share["id"]]


def test_a_stranger_gets_nothing_however_they_ask():
    share = _share(_pk(RECIPIENT))
    srv = _server([share])
    stranger = _pk(STRANGER)

    # Unauthenticated, bound to nothing: refused, as before.
    events, closed = _read(srv, object(), [{"kinds": [30078]}])
    assert events == [] and closed and "auth-required" in closed[0]

    # Authenticated as themselves and asking for the SHARER's documents: the filter is not bound to
    # them at all.
    events, closed = _read(srv, object(), [{"kinds": [30078], "authors": [_pk(SHARER)]}],
                           authed=[stranger])
    assert events == [] and closed and "auth-required" in closed[0]

    # Authenticated, asking for documents addressed to SOMEBODY ELSE.
    events, closed = _read(srv, object(), [{"kinds": [30078], "#p": [_pk(RECIPIENT)]}],
                           authed=[stranger])
    assert events == [] and closed and "auth-required" in closed[0]

    # And the per-event gate is the backstop: a filter they ARE allowed to send must not hand them
    # somebody else's document, whatever else rides in the same REQ.
    events, closed = _read(srv, object(),
                           [{"kinds": [30078], "#p": [stranger]}, {"kinds": [30078]}],
                           authed=[stranger])
    assert [e["id"] for e in events] == [], "a mixed REQ leaked a document past the gate"


def test_a_document_addressed_to_nobody_is_still_the_authors_alone():
    """The private datastore (settings, notes, the drive index) carries no `p` tag and must not
    become readable because this rule exists."""
    private = build_event(SHARER, 30078, "ciphertext", [["d", "pcai:files-index"]])
    srv = _server([private])
    events, closed = _read(srv, object(), [{"kinds": [30078], "#p": [_pk(RECIPIENT)]}],
                           authed=[_pk(RECIPIENT)])
    assert events == [], "a document addressed to nobody was served to a stranger"
    assert not closed, "a recipient-bound filter is allowed to ASK; it just matches nothing here"


@pytest.mark.skipif(shutil.which("node") is None, reason="node unavailable")
def test_the_shipped_client_authenticates_for_a_read_addressed_to_it():
    run = subprocess.run(["node", str(ROOT / "tests/client/nip78_addressed_auth_runtime.mjs")],
                         cwd=ROOT, text=True, capture_output=True, timeout=30)
    assert run.returncode == 0, run.stdout + run.stderr


# ---- what a security review of the change above found, each verified to fail without its fix ----

def test_a_private_note_that_happens_to_name_somebody_is_not_shared_with_them():
    """`p` IS NOT AN ACCESS GRANT IN NIP-78, and this relay stores 30078 written by any client.

    "Whoever is p-tagged may read it" would impose a rule on strangers' app data that their app
    never agreed to — a note ABOUT a contact, a mention, a bot's address, an attribution. So the
    grant is scoped to the documents that ARE a share (`l=pcai-musicshare` AND a matching `d`),
    and everything else stays the author's alone however it is tagged."""
    note = build_event(SHARER, 30078, "ciphertext",
                       [["d", "pcai:note:abc"], ["p", _pk(RECIPIENT)]])
    srv = _server([note])
    events, closed = _read(srv, object(), [{"kinds": [30078], "#p": [_pk(RECIPIENT)]}],
                           authed=[_pk(RECIPIENT)])
    assert events == [], "a p-tagged private note was served to the pubkey it names"
    assert not closed

    # Half a share is not a share: either tag alone is one anybody can write onto anything.
    for tags in ([["d", "pcai:musicshare:x:y"], ["p", _pk(RECIPIENT)]],
                 [["d", "pcai:note:abc"], ["p", _pk(RECIPIENT)], ["l", "pcai-musicshare"]]):
        srv = _server([build_event(SHARER, 30078, "c", tags)])
        events, _c = _read(srv, object(), [{"kinds": [30078], "#p": [_pk(RECIPIENT)]}],
                           authed=[_pk(RECIPIENT)])
        assert events == [], f"a half-labelled document was treated as a share: {tags}"


def test_a_document_addressed_to_a_third_party_is_not_mine_to_read():
    """The direct negative for the new branch: authenticated, asking with a filter I AM allowed to
    send, about a share somebody addressed to a third person."""
    share = _share(_pk(STRANGER))
    srv = _server([share])
    me = _pk(RECIPIENT)
    events, closed = _read(srv, object(), [{"kinds": [30078], "#p": [me]}], authed=[me])
    assert events == [], "a share addressed to a third party was served"
    assert not closed


def test_asking_for_several_recipients_at_once_is_refused():
    share = _share(_pk(RECIPIENT))
    srv = _server([share])
    me = _pk(RECIPIENT)
    events, closed = _read(srv, object(),
                           [{"kinds": [30078], "#p": [me, _pk(STRANGER)]}], authed=[me])
    assert events == [] and closed and "auth-required" in closed[0]

    # Two filters, each bound to a different person, is the same ask wearing a different shape.
    events, closed = _read(srv, object(),
                           [{"kinds": [30078], "#p": [me]}, {"kinds": [30078], "#p": [_pk(STRANGER)]}],
                           authed=[me])
    assert events == [] and closed and "auth-required" in closed[0]


def test_a_bound_filter_cannot_carry_an_unbound_one_into_the_live_stream():
    """THE GATE IN `_on_req` ONLY EVER GUARDED THE STORED PASS, and the live half is where the
    private libraries actually travel.

    Every filter in a REQ is registered for live delivery, and five of the seven `fanout` call
    sites passed no gate at all — including the one inside `backfill_author`, which restores Notes,
    the vault, the calendar, the addressbook, Budget and the files index from the private mirrors.
    So: pair a filter bound to yourself with an unbound sibling, hold the socket, and be handed
    another user's library the moment they press "sync my data". Nothing logs it."""
    srv = _server([])
    me, conn = _pk(RECIPIENT), object()
    events, closed = _read(srv, conn, [{"kinds": [30078], "#p": [me]}, {"kinds": [30078]}],
                           authed=[me])
    assert not closed, "the REQ itself is allowed — the leak was in what it registered"

    srv.sent = []
    victims_note = build_event(SHARER, 30078, "ciphertext", [["d", "pcai:note:private"]])
    srv.subs.fanout(victims_note, srv._send, srv._can_serve_event)
    assert srv.sent == [], "a live 30078 was fanned out to a subscription that may not read it"

    # And the share this connection IS entitled to still arrives, or the fix broke the feature.
    srv.sent = []
    srv.subs.fanout(_share(me), srv._send, srv._can_serve_event)
    assert [m[0] for _c, m in srv.sent] == ["EVENT"], "the recipient stopped receiving live shares"


def test_fanout_has_no_ungated_default():
    """The five leaking call sites all took a default that meant "send it to everybody". A default
    like that gets taken by whoever adds the next one, so there is no default."""
    srv = _server([])
    with pytest.raises(TypeError):
        srv.subs.fanout(_share(_pk(RECIPIENT)), srv._send)


def test_a_count_cannot_ride_along_on_a_bound_filter():
    """`count_filtered` counts each filter SEPARATELY while the gate validated the UNION of
    `authors`, so one bound filter carried an unbound sibling and the answer was a count of every
    user's private documents. No content and no ids — but "how many notes does this instance hold
    for everyone" is not the caller's to know."""
    import asyncio
    me = _pk(SHARER)
    srv = _server([build_event(SHARER, 30078, "c", [["d", "pcai:note:1"]]),
                   build_event(RECIPIENT, 30078, "c", [["d", "pcai:note:2"]])])
    conn = object()
    srv._auth_pubkeys[conn] = {me}
    srv.sent = []
    asyncio.run(srv._on_count(conn, "c1", [{"kinds": [30078], "authors": [me]}, {"kinds": [30078]}]))
    kinds = [m[0] for _c, m in srv.sent]
    assert "COUNT" not in kinds, "a mixed COUNT answered with everybody's private documents"
    assert any(k == "CLOSED" for k in kinds)

    # The ordinary, fully-bound count still works.
    srv.sent = []
    asyncio.run(srv._on_count(conn, "c2", [{"kinds": [30078], "authors": [me]}]))
    assert [m[0] for _c, m in srv.sent] == ["COUNT"]
