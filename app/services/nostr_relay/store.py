"""The built-in Nostr relay's store: its RULES and the store-neutral base class.

The relay keeps its events in PosterChanDB (`pcdb_store.PcdbRelayStore`, POSTERCHANDB_MODE=primary -- the only
store since #161; the Postgres store, its mirror and its promotion were deleted once every node had moved). What
lives here is what any store has to obey and what PcdbRelayStore inherits:

  * the rule constants -- which kinds age out (`_PRUNABLE_KINDS`), which are retired, which never expire, the
    git kinds, the orphaned-zap and git-comment shields. PosterChanDB evaluates the very same definitions, so a
    rule is written once;
  * `RelayStore`: the async layer (every call runs on the store's single writer thread or its reader pool, so
    the relay's event loop never blocks), the admission hook, the preserve and pay-to-stay bookkeeping, the
    repo-announcement cache and the prune pass loop. Every method that touches events is the subclass's.
"""

import time
import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

# Replaceable event kind ranges (NIP-01): keep only the newest per (pubkey, kind) — and for
# the parameterized range, per (pubkey, kind, d-tag).
_REPLACEABLE = lambda k: k in (0, 3) or 10000 <= k < 20000
_PARAM_REPLACEABLE = lambda k: 30000 <= k < 40000

_HEXSET = frozenset("0123456789abcdef")
def _is_hex64(s) -> bool:
    return isinstance(s, str) and len(s) == 64 and set(s) <= _HEXSET

# Puppet-addressed DM gift-wraps/seals are transient (consumed by write-back, then federated). Prune
# them on a short TTL so spamming derivable puppet npubs can't fill the disk (kind-1059 is otherwise
# never pruned). Kept longer than the write-back listener's DM replay window so a restart still sees them.
_BRIDGE_DM_TTL_DAYS = 4


# The age-based auto-cleaner deletes ONLY high-volume, reconstructable FEED content — never
# important events. Allowlist (not denylist) so a kind is pruned only if it is explicitly one
# of these: notes (1), reposts (6), reactions (7), NIP-22 comments (1111). EVERYTHING else is
# kept indefinitely — profiles (0), contacts (3), ALL replaceable identity/relay lists
# (10000-19999: relay list 10002, DM relays 10050, search relays 10007, blossom servers 10063,
# mute/bookmark/etc.), private DMs (legacy 4, NIP-59 seal 13, NIP-17 gift wrap 1059), and any
# other/unknown kind. (Client-published `origin='direct'` events are additionally never pruned
# regardless of kind — so the instance's OWN chat/articles/streams survive; only synced-in copies
# from the WoT firehose age out.) Prunable feed content: notes/reposts/reactions/comments, plus
# long-form articles (30023) and live-stream events (30311) — all of which accumulate from the sync.
#
# NIP-28 public chat (42) used to be in here and is NOT any more: it is RETIRED (see
# _RETIRED_KINDS below), so its rows are deleted outright by the retired rule rather than aged out
# by this one. Leaving it in both would mean the age rule deleted the copies older than the
# retention window and the retired rule the rest — the same rows twice over in the preview's
# upper-bound `total`, and a reader of this tuple would think 42 was still content the relay keeps.
_PRUNABLE_KINDS = (1, 6, 7, 1111, 30023, 30311)

# ---------------------------------------------------------------------------------------------
# RETIRED FEATURES — the kinds of screens this app removed. ONE list, because the same set has to
# be refused on every ingest path AND deleted by the pruner, and four scattered literals is how
# the effect-command lists drifted. Every entry names its feature and when it went.
#
#   40, 41, 42, 43, 44   NIP-28 public chat (channel create / metadata / message / hide / mute).
#                        The legacy chat client is gone; PosterChan's public-room protocol is
#                        CONCORD. Refused from the firehose since 2026-08 and on the WS write path
#                        since 2026-09-04; swept from here 2026-09-04.
#   30402, 30403         NIP-99 classified listing + DRAFT listing — the Shopping screen, removed
#                        2026-09-04 (c01677c9b). Both: the editor's "save draft" published 30403.
#   34550                NIP-72 moderated community DEFINITION — the Communities screen, removed
#                        2026-09-04 (c01677c9b).
#   34236                Divine's short video — Discover → Shorts, removed 2026-09-04 (c01677c9b).
#                        This is the kind that screen PUBLISHED (measured off wss://relay.divine.video).
#
# DELIBERATELY NOT RETIRED, each verified rather than assumed:
#   21, 22, 34235        NIP-71 video (normal / short / horizontal). The Shorts screen READ these
#                        alongside 34236 and published none of them; they are what every other
#                        client's video posts arrive as (measured on this relay: 629 rows of kind 22
#                        from 189 authors, mostly ancestor/firehose). Refusing them would be a
#                        decision about ordinary video, not about a screen we removed. The owner
#                        confirmed: "i just want to reject the divine like short-formed videos".
#   4550                 NIP-72 community POST APPROVAL. It appears NOWHERE in this repo — the
#                        removed Communities screen never wrote or read one — so it is not part of
#                        "the feature we removed"; the rows on this relay (283, 279 of them direct)
#                        were published by other people's clients. Left alone.
#   1111                 NIP-22 comments were how the removed screen posted INTO a community, but
#                        the kind is ordinary comment traffic everywhere else. Kind-scoped rules
#                        only; a community's posts stay.
#
# NOTHING HERE MAY OVERLAP the git kinds, the never-expire kinds (30078 is this app's own
# datastore) or CONCORD, which is the chat product in use. The asserts below are that guarantee.
_RETIRED_KINDS = (40, 41, 42, 43, 44, 30402, 30403, 34236, 34550)

# Concord's event kinds (static/js/client/concord.js), listed here for ONE reason: so the assert
# below fails loudly if a future retirement ever names one of them. "Legacy chat" means NIP-28.
_CONCORD_KINDS = (9, 1000, 1002, 1018, 1036, 1040, 1059, 1061, 1063, 1068, 1074, 1075)

# What a refused author is told. NIP-01 lets an OK:false carry a reason, and a client whose events
# vanish silently has no way to learn the feature is gone — so every path that can answer, answers.
_RETIRED_REASONS = {
    "chat": "blocked: NIP-28 public chat is retired on this relay; use Concord",
    "shop": "blocked: classified listings (NIP-99) are retired on this relay",
    "community": "blocked: NIP-72 communities are retired on this relay",
    "short": "blocked: Divine short videos (kind 34236) are retired on this relay",
}


def retired_kind_reason(kind: int):
    """The NIP-01 OK-message for a retired kind, or None if the kind is still accepted. The single
    place that decides `is this kind retired?` — the WS write path, the firehose and the store's
    insert backstop all ask this, so they cannot drift into disagreeing about one kind."""
    try:
        kind = int(kind)
    except (TypeError, ValueError):
        return None
    if kind not in _RETIRED_KINDS:
        return None
    if 40 <= kind <= 44:
        return _RETIRED_REASONS["chat"]
    if kind in (30402, 30403):
        return _RETIRED_REASONS["shop"]
    if kind == 34550:
        return _RETIRED_REASONS["community"]
    return _RETIRED_REASONS["short"]


# Events deleted per prune PASS. The writer thread is single, so this is the unit of ingestion stall:
# one pass' deletions run to completion before any queued write gets in.
# 20k keeps a pass to roughly a second while still clearing a large backlog in a handful of passes.
_PRUNE_CHUNK = 20000

# NIP-34 git-over-nostr events — a repo's source of truth (announcement 30617, repo state 30618,
# patches 1617, PULL REQUESTS 1618 and PR updates 1619, issues 1621, replies 1622, issue-status 1623,
# and status 1630-1633). These are
# the collaboration record; losing one loses code/history that isn't reconstructable from the WoT
# firehose. They are DELIBERATELY absent from _PRUNABLE_KINDS (so age/bridge/count-cap prunes never
# touch them — kept forever), and are also exempted from the NIP-40 expiration sweep below so a stray
# `expiration` tag can't quietly delete a repo. NEVER add any of these to _PRUNABLE_KINDS.
# 1618/1619 were absent here while _GIT_COMMENT_ROOT_KINDS below already shielded the COMMENTS on a
# PR — the children of an event the parent set forgot. The consequence was that a PR, once accepted,
# stayed deletable by a stray NIP-40 `expiration` tag (_NEVER_EXPIRE_KINDS is built from this tuple),
# so the one event carrying a contribution's commit ids could quietly disappear while its discussion
# was kept forever.
_GIT_KINDS = (30617, 30618, 1617, 1618, 1619, 1621, 1622, 1623, 1630, 1631, 1632, 1633)
assert not (set(_GIT_KINDS) & set(_PRUNABLE_KINDS)), "git kinds must never be prunable"

# A NIP-22 comment (kind 1111) whose ROOT is a NIP-34 issue (1621) / patch (1617) / PR (1618) is the
# collaboration record's CURRENT reply shape — NIP-34 dropped kind 1622, so gitworkshop et al. thread
# issue discussion as 1111 comments. In spirit these belong to _GIT_KINDS ("kept forever"), but the
# kind is shared with ordinary community/article comments that MUST keep aging out (the relay's bound
# on firehose growth), so the shield is TAG-scoped, not kind-scoped: NIP-22 repeats the root kind in
# an uppercase `K` tag at every depth, which PosterChanDB's `t:K:<kind>` posting list answers cheaply.
# Every rule that says "prunable feed content" uses this exemption (pcdb_store._prunable), so it cannot
# drift per rule.
_GIT_COMMENT_ROOT_KINDS = ("1617", "1621", "1618")

# ORPHANED ZAP RECEIPTS. A zap receipt (9735) is never an age-prunable kind -- a post's zap total is read
# from them -- but once the age prune has deleted the POST, its receipts count toward nothing anybody can
# see here. Measured on poster.place 2026-10-04: 154,594 such receipts older than retention, 505 MB plus
# 486k tag rows -- "the db is growing fast for only a few months". Only receipts that NAME a post (`e`) whose
# post is gone: a profile zap (no `e`) is kept, and a receipt for a post still stored is kept at any age.
# Applied by pcdb_store._orphan_zap_seqs (only to non-direct receipts older than the retention window).

# Kinds a NIP-40 `expiration` tag must NEVER be able to delete. The expiration sweep is otherwise
# unconditional (it ignores the kind allowlist AND the preserve clause, by design — an author's
# explicit intent), which makes a single stray tag a silent data-loss vector for anything that is
# somebody's only copy. Git events are a repo's source of truth; kind 30078 is this app's own
# datastore — settings, users, chats, Notes — and it is the one that would bite by ACCIDENT rather
# than by attack: NIP-37 (Draft Events) recommends stamping `expiration: now + 90 days`, so a note
# written or touched by any other client following that convention would quietly disappear 90 days
# later, from the relay that holds the only copy. Kept regardless, at ingest and in the sweep.
# GRASP-08 private-repository DISCOVERY list (NIP-51, kind 10318). Its `g` tags are ALL private
# items — each a JSON array NIP-44-encrypted to the list's author — naming the relays a user's
# private repositories live on. It is REPLACEABLE, so there is exactly one per person and the newest
# wins: lose it and the owner has no index of where their private repos ARE. That is the same class
# of loss as 30078 (this app's own datastore) and it is why 30078 is in this tuple, with the same
# accidental trigger — NIP-37 recommends stamping `expiration: now + 90 days`, so any client
# following that convention on a list would take somebody's private repos off the map three months
# later, from the relay holding the only copy. It is already outside _PRUNABLE_KINDS (every prune
# rule is gated on _PRUNABLE_KINDS or _RETIRED_KINDS, so no cleaner can reach it); this
# closes the one path that could.
# Kinds that must NEVER take the `tie_direct` shortcut below (see the addressable-replaceable
# branch in _add_event_sync). That shortcut hands a same-second tie to the LAST direct write instead
# of to the lowest event id NIP-01 specifies, which is right for a person saving one document twice
# in a second and wrong for anything two parties must agree on independently.
#
# A repo announcement (30617) and a repo state (30618) are exactly that: `git_auth.decide_push_ref`
# authorises a push by SHA-equality against the newest maintainer-signed 30618, and reads the
# maintainer set off the 30617 — so both are an AUTHORISATION input, and the relay resolving a tie
# differently from the client means `pre-receive` deciding against a state the pusher considers
# superseded. ngit v3 honours the lower-event-ID tie-break and grinds nonces to avoid same-second
# collisions, so the practical window is small; the cost when it opens is a push refused (or
# allowed) for a reason neither side can see. Conformance wins here, and the rapid-save argument
# does not apply: a tie needs two pushes to the SAME repo in the SAME second.
_STRICT_TIE_KINDS = (30617, 30618)

_GRASP_PRIVATE_LIST_KIND = 10318
_NEVER_EXPIRE_KINDS = _GIT_KINDS + (30078, _GRASP_PRIVATE_LIST_KIND)
assert not (set(_NEVER_EXPIRE_KINDS) & set(_PRUNABLE_KINDS)), "never-expire kinds must never be prunable"

# The retired rule is the ONLY rule in this file that deletes by kind alone — no age, no origin, no
# preserve clause — so what may NOT be on that list is the whole safety argument, and it is asserted
# rather than remembered. 30078 (settings, Notes, calendars, contacts, the vault, the desktop) and
# the git kinds are somebody's only copy; Concord is the chat product this app actually ships.
assert not (set(_RETIRED_KINDS) & set(_NEVER_EXPIRE_KINDS)), \
    "a retired kind must never be a never-expire kind — 30078 is this app's own datastore"
assert not (set(_RETIRED_KINDS) & set(_GIT_KINDS)), "git events are a repo's source of truth"
assert not (set(_RETIRED_KINDS) & set(_CONCORD_KINDS)), \
    "Concord is the chat product in use — 'legacy chat' means NIP-28 (40-44), never Concord"
assert not (set(_RETIRED_KINDS) & set(_PRUNABLE_KINDS)), \
    "a retired kind is deleted outright, so it must not ALSO be aged out as feed content"


class RelayStore:
    def __init__(self, *, read_workers: int = 4, max_events: int = 0, retention_days: int = 30):
        # There is exactly ONE age window (retention_days) and it covers every origin, the fediverse
        # mirror included; the old per-bridge `bridge_retention_days` second cleaner is gone.
        self.max_events = max_events
        self.retention_days = retention_days
        # Authors nothing auto-deletes: local users, this server's NIP-05 holders, and the puppets of
        # local users' linked fediverse accounts (see thread._collect_preserve_pubkeys).
        self.preserve_pubkeys: frozenset = frozenset()
        # --- pay-to-stay (OPTIONAL, and OFF unless an admin turns it on) --------------------
        # Everything a client published HERE (origin='direct') is data entrusted to this relay and
        # the rules above never touch it, at any age. `free_retention_days` is the one exception,
        # and it exists only when the admin enables the paid-retention feature: a direct write by an
        # author who has NO account here (not in preserve_pubkeys) and NO paid subscription ages out
        # after that window; a subscriber's keeps `paid_retention_days` (0 = forever). Both default
        # to 0, so on every node that never turns this on the prune behaves exactly as it always did.
        self.free_retention_days = 0
        self.paid_retention_days = 0
        self.subscriber_pubkeys: frozenset = frozenset()
        self.tiered_ok = False        # ledger READ succeeded → the tiered rules may run (fail-closed)
        self.paid_tier_enabled = False        # the admin master switch, mirrored here
        self._last_good_subscribers: frozenset = frozenset()   # see PcdbRelayStore._exempt_subscribers
        self._write_exec = ThreadPoolExecutor(max_workers=1, thread_name_prefix="relay-db-w")
        self._read_exec = ThreadPoolExecutor(max_workers=read_workers, thread_name_prefix="relay-db-r")
        self._loop: asyncio.AbstractEventLoop | None = None
        self._repo_ann_cache: dict = {}   # (owner_hex, repo_id) -> (bool, expiry_ts): 30617 present?

    async def is_repo_announced(self, owner_hex: str, repo_id: str) -> bool:
        """True iff a PUBLIC NIP-34 repo announcement (kind 30617) for owner/repo exists on THIS relay.
        Private repos are NEVER announced (grasp_selfhost --private skips 30617), so a present 30617 ==
        public — this is what scopes issue/patch (1617/1621/…) acceptance to KNOWN repos WITHOUT leaking
        private ones, and without requiring this node to HOST the repo (so a proxy node whose relay the
        client reads still accepts collaboration for repos hosted on a peer). Cached (60s) so the collab
        gate never hits the DB per event."""
        owner = (owner_hex or "").strip().lower()
        rid = (repo_id or "").strip().lower()
        if rid.endswith(".git"):
            rid = rid[:-4]
        if len(owner) != 64 or not rid:
            return False
        key = (owner, rid)
        now = time.time()
        hit = self._repo_ann_cache.get(key)
        if hit and hit[1] > now:
            return hit[0]
        try:
            rows = await self.query([{"kinds": [30617], "authors": [owner], "#d": [rid], "limit": 1}],
                                    hard_cap=1)
            ok = bool(rows)
        except Exception:
            ok = False
        self._repo_ann_cache[key] = (ok, now + 60.0)
        return ok

    async def _w(self, fn, *a):
        return await self._loop.run_in_executor(self._write_exec, fn, *a)

    async def _r(self, fn, *a):
        return await self._loop.run_in_executor(self._read_exec, fn, *a)

    async def add_events_bulk(self, events: list, origin: str = "wot") -> int:
        events = [ev for ev in (events or []) if self._admitted(ev)]
        if not events:
            return 0
        return await self._w(self._add_events_bulk_sync, events, origin)

    async def filter_existing(self, ids: list) -> set:
        """Return the subset of `ids` already stored — ONE query instead of per-event has_event."""
        return await self._r(self._filter_existing_sync, ids)

    # THE ONE DOOR. Six paths store events (the WebSocket, the firehose, the member sync, the
    # backfill, the thread-ancestor fetch, the metadata lookup), each with its OWN copy of the
    # admission rules, and they had drifted: the ancestor fetch and two syncs never checked the
    # bridge rules at all, which is how 53k bridged events got in. The relay installs its policy here
    # (thread.py `_admit`), so a door added next year inherits it instead of re-learning it. A hook
    # that RAISES admits — this is a filter, and a bug in it must not stop the relay storing anything.
    admit = None
    # Set ON THE WRITER THREAD when the store closes (its final flush): from then on no event is stored or
    # deleted. Every event write is queued on that thread, so one queued BEHIND the close is refused rather than
    # written into a store whose last flush already happened.
    _writes_closed = False

    def _admitted(self, ev: dict) -> bool:
        if self.admit is None:
            return True
        try:
            return bool(self.admit(ev))
        except Exception as e:
            logger.debug("[nostr-relay] admit hook failed (admitting): %s", e)
            return True

    async def add_event(self, ev: dict, origin: str = "wot") -> bool:
        """Insert an event (already verified + WoT-gated by the caller). Returns stored?"""
        if not self._admitted(ev):
            return False
        return await self._w(self._add_event_sync, ev, origin)

    async def add_event_new(self, ev: dict, origin: str = "wot") -> tuple:
        """(stored, new). This base cannot tell a duplicate from a new row, so it says new whenever stored --
        the old behaviour; PcdbRelayStore answers exactly. Fan out on `new`, never on `stored`."""
        ok = await self.add_event(ev, origin)
        return ok, ok

    async def delete_pubkeys(self, pubkeys: list, spare_preserved: bool = True) -> int:
        """Purge all events authored by these pubkeys. `spare_preserved=False` only for an admin's
        explicit decision (the blocklist, delete-author); see _delete_pubkeys_sync."""
        return await self._w(self._delete_pubkeys_sync, list(pubkeys), spare_preserved)

    async def has_event(self, eid: str) -> bool:
        return await self._r(self._has_sync, eid)

    async def query(self, filters: list, hard_cap: int = 5000) -> list:
        return await self._r(self._query_sync, filters, hard_cap)

    async def neg_items(self, filters: list, cap: int = 500000) -> list:
        return await self._r(self._neg_items_sync, filters, cap)

    async def kv_get(self, key: str) -> str | None:
        return await self._r(self._kv_get_sync, key)

    async def kv_set(self, key: str, value: str) -> None:
        await self._w(self._kv_set_sync, key, value)

    async def bridge_nip05_set(self, name: str, pubkey: str) -> None:
        await self._w(self._bridge_nip05_set_sync, name, pubkey)

    async def bridge_nip05_all(self) -> dict:
        return await self._r(self._bridge_nip05_all_sync)

    async def bridge_puppet_add(self, pubkey: str) -> None:
        await self._w(self._bridge_puppet_add_sync, pubkey)

    async def bridge_puppets_all(self) -> set:
        return await self._r(self._bridge_puppets_all_sync)

    async def wot_replace(self, members: list, extra: list | None = None) -> int:
        return await self._w(self._wot_replace_sync, members, extra)

    async def wot_add(self, pubkeys: list) -> int:
        """Incrementally add members (new signups followed by the operator) WITHOUT a full rebuild —
        so they can post + receive DMs immediately, not after the daily upstream-driven rebuild."""
        return await self._w(self._wot_add_sync, list(pubkeys))

    async def wot_members(self) -> set:
        return await self._r(self._wot_members_sync)

    async def wot_missing_metadata(self) -> list:
        return await self._r(self._wot_missing_metadata_sync)

    async def wot_missing_metadata_groups(self) -> dict:
        """{"profile": [...], "relays": [...]}: members with no profile, and members with one but no relay list."""
        return await self._r(self._wot_missing_metadata_groups_sync)

    def _wot_missing_metadata_groups_sync(self) -> dict:
        return {"profile": self._wot_missing_metadata_sync(), "relays": []}

    def set_preserve_pubkeys(self, pubkeys) -> None:
        """Authors whose notes are NEVER pruned (local users / operators)."""
        self.preserve_pubkeys = frozenset(p for p in (pubkeys or []) if p)

    def extend_preserve_pubkeys(self, pubkeys) -> None:
        """UNION more authors into the preserve set — never removes any. Used by the prune/purge
        refresh so a partial/failed operator re-collection can't SHRINK the set and expose a user to
        deletion (preserve is deliberately grow-only; the publish gate is what tracks removals)."""
        add = frozenset(p for p in (pubkeys or []) if p)
        if add - self.preserve_pubkeys:
            self.preserve_pubkeys = self.preserve_pubkeys | add

    def set_subscribers(self, pubkeys, *, ledger_ok: bool) -> None:
        """Install the pay-to-stay subscriber set (authors with a live paid subscription).

        Unlike preserve, this set is REPLACED, not grown — a lapsed subscription has to be able to
        leave it. That makes an unreadable ledger indistinguishable from "nobody subscribed", which
        would delete exactly the data people paid to keep, so the read result is passed in
        explicitly: `ledger_ok=False` (relay unreachable, no ledger document, decrypt failed) turns
        the tiered rules OFF for the pass rather than running them against an empty set.

        A successful read is also remembered (`_last_good_subscribers`) for the SYNCED-content
        exemption below, which needs an answer even when this pass couldn't get a fresh one."""
        self.subscriber_pubkeys = frozenset(p for p in (pubkeys or []) if _is_hex64(p))
        self.tiered_ok = bool(ledger_ok)
        if ledger_ok:
            self._last_good_subscribers = self.subscriber_pubkeys

    def set_paid_tier_enabled(self, on: bool) -> None:
        """Master switch, mirrored onto the store. Turning it OFF drops the remembered subscriber set
        too — otherwise a stale in-memory copy would go on exempting people from the ordinary age
        prune on a relay whose operator has switched the whole feature off."""
        self.paid_tier_enabled = bool(on)
        if not self.paid_tier_enabled:
            self._last_good_subscribers = frozenset()

    async def prune_preview(self) -> dict:
        return await self._r(self._prune_preview_sync)

    async def prune(self, chunk: int = _PRUNE_CHUNK) -> int:
        """Run the prune to completion in bounded passes, yielding the writer thread between them so
        queued ingest/publish writes interleave instead of waiting out the whole backlog. `chunk=0`
        restores the single-transaction behaviour. Each pass commits, so an interrupted prune (relay
        restart mid-run) keeps the work it already did and resumes on the next call."""
        total = 0
        while True:
            removed, more = await self._w(self._prune_sync, chunk)
            total += removed
            # `not removed` is the belt-and-braces exit: a rule that reports "capped" but deletes
            # nothing (e.g. rows vanishing under a concurrent NIP-09 delete) must not spin forever.
            if not more or not removed:
                break
            await asyncio.sleep(0.05)   # hand the writer thread back between passes
        return total

    async def count(self) -> int:
        return await self._r(self._count_sync)

    async def count_filtered(self, filters: list, protect_nip78: bool = False) -> int:
        return await self._r(self._count_filtered_sync, filters, protect_nip78)
