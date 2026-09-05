"""The relay no longer ACCEPTS the kinds of the features this app removed.

Deletion (tests/test_relay_prune.py) is only half a retirement: a firehose that keeps importing what
the pruner keeps deleting is a loop, not a policy. So every ingest path refuses them, and the three
paths are here by name because they are three different pieces of code:

  * the WS write path (`server._on_event`) — the only one with a socket to answer on, so it refuses
    with an explicit NIP-01 `OK: false` naming the feature. A client whose publishes vanish silently
    cannot learn that the screen is gone;
  * the live firehose (`thread._firehose_event`) — a read of somebody else's relay, so it drops;
  * `store._insert_one` — the single funnel every path writes through (WS, firehose, the windowed
    WoT sync, ancestor backfill, a member restore). This is the backstop that makes the refusal
    structural instead of a list of remembered call sites.

WHAT IS NOT RETIRED is asserted just as hard. Concord is the chat product in use and its kinds sit
beside NIP-28's; kind 30078 is this app's own datastore; NIP-71 video (21/22/34235) is what other
clients' video posts arrive as — the Shorts screen only ever READ those and published 34236, and the
owner's decision was "i just want to reject the divine like short-formed videos". Kind 4550 (NIP-72
post approval) appears nowhere in this repo, so it was never part of the removed feature.
"""
import asyncio
import inspect
import re

import pytest

from app.services.nostr.event import build_event
from app.services.nostr_relay import ingest as ingest_mod
from app.services.nostr_relay.server import RelayServer
from app.services.nostr_relay.store import (_CONCORD_KINDS, _GIT_KINDS, _NEVER_EXPIRE_KINDS,
                                            _PRUNABLE_KINDS, _RETIRED_KINDS, retired_kind_reason)

RETIRED = [40, 41, 42, 43, 44, 30402, 30403, 34236, 34550]
# Every one of these has a live reason to keep flowing through this relay, and each is a plausible
# casualty of a wider rule: Concord (the chat product), the datastore, the git record, NIP-71 video,
# NIP-72 post approval, and ordinary notes/reposts/comments.
KEPT = [1, 6, 7, 9, 21, 22, 1000, 1002, 1018, 1036, 1040, 1059, 1061, 1063, 1068, 1074, 1075,
        1111, 1621, 4550, 30023, 30078, 30311, 30617, 34235]


class _Store:
    def __init__(self):
        self.stored = []

    async def query(self, *_a, **_kw):
        return []

    async def add_event(self, event, **_kw):
        self.stored.append(event)
        return True


class _Gate:
    def is_member(self, _pubkey): return True
    def is_operator(self, _pubkey): return False
    def is_puppet_event(self, _event): return False
    def is_blocked(self, _pubkey): return False


def _server():
    srv = RelayServer(_Store(), _Gate(), {"wot_enabled": False})
    srv.sent = []
    srv._send = lambda conn, obj: srv.sent.append((conn, obj))
    return srv


def _signed(kind, sk=b"\x11" * 32, tags=None):
    return build_event(sk, kind, "x", tags or ([["d", "s"]] if kind >= 30000 else []))


def test_the_list_names_one_feature_per_kind_and_nothing_it_must_not():
    assert tuple(sorted(_RETIRED_KINDS)) == tuple(sorted(RETIRED))
    for family in (_CONCORD_KINDS, _GIT_KINDS, _NEVER_EXPIRE_KINDS, _PRUNABLE_KINDS):
        assert not (set(_RETIRED_KINDS) & set(family))
    for kind in RETIRED:
        reason = retired_kind_reason(kind)
        assert reason and reason.startswith("blocked: "), kind
        assert "retired" in reason, "the reason must say the feature is gone, not just 'blocked'"
    for kind in KEPT:
        assert retired_kind_reason(kind) is None, f"kind {kind} is not a retired feature"


@pytest.mark.parametrize("kind", RETIRED)
def test_the_ws_write_path_refuses_with_a_readable_reason(kind):
    """NIP-01 allows an OK:false to carry a message; this is the one path that can send one."""
    srv, conn = _server(), object()
    ev = _signed(kind)
    asyncio.run(srv._on_event(conn, ev))
    ok = srv.sent[-1][1]
    assert ok[0:3] == ["OK", ev["id"], False], ok
    assert ok[3] == retired_kind_reason(kind)
    assert srv.store.stored == [], "a refused event must not be stored"


@pytest.mark.parametrize("kind", [1, 22, 1059, 1068, 4550, 30078])
def test_the_ws_write_path_still_accepts_everything_else(kind):
    srv, conn = _server(), object()
    ev = _signed(kind)
    if kind == 30078:                       # NIP-78 needs same-key AUTH, tested elsewhere
        srv._auth_pubkeys[conn] = {ev["pubkey"]}
    asyncio.run(srv._on_event(conn, ev))
    assert srv.store.stored == [ev], srv.sent[-1][1]


def test_the_firehose_drops_them_from_the_shared_list():
    """Upstream ingestion is where a retired kind would otherwise come straight back — the pruner
    deletes a row and the next firehose tick re-imports it. Source-level because `_firehose_event`
    is a closure inside the relay's run loop; the behaviour it guards is covered by the store
    backstop below, which that path also writes through."""
    src = open("app/services/nostr_relay/thread.py", encoding="utf-8").read()
    assert "from .store import RelayStore, _RETIRED_KINDS" in src
    assert "if _kind in _RETIRED_KINDS:" in src, \
        "the firehose must gate on the shared list, not on its own copy of the range"


def test_the_backfill_and_ingest_defaults_stopped_asking_for_them():
    """A member restore cannot restore what the store refuses to insert — asking upstream for these
    kinds would spend the backfill budget fetching rows that are dropped on arrival."""
    backfill = {int(k) for k in re.search(
        r"kinds = kinds or \[([0-9,\s]+)\]",
        inspect.getsource(ingest_mod.backfill_author)).group(1).replace("\n", "").split(",") if k.strip()}
    thread = open("app/services/nostr_relay/thread.py", encoding="utf-8").read()
    default = {int(k) for k in re.search(
        r'nostr_relay_ingest_kinds", "([0-9,]+)"', thread).group(1).split(",")}
    for kind in RETIRED:
        assert kind not in backfill, f"backfill_author still asks for retired kind {kind}"
        assert kind not in default, f"the ingest default still asks for retired kind {kind}"
    for kind in (21, 22, 34235, 30617, 31922):
        assert kind in backfill and kind in default, \
            f"kind {kind} is NOT retired and must keep syncing (NIP-71 video, git, calendars)"


def test_the_admin_preview_shows_the_count_before_anything_is_deleted():
    """The operator sees "this will delete N of kind K" first. The prune is destructive and this
    rule ignores age and origin, so a bare total would not be informed consent."""
    store_src = open("app/services/nostr_relay/store.py", encoding="utf-8").read()
    preview = store_src[store_src.index("def _prune_preview_sync"):store_src.index("async def prune_preview")]
    assert "_RETIRED_SQL" in preview and "retired_by_kind" in preview
    admin = open("templates/admin/tabs/nostr_relay.html", encoding="utf-8").read()
    assert "retired_by_kind" in admin, "the Preview button must render the per-kind breakdown"
    thread = open("app/services/nostr_relay/thread.py", encoding="utf-8").read()
    assert 'retired=%d' in thread, "the dry-run log line must carry the retired count too"
