"""THE CLIENT WRITES A REPLY; THE BRIDGE HAS TO BE ABLE TO READ IT.

"we need some basic fediverse testing, every time you touch the code, something breaks."

This is the test that would have caught the break, and the reason nothing did: NEITHER SIDE WAS
WRONG ON ITS OWN. The client moved ordinary replies to NIP-22 (kind 1111, with the parent's author
pubkey where NIP-10 keeps its marker) and the write-back service went on looking for NIP-10. Every
test of the client passed. Every test of the bridge passed. Replies to fediverse posts silently
stopped federating, on Nostr the whole time, with nothing in any log.

So this asks the two halves the SAME question and requires the same answer:

    the shipped app.js builds the reply  →  the shipped bridge parsers read it back

For every parent shape somebody actually replies to. It is a contract test, and the contract is the
only thing that was broken.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
RUNTIME = ROOT / "tests/client/reply_shape_runtime.cjs"

from app.services.fedi_nostr_writeback_service import (   # noqa: E402
    _is_reply, _reply_parent_id, _WRITEBACK_KINDS)


@pytest.fixture(scope="module")
def replies():
    """What the SHIPPED client signs, for each parent shape."""
    if NODE is None:
        pytest.skip("needs node to run the shipped client code")
    done = subprocess.run([NODE, str(RUNTIME)], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]
    line = [l for l in done.stdout.splitlines() if l.startswith("REPLIES ")][-1]
    got = json.loads(line[len("REPLIES "):])
    assert got, "the reply-shape runtime produced nothing"
    return got


def _event(shape):
    return {"kind": shape["kind"], "id": "f" * 64, "pubkey": "9" * 64,
            "content": "a reply", "tags": shape["tags"]}


def test_every_shape_is_covered(replies):
    """The check before the checks: a fixture that silently stops producing shapes makes every rule
    below pass about nothing."""
    assert set(replies) >= {"plain_note", "nip10_reply", "nip22_comment", "article"}, replies.keys()


@pytest.mark.parametrize("shape", ["plain_note", "nip10_reply", "nip22_comment", "article"])
def test_the_bridge_subscribes_to_what_the_client_signs(replies, shape):
    """A kind the bridge does not ask the relay for is a reply it can never see — no error, no log,
    nothing. This is how kind 1111 was missed."""
    kind = replies[shape]["kind"]
    assert kind in _WRITEBACK_KINDS, (
        f"replying to a {shape} produces kind {kind}, which the write-back never subscribes to — "
        f"those replies reach the fediverse never. Subscribed: {_WRITEBACK_KINDS}")


@pytest.mark.parametrize("shape", ["plain_note", "nip10_reply", "nip22_comment", "article"])
def test_the_bridge_sees_it_as_a_reply(replies, shape):
    """If this says False the note is treated as top-level and CROSS-POSTED as a standalone public
    status — a Nostr conversation leaked out of context, which is the worse direction."""
    ev = _event(replies[shape])
    assert _is_reply(ev) is True, (
        f"a reply to a {shape} (kind {ev['kind']}) does not read as a reply to the bridge: "
        f"{ev['tags']}")


@pytest.mark.parametrize("shape", ["plain_note", "nip10_reply", "nip22_comment"])
def test_the_bridge_resolves_the_parent_the_client_aimed_at(replies, shape):
    """And the IMMEDIATE parent, never the thread root — resolving to the root federates a reply
    aimed at somebody else onto the thread's opening post."""
    r = replies[shape]
    got = _reply_parent_id(_event(r))
    assert got == r["parentId"], (
        f"the client replied to {r['parentId'][:12]} and the bridge resolved "
        f"{(got or 'nothing')[:12]}: {r['tags']}")


def test_an_article_reply_resolves_to_the_article(replies):
    """MEASURED, not assumed. The generic reply path treats a long-form article like any other
    parent: a NIP-10 kind-1 reply whose root-marked `e` tag is the article's EVENT id — not a NIP-22
    comment addressed to `30023:<pk>:<d>`.

    (The article VIEW has its own `articleCommentTags`, which does build the NIP-22 address form. So
    the two paths disagree about what a comment on an article is. That is worth knowing and is not
    this test's business to change — what matters here is that whatever the client signs, the bridge
    can resolve, and it can.)"""
    r = replies["article"]
    got = _reply_parent_id(_event(r))
    assert got == r["parentId"], (
        f"the client replied to article {r['parentId'][:12]} and the bridge resolved "
        f"{(got or 'nothing')[:12]}: {r['tags']}")


def test_the_two_article_comment_paths_are_still_different(replies):
    """A canary, not a rule. If the generic path ever starts producing the NIP-22 address form, the
    note in the test above is stale and the bridge needs to learn `A`/`a` addressing — which it does
    not do today, so nothing would federate from it."""
    tags = {t[0] for t in replies["article"]["tags"]}
    assert not (tags & {"A", "a"}), (
        "the generic reply path now addresses articles by NIP-22 address. The write-back resolves "
        "parents by EVENT ID only, so those comments will federate nothing until it can read an "
        "address: %r" % (replies["article"]["tags"],))
