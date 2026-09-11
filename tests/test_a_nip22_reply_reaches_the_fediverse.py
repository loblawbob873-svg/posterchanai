"""EVERY REPLY IS A NIP-22 COMMENT NOW, AND THE BRIDGE WAS STILL WATCHING FOR NIP-10.

"why are my replies not getting sent over the fediverse bridge for a fedi discussion", then
"nothing is getting sent today for replies it seems".

The client publishes a reply to a kind-1 as a **kind 1111** NIP-22 comment — `replyKindFor` returns
1111 whenever `_commentScope` yields a scope, and for an ordinary note it always does. The write-back
service was built for NIP-10 and never caught up, in three separate places, each of which alone is
enough to drop the reply on the floor:

  1. `_WRITEBACK_KINDS` had no 1111, so the bridge never SUBSCRIBED to the events people write;
  2. `_is_reply` and `_reply_parent_id` could not read them. NIP-22 puts the parent's AUTHOR PUBKEY
     in `t[3]`, where NIP-10 keeps its marker — so the marker branch sees an unknown marker and the
     positional branch, which takes e-tags with an EMPTY fourth element, skips every one of them.
     Measured on 100 real kind-1111 events: `is_reply` False and `parent` None for ALL of them;
  3. the dispatch read `elif kind == 1`, so even a parsed 1111 fell off the end of the chain and
     federated nothing, silently.

Nothing was broken, nothing logged, and the replies were on Nostr the whole time.
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = (ROOT / "app/services/fedi_nostr_writeback_service.py").read_text(encoding="utf-8")

from app.services.fedi_nostr_writeback_service import (      # noqa: E402
    _is_reply, _reply_parent_id, _nip22_parent, _WRITEBACK_KINDS)

ROOT_ID = "a" * 64
PARENT_ID = "b" * 64
AUTHOR = "c" * 64
ROOT_PK = "d" * 64


def nip22(parent=PARENT_ID, root=ROOT_ID, parent_kind="1"):
    """A NIP-22 comment exactly as the client builds one: UPPERCASE scope = the unchanged root,
    lowercase = the immediate parent, and the author pubkey in the marker position."""
    return {"kind": 1111, "pubkey": AUTHOR, "id": "e" * 64, "content": "hi", "tags": [
        ["E", root, "wss://r", ROOT_PK], ["K", "1"], ["P", ROOT_PK, "wss://r"],
        ["e", parent, "wss://r", ROOT_PK], ["k", parent_kind], ["p", ROOT_PK, "wss://r"]]}


def test_the_bridge_subscribes_to_the_kind_people_actually_write():
    assert 1111 in _WRITEBACK_KINDS, (
        "the write-back does not subscribe to NIP-22 comments, which is every reply the client "
        "makes — so replies reach the fediverse never")


def test_a_nip22_comment_is_recognised_as_a_reply():
    """If this says False the reply is treated as a top-level note and CROSS-POSTED as a standalone
    public status, which leaks a conversation out of context — the opposite failure, and worse."""
    assert _is_reply(nip22()) is True


def test_the_immediate_parent_wins_over_the_root():
    """THE RULE THE NIP-10 PATH ALREADY HAD, kept. Resolving to the root would federate a reply
    aimed at somebody else onto the thread's opening post."""
    ev = nip22(parent=PARENT_ID, root=ROOT_ID)
    assert _reply_parent_id(ev) == PARENT_ID
    assert _nip22_parent(ev) == PARENT_ID


def test_a_top_level_comment_scopes_to_its_root():
    """A NIP-22 comment ON the root carries only the uppercase scope."""
    ev = {"kind": 1111, "pubkey": AUTHOR, "id": "f" * 64, "content": "hi",
          "tags": [["E", ROOT_ID, "wss://r", ROOT_PK], ["K", "1"], ["P", ROOT_PK]]}
    assert _reply_parent_id(ev) == ROOT_ID
    assert _is_reply(ev) is True


def test_nip10_replies_are_untouched():
    """The old shape has to keep working: most of the network still writes it."""
    marked = {"kind": 1, "pubkey": AUTHOR, "id": "0" * 64, "content": "hi",
              "tags": [["e", ROOT_ID, "", "root"], ["e", PARENT_ID, "", "reply"]]}
    assert _reply_parent_id(marked) == PARENT_ID
    positional = {"kind": 1, "pubkey": AUTHOR, "id": "1" * 64, "content": "hi",
                  "tags": [["e", ROOT_ID], ["e", PARENT_ID]]}
    assert _reply_parent_id(positional) == PARENT_ID
    assert _is_reply(positional) is True


def test_a_plain_note_is_not_a_reply():
    assert _is_reply({"kind": 1, "pubkey": AUTHOR, "id": "2" * 64, "tags": [], "content": "hi"}) is False
    assert _nip22_parent({"kind": 1, "tags": [["e", PARENT_ID]]}) is None


def test_the_dispatch_handles_the_comment_kind():
    """Parsing it is not enough: the branch has to run."""
    assert "elif kind in (1, 1111):" in SRC, (
        "the reply branch is keyed on kind 1 alone again, so a parsed NIP-22 comment falls off the "
        "end of the chain and federates nothing")


def test_a_quote_is_still_not_a_reply():
    """A `q` tag is an embed. Reading one as a reply federates a quote as though it answered the
    quoted post."""
    ev = {"kind": 1, "pubkey": AUTHOR, "id": "3" * 64, "content": "hi",
          "tags": [["q", PARENT_ID], ["e", PARENT_ID]]}
    assert _is_reply(ev) is False
