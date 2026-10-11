"""A CONCORD READ WHOSE RESULT A WRITE IS BUILT ON MUST ACTUALLY ASK ITS RELAYS (`urgent:true`).

Relay.queryFrom skips, with no error, a relay that is busy with another read of the same slot or resting
after a failure, and answers [] for it. Three bugs on 2026-10-11 were that one fact: room history
("no channels readable yet — 8 relays asked"), community creation ("Invite List sync is incomplete"),
and creating a channel right after opening a room -- the control read beside the room's own opening load
came back empty, so the new channel was built on an empty control set (refused, or written on nothing).
Deploy 205's gate caught the last one as a one-off forum-test failure under load.

Rule: every cordQuery in concord.js whose result is followed, within the same handler, by a write
(createChannelWrap / relayPublishRoom / api.create / writeList / remember) passes urgent:true.
"""
import re
from pathlib import Path

SRC = (Path(__file__).resolve().parents[2] / "static/js/client/concord.js").read_text(encoding="utf-8")
WRITES = re.compile(r"createChannelWrap|relayPublishRoom|api\.create\(|writeList\(|api\.remember\(")


def _calls():
    for m in re.finditer(r"await cordQuery\(", SRC):
        depth, i = 0, m.end() - 1
        while i < len(SRC):
            c = SRC[i]
            depth += c == "("
            depth -= c == ")"
            if depth == 0:
                break
            i += 1
        yield m.start(), SRC[m.start():i + 1]


def test_reads_that_feed_a_write_ask_every_relay():
    bad = []
    for at, call in _calls():
        after = SRC[at + len(call):at + len(call) + 900]
        if WRITES.search(after) and "urgent:true" not in call.replace(" ", ""):
            line = SRC.count("\n", 0, at) + 1
            bad.append((line, call[:160]))
    assert not bad, ("a Concord read that a write is built on may skip busy/resting relays -- pass urgent:true", bad)


def test_the_rule_still_sees_the_calls_it_is_about():
    assert sum(1 for _ in _calls()) >= 3
