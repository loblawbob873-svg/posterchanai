"""NIP-28 public chat: channel edits and joined-channel notifications.

Two classes of silent failure are locked down here.

`ingest_kinds` decides what the relay pulls from the upstream cluster. Kinds 40 (channel) and 42
(message) were in it but 41 (channel METADATA edit) was not — so a rename or a new channel picture
stopped dead on whichever node it was published from, and every other node kept serving the original
metadata forever with nothing in any log to say so. Kind 10005 (NIP-51 "public chats", the joined-channel
list) has the same shape of problem: each node's push watcher reads it to decide whose devices to notify
about a channel message, so a list published on one node has to reach the others.

`_root_channel` decides which channel a kind-42 belongs to. A REPLY inside a channel carries a second
`e` tag (the message being replied to), so taking the first `e` blindly misfiles the message into
whatever channel that tag happened to name — which for a push means notifying the wrong room's members.
"""
import re
from pathlib import Path

from app.services.nostr_push_service import _KINDS, _root_channel

THREAD = Path(__file__).resolve().parents[1] / "app" / "services" / "nostr_relay" / "thread.py"


def _default_ingest_kinds() -> set[int]:
    """The default string baked into _read_config (an admin override is a separate, deliberate act)."""
    src = THREAD.read_text()
    m = re.search(r'g\("nostr_relay_ingest_kinds",\s*"([0-9,]+)"\)', src)
    assert m, "could not find the nostr_relay_ingest_kinds default in thread.py"
    return {int(k) for k in m.group(1).split(",") if k.strip()}


def test_ingest_kinds_exclude_retired_nip28_chat():
    kinds = _default_ingest_kinds()
    assert not kinds.intersection(range(40, 45))


def test_relay_rejects_nip28_but_keeps_concord_wraps():
    server = (THREAD.parent / "server.py").read_text()
    assert 'if 40 <= kind <= 44:' in server
    assert 'NIP-28 chat is not supported; use Concord' in server
    assert 'elif kind in (1059, 21059):' in server


def test_ingest_kinds_carry_the_joined_channel_list():
    assert 10005 in _default_ingest_kinds(), \
        "kind 10005 (NIP-51 public chats) missing — chat push would only work on the node the list was published to"


def test_push_watches_chat_mentions():
    assert 42 in _KINDS, "a chat message that p-tags you must reach the OS-notification poll"


def test_root_channel_prefers_the_root_marker():
    """A reply: root `e` second, replied-to `e` first. Position must not decide."""
    ev = {"tags": [["e", "MSG", "wss://r", "reply"], ["e", "CHAN", "wss://r", "root"], ["p", "PK"]]}
    assert _root_channel(ev) == "CHAN"


def test_root_channel_falls_back_to_the_first_e_tag():
    """Plenty of clients omit markers on a top-level channel message."""
    assert _root_channel({"tags": [["e", "CHAN"]]}) == "CHAN"
    assert _root_channel({"tags": [["p", "PK"]]}) == ""
    assert _root_channel({"tags": []}) == ""


def test_root_channel_ignores_malformed_tags():
    ev = {"tags": [["e"], ["e", ""], ["e", "CHAN"]]}
    assert _root_channel(ev) == "CHAN"
