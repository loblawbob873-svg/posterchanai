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
import time
from datetime import datetime, timedelta
from urllib.parse import urlparse

import httpx
from sqlalchemy.orm import Session

from app.models import FediBridgeDelivered, FediPuppet
from app.services import pleroma_service, settings_store
from app.services import fedi_bridge_identity as ident
from app.services.nostr import bech32
from app.services.fedi_timeline_service import (   # reuse the proven normalizers + emoji parsing
    _norm, _canonical_uri, _EMOJI_SHORTCODE_RE)

logger = logging.getLogger(__name__)

_POLL_TIMEOUT = 90
_DRAIN_BUDGET = 70
_PAGE = 20
_MAX_PAGES = 8
_MODERATION_TTL = 600          # seconds to cache the read account's block/mute lists
_mod_cache: dict = {"at": 0.0, "blocked_accts": set()}
_DELETION_INTERVAL = 300       # min seconds between deletion re-checks (deletions are rare)
_DELETION_BATCH = 25           # recent mirrored notes re-checked for deletion per cycle
_last_deletion_check = 0.0
_MAX_ANCESTORS = 8             # cap ancestors backfilled to anchor an orphan reply's thread
_BACKFILL_PAGES = 3            # pages of recent history to mirror on first connect (≈60 posts)
_MAX_QUOTE_DEPTH = 2           # cap quote-of-quote recursion when mirroring referenced notes


# --- settings ---------------------------------------------------------------

def _get(key: str, default: str = "") -> str:
    v = settings_store.get(key, default)
    return v if v not in (None, "") else default


def _broadcast_on() -> bool:
    return str(_get("fedi_bridge_broadcast", "false")).lower() in ("1", "true", "yes", "on")


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


def _build_content(post: dict, quote_bech: str | None = None) -> str:
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
            qacct = q.get("acct") or "?"
            qtext = (q.get("text") or "").strip()
            snippet = (qtext[:280] + "…") if len(qtext) > 280 else qtext
            parts.append(f"\n↪ quoting @{qacct}: {snippet}".rstrip())
    return "\n".join(parts).strip() or "​"   # never publish empty content


def _emoji_tags(content: str, *emoji_maps: dict) -> list:
    """NIP-30 custom-emoji tags for every :shortcode: that actually appears in `content` and has a
    known URL in one of the supplied {shortcode: url} maps. Deduped; bounded so a spam note can't
    publish hundreds of tags."""
    if not content:
        return []
    merged: dict = {}
    for m in emoji_maps:
        if m:
            merged.update(m)
    if not merged:
        return []
    out, seen = [], set()
    for sc in _EMOJI_SHORTCODE_RE.findall(content):
        if sc in seen:
            continue
        url = merged.get(sc)
        if url:
            out.append(["emoji", sc, url])
            seen.add(sc)
        if len(out) >= 30:
            break
    return out


async def _backfill_ancestors(db: Session, port: int, platform: str, instance_url: str,
                              instance_host: str, post: dict, token: str = "") -> None:
    """Mirror a reply's ancestor chain (toward the conversation root) so the reply threads under a
    real parent instead of appearing as an orphan with missing context. Ancestors come root-first,
    so each is delivered before its child and never triggers a further backfill (recursion guard).
    `token` lets the personal plane backfill from the USER's instance (else the bridge read account)."""
    token = token or _get("fedi_bridge_access_token")
    try:
        ctx = await pleroma_service.fetch_context(instance_url, token, post["id"])
        ancestors = ctx.get("ancestors") or []
    except Exception as e:
        logger.debug("[fedi-bridge] ancestor fetch failed for %s: %s", post.get("id"), e)
        return
    blocked = _blocked_domains()
    for raw in ancestors[-_MAX_ANCESTORS:]:           # closest N (always includes the immediate parent)
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
                           backfill=False, token=token)
        except Exception as e:
            logger.debug("[fedi-bridge] ancestor deliver failed: %s", e)


async def _rewrite_mentions(db: Session, port: int, instance_host: str, content: str,
                            mentions: list) -> tuple:
    """Make fediverse @handle mentions CLICKABLE on Nostr: for each mentioned account, provision its
    puppet, replace the `@handle` text with a `nostr:<npub>` reference (which clients render as a
    profile link), and p-tag it. Returns (rewritten_content, [p-tags])."""
    ptags = []
    # Longest acct first so '@user@host' is replaced before a bare '@user' substring.
    for m in sorted(mentions or [], key=lambda x: len(x.get("acct", "")), reverse=True):
        url = (m.get("url") or "").strip()
        acct = (m.get("acct") or m.get("username") or "").strip()
        username = (m.get("username") or "").strip()
        if not url or not acct:
            continue
        try:
            p = await ident.ensure_puppet(
                db, port, {"url": url, "acct": acct, "username": username, "display_name": username},
                instance_host)
        except Exception:
            p = None
        if not p:
            continue
        ptags.append(["p", p["pubkey_hex"]])
        ref = "nostr:" + p["npub"]
        for token in ("@" + acct, "@" + username):
            if token and token != "@" and token in content:
                content = content.replace(token, ref)
    return content, ptags


def _raw_quote_status(platform: str, raw: dict) -> dict | None:
    """The full quoted status object embedded in `raw` (Pleroma `quote`, or a Misskey renote that
    carries its own text), or None for a plain post/boost."""
    if platform == "misskey":
        rn = raw.get("renote")
        return rn if (isinstance(rn, dict) and (raw.get("text") or "").strip()) else None
    # Pleroma: a quote-post carries `quote`; a boost-with-comment carries `reblog` (+ own content,
    # which is why it wasn't filtered as a pure boost). Mirror _norm_pleroma's quote selection.
    sub = raw.get("quote") or raw.get("reblog")
    return sub if isinstance(sub, dict) else None


async def _resolve_quote(db: Session, port: int, platform: str, instance_url: str, instance_host: str,
                         raw: dict, token: str, depth: int) -> tuple:
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
    eid = await _deliver(db, port, platform, instance_url, instance_host, q_raw, qpost,
                         backfill=False, token=token, _depth=depth + 1)
    if not eid:
        return None, None
    row = _existing_mirror(db, instance_url, quri, qpost.get("id"))
    return eid, (row.nostr_pubkey if row else None)


async def _deliver(db: Session, port: int, platform: str, instance_url: str, instance_host: str,
                   raw: dict, post: dict, backfill: bool = True, token: str = "",
                   extra_ptags: list | None = None, _depth: int = 0) -> str | None:
    # NEVER mirror a non-public status as a PUBLIC Nostr note — a `direct` (DM) or `private`
    # (followers-only) status published as a public kind-1 leaks private content into the feed.
    if (raw.get("visibility") or "public").lower() in ("direct", "private"):
        return None
    account = raw.get("account") or {}
    p = await ident.ensure_puppet(db, port, account, instance_host)
    if not p:
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
                await _backfill_ancestors(db, port, platform, instance_url, instance_host, post, token=token)
            parent = _parent_event(db, instance_url, parent_id)
            if parent:
                tags.append(["e", parent.nostr_event_id, "", "reply"])
                if parent.nostr_pubkey:
                    tags.append(["p", parent.nostr_pubkey])
        # Quote (NIP-18): mirror the quoted note (so it's on the relay) then reference it with a `q`
        # tag AND a nostr:<note> in the content, so Nostr clients embed the quoted post's CONTENT —
        # not a dangling link. Depth-bounded so a chain of quote-of-quote can't recurse without limit.
        quote_bech = None
        quoted_ev_id, quoted_pk = await _resolve_quote(db, port, platform, instance_url, instance_host,
                                                       raw, token, _depth)
        if quoted_ev_id:
            qtag = ["q", quoted_ev_id]
            if quoted_pk:
                qtag += ["", quoted_pk]
            tags.append(qtag)
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
        content = _build_content(post, quote_bech)
        content, mention_ptags = await _rewrite_mentions(db, port, instance_host, content, raw.get("mentions") or [])
        for t in mention_ptags:
            if not any(x[0] == "p" and x[1] == t[1] for x in tags if len(x) >= 2):
                tags.append(t)
        # NIP-30 custom emoji: tag every :shortcode: still present in the content so clients render the
        # actual emoji image instead of the raw shortcode text.
        tags.extend(_emoji_tags(content, post.get("content_emojis")))
        ev = ident.build_event(p, 1, content, tags=tags, object_uri=uri, broadcast=_broadcast_on())
        ok, msg = await ident.publish(port, ev)
        if not ok:
            logger.debug("[fedi-bridge] publish failed for %s: %s", post.get("id"), msg)
            return None
        db.add(FediBridgeDelivered(platform=platform, instance_url=instance_url, note_id=post["id"],
                                   note_uri=uri, author_acct=p["acct"], nostr_event_id=ev["id"],
                                   nostr_pubkey=p["pubkey_hex"]))
        try:
            db.commit()
        except Exception:
            db.rollback()
        return ev["id"]
    finally:
        _inflight.discard(inflight_key)


async def _process(db: Session, port: int, platform: str, instance_url: str, instance_host: str,
                   blocked_domains: set, include_replies: bool, raw: dict) -> None:
    # Skip pure boosts (a reblog with no own content): the original federates in on its own, so
    # mirroring the boost would just duplicate it. Quote-posts (own text) ARE mirrored.
    if raw.get("reblog") and not (raw.get("content") or "").strip():
        return
    post = _norm(platform, raw)
    if not post.get("id"):
        return
    if not include_replies and post.get("in_reply_to_id"):
        return
    acct = post.get("author", {}).get("acct") or ""
    host = _host_of(acct, instance_host)
    if _domain_blocked(host, blocked_domains) or _author_muted(acct, host, instance_host):
        return
    uri = _canonical_uri(platform, instance_url, post)
    if _seen(db, instance_url, post["id"], uri):
        return
    await _deliver(db, port, platform, instance_url, instance_host, raw, post)


# --- deletions --------------------------------------------------------------

async def _check_deletions(db: Session, port: int, instance_url: str, token: str, broadcast: bool) -> None:
    """Re-check the most recent mirrored notes; if one was deleted on the fediverse (definitive
    404/410), publish a NIP-09 deletion from its puppet (which removes it on the relay and federates
    upstream iff broadcasting), then drop the bookkeeping row. Bounded + throttled — deletions are
    rare and per-status checks cost a request each."""
    global _last_deletion_check
    now = time.monotonic()
    if now - _last_deletion_check < _DELETION_INTERVAL:
        return
    _last_deletion_check = now
    rows = (db.query(FediBridgeDelivered)
            .filter(FediBridgeDelivered.instance_url == instance_url)
            .order_by(FediBridgeDelivered.id.desc()).limit(_DELETION_BATCH).all())
    for row in rows:
        try:
            if not await pleroma_service.status_deleted(instance_url, token, row.note_id):
                continue
            actor_uri = None
            if row.nostr_pubkey:
                pup = db.query(FediPuppet).filter(FediPuppet.pubkey_hex == row.nostr_pubkey).first()
                actor_uri = pup.actor_uri if pup else None
            if actor_uri:
                await ident.delete_note(port, actor_uri, row.nostr_event_id, broadcast)
            db.delete(row)
            db.commit()
            logger.info("[fedi-bridge] mirrored note %s deleted on source → NIP-09 delete published", row.note_id)
        except Exception as e:
            db.rollback()
            logger.debug("[fedi-bridge] deletion check failed for %s: %s", row.note_id, e)


async def _backfill_recent(db: Session, port: int, platform: str, instance_url: str,
                           instance_host: str, blocked_domains: set, include_replies: bool) -> None:
    """On a fresh connect, mirror a bounded window of RECENT history (paging backward with max_id) so
    the Nostr global timeline isn't empty until new posts trickle in. Mirrors oldest-first (parents
    before replies) and then sets the forward cursor to the newest seen."""
    token = _get("fedi_bridge_access_token")
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
    for raw in sorted(collected, key=lambda r: r.get("created_at") or ""):   # oldest-first
        try:
            await _process(db, port, platform, instance_url, instance_host,
                           blocked_domains, include_replies, raw)
            done += 1
        except Exception as e:
            logger.debug("[fedi-bridge] backfill mirror failed: %s", e)
    if newest:
        settings_store.put("fedi_bridge_global_since", newest)
    logger.info("[fedi-bridge] initial backfill mirrored %d recent post(s)", done)


# --- poll -------------------------------------------------------------------

async def poll_once(db: Session) -> None:
    if str(_get("fedi_bridge_enabled", "false")).lower() not in ("1", "true", "yes", "on"):
        return
    instance_url = _get("fedi_bridge_instance_url")
    token = _get("fedi_bridge_access_token")
    if not (instance_url and token):
        return
    platform = "pleroma"   # Pleroma/Mastodon API first; misskey is a fast-follow
    ttype = _get("fedi_bridge_type", "global")
    include_replies = _get("fedi_bridge_include_replies", "true").lower() == "true"
    instance_host = urlparse(instance_url).netloc.split(":")[0].lower()
    blocked_domains = _blocked_domains()
    port = _port()
    await _refresh_moderation(instance_url, token)

    since = _get("fedi_bridge_global_since")

    async def _fetch(cursor, first):
        return await pleroma_service.fetch_timeline(instance_url, token, ttype, limit=_PAGE,
                                                    min_id=(None if first else cursor))

    if not since:
        # No cursor. If we already have mirrored history for this instance, the cursor was LOST
        # (restart with a wiped local_settings.json) — resume forward from the newest delivered note
        # so the gap during downtime is recovered, not skipped. Otherwise it's a fresh connect:
        # backfill a bounded window of recent posts so the global timeline isn't empty on day one.
        last = (db.query(FediBridgeDelivered)
                .filter(FediBridgeDelivered.instance_url == instance_url)
                .order_by(FediBridgeDelivered.id.desc()).first())
        if last and last.note_id:
            since = last.note_id
            settings_store.put("fedi_bridge_global_since", since)
            logger.info("[fedi-bridge] cursor lost — resuming forward from newest delivered note %s", since)
        else:
            await _backfill_recent(db, port, platform, instance_url, instance_host,
                                   blocked_domains, include_replies)
            return

    cursor = since
    drain_start = time.monotonic()
    for _page in range(_MAX_PAGES):
        if time.monotonic() - drain_start > _DRAIN_BUDGET:
            break
        raw_posts = await _fetch(cursor, False)
        if not raw_posts:
            break
        # oldest-first so parents are mirrored before their replies (ISO8601 sorts lexically).
        raw_posts = sorted(raw_posts, key=lambda r: r.get("created_at") or "")
        last = None
        transient = False
        for raw in raw_posts:
            try:
                await _process(db, port, platform, instance_url, instance_host,
                               blocked_domains, include_replies, raw)
                last = raw.get("id") or last
            except (httpx.TransportError, asyncio.TimeoutError) as e:
                logger.warning("[fedi-bridge] transient fetch/deliver error, retrying next cycle: %s", e)
                transient = True
                break
            except Exception as e:
                logger.warning("[fedi-bridge] post mirror failed: %s", e)
                last = raw.get("id") or last
        if last and last != cursor:
            cursor = last
            settings_store.put("fedi_bridge_global_since", cursor)
        if transient or len(raw_posts) < _PAGE:
            break

    # Propagate fediverse deletions → NIP-09 (throttled). Federates upstream iff broadcasting is on.
    await _check_deletions(db, port, instance_url, token, _broadcast_on())


# --- maintenance ------------------------------------------------------------

def cleanup_state() -> None:
    """Prune delivered-map rows for notes that have aged out of the relay (mirrors are reconstructable
    and the relay prunes them); keeps the bookkeeping table bounded alongside the firehose."""
    from app.database import SessionLocal
    try:
        days = int(_get("fedi_bridge_retention_days", "0") or "0")
    except ValueError:
        days = 0
    keep_days = days or int(_get("nostr_relay_retention_days", "30") or "30")
    db = SessionLocal()
    try:
        cutoff = datetime.utcnow() - timedelta(days=max(1, keep_days))
        db.query(FediBridgeDelivered).filter(FediBridgeDelivered.created_at < cutoff).delete()
        db.commit()
    except Exception as e:
        db.rollback()
        logger.warning("[fedi-bridge] state cleanup failed: %s", e)
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
            logger.warning("[fedi-bridge] poll job error: %s", e)
            db.rollback()
        finally:
            db.close()

    async def _cleanup():
        await asyncio.get_event_loop().run_in_executor(None, cleanup_state)

    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(_job, "interval", seconds=secs, id="fedi_bridge_poll", max_instances=1, coalesce=True)
    _scheduler.add_job(_cleanup, "interval", hours=24, id="fedi_bridge_cleanup", max_instances=1, coalesce=True)
    _scheduler.start()
    logger.info("[fedi-bridge] global-timeline → Nostr mirror poller started (every %ss)", secs)


def stop_fedi_bridge_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        try:
            _scheduler.shutdown(wait=False)
        except Exception as e:
            logger.warning("[fedi-bridge] scheduler shutdown error: %s", e)
        _scheduler = None
