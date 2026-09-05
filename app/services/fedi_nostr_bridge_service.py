"""Mirror the fediverse GLOBAL timeline into the Nostr global timeline (public plane).

A background poller (started from app.main on port 3051, like the other bridges) reads ONE shared,
admin-configured fediverse account's global/federated public timeline and republishes each post to
the built-in Nostr relay as a kind-1 note signed by the author's deterministic "puppet" key (see
fedi_bridge_identity + nostr.bridge_keys). Every fedi author thus appears on the Nostr side as a
first-class npub with a NIP-05 on this instance; replies keep their thread (NIP-10 e/p tags) and
quote-posts keep their reference. The read account never posts on anyone's behalf — interaction and
personal DMs/notifications go through each user's own linked account (see nostr_bridge router /
fedi_nostr_personal_service).

Moderation is enforced AT INGEST: an admin fediverse-domain blocklist plus the read account's own
block/mute lists — a matching author is never mirrored. State (FediPuppet / FediBridgeDelivered) is
in the DB; the cursor is the local-only `fedi_bridge_global_since`. Per-process, like the other
pollers — correct on the single port-3051 instance.
"""
import asyncio
import logging
import re
import contextvars
import time
from datetime import datetime, timedelta
from urllib.parse import urlparse

import httpx
from sqlalchemy.orm import Session
from sqlalchemy.exc import OperationalError, InterfaceError   # CONNECTION-level (transient) DB errors — retry, don't skip

from app.models import FediBridgeDelivered, FediBridgeSkipped, FediPuppet, FediReconcileState
from app.services import pleroma_service, settings_store
from app.services import fedi_bridge_identity as ident
from app.services.nostr import bech32
from app.services.fedi_normalize import (   # proven normalizers, extracted from the old timeline bridge
    _norm, _canonical_uri, emoji_tags_for)

logger = logging.getLogger(__name__)

_POLL_TIMEOUT = 90
_DRAIN_BUDGET = 70
_PAGE = 20
_MAX_PAGES = 8
_MODERATION_TTL = 600          # seconds to cache the read account's block/mute lists
# `at` starts at -_MODERATION_TTL, NOT 0.0: time.monotonic() is seconds since BOOT on Linux, so a 0.0
# seed made the freshness test "has the host been up 10 minutes?" — after a reboot the block/mute set
# stayed EMPTY for the first ~6 polls and blocked/muted authors were mirrored to the public firehose.
# Worse, each one got a FediBridgeDelivered row, so it never re-evaluated. Seeding negative forces a
# fetch on the very first poll.
_mod_cache: dict = {"at": -_MODERATION_TTL, "blocked_accts": set()}
_DELETION_INTERVAL = 300       # how often the (separate) deletion job runs — deletions are rare
_DELETION_BATCH = 25           # recent mirrored notes re-checked for deletion per cycle
_DELETION_CONCURRENCY = 6      # parallel status checks (was a 25-deep serial loop inside the poll)
_MAX_ANCESTORS = 8             # DEFAULT ancestors backfilled (root-first) to anchor a reply's thread.
                               # Delivered oldest→newest so each ancestor's parent is mirrored just before it →
                               # NIP-10 e-tag linked. Kept conservative because the personal-notification plane
                               # reuses this default and has NO per-poll drain budget.
_BRIDGE_MAX_ANCESTORS = 15     # WIDER ancestor window for the timeline-bridge drain — lets deep UNLISTED
                               # sub-threads (which enter Nostr ONLY as backfilled ancestors) thread to their
                               # true root. Safe to be wide because the drain passes a `_deadline` down to
                               # _backfill_ancestors, which RAISES (→ whole reply retries next cycle, no orphan)
                               # once past it — time is bounded by the deadline, NOT by a small cap. The
                               # personal plane passes no deadline (0) → unbounded there, as before.
_BACKFILL_PAGES = 3            # pages of recent history to mirror on first connect (≈60 posts)
_MAX_QUOTE_DEPTH = 2           # cap quote-of-quote recursion when mirroring referenced notes


# --- settings ---------------------------------------------------------------

def _get(key: str, default: str = "") -> str:
    v = settings_store.get(key, default)
    return v if v not in (None, "") else default


def _broadcast_on() -> bool:
    return str(_get("fedi_bridge_broadcast", "false")).lower() in ("1", "true", "yes", "on")


def _relay_hint() -> str:
    """Public relay URL for the NIP-18 q-tag relay hint, so other clients can locate the quoted note
    and render the embed. Prefer an explicit, WELL-FORMED client_relay_url (ws/wss only — a
    misconfigured value must not become a bad advisory hint); else derive wss://<nip05-domain>/relay
    (ident.nip05_domain does the shared lowercase normalization). Empty when neither is usable, which
    degrades to the old always-empty slot (clients resolve the quote from their own relays)."""
    url = _get("client_relay_url").strip()
    if url.startswith(("ws://", "wss://")):
        return url
    domain = ident.nip05_domain()
    return f"wss://{domain}/relay" if domain else ""


def _port() -> int:
    try:
        return int(_get("nostr_relay_port", "3052") or "3052")
    except ValueError:
        return 3052


# --- moderation -------------------------------------------------------------

def _blocked_domains() -> set:
    raw = _get("fedi_bridge_blocked_domains", "")
    return {d.strip().lower().lstrip("@") for d in raw.replace(",", "\n").split() if d.strip()}


def _host_of(acct: str, instance_host: str) -> str:
    host = (acct or "").partition("@")[2].lower()
    return host or instance_host


def _domain_blocked(host: str, blocked: set) -> bool:
    """True if `host` equals a blocked domain or is a subdomain of one (a.b.c blocked by b.c)."""
    if not host:
        return False
    h = host.lower()
    return any(h == d or h.endswith("." + d) for d in blocked)


async def _refresh_moderation(instance_url: str, token: str) -> None:
    """Refresh (cached) the read account's blocked + muted handles, so they're never mirrored."""
    now = time.monotonic()
    if now - _mod_cache["at"] < _MODERATION_TTL:
        return
    accts: set = set()
    try:
        for a in (await pleroma_service.fetch_blocks(instance_url, token)
                  + await pleroma_service.fetch_mutes(instance_url, token)):
            h = (a.get("acct") or a.get("username") or "").strip().lower().lstrip("@")
            if h:
                accts.add(h)
    except Exception as e:
        logger.debug("[fedi-bridge] moderation list refresh failed: %s", e)
        return
    _mod_cache["at"] = now
    _mod_cache["blocked_accts"] = accts


def _author_muted(acct: str, host: str, instance_host: str) -> bool:
    # The read account's block/mute list stores handles as user@host (remote) or bare user (local).
    a = (acct or "").lower()
    blocked = _mod_cache["blocked_accts"]
    if a in blocked:
        return True
    # A bare username in the list refers to a LOCAL account only.
    return host == instance_host and a.partition("@")[0] in blocked


# --- delivery ---------------------------------------------------------------

# Fediverse audiences safe to mirror as a PUBLIC Nostr note. ALLOWLIST (not a blocklist of
# direct/private) so ANY other value — Pleroma `list`/`local`, or a
# missing/unknown one — is never leaked to the public firehose. `unlisted` (Mastodon/Pleroma) and
# `home` is "not listed but link-public", mirrored by design.
_PUBLIC_AUDIENCE = ("public", "unlisted", "home")


def _is_public_audience(raw: dict) -> bool:
    """True iff a raw fediverse status/note is public-audience and may be mirrored as a public kind-1.
    Reads the RAW platform object (the normalizers drop `visibility`). Shared by the timeline mirror and
    the personal-notification plane so their guards can never drift. A MISSING/blank visibility is
    treated as NON-public — every real fediverse API sets `visibility` on the statuses we mirror
    (public-timeline/context/notification), so an absent one is abnormal and must not be leaked."""
    return str(raw.get("visibility") or "").lower() in _PUBLIC_AUDIENCE


def _seen(db: Session, instance_url: str, note_id: str, uri: str | None) -> bool:
    q = db.query(FediBridgeDelivered).filter(FediBridgeDelivered.instance_url == instance_url,
                                             FediBridgeDelivered.note_id == note_id)
    if q.first():
        return True
    if uri:
        if db.query(FediBridgeDelivered).filter(FediBridgeDelivered.note_uri == uri).first():
            return True
    return False


def _parent_event(db: Session, instance_url: str, parent_note_id: str):
    return db.query(FediBridgeDelivered).filter(
        FediBridgeDelivered.instance_url == instance_url,
        FediBridgeDelivered.note_id == parent_note_id).first()


def _delivered_by_uri(db: Session, uri: str):
    return db.query(FediBridgeDelivered).filter(FediBridgeDelivered.note_uri == uri).first() if uri else None


def _existing_mirror(db: Session, instance_url: str, uri: str | None, note_id: str | None):
    """The already-mirrored row for a note, by canonical URI (cross-instance) then same-instance id."""
    return (_delivered_by_uri(db, uri) if uri else None) or \
           (_parent_event(db, instance_url, note_id) if note_id else None)


# Notes currently mid-delivery this cycle — breaks a same-cycle quote CYCLE (A quotes B, B quotes A)
# from re-entering _deliver and double-publishing before the FediBridgeDelivered row commits.
_inflight: set = set()


class _PublishFailed(Exception):
    """Raised by _deliver when the relay PUBLISH failed — as opposed to an intentional skip (which
    returns None). Lets the drain distinguish 'this note never landed, retry it next poll' from
    'correctly skipped, advance past it'. Without it a single local-relay restart silently drops every
    in-flight note from the mirror (cursor advances past notes that were never delivered)."""


# The reply-addressing block at the very start of a note: a run of mention tokens — either a resolved
# `nostr:npub…` ref (fixed 58-char bech32 body, anchored so it can't swallow a glued word) OR an
# unresolved literal `@handle` (puppet provisioning failed). Only a run of 3+ is treated as an
# addressing WALL to strip — a reply that opens with one or two @names is likely writing to them as
# actual content, so those are kept (the p-tags still notify everyone regardless).
_MENTION_TOKEN = r'(?:nostr:npub1[0-9a-z]{58}|@[A-Za-z0-9_](?:[A-Za-z0-9_.\-]*[A-Za-z0-9_])?(?:@[A-Za-z0-9.\-]+)?)'
_LEADING_MENTIONS_RE = re.compile(r'^(?:' + _MENTION_TOKEN + r'\s+){3,}', re.I)


def _build_content(post: dict, quote_bech: str | None = None, q_raw: dict | None = None) -> str:
    """The kind-1 note body: the post text plus any media URLs (Nostr clients render image/video
    URLs inline). When the post quotes another that we mirrored, append a `nostr:<note>` reference so
    clients embed the quoted note's CONTENT (NIP-18); otherwise fall back to an inline text snippet."""
    parts = []
    text = (post.get("text") or "").strip()
    if text:
        parts.append(text)
    for m in (post.get("media") or []):
        if m.get("url"):
            parts.append(m["url"])
    q = post.get("quote")
    if q:
        if quote_bech:
            parts.append(f"\nnostr:{quote_bech}")
        else:
            # Audience-check the QUOTED post before inlining any of it. This branch is reached exactly
            # when _deliver REFUSED to mirror the quote as its own event — including when it refused
            # because the quote is followers-only/direct — and it was then pasting that post's author
            # handle and first 280 chars into a PUBLIC kind-1 anyway. Without the raw object we can't
            # prove it's public, so fail closed and name nothing.
            if q_raw is not None and _is_public_audience(q_raw):
                qacct = q.get("acct") or "?"
                qtext = (q.get("text") or "").strip()
                snippet = (qtext[:280] + "…") if len(qtext) > 280 else qtext
                parts.append(f"\n↪ quoting @{qacct}: {snippet}".rstrip())
            else:
                parts.append("\n↪ quoting a post that isn’t public")
    return "\n".join(parts).strip() or "​"   # never publish empty content


def _emoji_tags(content: str, *emoji_maps: dict) -> list:
    """NIP-30 custom-emoji tags for the :shortcodes: in `content` present in any of the supplied maps.
    Merges the maps, then defers to the shared builder so profile + note emoji matching can't drift."""
    merged: dict = {}
    for m in emoji_maps:
        if m:
            merged.update(m)
    return emoji_tags_for(content, merged, limit=30)


async def _backfill_ancestors(db: Session, port: int, platform: str, instance_url: str,
                              instance_host: str, post: dict, token: str = "",
                              max_ancestors: int = _MAX_ANCESTORS, deadline: float = 0.0) -> None:
    """Mirror a reply's ancestor chain (toward the conversation root) so the reply threads under a
    real parent instead of appearing as an orphan with missing context. Ancestors come root-first,
    so each is delivered before its child and never triggers a further backfill (recursion guard).
    `token` lets the personal plane backfill from the USER's instance (else the bridge read account).
    `deadline` (monotonic, 0 = unbounded): past it we RAISE _PublishFailed so the WHOLE reply aborts and
    retries next cycle (already-delivered ancestors are _seen-skipped then, so it converges incrementally)
    — this bounds a single deep reply's cost by TIME without orphaning it or wedging the poll on timeout."""
    token = token or _get("fedi_bridge_access_token")
    if deadline and time.monotonic() > deadline:
        raise _PublishFailed("drain deadline (before ancestor fetch)")   # retry the whole reply next cycle
    try:
        ctx = await pleroma_service.fetch_context(instance_url, token, post["id"])
        ancestors = ctx.get("ancestors") or []
    except Exception as e:
        logger.debug("[fedi-bridge] ancestor fetch failed for %s: %s", post.get("id"), e)
        return
    blocked = _blocked_domains()
    for raw in ancestors[-max_ancestors:]:            # closest N (always includes the immediate parent)
        if deadline and time.monotonic() > deadline:
            raise _PublishFailed("drain deadline (mid-ancestor)")   # abort → reply retries next cycle (no orphan)
        anc = _norm(platform, raw)
        if not anc.get("id"):
            continue
        uri = _canonical_uri(platform, instance_url, anc)
        if _seen(db, instance_url, anc["id"], uri):
            continue
        acct = anc.get("author", {}).get("acct") or ""
        host = _host_of(acct, instance_host)
        if _domain_blocked(host, blocked) or _author_muted(acct, host, instance_host):
            continue
        try:
            await _deliver(db, port, platform, instance_url, instance_host, raw, anc,
                           backfill=False, token=token, _deadline=deadline)
        except _PublishFailed:
            raise   # relay publish failed mid-backfill — abort so the WHOLE reply retries (no orphaned thread)
        except Exception as e:
            logger.debug("[fedi-bridge] ancestor deliver failed: %s", e)


_linked_actors: dict = {}          # fedi actor url AND "username@host" -> real Nostr pubkey hex
_linked_actors_ts: float = 0.0
_linked_actors_partial: bool = False
_linked_user_lkg: dict = {}        # user_id -> (real_pubkey_hex, [keys]) LAST-KNOWN-GOOD resolution.
                                   # Retained across rebuilds so a transient verify_credentials failure
                                   # can't drop a linked user from the map. Without this, a user whose
                                   # instance blips vanishes for a whole TTL — and a user ON the bridge's
                                   # OWN read instance is uniquely hurt: their mention statuses are always
                                   # mirrored by the drain FIRST (so _seen), so the personal plane never
                                   # re-delivers the p-tag — the drain's tag is their ONLY notification
                                   # path. Mirrors the _self_acct_cache resilience pattern.
_LINKED_TTL = 900                  # rebuild the linked-user map every 15 min…
_LINKED_RETRY_TTL = 120            # …but retry sooner when a user's instance failed to resolve
_LINKED_VC_TIMEOUT = 8             # per-user verify timeout so one hung instance can't stall the poll


async def _linked_actor_pubkeys(db: Session) -> dict:
    """Map each LINKED bridge user's fediverse identity -> their REAL Nostr pubkey, so a mirrored note
    @mentioning them p-tags their REAL key (→ a Nostr NOTIFICATION for them); a bare puppet p-tag isn't
    (their client watches their own key). Indexed by BOTH the actor url and "username@host" so a
    cross-instance rendering of the mention still matches. Covers Pleroma links. All users are
    resolved CONCURRENTLY with a bounded per-call timeout so one slow instance can't stall the mirror.
    Cached by timestamp (an empty result is cached too — no rebuild storm during an outage); a shorter
    retry TTL applies when some user failed this cycle."""
    global _linked_actors, _linked_actors_ts, _linked_actors_partial
    now = time.time()
    ttl = _LINKED_RETRY_TTL if _linked_actors_partial else _LINKED_TTL
    if _linked_actors_ts and now - _linked_actors_ts < ttl:
        return _linked_actors
    from app.models import User
    from app.services.nostr.nostr_service import to_pubkey_hex
    try:
        users = db.query(User).filter(User.nostr_npub.isnot(None)).all()
    except Exception:
        return _linked_actors                       # DB hiccup → keep the last good map

    async def _resolve(u):
        pk = to_pubkey_hex(u.nostr_npub or "")
        if not pk:
            return ("skip", u.id, None, None)
        try:
            if u.pleroma_instance_url and u.pleroma_access_token:
                me = await asyncio.wait_for(
                    pleroma_service.verify_credentials(u.pleroma_instance_url, u.pleroma_access_token),
                    _LINKED_VC_TIMEOUT)
                inst, url = u.pleroma_instance_url, ((me or {}).get("url") or "").strip()
                un = ((me or {}).get("username") or "").strip()
            else:
                return ("skip", u.id, None, None)    # no linked fedi account — not a failure
        except Exception:
            return ("fail", u.id, pk, None)          # instance down/slow → retry sooner
        keys = []
        if url:
            keys.append(url.rstrip("/").lower())
        if un:
            keys.append(f"{un.lower()}@{urlparse(inst).netloc.lower()}")
        return ("ok", u.id, pk, keys)

    now_ids, fresh, partial = set(), {}, False
    for r in await asyncio.gather(*[_resolve(u) for u in users], return_exceptions=True):
        if isinstance(r, Exception):
            partial = True
            continue
        status, uid, pk, keys = r
        now_ids.add(uid)
        if status == "ok" and keys:
            _linked_user_lkg[uid] = (pk, keys)       # refresh last-known-good
            for k in keys:
                fresh[k] = pk                        # resolved THIS cycle → wins any key collision
        elif status == "fail":
            partial = True                           # transient blip → keep this user's prior lkg entry
        else:
            _linked_user_lkg.pop(uid, None)          # 'skip' or 'ok' with no usable keys → forget them
    # Prune users who dropped out of the query entirely (account deleted / nostr_npub cleared) — else a
    # stale handle->pubkey mapping would linger for the process lifetime and misroute (phantom) mentions.
    # Only prune when the rebuild was CLEAN. On `partial` a resolve raised, so that user's uid never
    # entered now_ids and the prune below treated them as "gone" — discarding exactly the last-known-good
    # entry this map exists to protect. They then became permanently un-mentionable: a mirrored note
    # @-mentioning them p-tags only their puppet, never their real key, and for a user on the bridge's
    # own read instance that p-tag is their ONLY notification path.
    if not partial:
        for uid in [i for i in _linked_user_lkg if i not in now_ids]:
            _linked_user_lkg.pop(uid, None)
    # Rebuild the lookup from last-known-good (so a user who merely failed to resolve this cycle stays
    # mentionable instead of vanishing for a whole TTL), then let THIS cycle's fresh resolutions win any
    # key collision — e.g. a fedi handle reassigned between users routes to the CURRENT owner.
    out = {}
    for pk, keys in _linked_user_lkg.values():
        for k in keys:
            out.setdefault(k, pk)
    out.update(fresh)
    _linked_actors, _linked_actors_ts, _linked_actors_partial = out, now, partial
    return out


async def _rewrite_mentions(db: Session, port: int, instance_host: str, content: str,
                            mentions: list, blocked: set = frozenset()) -> tuple:
    """Make fediverse @handle mentions CLICKABLE on Nostr: for each mentioned account, provision its
    puppet, replace the `@handle` text with a `nostr:<npub>` reference (which clients render as a
    profile link), and p-tag it. A mention of a LINKED bridge user is ALSO p-tagged with their REAL
    Nostr pubkey, so the mirrored note surfaces as a notification for them. Returns (content, [p-tags])."""
    ptags = []
    linked = await _linked_actor_pubkeys(db)   # fedi actor url -> real Nostr pubkey (cached)
    # How many mentions share each bare username. A bare "@bob" is only AMBIGUOUS when two mentioned
    # accounts are both called "bob"; with a single "bob" the post's own mention list settles who it
    # is, whatever instance they're on.
    _uname_n: dict = {}
    for _m in (mentions or []):
        _u = (_m.get("username") or "").strip().lower()
        if _u:
            _uname_n[_u] = _uname_n.get(_u, 0) + 1
    # Longest acct first so '@user@host' is replaced before a bare '@user' substring.
    for m in sorted(mentions or [], key=lambda x: len(x.get("acct", "")), reverse=True):
        url = (m.get("url") or "").strip()
        acct = (m.get("acct") or m.get("username") or "").strip()
        username = (m.get("username") or "").strip()
        if not url or not acct:
            continue
        # Same blocklist/mute gate as every other ensure_puppet caller (ancestors, quotes, feed items,
        # personal plane). Without it a blocked or muted account still got a puppet, a published kind-0,
        # a NIP-05 registration and a p-tag on a public note purely by being MENTIONED — the blocklist
        # filtered ITEMS, not identities, so the blocked party's identity reached the relay anyway.
        _mhost = acct.rsplit("@", 1)[-1].lower() if "@" in acct else (instance_host or "").lower()
        if _domain_blocked(_mhost, blocked) or _author_muted(acct, _mhost, instance_host):
            continue
        try:
            p = await ident.ensure_puppet(
                db, port, {"url": url, "acct": acct, "username": username, "display_name": username},
                instance_host, profile_refresh=False)   # synthetic mention account → don't touch the kind-0
        except Exception:
            p = None
        if not p:
            continue
        ptags.append(["p", p["pubkey_hex"]])
        # A linked local user → ALSO p-tag their REAL key so the note notifies them. Match on the actor
        # url, else "username@host" (robust when a federating instance renders the mention url oddly).
        real = linked.get(url.rstrip("/").lower())
        if not real:
            a = acct.lstrip("@").lower()
            cand = a if "@" in a else (f"{username.lower()}@{urlparse(url).netloc.lower()}" if username else None)
            if cand:
                real = linked.get(cand)
        if real and ["p", real] not in ptags:
            ptags.append(["p", real])
        ref = "nostr:" + p["npub"]
        # Replace @acct (fully qualified) plainly, but the BARE @username only where it isn't followed
        # by more handle characters. A global str.replace of "@bob" rewrote the LOCAL @bob to the REMOTE
        # bob@other.host's npub when a note mentioned both, and turned an unrelated "@anna_x" into
        # "nostr:npub1…a_x" — a malformed reference no client can resolve.
        # Boundary-guard the qualified form too: a plain str.replace of "@ann" also matched INSIDE
        # "@anna_x" (and "@bob@other.host" inside "@bob@other.host.evil"), producing a malformed
        # nostr: reference no client can resolve.
        if acct:
            content = re.sub(r"@" + re.escape(acct) + r"(?![A-Za-z0-9_.\-@])", ref, content)
        # The BARE "@username" form. Restricting it to LOCAL mentions (as this did) silently stopped
        # linkifying the COMMON case: a post federated in from another instance keeps its author's
        # rendering, so a mention of Appelmoesje@poa.st appears in the text as plain "@Appelmoesje"
        # — never the qualified form line 447 looks for. Those mentions kept their p-tag but stayed
        # unclickable text.
        # The real hazard is only ambiguity: two mentioned accounts sharing a username, where a bare
        # "@bob" could bind to the wrong one. So claim the bare form when this username is unique in
        # the post's mention list (the list is authoritative about who "@bob" is), and fall back to
        # the local-only rule when it isn't. The negative lookahead still stops "@ann" eating "@anna_x".
        _mhost2 = acct.rsplit("@", 1)[-1].lower() if "@" in acct else ""
        _unique = _uname_n.get(username.lower(), 0) <= 1
        if username and (_unique or not _mhost2 or _mhost2 == (instance_host or "").lower()):
            content = re.sub(r"@" + re.escape(username) + r"(?![A-Za-z0-9_.\-@])", ref, content)
    return content, ptags


def _raw_quote_status(platform: str, raw: dict) -> dict | None:
    """The full quoted status object embedded in `raw` (Pleroma `quote` that
    carries its own text), or None for a plain post/boost."""
    # Pleroma: a quote-post carries `quote`; a boost-with-comment carries `reblog` (+ own content,
    # which is why it wasn't filtered as a pure boost). Mirror _norm_pleroma's quote selection.
    sub = raw.get("quote") or raw.get("reblog")
    return sub if isinstance(sub, dict) else None


async def _resolve_quote(db: Session, port: int, platform: str, instance_url: str, instance_host: str,
                         raw: dict, token: str, depth: int, deadline: float = 0.0) -> tuple:
    """Ensure the note quoted by `raw` is mirrored on the relay, returning (event_id, puppet_pubkey)
    or (None, None). Reuses an existing mirror; otherwise mirrors it (moderation-checked,
    depth-bounded) so a Nostr client can embed the quoted post's content via the q tag / nostr ref."""
    q_raw = _raw_quote_status(platform, raw)
    if not q_raw or not q_raw.get("id"):
        return None, None
    qpost = _norm(platform, q_raw)
    quri = _canonical_uri(platform, instance_url, qpost)
    row = _existing_mirror(db, instance_url, quri, qpost.get("id"))
    if row:
        return row.nostr_event_id, row.nostr_pubkey
    if depth >= _MAX_QUOTE_DEPTH:
        return None, None
    acct = qpost.get("author", {}).get("acct") or ""
    host = _host_of(acct, instance_host)
    if _domain_blocked(host, _blocked_domains()) or _author_muted(acct, host, instance_host):
        return None, None
    try:
        eid = await _deliver(db, port, platform, instance_url, instance_host, q_raw, qpost,
                             backfill=False, token=token, _depth=depth + 1, _deadline=deadline)
    except _PublishFailed:
        return None, None   # quote is SECONDARY — skip the embed rather than abort the parent. (Re-raising
                            # here would make the personal plane's _ensure_status_event drop a notification's
                            # whole e-tag/thread on a transient quote failure; and the parent's OWN publish
                            # failing transiently already retries the note, re-resolving the quote next cycle.)
    if not eid:
        return None, None
    row = _existing_mirror(db, instance_url, quri, qpost.get("id"))
    return eid, (row.nostr_pubkey if row else None)


# --- skip ledger -------------------------------------------------------------
# Every early return used to drop a post with no row, no log and no counter, which made a genuine
# coverage gap indistinguishable from "the instance never showed it to us".
#
# PERFORMANCE: this sits on the firehose hot path, so it is deliberately split. POLICY skips
# (replies excluded, domain blocked, author muted) fire on a large share of every poll and are not
# gaps at all — they are configuration working — so they only bump an in-memory counter. A DB row is
# written ONLY for the rare cases where a post we WANTED was lost, which is what the reconciler and
# a human actually need. A bounded seen-set stops the same URI being re-inserted.
_SKIP_COUNTS: dict = {}
_SKIP_SEEN: set = set()
_SKIP_SEEN_MAX = 20000
# Reasons worth a DB row AT ALL. "not-public" is deliberately NOT here: it is an audience decision
# like domain-blocked/author-muted, not a lost post, and with fedi_bridge_type="home" a followers-only
# post from a followed account would hit it on every poll.
_SKIP_PERSIST = ("oversized", "relay-rejected", "no-puppet")
_SKIP_RETENTION_DAYS = 7   # diagnostic only — pruned by cleanup_state alongside the delivered map
# ...and even those are only WRITTEN while reconciling. The drain is the firehose hot path (hundreds
# of posts per poll): one misbehaving account — say a puppet the relay has blocked — would otherwise
# take an INSERT+COMMIT on every post it ever makes, forever, since each post has a distinct uri and
# nothing suppresses repeats per author. Counters still tally every skip there. The reconciler is
# bounded (_RECON_BATCH authors x _RECON_LOOKBACK posts per pass) and is the only place that NEEDS
# the row, because it is what stops it re-attempting the same doomed post every 15 minutes.
# A ContextVar, not a flag: the poll and reconcile jobs are separate tasks that interleave at awaits.
_recon_ctx: contextvars.ContextVar = contextvars.ContextVar("fedi_bridge_reconciling", default=False)


def skip_counts() -> dict:
    """Snapshot of the in-memory policy-skip tallies (admin/coverage reporting)."""
    return dict(_SKIP_COUNTS)
# Every early return below used to drop a post with no row, no log and no counter, which made a
# genuine coverage gap indistinguishable from "the instance never showed it to us". Recording the
# REASON is what lets the reconciler (and a human) tell "we chose not to" from "we never saw it".
def _record_skip(db: Session, reason: str, *, platform: str = "", instance_url: str = "",
                 post: dict | None = None, uri: str = "", detail: str = "") -> None:
    """Best-effort: a diagnostic row must never be able to fail a delivery or poison the txn."""
    _SKIP_COUNTS[reason] = _SKIP_COUNTS.get(reason, 0) + 1
    if reason not in _SKIP_PERSIST or not _recon_ctx.get():
        return                      # counted only — see _SKIP_PERSIST on why the drain never writes
    key = uri or f"{instance_url}|{(post or {}).get('id')}"
    if key in _SKIP_SEEN:
        return                      # already recorded this post; don't churn the table
    if len(_SKIP_SEEN) >= _SKIP_SEEN_MAX:
        _SKIP_SEEN.clear()
    _SKIP_SEEN.add(key)
    try:
        post = post or {}
        db.add(FediBridgeSkipped(
            platform=platform or None, instance_url=instance_url or None,
            note_id=str(post.get("id") or "") or None,
            note_uri=(uri or post.get("uri") or "")[:512] or None,
            author_acct=((post.get("author") or {}).get("acct") or "")[:255] or None,
            reason=reason[:40], detail=(detail or "")[:500] or None))
        db.commit()
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        logger.debug("[fedi-bridge] could not record skip (%s): %s", reason, e)


def _source_ts(post: dict) -> int | None:
    """Unix seconds for when a fediverse post was published, or None if it can't be read.

    Mastodon/Pleroma send ISO-8601 ("2026-07-29T18:52:01.000Z"). Returns None rather than guessing,
    so the caller falls back to build_event's default instead of pinning a wrong time.
    """
    v = (post or {}).get("created_at")
    if not v:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    try:
        from datetime import datetime, timezone
        t = str(v).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(t)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        ts = int(dt.timestamp())
        # Bound must stay INSIDE the relay's own limit. server.py rejects created_at > now+900 with
        # "invalid: created_at too far in the future", and `invalid` is on this module's PERMANENT
        # reject list — so a too-future stamp would not merely be corrected, it would make the post
        # be SKIPPED and the cursor advance past it. A little slack absorbs normal inter-instance
        # clock skew; anything beyond that falls back to now rather than risking the drop.
        now = int(time.time())
        return ts if 0 < ts <= now + 300 else None
    except Exception:
        return None


async def _deliver(db: Session, port: int, platform: str, instance_url: str, instance_host: str,
                   raw: dict, post: dict, backfill: bool = True, token: str = "",
                   extra_ptags: list | None = None, _depth: int = 0,
                   _max_anc: int = _MAX_ANCESTORS, _deadline: float = 0.0) -> str | None:
    # Only a PUBLIC-audience status may become a public Nostr note (see _is_public_audience).
    if not _is_public_audience(raw):
        _record_skip(db, "not-public", platform=platform, instance_url=instance_url, post=post,
                     detail=str(raw.get("visibility") or ""))
        return None
    account = raw.get("account") or {}
    from app.services.fedi_only_service import suppress_mirror
    if suppress_mirror(db, account, instance_host):
        return None
    p = await ident.ensure_puppet(db, port, account, instance_host)
    if not p:
        _record_skip(db, "no-puppet", platform=platform, instance_url=instance_url, post=post,
                     detail="account has no usable actor URI")
        return None
    uri = _canonical_uri(platform, instance_url, post)
    # Idempotency: already mirrored in a prior cycle → return its event id (never double-publish).
    existing = _existing_mirror(db, instance_url, uri, post.get("id"))
    if existing:
        return existing.nostr_event_id
    # Same-cycle quote-cycle guard: the row commits only at the end, so without this an A↔B mutual
    # quote re-enters _deliver for a note still mid-flight and publishes it twice.
    inflight_key = uri or f"{instance_url}|{post.get('id')}"
    if inflight_key in _inflight:
        return None
    _inflight.add(inflight_key)
    try:
        tags: list = []
        # Threading (NIP-10): if the parent isn't mirrored yet, backfill the ancestor chain first so
        # this reply threads under a real root (no missing-parent orphans); then tag the parent.
        parent_id = post.get("in_reply_to_id")
        if parent_id:
            if backfill and not _parent_event(db, instance_url, parent_id):
                await _backfill_ancestors(db, port, platform, instance_url, instance_host, post,
                                          token=token, max_ancestors=_max_anc, deadline=_deadline)
            parent = _parent_event(db, instance_url, parent_id)
            if parent and parent.nostr_event_id:
                # The delivered-map row can outlive the event it points at (pruned/deleted). Confirm the
                # parent event still exists before linking — else the reply carries a DANGLING `e` tag to a
                # parent that never loads (the broken-thread symptom). On a DB blip, assume present.
                try:
                    from sqlalchemy import text
                    _exists = db.execute(text("SELECT 1 FROM events WHERE id=:id LIMIT 1"),
                                         {"id": parent.nostr_event_id}).first() is not None
                except Exception:
                    try:
                        db.rollback()   # a failed SELECT aborts the PG txn — roll back so the later dedup-row commit isn't poisoned
                    except Exception:
                        pass
                    _exists = True
                if not _exists:
                    parent = None
            if parent:
                tags.append(["e", parent.nostr_event_id, "", "reply"])
                if parent.nostr_pubkey:
                    tags.append(["p", parent.nostr_pubkey])
        # Quote (NIP-18): mirror the quoted note (so it's on the relay) then reference it with a `q`
        # tag AND a nostr:<note> in the content, so Nostr clients embed the quoted post's CONTENT —
        # not a dangling link. Depth-bounded so a chain of quote-of-quote can't recurse without limit.
        quote_bech = None
        quoted_ev_id, quoted_pk = await _resolve_quote(db, port, platform, instance_url, instance_host,
                                                       raw, token, _depth, deadline=_deadline)
        if quoted_ev_id:
            # NIP-18 q tag: ['q', <id>, <relay-url>, <pubkey>] — include the relay hint so other
            # clients can locate the quoted note and render the embed (was emitted with an empty slot).
            tags.append(["q", quoted_ev_id, _relay_hint(), quoted_pk or ""])
            try:
                quote_bech = bech32.encode("note", bytes.fromhex(quoted_ev_id))
            except Exception:
                quote_bech = None
        # Extra p-tags (personal plane: notify the local user about a mention so it surfaces as a
        # notification AND threads in the conversation).
        for pk in (extra_ptags or []):
            if pk and not any(t[0] == "p" and t[1] == pk for t in tags if len(t) >= 2):
                tags.append(["p", pk])

        # Make fediverse @mentions clickable: rewrite them to nostr: refs + p-tag the mentioned puppets.
        # Pass the RAW quoted status so the snippet fallback can audience-check it (the normalizers
        # drop `visibility`, so `post["quote"]` alone can never prove the quote is public).
        content = _build_content(post, quote_bech, _raw_quote_status(platform, raw))
        content, mention_ptags = await _rewrite_mentions(db, port, instance_host, content,
                                                         raw.get("mentions") or [], _blocked_domains())
        # Fediverse replies prefix the body with the full @-recipient list (which fedi clients hide);
        # after the rewrite that's a wall of leading `nostr:npub…` refs burying the actual message. Drop
        # the leading run of mention refs on replies — the p-tags (added above) still thread/notify. Keep
        # the original if stripping would empty it (a mentions-only reply).
        if post.get("in_reply_to_id"):
            stripped = _LEADING_MENTIONS_RE.sub("", content).strip()
            if stripped:
                content = stripped
        for t in mention_ptags:
            if not any(x[0] == "p" and x[1] == t[1] for x in tags if len(x) >= 2):
                tags.append(t)
        # NIP-30 custom emoji: tag every :shortcode: still present in the content so clients render the
        # actual emoji image instead of the raw shortcode text.
        tags.extend(_emoji_tags(content, post.get("content_emojis")))
        # Oversized guard: an event over the relay's 512KB frame cap is dropped by the websockets layer with a
        # raw ConnectionClosed (no OK, no NOTICE) — which looks transient and would wedge the drain retrying
        # the giant post forever. `content` dominates the serialized size, so cap it well under 512KB here and
        # SKIP (permanent → cursor advances). A post this large is pathological and never worth mirroring.
        if len(content or "") > 300_000:
            logger.info("[fedi-bridge] skipping oversized post %s (%d chars) — over relay frame cap",
                        post.get("id"), len(content or ""))
            _record_skip(db, "oversized", platform=platform, instance_url=instance_url, post=post,
                         uri=uri, detail=f"{len(content or '')} chars")
            return None
        # PIN created_at to when the post was actually published on the fediverse, instead of letting
        # build_event stamp "now". Two reasons, both real:
        #   1. Timeline order. A drain after downtime (or the reconciler repairing old posts) mirrored
        #      a backlog all stamped NOW, so days-old fediverse posts arrived at the TOP of the Nostr
        #      timeline as if brand new. With the source time they slot in chronologically.
        #   2. Idempotency. build_event's own docstring says pinning the timestamp is what makes a
        #      re-published event produce an IDENTICAL id, so the relay dedups it instead of storing a
        #      second copy — which is what made a lost dedup row turn into a duplicate note.
        # Unparseable/absent → fall back to now (build_event's default), never drop the post.
        ev = ident.build_event(p, 1, content, tags=tags, object_uri=uri, broadcast=_broadcast_on(),
                               created_at=_source_ts(post))
        ok, msg = await ident.publish(port, ev)
        if not ok:
            logger.debug("[fedi-bridge] publish failed for %s: %s", post.get("id"), msg)
            # Distinguish a PERMANENT relay rejection (SKIP → cursor advances, return None) from a TRANSIENT
            # failure (retry → raise, don't advance). Without this split ONE permanently-rejected post wedges
            # the cursor forever and NOTHING new mirrors.
            #   Whitelist keyed to the SAME-REPO relay's exact OK-false vocabulary (nostr_relay/server.py):
            #   PERMANENT = "invalid: …" (bad id/sig, empty, expired, future) and "blocked: …" (author blocked,
            #     not-in-WoT, bridged-not-accepted). "duplicate" kept defensively (already-stored → skip).
            #   TRANSIENT (default, NOT whitelisted → raise): the relay's ONE retryable reject
            #     "error: not stored, retry", PLUS connection failures — ident.publish returns (False, str(e))
            #     / "unreachable" on a dead socket, arbitrary text that must NOT be treated as permanent.
            # ⚠ If the relay gains a NEW permanent rejection with a different prefix, add it here or the drain
            #   will wedge on it (default-transient errs toward retry so a relay RESTART never drops posts).
            if (msg or "").lower().startswith(("blocked", "invalid", "duplicate")):
                # A permanent rejection advances the cursor, so this post is gone for good unless
                # something records it. This row is what the reconciler and the admin count read.
                _record_skip(db, "relay-rejected", platform=platform, instance_url=instance_url,
                             post=post, uri=uri, detail=msg or "")
                return None
            raise _PublishFailed(msg or "publish failed")
        row_kw = dict(platform=platform, instance_url=instance_url, note_id=post["id"],
                      note_uri=uri, author_acct=p["acct"], nostr_event_id=ev["id"],
                      nostr_pubkey=p["pubkey_hex"])
        db.add(FediBridgeDelivered(**row_kw))
        try:
            db.commit()
        except Exception:
            db.rollback()
            # The relay ALREADY has this note. If the dedup row doesn't persist, the next poll re-mirrors
            # it as a DIFFERENT event (build_event stamps a fresh created_at) → a duplicate. The session
            # may be broken (idle-in-txn/conn death), so persist the row on a FRESH session.
            try:
                from app.database import SessionLocal
                s2 = SessionLocal()
                try:
                    s2.add(FediBridgeDelivered(**row_kw)); s2.commit()
                finally:
                    s2.close()
            except Exception as e2:
                logger.debug("[fedi-bridge] dedup-row retry failed for %s: %s", post.get("id"), e2)
        return ev["id"]
    finally:
        _inflight.discard(inflight_key)


async def _process(db: Session, port: int, platform: str, instance_url: str, instance_host: str,
                   blocked_domains: set, include_replies: bool, raw: dict, deadline: float = 0.0) -> None:
    # Skip pure boosts (a reblog with no own content): the original federates in on its own, so
    # mirroring the boost would just duplicate it. Quote-posts (own text) ARE mirrored.
    if raw.get("reblog") and not (raw.get("content") or "").strip():
        return
    post = _norm(platform, raw)
    if not post.get("id"):
        return
    if not include_replies and post.get("in_reply_to_id"):
        _record_skip(db, "replies-excluded", platform=platform, instance_url=instance_url, post=post)
        return
    acct = post.get("author", {}).get("acct") or ""
    host = _host_of(acct, instance_host)
    if _domain_blocked(host, blocked_domains):
        _record_skip(db, "domain-blocked", platform=platform, instance_url=instance_url, post=post, detail=host)
        return
    if _author_muted(acct, host, instance_host):
        _record_skip(db, "author-muted", platform=platform, instance_url=instance_url, post=post, detail=acct)
        return
    uri = _canonical_uri(platform, instance_url, post)
    if _seen(db, instance_url, post["id"], uri):
        return
    await _deliver(db, port, platform, instance_url, instance_host, raw, post,
                   _max_anc=_BRIDGE_MAX_ANCESTORS, _deadline=deadline)   # bridge drain: wide window, time-bounded


# --- deletions --------------------------------------------------------------

async def _check_deletions(db: Session, port: int, instance_url: str, token: str, broadcast: bool) -> None:
    """Re-check the most recent mirrored notes; if one was deleted on the fediverse (definitive
    404/410), publish a NIP-09 deletion from its puppet (which removes it on the relay and federates
    upstream iff broadcasting), then drop the bookkeeping row. Bounded + throttled — deletions are
    rare and per-status checks cost a request each."""
    # ROTATE through the table rather than re-checking the newest _DELETION_BATCH rows every cycle. A
    # busy global feed inserts hundreds of rows per 5-minute interval, so a fixed id.desc() window only
    # ever covered posts deleted within SECONDS of being mirrored; every later delete was missed and the
    # mirrored copy stayed on the relay indefinitely. Cursor is node-local and wraps at the end.
    # NOTE the _cursor suffix: settings_store._RUNTIME_SUFFIXES keeps it NODE-LOCAL. Without it this
    # sweep position would sync through the relay and nodes would clobber each other's progress.
    _dck = f"fedi_bridge_del_{(instance_url or '').rstrip('/').rsplit('/', 1)[-1].lower()}_cursor"
    try:
        _after = int(settings_store.get(_dck, "0") or 0)
    except (TypeError, ValueError):
        _after = 0
    def _sweep(after_id: int):
        return (db.query(FediBridgeDelivered)
                .filter(FediBridgeDelivered.instance_url == instance_url,
                        FediBridgeDelivered.note_id != "",   # skip write-back TOMBSTONES (no status to check)
                        FediBridgeDelivered.id > after_id)
                .order_by(FediBridgeDelivered.id.asc()).limit(_DELETION_BATCH).all())
    rows = _sweep(_after)
    if not rows and _after:
        rows = _sweep(0)                     # wrapped — restart the sweep from the oldest row
    if rows:
        settings_store.put(_dck, str(rows[-1].id))
    if not rows:
        return
    # The status checks are read-only + independent, so run them CONCURRENTLY (bounded) instead of 25
    # sequential HTTP round-trips — that serial loop was the tail that pushed the poll past its budget.
    sem = asyncio.Semaphore(_DELETION_CONCURRENCY)

    async def _is_deleted(row):
        async with sem:
            try:
                return row, await pleroma_service.status_deleted(instance_url, token, row.note_id)
            except Exception as e:
                logger.debug("[fedi-bridge] deletion check failed for %s: %s", row.note_id, e)
                return row, False

    for row, deleted in await asyncio.gather(*[_is_deleted(r) for r in rows]):
        if not deleted:
            continue
        try:
            actor_uri = None
            if row.nostr_pubkey:
                pup = db.query(FediPuppet).filter(FediPuppet.pubkey_hex == row.nostr_pubkey).first()
                actor_uri = pup.actor_uri if pup else None
            if not actor_uri:
                # No puppet ⇒ this row is not a MIRRORED note, it's a local user's own cross-post recorded by
                # the write-back. Its row is what stops that note being federated again (and being echoed back
                # into the timeline as a puppet note). Deleting the row because the status 404s — which is
                # exactly what happens right after the user deletes it — would resurrect the deleted post on
                # the next replay. Leave it alone; it is a permanent marker, not bookkeeping to reap.
                continue
            await ident.delete_note(port, actor_uri, row.nostr_event_id, broadcast)
            db.delete(row)
            db.commit()
            logger.info("[fedi-bridge] mirrored note %s deleted on source → NIP-09 delete published", row.note_id)
        except Exception as e:
            db.rollback()
            logger.debug("[fedi-bridge] deletion publish failed for %s: %s", row.note_id, e)


async def _backfill_recent(db: Session, port: int, platform: str, instance_url: str,
                           instance_host: str, blocked_domains: set, include_replies: bool,
                           ttype: str = None, cursor_key: str = "fedi_bridge_global_since",
                           deadline: float = 0.0) -> None:
    """On a fresh connect, mirror a bounded window of RECENT history (paging backward with max_id) so
    the Nostr global timeline isn't empty until new posts trickle in. Mirrors oldest-first (parents
    before replies) and then sets the forward cursor to the newest seen. `ttype`/`cursor_key` let the
    local-timeline drain reuse this with its own timeline + cursor."""
    token = _get("fedi_bridge_access_token")
    if ttype is None:
        ttype = _get("fedi_bridge_type", "global")
    collected: list = []
    max_id = None
    for _ in range(_BACKFILL_PAGES):
        batch = await pleroma_service.fetch_timeline(instance_url, token, ttype, limit=_PAGE, max_id=max_id)
        if not batch:
            break
        collected.extend(batch)
        max_id = batch[-1].get("id")            # oldest in this page (timeline is newest-first)
        if not max_id or len(batch) < _PAGE:
            break
    if not collected:
        return
    newest = max((r.get("id") for r in collected if r.get("id")), default=None)
    done = 0
    # ONE-TIME best-effort recent-history fill (only runs on a fresh connect / lost cursor). Deliver
    # oldest-first; on ANY per-post failure just roll back and SKIP it — dropping a couple of the ~60
    # backfilled posts is fine, the normal forward drain carries on from `newest`. Then advance the cursor
    # to `newest` == max(id): this matches the forward drain's id-based `min_id` resume EXACTLY (no
    # created_at-vs-id ordering gap on the federated firehose), and setting the cursor means the next poll
    # takes the normal forward path instead of re-running this backfill. A deadline break just stops early
    # WITHOUT advancing, so the whole window re-runs next poll (delivered posts are _seen-skipped).
    over_budget = False
    for raw in sorted(collected, key=lambda r: r.get("created_at") or ""):   # oldest-first
        if deadline and time.monotonic() > deadline:
            over_budget = True
            break   # never overrun _POLL_TIMEOUT — resume the fill next poll (cursor NOT advanced below)
        try:
            await _process(db, port, platform, instance_url, instance_host,
                           blocked_domains, include_replies, raw, deadline=deadline)
            done += 1
        except Exception as e:
            try:
                db.rollback()   # a failed publish OR a txn-aborting DB error — clear the session, skip the post
            except Exception:
                pass
            logger.debug("[fedi-bridge] backfill mirror failed (skipping): %s", e)
    # Only claim the window if something actually landed. _PublishFailed is a plain Exception, so the
    # handler above catches it too — a relay restart during first connect could fail EVERY post and
    # still commit the cursor, marking history complete with zero posts mirrored and no retry path.
    if newest and not over_budget and done:
        settings_store.put(cursor_key, newest)
    logger.info("[fedi-bridge] initial %s backfill mirrored %d recent post(s)%s",
                ttype, done, " (over budget — resuming next poll)" if over_budget else "")


# --- poll -------------------------------------------------------------------

async def _drain_timeline(db: Session, port: int, platform: str, instance_url: str, token: str,
                          instance_host: str, blocked_domains: set, include_replies: bool,
                          ttype: str, cursor_key: str, deadline: float) -> None:
    """Drain ONE fediverse timeline (`ttype`) FORWARD into Nostr using its own cursor (`cursor_key`),
    bounded by the shared monotonic `deadline` so both timelines share one poll budget. On a lost/absent
    cursor it resumes forward from the newest already-delivered note (recovering the downtime gap) or, on
    a truly fresh install, backfills a bounded recent window. ONE code path for the global AND local
    drains, so a pagination/cursor fix can't drift between them. Deduped across timelines by
    _process → _seen (a post in both is mirrored exactly once)."""
    async def _fetch(cursor, first):
        return await pleroma_service.fetch_timeline(instance_url, token, ttype, limit=_PAGE,
                                                    min_id=(None if first else cursor))

    since = _get(cursor_key)
    if not since:
        # No cursor. If we already have mirrored history for this instance, the cursor was LOST (restart
        # with a wiped local_settings.json) — resume forward from the newest delivered note so the gap
        # during downtime is recovered, not skipped. Otherwise it's a fresh connect: backfill a bounded
        # window of recent posts so the timeline isn't empty on day one.
        # Exclude write-back TOMBSTONE rows (note_id="") — they can be the newest row for an instance but
        # carry no status id, so resuming from them would skip forward-resume and drop downtime posts.
        last = (db.query(FediBridgeDelivered)
                .filter(FediBridgeDelivered.instance_url == instance_url,
                        FediBridgeDelivered.note_id != "")
                .order_by(FediBridgeDelivered.id.desc()).first())
        if last and last.note_id:
            since = last.note_id
            settings_store.put(cursor_key, since)
            logger.info("[fedi-bridge] %s cursor lost — resuming forward from newest delivered note %s", ttype, since)
        else:
            await _backfill_recent(db, port, platform, instance_url, instance_host,
                                   blocked_domains, include_replies, ttype, cursor_key, deadline=deadline)
            return

    cursor = since
    for _page in range(_MAX_PAGES):
        if time.monotonic() > deadline:
            break
        raw_posts = await _fetch(cursor, False)
        if not raw_posts:
            break
        # Sort by ID, not created_at. The cursor IS an id (min_id/sinceId), so the watermark `last` is only
        # valid if iteration follows id order. created_at is the ORIGIN's publish time while the id is
        # assigned at LOCAL ingest, so a late-federating post sorts early while holding a high id — and
        # either break below then committed a cursor past posts that were never mirrored. Those are never
        # re-fetched (min_id excludes them) and nothing logs a gap. Ids are time-ordered on both platforms
        # (Pleroma FlakeId), so this also keeps parents ahead of their replies.
        raw_posts = sorted(raw_posts, key=lambda r: str(r.get("id") or ""))
        last = None
        transient = False
        for raw in raw_posts:
            if time.monotonic() > deadline:
                break   # out of the poll budget mid-page: `last` holds the cursor (committed just below), the
                        # rest of the page + its ancestor backfills resume next poll. This per-POST check (the
                        # loop above only checks per-PAGE) keeps a wide-window ancestor backfill from running
                        # the poll past _POLL_TIMEOUT and getting cancelled before the cursor advances.
            try:
                await _process(db, port, platform, instance_url, instance_host,
                               blocked_domains, include_replies, raw, deadline=deadline)
                last = raw.get("id") or last
            except (httpx.TransportError, asyncio.TimeoutError, _PublishFailed, OperationalError, InterfaceError) as e:
                # A relay PUBLISH failure (local relay restart, dead socket) OR a transient CONNECTION-level DB
                # error (PG connection dropped mid-_seen/commit) must NOT advance the cursor — else this
                # in-flight note is skipped forever (permanently missing from the mirror). Stop the page here;
                # `last` holds the last delivered note, so it retries next poll. Roll back so a poisoned PG
                # session doesn't break the sibling (global) drain that reuses this same session.
                try:
                    db.rollback()
                except Exception:
                    pass
                logger.warning("[fedi-bridge] %s drain transient/publish error, retrying next cycle: %s", ttype, e)
                transient = True
                break
            except Exception as e:
                # Genuinely-bad post (e.g. IntegrityError/DataError) → SKIP it (advance) so it can't wedge the
                # drain. But roll back FIRST: such an error may have aborted the PG txn, and without this the
                # poisoned session breaks every later _seen/query in this page AND the sibling (global) drain
                # that reuses this same session — silently dropping those posts too.
                try:
                    db.rollback()
                except Exception:
                    pass
                logger.warning("[fedi-bridge] %s post mirror failed (skipping): %s", ttype, e)
                last = raw.get("id") or last
        if last and last != cursor:
            cursor = last
            settings_store.put(cursor_key, cursor)
        if transient or len(raw_posts) < _PAGE:
            break


async def poll_once(db: Session) -> None:
    if str(_get("fedi_bridge_enabled", "false")).lower() not in ("1", "true", "yes", "on"):
        return
    instance_url = _get("fedi_bridge_instance_url")
    token = _get("fedi_bridge_access_token")
    if not (instance_url and token):
        return
    platform = "pleroma"   # Pleroma/Mastodon API
    ttype = _get("fedi_bridge_type", "global")
    include_replies = _get("fedi_bridge_include_replies", "true").lower() == "true"
    instance_host = urlparse(instance_url).netloc.split(":")[0].lower()
    blocked_domains = _blocked_domains()
    port = _port()
    # Anchor the budget BEFORE the moderation fetch. _refresh_moderation makes two HTTP calls (15s
    # timeout each), so anchoring after it could put the drain's deadline at 30+70=100s while _job
    # cancels at _POLL_TIMEOUT=90 — turning the clean "stop and resume next poll" path into a hard
    # mid-page cancellation.
    _budget_start = time.monotonic()
    await _refresh_moderation(instance_url, token)

    deadline = _budget_start + _DRAIN_BUDGET   # ONE budget shared by both timeline drains this poll. Bounds
                                                  # the per-POST loop in _drain_timeline AND (threaded down as
                                                  # _deadline) each post's ancestor backfill + the fresh-connect
                                                  # _backfill_recent — so no single deep reply overruns _POLL_TIMEOUT.
    # Drain the LOCAL timeline FIRST (when it's a distinct feed): it's low-volume and finishes fast, so
    # the instance's OWN users — whose posts the federated firehose dilutes/drops (an active local
    # account got ~7% of a week mirrored) — are never starved by the high-volume global drain. Wrapped so
    # a local-drain failure can't break the main global mirror. Skip when the configured type IS local.
    if ttype != "local":
        try:
            await _drain_timeline(db, port, platform, instance_url, token, instance_host,
                                  blocked_domains, include_replies, "local", "fedi_bridge_local_since", deadline)
        except Exception as e:
            logger.warning("[fedi-bridge] local drain failed: %s: %s", type(e).__name__, e, exc_info=True)

    await _drain_timeline(db, port, platform, instance_url, token, instance_host,
                          blocked_domains, include_replies, ttype, "fedi_bridge_global_since", deadline)
    # NOTE: deletion propagation used to run here — it's now a SEPARATE scheduled job so its HTTP
    # status checks can't eat this poll's time budget (the "poll exceeded 90s" cause).


# --- reconciliation ---------------------------------------------------------
# The drain reads TIMELINES. A timeline is a filtered, ephemeral view the instance may legitimately
# omit posts from, and the forward-only min_id cursor then advances straight past them — permanently
# and silently. (Already known here in weaker form: see the local-drain-first note in poll_once, "an
# active local account got ~7% of a week mirrored".) Reconciliation re-reads an author's OWN outbox
# and re-delivers whatever never landed. It is deliberately CAUSE-AGNOSTIC: it repairs timeline
# omissions, permanent relay rejections that later stop applying, restart gaps and cursor drift
# alike, without anyone having to diagnose each one first.
_RECON_BATCH = 4        # authors per pass — small on purpose: each costs 1-2 API calls and the
                        # instance rate-limits (a 40-post/12-page audit tripped 429 by hand).
_RECON_LOOKBACK = 40    # statuses per author — one page, covers a normal posting day
_RECON_INTERVAL = 900   # seconds between passes
_RECON_SEED_SCAN = 300     # newest delivery rows scanned when seeding (PK index, constant cost)
_RECON_SEED_PER_PASS = 8   # new authors added to the rotation each pass (bounded: 8 small INSERTs)
# A neglected author can owe dozens of posts, and each repair costs the INSTANCE more than the audit
# does — _deliver backfills ancestors and resolves quotes. Measured: one 14-repair pass took requests
# to the instance from ~7-20/min to 34/min. Cap the burst; the remainder is picked up next pass, and
# the rotation is a background repair, not a deadline.
_RECON_MAX_REPAIRS_PER_PASS = 6


class _RateLimited(Exception):
    """The instance returned 429. Raised to abort the whole pass: continuing to the next author just
    spends the remaining budget collecting more 429s from a server already asking us to stop."""


def _seed_recon_state(db: Session, instance_url: str, instance_host: str, want: int) -> int:
    """Add up to `want` not-yet-tracked authors to the rotation. Returns how many were added.

    Runs EVERY pass, independently of whether this pass could fill its batch. Gating it on "we came
    up short" is what wedged the rotation: once _RECON_BATCH rows existed the seeding branch was
    unreachable, so the table stayed frozen at the first four authors ever seen — all remote — and a
    local account could never enter it no matter how long it ran.

    LOCAL accounts first: they are the population the documented gap hits (poll_once: "an active
    local account got ~7% of a week mirrored") while being a rounding error in the firehose window
    (82 of 3118). Sourced from fedi_puppets.instance_host — 8.7k small rows bounded by LIMIT, not a
    LIKE over the 121k-row delivery table."""
    if want <= 0:
        return 0
    known = {r[0] for r in db.query(FediReconcileState.acct)
                             .filter(FediReconcileState.instance_url == instance_url).all()}
    fresh, seen = [], set()
    def _take(acct):
        if acct and acct not in known and acct not in seen and len(fresh) < want:
            seen.add(acct)
            fresh.append(acct)
    if instance_host:
        for (a,) in (db.query(FediPuppet.acct)
                       .filter(FediPuppet.instance_host == instance_host)
                       .limit(_RECON_SEED_SCAN).all()):
            _take(a)
    for (a,) in (db.query(FediBridgeDelivered.author_acct)
                   .filter(FediBridgeDelivered.instance_url == instance_url,
                           FediBridgeDelivered.author_acct.isnot(None),
                           FediBridgeDelivered.author_acct != "")
                   .order_by(FediBridgeDelivered.id.desc())
                   .limit(_RECON_SEED_SCAN).all()):
        _take(a)
    for a in fresh:
        db.add(FediReconcileState(instance_url=instance_url, acct=a))
    if fresh:
        try:
            db.commit()
        except Exception:
            db.rollback()
            return 0
    return len(fresh)


def _recon_candidates(db: Session, instance_url: str, limit: int, instance_host: str = "") -> list:
    """Authors to audit this pass, least-recently-checked first (NULL = never audited sorts first).

    PERFORMANCE: driven by fedi_reconcile_state — one small row per author — rather than a
    SELECT DISTINCT over the 121k-row delivery table, which is a seq scan that grows with the
    firehose."""
    _seed_recon_state(db, instance_url, instance_host, _RECON_SEED_PER_PASS)
    rows = (db.query(FediReconcileState)
              .filter(FediReconcileState.instance_url == instance_url)
              .order_by(FediReconcileState.last_checked_at.asc().nullsfirst())
              .limit(limit).all())
    return [r.acct for r in rows]


async def _reconcile_author(db: Session, port: int, platform: str, instance_url: str,
                            instance_host: str, token: str, acct: str,
                            blocked_domains: set, include_replies: bool, deadline: float,
                            _repair_budget=None) -> int:
    """Audit ONE author: outbox vs delivered/skipped, re-deliver the difference. Returns the count
    repaired. Never raises — one unreachable author must not stop the pass."""
    st = (db.query(FediReconcileState)
            .filter(FediReconcileState.instance_url == instance_url,
                    FediReconcileState.acct == acct).first())
    if not st:
        st = FediReconcileState(instance_url=instance_url, acct=acct)
        db.add(st)
    repaired = 0
    if _repair_budget is None:
        _repair_budget = [_RECON_MAX_REPAIRS_PER_PASS]
    _tok = _recon_ctx.set(True)
    try:
        if not st.account_id:
            acc = await pleroma_service.lookup_account(instance_url, token, acct)
            if not acc:
                st.last_error = "account lookup failed"
                st.last_checked_at = datetime.utcnow()
                db.commit()
                return 0
            st.account_id = str(acc.get("id"))
        try:
            statuses = await pleroma_service.fetch_account_statuses(
                instance_url, token, st.account_id, limit=_RECON_LOOKBACK)
        except httpx.HTTPStatusError as he:
            if he.response is not None and he.response.status_code == 429:
                st.last_error = "rate-limited (429)"
                st.last_checked_at = datetime.utcnow()
                try:
                    db.commit()
                except Exception:
                    db.rollback()
                raise _RateLimited()
            raise
        for raw in statuses:
            if time.monotonic() > deadline:
                break
            post = _norm(platform, raw)
            if not post.get("id"):
                continue
            uri = _canonical_uri(platform, instance_url, post)
            # Already mirrored, or already CONSIDERED and deliberately skipped → leave it alone.
            # Checking the skip ledger is what stops the pass from re-attempting a not-public or
            # oversized post on every single run forever.
            if _seen(db, instance_url, post["id"], uri):
                continue
            if uri and db.query(FediBridgeSkipped.id).filter(FediBridgeSkipped.note_uri == uri).first():
                continue                      # already considered and deliberately skipped
            # Route through _process, NOT _deliver: it owns the policy (boosts, include_replies,
            # blocked domains, muted authors). Calling _deliver directly would let reconciliation
            # mirror exactly what the drain is configured to refuse — a bridge that disagrees with
            # itself depending on which path saw the post first.
            if _repair_budget[0] <= 0:
                break               # burst cap for this pass; the rest waits for the next one
            await _process(db, port, platform, instance_url, instance_host,
                           blocked_domains, include_replies, raw, deadline)
            if _existing_mirror(db, instance_url, uri, post.get("id")):
                repaired += 1
                _repair_budget[0] -= 1
                logger.info("[fedi-bridge] reconcile repaired %s by %s", post.get("id"), acct)
        st.last_error = None
    except _RateLimited:
        raise                       # abort the PASS, not just this author — see reconcile_once
    except Exception as e:
        # 429 is the expected failure here — back off by simply stamping the check and moving on.
        # ROLL BACK FIRST: a DB-level error leaves the Postgres txn aborted, and every later use of
        # this session (including the commit below, and the FIRST query of the NEXT author, which is
        # outside this try) then raises. Without it one bad author wedges the whole rotation: its
        # last_checked_at never gets stamped, so NULLS FIRST keeps it at the head of every pass.
        try:
            db.rollback()
        except Exception:
            pass
        if st not in db:            # rollback EXPUNGES a still-pending new row — re-add or it's lost
            db.add(st)
        st.last_error = f"{type(e).__name__}: {e}"[:300]
        logger.warning("[fedi-bridge] reconcile of %s failed: %s", acct, e)
    finally:
        _recon_ctx.reset(_tok)
    st.last_checked_at = datetime.utcnow()
    st.last_repaired = repaired
    st.total_repaired = (st.total_repaired or 0) + repaired
    try:
        db.commit()
    except Exception:
        db.rollback()
    return repaired


async def reconcile_once(db: Session) -> int:
    """One reconciliation pass over a bounded batch of authors. Returns posts repaired."""
    if str(_get("fedi_bridge_enabled", "false")).lower() not in ("1", "true", "yes", "on"):
        return 0
    instance_url = _get("fedi_bridge_instance_url")
    token = _get("fedi_bridge_access_token")
    if not (instance_url and token):
        return 0
    platform = "pleroma"
    instance_host = urlparse(instance_url).netloc.split(":")[0].lower()
    include_replies = _get("fedi_bridge_include_replies", "true").lower() == "true"
    blocked_domains = _blocked_domains()
    port = _port()
    deadline = time.monotonic() + _DRAIN_BUDGET
    total = 0
    budget = [_RECON_MAX_REPAIRS_PER_PASS]      # shared across the batch, not per author
    for acct in _recon_candidates(db, instance_url, _RECON_BATCH, instance_host):
        if time.monotonic() > deadline:
            break
        if budget[0] <= 0:
            logger.info("[fedi-bridge] reconcile hit the per-pass repair cap (%d); resuming next pass",
                        _RECON_MAX_REPAIRS_PER_PASS)
            break
        try:
            total += await _reconcile_author(db, port, platform, instance_url, instance_host,
                                             token, acct, blocked_domains, include_replies, deadline,
                                             budget)
        except _RateLimited:
            logger.warning("[fedi-bridge] instance rate-limited (429) — ending this reconcile pass")
            break
    if total:
        logger.info("[fedi-bridge] reconciliation repaired %d post(s)", total)
    return total


# --- maintenance ------------------------------------------------------------

def cleanup_state() -> None:
    """Prune delivered-map rows for notes that have aged out of the relay (mirrors are reconstructable
    and the relay prunes them); keeps the bookkeeping table bounded alongside the firehose."""
    from app.database import SessionLocal
    # Follows Auto-clean (Admin → Relay), the relay's ONE retention window — the mirror no longer has
    # a retention setting of its own. These rows are bookkeeping, not content: dropping one only means
    # a post the relay has already aged out could be re-mirrored if the source resurfaces it, so the
    # ledger must not outlive the events it describes, nor be reaped before them. 0 (Auto-clean off)
    # therefore means keep the ledger too — matching "nothing is auto-deleted".
    try:
        keep_days = int(_get("nostr_relay_retention_days", "30") or "30")
    except ValueError:
        keep_days = 30
    db = SessionLocal()
    try:
        # Auto-clean off (0) → the events are kept, so their ledger is kept too. The SKIP ledger below
        # still prunes: it is diagnostics, not bookkeeping about anything that exists.
        if keep_days > 0:
            cutoff = datetime.utcnow() - timedelta(days=keep_days)
            # Keep note_id="" rows: those are the write-back TOMBSTONES/markers _check_deletions goes
            # out of its way to preserve ("a permanent marker, not bookkeeping to reap"). Reaping them
            # let a reply to an old thread pull the user's own status back through fetch_context, where
            # _seen no longer knew about it — so their post was re-published under a PUPPET key,
            # appearing twice in the global timeline under two identities.
            (db.query(FediBridgeDelivered)
               .filter(FediBridgeDelivered.created_at < cutoff, FediBridgeDelivered.note_id != "")
               .delete(synchronize_session=False))
        # The skip ledger is PURELY diagnostic, so it gets a short retention of its own rather than the
        # relay's — nothing reads a month-old skip, and leaving it unpruned is how a diagnostic table
        # quietly becomes the biggest one in the database. (fedi_reconcile_state is self-limiting: one
        # small row per author, reused forever, so it needs no pruning.)
        (db.query(FediBridgeSkipped)
           .filter(FediBridgeSkipped.created_at < datetime.utcnow() - timedelta(days=_SKIP_RETENTION_DAYS))
           .delete(synchronize_session=False))
        db.commit()
    except Exception as e:
        db.rollback()
        logger.warning("[fedi-bridge] state cleanup failed: %s: %s", type(e).__name__, e)
    finally:
        db.close()


# --- scheduler --------------------------------------------------------------

_scheduler = None


def start_fedi_bridge_scheduler() -> None:
    """Start the global-timeline mirror poller (idempotent). Call from a running loop (app startup)."""
    global _scheduler
    if _scheduler is not None:
        return
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    try:
        secs = max(60, int(_get("fedi_bridge_poll_seconds", "90") or "90"))
    except ValueError:
        secs = 90

    async def _job():
        from app.database import SessionLocal
        db = SessionLocal()
        try:
            await asyncio.wait_for(poll_once(db), timeout=_POLL_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning("[fedi-bridge] poll exceeded %ss and was cancelled; retrying next cycle", _POLL_TIMEOUT)
            db.rollback()
        except Exception as e:
            logger.warning("[fedi-bridge] poll job error: %s: %s", type(e).__name__, e, exc_info=True)
            db.rollback()
        finally:
            db.close()

    async def _cleanup():
        await asyncio.get_event_loop().run_in_executor(None, cleanup_state)

    async def _deljob():
        # Deletion propagation on its OWN cadence, decoupled from the mirror poll so its HTTP status
        # checks never eat the poll's budget. Own timeout guard, own session.
        if str(_get("fedi_bridge_enabled", "false")).lower() not in ("1", "true", "yes", "on"):
            return
        instance_url, token = _get("fedi_bridge_instance_url"), _get("fedi_bridge_access_token")
        if not (instance_url and token):
            return
        from app.database import SessionLocal
        db = SessionLocal()
        try:
            await asyncio.wait_for(
                _check_deletions(db, _port(), instance_url, token, _broadcast_on()), timeout=80)
        except asyncio.TimeoutError:
            logger.warning("[fedi-bridge] deletion check exceeded 80s; retrying next cycle")
            db.rollback()
        except Exception as e:
            logger.warning("[fedi-bridge] deletion job error: %s: %s", type(e).__name__, e)
            db.rollback()
        finally:
            db.close()

    async def _reconjob():
        # Reconciliation on its OWN cadence, like deletions: it makes outbound API calls per author
        # and must never eat the mirror poll's time budget.
        if str(_get("fedi_bridge_enabled", "false")).lower() not in ("1", "true", "yes", "on"):
            return
        from app.database import SessionLocal
        db = SessionLocal()
        try:
            await asyncio.wait_for(reconcile_once(db), timeout=120)
        except asyncio.TimeoutError:
            logger.warning("[fedi-bridge] reconciliation exceeded 120s; resuming next cycle")
            db.rollback()
        except Exception as e:
            logger.warning("[fedi-bridge] reconciliation job error: %s: %s", type(e).__name__, e)
            db.rollback()
        finally:
            db.close()

    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(_job, "interval", seconds=secs, id="fedi_bridge_poll", max_instances=1, coalesce=True)
    _scheduler.add_job(_reconjob, "interval", seconds=_RECON_INTERVAL, id="fedi_bridge_reconcile",
                       max_instances=1, coalesce=True)
    _scheduler.add_job(_deljob, "interval", seconds=_DELETION_INTERVAL, id="fedi_bridge_deletions", max_instances=1, coalesce=True)
    _scheduler.add_job(_cleanup, "interval", hours=24, id="fedi_bridge_cleanup", max_instances=1, coalesce=True)
    _scheduler.start()
    logger.info("[fedi-bridge] global-timeline → Nostr mirror poller started (every %ss)", secs)


def stop_fedi_bridge_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        try:
            _scheduler.shutdown(wait=False)
        except Exception as e:
            logger.warning("[fedi-bridge] scheduler shutdown error: %s: %s", type(e).__name__, e)
        _scheduler = None
