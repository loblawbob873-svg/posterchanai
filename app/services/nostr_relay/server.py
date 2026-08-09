"""NIP-01 relay server: WebSocket EVENT/REQ/CLOSE/COUNT + live subscription fan-out,
plus the NIP-11 relay info document served over a plain HTTP GET.

Runs on the relay thread's own asyncio loop (see thread.py). All persistence goes through
`RelayStore` (off-loop executors), and every write is gated by the WoT (`gate.is_member`)
so only web-of-trust pubkeys are ever accepted.
"""

import re
import time
import json
import asyncio
import logging
import ipaddress
from collections import deque

from websockets.datastructures import Headers
from websockets.http11 import Response

from app.services.nostr.event import verify_event
from .langfilter import blocked_language, blocked_word
from .bridges import reveals_blocked_bridge, author_on_blocked_bridge, is_bridged_post


# pcai: CONFIG d-tags eligible for the opt-in DR backup to upstream (small + critical). Bulky /
# per-item docs (chat conversations/messages, upload refs, drafts, ai-requests) are NEVER broadcast.
_BACKUP_NS = ("pcai:setting:", "pcai:user:", "pcai:usercfg:", "pcai:bot:")

# The user's own encrypted libraries. Withheld from the PUBLIC upstreams by _broadcastable, and the
# only things _private_mirrorable will send to the operator's own relays.
#
# Spelled out rather than shortened to "pcai:note"/"pcai:pw". Those bare prefixes cover today's five
# namespaces by accident of spelling, and would silently adopt any future `pcai:notes-export` or
# `pcai:pwpolicy` into the set of things that get copied off this machine. An explicit list makes
# adding one a decision.
# Per-item namespaces (one event per note / entry / folder / message) are prefixes...
_PRIVATE_NS = ("pcai:note:", "pcai:notefolder:", "pcai:pw:", "pcai:pwfolder:",
               # Calendars and addressbooks are the same shape and the same risk as notes — one
               # encrypted event per item, with this Postgres as the only copy — and they were the
               # one private library with NO mirror at all, so a restore handed back the notes and
               # the vault and left the calendar and the phone's contacts empty. `pcai:calmeta:`
               # travels with them and is not optional: it is the collection itself (display name,
               # colour, and the `kind` that tells a VADDRESSBOOK from a calendar), and restored
               # items with no collection to live in are items a client never shows.
               "pcai:cal:", "pcai:calmeta:",
               # `pcai:upload:` is the key that makes a chat attachment readable. Mirroring the
               # messages without it copies the conversation and leaves every file in it unopenable
               # — a restore that looks complete and is not.
               "pcai:files-index-bak:", "pcai:conv:", "pcai:msg:", "pcai:upload:")
# ...and the singleton documents are EXACT names, not prefixes. `pcai:budget` as a prefix also
# matches `pcai:budgeting`, which is how an unrelated future doc gets copied off the box without
# anyone deciding it should be.
_PRIVATE_DOCS = ("pcai:pwkey", "pcai:budget", "pcai:files-index", "pcai:drafts", "pcai:voices",
                 "pcai:news-feeds", "pcai:news-read", "pcai:client-prefs")
# Kinds that are private in their entirety rather than by namespace: an UNFINISHED article or
# listing is the purest case of "exists once, irreplaceable" — _broadcastable keeps it off the
# public network precisely because it is not published yet, which also left it with no second copy
# anywhere.
_PRIVATE_KINDS = (30024, 30403)


def _private_mirrorable(ev) -> bool:
    """Whether a write belongs on the operator's PRIVATE mirror relays.

    Redundancy for the data that has none. A note or a vault entry lives in exactly one Postgres —
    this relay's — so losing that box loses the library, which is a worse failure than any of the
    ones the public fan-out protects against. Everything public is already on twenty relays; the
    irreplaceable half was on one.

    A SEPARATE list, not the public upstreams, and that distinction is the whole design. These
    events are ciphertext, so mirroring them is not a disclosure of content — but each one carries
    the author's pubkey, a stable `d` tag and a timestamp, so a copy on a stranger's relay is a
    permanent public record of how many passwords someone has and when each one changed, and nothing
    can withdraw it later. On relays the operator runs, that record is already theirs. On
    relay.example.social it belongs to somebody else, forever. So this fans out to whatever the
    operator explicitly names and nowhere else; blank means no mirroring at all.
    """
    k = ev.get("kind")
    if k in _PRIVATE_KINDS:
        return True
    if k != 30078:
        return False
    d = next((t[1] for t in ev.get("tags", []) if len(t) >= 2 and t[0] == "d"), "")
    return d.startswith(_PRIVATE_NS) or d in _PRIVATE_DOCS


def _broadcastable(ev, cfg=None) -> bool:
    """Whether a direct write should be re-broadcast to the upstream relays. Notes, profiles,
    PUBLISHED articles (kind 30023), reactions, DMs — yes, blast them everywhere. NOT private/
    internal events: NIP-23 **drafts** (kind 30024) stay on this relay until published, and the
    app's own encrypted **datastore** docs (kind 30078 with a `pcai:` d-tag — settings/users/chats)
    are this node's internal state, never the public network's business — EXCEPT when the operator
    opts into DR backup (`backup_datastore`), which broadcasts the small CONFIG docs (settings/
    accounts/per-user config/bots). They're NIP-44 ciphertext to everyone but the operator, and a
    fresh node restores them from upstream with the operator nsec."""
    k = ev.get("kind")
    if k in (30024, 30403):   # NIP-23 article drafts / NIP-99 listing drafts — stay local until published
        return False
    if k == 30078:
        d = next((t[1] for t in ev.get("tags", []) if len(t) >= 2 and t[0] == "d"), "")
        if d.startswith("pcai:"):
            if cfg and cfg.get("backup_datastore") and d.startswith(_BACKUP_NS):
                return True
            return False
    # A DELETION of a private datastore document must not be broadcast either — and this one is a
    # DISCLOSURE, not just load. The document it removes was never federated (kind 30078 with a
    # `pcai:` d-tag, above), but a kind-5 is an ordinary event that fans out to every upstream, and
    # it carries the coordinate it deletes in the clear:
    #
    #   ["a", "30078:<pubkey>:pcai:mail:someone@example.com:INBOX:6623"]
    #
    # The mail itself is ciphertext; that tag publishes the account's email address, the folder name
    # and the message id to ~20 relays somebody else runs, permanently, where nothing can withdraw
    # it. The same shape leaks note ids, calendar item uids and contact uids. Emptying a folder or
    # deleting a calendar also means one broadcast per item — a thousand deletions is a thousand
    # events times twenty relays, which is how the outbox queue pinned and the relay pegged.
    if k == 5:
        for t in ev.get("tags", []):
            if len(t) < 2 or t[0] != "a":
                continue
            coord = str(t[1])
            if ":pcai:" not in coord:
                continue
            # …EXCEPT the DR-backup namespaces, which _broadcastable deliberately DOES federate when
            # `backup_datastore` is on (settings, accounts, per-user config, bots). Those documents
            # are upstream, so their TOMBSTONES have to be too — suppressing the delete leaves the
            # upstream copy permanent, and a rebuilt node restoring from upstream brings back the
            # bot you removed, the user you deleted and the setting you unset. That is the same
            # resurrection CLAUDE.md documents for settings, and it would apply to accounts here.
            d = coord.split(":", 2)[2] if coord.count(":") >= 2 else ""
            if cfg and cfg.get("backup_datastore") and d.startswith(_BACKUP_NS):
                continue
            return False
    # Opt-out marker: e.g. game bots tag the mid-game move boards so only the opening + final post
    # federate to the wider network (the middle plays stay local-only — anti-spam).
    if any(t and len(t) >= 1 and t[0] == "nofederate" for t in ev.get("tags", [])):
        return False
    return True
from . import negentropy

logger = logging.getLogger(__name__)


def _is_ephemeral(kind: int) -> bool:
    """NIP-01 ephemeral range: transmit to subscribers but never persist."""
    return 20000 <= kind < 30000


def _event_expiration(ev: dict):
    """NIP-40: the `expiration` unix ts from an event's tags, or None if it has none/invalid."""
    for t in ev.get("tags", []):
        if len(t) >= 2 and t[0] == "expiration":
            try:
                return int(t[1])
            except (ValueError, TypeError):
                return None
    return None


def _match_one(flt: dict, ev: dict) -> bool:
    if "ids" in flt and ev["id"] not in flt["ids"]:
        return False
    if "authors" in flt and ev["pubkey"] not in flt["authors"]:
        return False
    if "kinds" in flt and int(ev["kind"]) not in {int(k) for k in flt["kinds"]}:
        return False
    if flt.get("since") is not None and int(ev["created_at"]) < int(flt["since"]):
        return False
    if flt.get("until") is not None and int(ev["created_at"]) > int(flt["until"]):
        return False
    for key, vals in flt.items():
        if isinstance(key, str) and key.startswith("#") and len(key) == 2 and vals:
            want = {str(v) for v in vals}
            have = {str(t[1]) for t in ev.get("tags", []) if len(t) >= 2 and t[0] == key[1]}
            if not (want & have):
                return False
    if flt.get("search"):
        low = (ev.get("content") or "").lower()
        if not all(t in low for t in re.findall(r"\w+", flt["search"].lower())):
            return False
    return True


def _matches(filters: list, ev: dict) -> bool:
    return any(_match_one(f or {}, ev) for f in (filters or []))


class SubscriptionManager:
    """Open REQ subscriptions, keyed by connection. Per-event O(open-subs) fan-out."""

    def __init__(self):
        # conn -> {sub_id: filters}
        self._subs: dict = {}

    def add(self, conn, sub_id: str, filters: list) -> None:
        self._subs.setdefault(conn, {})[sub_id] = filters

    def remove(self, conn, sub_id: str) -> None:
        if conn in self._subs:
            self._subs[conn].pop(sub_id, None)

    def remove_conn(self, conn) -> None:
        self._subs.pop(conn, None)

    def count(self, conn) -> int:
        return len(self._subs.get(conn, {}))

    def total(self) -> int:
        """Open subscriptions across every connection — the fan-out cost per stored event."""
        return sum(len(s) for s in self._subs.values())

    def fanout(self, ev: dict, send) -> None:
        """Enqueue `ev` to every matching open subscription via `send(conn, obj)` (a
        non-blocking, drop-on-slow enqueue) — so one slow client can't stall the firehose."""
        for conn, subs in list(self._subs.items()):
            for sub_id, filters in list(subs.items()):
                if _matches(filters, ev):
                    send(conn, ["EVENT", sub_id, ev])


_IP_CHARS = re.compile(r"^[0-9a-fA-F:.%\[\]]{3,45}$")


def _one_header(hdrs, name: str) -> str:
    """Exactly one value for `name`, else "" — and never an exception.

    A repeated header is either a broken proxy or a client muddying the water, and websockets'
    ``Headers.get`` RAISES ``MultipleValuesError`` for it — which is NOT a KeyError, so the usual
    ``.get(name, "")`` guard doesn't catch it. That turned "send X-Real-IP twice" into "the relay
    records my connection as ip=?", i.e. a client could opt out of the very log we added to find it.
    Duplicated means untrustworthy, so skip the source and fall through to one that isn't (ending at
    the socket peer, which nobody can forge)."""
    try:
        values = hdrs.get_all(name)
    except AttributeError:                        # plain mapping (tests / non-websockets callers)
        try:
            return (hdrs.get(name, "") or "").strip()
        except Exception:
            return ""
    except Exception:
        return ""
    return (values[0] or "").strip() if len(values) == 1 else ""


def _client_ip(hdrs, connection) -> str:
    """The real client IP behind the proxy chain (client → Cloudflare → cloudflared → nginx → here).

    Order matters, because only some of these are trustworthy:

    * ``X-Real-IP`` — set by OUR nginx from ``$remote_addr``, which nginx has already resolved to the
      true client via ``set_real_ip_from`` + ``real_ip_header CF-Connecting-IP``. nginx OVERWRITES
      whatever the client sent, so this is authoritative.
    * ``CF-Connecting-IP`` — Cloudflare's own, for a path that reaches us without nginx's X-Real-IP.
    * ``X-Forwarded-For`` first hop — the legacy fallback, and the reason this function exists: with
      ``$proxy_add_x_forwarded_for`` nginx APPENDS the real IP to whatever the client supplied, so
      the FIRST element is client-controlled. Preferring it (as this used to) meant a client could
      choose the IP we logged and the one the "online people" count dedups on.
    * the socket peer — direct/turnkey access with no proxy at all.

    Anything that isn't IP-shaped is discarded rather than logged: header values can't contain CRLF,
    but they can contain enough printable text to make a log line lie about itself."""
    for value in (_one_header(hdrs, "X-Real-IP"), _one_header(hdrs, "CF-Connecting-IP"),
                  _one_header(hdrs, "X-Forwarded-For").split(",")[0]):
        ip = (value or "").strip()
        if ip and _IP_CHARS.match(ip):
            return ip
    ra = getattr(connection, "remote_address", None)
    ip = (ra[0] if ra else "") or ""
    return ip if _IP_CHARS.match(ip) else ""


class _OutQ:
    """One connection's outbound frame queue: a bounded deque + a wakeup event.

    Not an ``asyncio.Queue`` because when the queue is full we need to choose WHAT to drop. An
    EVENT is re-pullable, so dropping one is survivable. An ``EOSE``/``OK``/``CLOSED`` is NOT: the
    client's ``query()`` then waits out its own timeout, and the zombie detector answers that by
    tearing the socket down and reconnecting — "slow, keeps disconnecting, content missing" on any
    link too slow to drain the queue (it never reproduces on a LAN, where nothing ever queues up).
    So a control frame evicts the OLDEST event instead of being dropped itself.

    Single-threaded by the same invariant the old ``put_nowait`` relied on: only the relay's event
    loop touches this. ``sent``/``dropped`` are what the per-connection close log reports.
    """

    __slots__ = ("dq", "wake", "maxlen", "sent", "dropped")

    def __init__(self, maxlen: int):
        self.dq = deque()
        self.wake = asyncio.Event()
        self.maxlen = max(16, int(maxlen or 8192))
        self.sent = 0
        self.dropped = 0

    def push(self, msg: str, is_event: bool) -> bool:
        if len(self.dq) >= self.maxlen:
            self.dropped += 1
            if is_event:
                return False                      # re-pullable — drop it, never stall the fanout
            # Control frame: make room by evicting the oldest EVENT. Find the index first and
            # delete after the loop — deleting mid-iteration only works because of the break that
            # would follow it, which is a trap for the next person to edit this.
            victim = next((i for i, q in enumerate(self.dq) if q.startswith('["EVENT"')), None)
            if victim is None:
                return False                      # nothing but control frames queued: nothing to evict
            del self.dq[victim]
        self.dq.append(msg)
        self.wake.set()
        return True

    async def pop(self) -> str:
        while not self.dq:
            self.wake.clear()
            await self.wake.wait()
        return self.dq.popleft()


class RelayServer:
    # Write-path tallies since this process started, for the Server Stats relay panel. Counted in
    # _send, which is the single funnel every accept/reject OK frame goes through — a per-branch
    # counter in _on_event's ~20 rejection paths would drift the first time one was added. Declared
    # on the CLASS so `+=` works on an instance built without __init__ (the queue tests do that);
    # the first bump shadows it with a per-instance int, so two relays can't share a tally.
    _accepted = 0
    _rejected = 0

    def __init__(self, store, gate, config: dict, outbox_cb=None, private_cb=None):
        self.store = store
        self.gate = gate                 # .is_member(pubkey) -> bool
        self.cfg = config
        self.outbox_cb = outbox_cb       # async fn(event) | None (Phase 4)
        self.private_cb = private_cb     # async fn(event) | None — the operator's own mirror relays
        self.subs = SubscriptionManager()
        self._conns = 0
        self._neg: dict = {}   # conn -> {sub_id: negentropy item set} (NIP-77 sessions)
        self._outq: dict = {}  # conn -> bounded outbound queue (decouples slow clients)
        self._conn_ips: dict = {}  # conn -> client IP (for the deduped "online people" estimate)
        self._call_seen: dict = {}  # pubkey -> last kind-25050 (call signaling) time, for the live "in call" tally
        # Fediverse-bridge NIP-05: local-part -> puppet pubkey, populated as puppet kind-0 profiles
        # are stored (and warmed from the store at startup). Resolved on ?name= lookups only; never
        # enumerated in the no-name nostr.json dump (there can be tens of thousands).
        self._bridge_nip05: dict = {}
        self._bridge_pubkeys: set = set()   # puppet pubkeys (values of _bridge_nip05) — DM inbox set

    def _send(self, conn, obj) -> None:
        """Enqueue a message to a client WITHOUT blocking — a slow consumer must never stall the
        firehose/fanout or other clients. On a full queue an EVENT is dropped (re-pullable) and a
        control frame evicts the oldest event instead; see _OutQ for why that distinction matters."""
        if obj and obj[0] == "OK" and len(obj) >= 3:
            # Counted before the queue lookup on purpose: the verdict is a fact about the write, not
            # about whether this particular socket was still around to be told.
            if obj[2]:
                self._accepted += 1
            else:
                self._rejected += 1
        q = self._outq.get(conn)
        if q is None:
            return
        q.push(json.dumps(obj), obj[0] == "EVENT" if obj else False)
        # The session line below only prints when a connection CLOSES, which can be hours after it
        # started struggling — no use while someone is telling you it's happening right now. So say
        # it once, the moment a connection first falls behind. `dropped` only ever increments, so
        # == 1 fires exactly once per connection.
        if q.dropped == 1:
            logger.warning("[nostr-relay] conn ip=%s is not keeping up — queue full (%d), dropping "
                           "frames; see its 'conn closed' line for the total",
                           self._conn_ips.get(conn) or "?", q.maxlen)

    async def _keepalive(self, conn) -> None:
        """Push a tiny application-level NOTICE to the client every ~40s. Unlike a WebSocket PING frame —
        which a CDN (Cloudflare fronts this relay) may answer at its own edge, and whose idle-timeout
        accounting can ignore — a data frame traverses the WHOLE proxy chain to the browser: it resets the
        CDN's idle timer AND refreshes the client's last-received clock, so the connection never silently
        rots into a 'zombie'. This is what keeps a mobile PWA's feed live when its JS timers are throttled
        and the radio briefly suspends (the "no new posts after ~2-3 min" symptom). The client ignores an
        unrecognised NOTICE; one tiny frame per connection per 40s."""
        try:
            while True:
                await asyncio.sleep(40)
                self._send(conn, ["NOTICE", "keepalive"])
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def _writer(self, conn, q) -> None:
        """Drain one connection's outbound queue at the client's own pace."""
        try:
            while True:
                msg = await q.pop()
                await conn.send(msg)
                q.sent += 1
        except Exception:
            pass

    # --- NIP-11 -------------------------------------------------------------

    def nip11_doc(self, host: str = "") -> bytes:
        c = self.cfg
        # Relay icon/avatar (shown by clients like Yakihonne): configured URL, else the
        # PosterChan avatar served from this host's /static.
        icon = c.get("icon") or (
            f"https://{host}/static/posterchan-relay.png" if host else "")
        doc = {
            "name": c.get("name") or "PosterChanAI Relay",
            "description": c.get("description") or "Web-of-trust relay",
            "software": "https://github.com/loblawbob873-svg/posterchanai",
            # 1 core, 2 contacts, 9 deletes, 11 info, 17 private DMs, 22 comments, 23 long-form,
            # 40 expiration, 44 encryption, 45 COUNT, 50 search, 59 gift wrap, 65 relay-list,
            # 77 negentropy sync
            "supported_nips": [1, 2, 9, 11, 17, 22, 23, 40, 44, 45, 50, 59, 65, 77],
            "limitation": {
                "max_message_length": c.get("max_message_size", 262144),
                "max_subscriptions": c.get("max_subs_per_conn", 20),
                "max_filters": c.get("max_filters_per_req", 10),
            },
        }
        # Advertising restricted_writes makes outbox-model clients (e.g. Yakihonne) refuse to
        # treat this as a sole write relay and silently inject their default relays — which
        # resets a user who wants ONLY this relay in their NIP-65 list. The WoT gate still
        # enforces writes at runtime regardless; this only controls what NIP-11 advertises.
        # Off by default so single-relay setups stick; flip on to be honest to spam clients.
        if c.get("advertise_restricted_writes", False):
            doc["limitation"]["restricted_writes"] = True
        if icon:
            doc["icon"] = icon
            doc["banner"] = icon
        if c.get("pubkey"):
            doc["pubkey"] = c["pubkey"]
        if c.get("contact"):
            doc["contact"] = c["contact"]
        # Pay-to-stay, when an admin enabled it: NIP-11 has fields for exactly this, and a client
        # that reads them can warn a user before their notes age out. Absent entirely otherwise —
        # advertising `retention` on a relay that deletes nothing would be a lie in the other
        # direction. `retention` describes what a VISITOR's own posts get (the free tier); the
        # subscription that extends it is the `fees.subscription` entry.
        try:
            from app.services import paid_retention_service as prs
            pol = prs.policy()
        except Exception:
            pol = {}
        if pol.get("enabled") and pol.get("free_days"):
            from .store import _PRUNABLE_KINDS
            doc["retention"] = [
                # Only high-volume feed content is ever aged out; everything else is kept.
                {"kinds": list(_PRUNABLE_KINDS), "time": int(pol["free_days"]) * 86400},
                {"time": None},
            ]
            if pol.get("sats_per_month"):
                doc["fees"] = {"subscription": [{"amount": int(pol["sats_per_month"]) * 1000,
                                                 "unit": "msats", "period": 2592000}]}
        return json.dumps(doc).encode("utf-8")

    def _register_bridge_nip05(self, ev: dict) -> None:
        """Track a just-stored puppet kind-0: ALWAYS add its pubkey to the DM-inbox set (so DM replies
        to it are accepted even when no NIP-05 domain is configured) + persist it, and ADDITIONALLY
        register its NIP-05 local-part → pubkey when the profile carries a `nip05` on our domain."""
        pk = ev.get("pubkey", "")
        if not pk:
            return
        # (a) DM-inbox membership — independent of NIP-05, so it never "fails closed" without a domain.
        if pk not in self._bridge_pubkeys:
            self._bridge_pubkeys.add(pk)
            try:
                asyncio.create_task(self.store.bridge_puppet_add(pk))
            except Exception:
                pass
        # (b) NIP-05 name (only if the profile declares one on our domain).
        try:
            meta = json.loads(ev.get("content") or "{}")
        except (ValueError, TypeError):
            return
        nip05 = (meta.get("nip05") or "").strip().lower()
        local = nip05.split("@", 1)[0] if nip05 else ""
        if not local or self._bridge_nip05.get(local) == pk:
            return
        self._bridge_nip05[local] = pk
        try:
            asyncio.create_task(self.store.bridge_nip05_set(local, pk))   # persist off-loop
        except Exception:
            pass

    async def warm_bridge_nip05(self) -> int:
        """Load the persisted puppet NIP-05 map into memory at startup (before serving)."""
        try:
            self._bridge_nip05 = dict(await self.store.bridge_nip05_all())
        except Exception as e:
            logger.debug("[nostr-relay] bridge nip05 warm failed: %s", e)
            self._bridge_nip05 = {}
        # DM-inbox set = named puppets + ALL tracked puppet pubkeys (the latter covers puppets that
        # never got a NIP-05 name, so DM-to-puppet acceptance doesn't depend on a configured domain).
        self._bridge_pubkeys = set(self._bridge_nip05.values())
        try:
            self._bridge_pubkeys |= set(await self.store.bridge_puppets_all())
        except Exception as e:
            logger.debug("[nostr-relay] bridge puppet warm failed: %s", e)
        return len(self._bridge_nip05)

    def nip05_doc(self, raw_path: str) -> bytes:
        """Build a NIP-05 `/.well-known/nostr.json` response. With `?name=<n>` return only that
        identity (+ its relays); without it, return all. Empty when disabled or unknown."""
        n = self.cfg.get("nip05") or {}
        enabled = n.get("enabled", False)
        names = (n.get("names") or {}) if enabled else {}
        # Fediverse-bridge puppets get a NIP-05 here too, but there can be tens of thousands of them,
        # so they are resolved ONLY by explicit ?name= lookup and never dumped in the enumerate-all
        # response (which stays the small operator/admin set).
        bridge_names = self._bridge_nip05 if enabled else {}
        relays = n.get("relays") or []
        want = None
        if "?" in raw_path:
            from urllib.parse import parse_qs
            vals = parse_qs(raw_path.split("?", 1)[1]).get("name")
            if vals:
                want = vals[0]
        if want is not None:
            pk = names.get(want) or bridge_names.get(want)
            out_names = {want: pk} if pk else {}
        else:
            out_names = dict(names)
        doc = {"names": out_names}
        if relays and out_names:
            doc["relays"] = {pk: list(relays) for pk in out_names.values()}
        return json.dumps(doc).encode("utf-8")

    def process_request(self, connection, request):
        """Route the opening handshake: WebSocket upgrade → proceed (None); a NIP-11
        request (`Accept: application/nostr+json`) → the info doc; any other browser GET →
        the cyberpunk welcome page explaining how to connect a client."""
        try:
            hdrs = request.headers
            if hdrs.get("Upgrade", "").lower() == "websocket":
                try:
                    setattr(connection, "_pcai_ip", _client_ip(hdrs, connection))
                except Exception:
                    pass
                return None  # let the WebSocket handshake proceed
            accept = hdrs.get("Accept", "")
            host = hdrs.get("Host", "") or f"{self.cfg.get('bind','')}:{self.cfg.get('port','')}"
            # The reverse proxy preserves the public path (e.g. /relay), so the welcome page
            # advertises the exact wss URL clients should use, not a guessed root.
            raw_path = getattr(request, "path", "") or "/relay"
            path = raw_path.split("?", 1)[0]
        except Exception:
            return None
        # NIP-05: nginx proxies /.well-known/nostr.json here (so the identity server "runs as a
        # subprocess" alongside the relay). Honour the ?name= query per the spec.
        if path == "/.well-known/nostr.json":
            headers = Headers({
                "Content-Type": "application/json",
                "Access-Control-Allow-Origin": "*",
            })
            return Response(200, "OK", headers, self.nip05_doc(raw_path))
        if "application/nostr+json" in accept:
            headers = Headers({
                "Content-Type": "application/nostr+json",
                "Access-Control-Allow-Origin": "*",
            })
            return Response(200, "OK", headers, self.nip11_doc(host))
        # CORS on the HTML response too — web clients (nostrudel) fetch /relay from the browser
        # and the Same-Origin Policy blocks reading a response with no Access-Control-Allow-Origin.
        headers = Headers({
            "Content-Type": "text/html; charset=utf-8",
            "Access-Control-Allow-Origin": "*",
        })
        return Response(200, "OK", headers, self._welcome_html(host, path))

    def _welcome_html(self, host: str, path: str = "/relay") -> bytes:
        import html as _html
        url = f"wss://{_html.escape(host)}{_html.escape(path)}"
        name = _html.escape(self.cfg.get("name") or "PosterChanAI Relay")
        desc = _html.escape(self.cfg.get("description") or "Web-of-trust Nostr relay")
        return _WELCOME_HTML.replace("{{URL}}", url).replace("{{NAME}}", name).replace(
            "{{DESC}}", desc).replace("{{RETENTION}}", _retention_block()).encode("utf-8")

    # --- connection handling ------------------------------------------------

    async def handle(self, conn) -> None:
        # Two spellings on purpose: the log wants a placeholder to print, while online_count() must
        # keep "" meaning UNKNOWN — collapsing every unidentified connection under one "?" there
        # would count them as a single person.
        raw_ip = getattr(conn, "_pcai_ip", "") or ""
        ip = raw_ip or "?"
        if self._conns >= self.cfg.get("max_connections", 5000):
            # Was a silent 1013: the client just saw its socket close and reconnected forever.
            logger.warning("[nostr-relay] conn refused ip=%s — at max_connections (%d)",
                           ip, self._conns)
            await conn.close(code=1013, reason="overloaded")
            return
        self._conns += 1
        self._conn_ips[conn] = raw_ip
        # Bigger than the query hard_cap (5000) so a full-page REQ response fits without the
        # synchronous send loop overflowing. Overflow no longer costs the EOSE (see _OutQ) — it
        # costs EVENTS, which is the difference between a stale feed and a hung query.
        q = _OutQ(self.cfg.get("outq_size", 8192))
        self._outq[conn] = q
        writer = asyncio.create_task(self._writer(conn, q))
        keepalive = asyncio.create_task(self._keepalive(conn))
        opened = time.time()
        try:
            async for raw in conn:
                await self._dispatch(conn, raw)
        except Exception as e:
            if "ConnectionClosed" not in type(e).__name__:
                logger.debug("[nostr-relay] connection handler error: %r", e)
        finally:
            writer.cancel()
            keepalive.cancel()
            self._log_session(conn, ip, opened, q)
            self._outq.pop(conn, None)
            self._conn_ips.pop(conn, None)
            self.subs.remove_conn(conn)
            self._neg.pop(conn, None)
            self._conns -= 1

    # Close codes that mean "the client is done with us": a normal close, a page/tab going away,
    # or no code at all (1005) — the ordinary end of a session, not a fault.
    _CLEAN_CLOSE = (1000, 1001, 1005)

    @staticmethod
    def _is_internal(ip: str) -> bool:
        """Is this one of our own machines rather than a person out on the internet?

        Measured on the live relay: ~13 connections close every 90s (≈520/hour), and EVERY one of
        them has the proxy as its TCP peer — the app, the bridge on router.lan, the bots and the
        node agents all open short-lived query sockets from the LAN. Excluding only loopback (the
        first cut of this) would have left the journal full of our own machinery at INFO, burying
        the user reports this log exists to surface.

        An address we can't place — unparseable, or the "?" the caller uses when it has none — counts
        as REMOTE. Our own machinery always has a LAN peer to report, so "unknown" is genuinely odd,
        and a stray line is cheaper than a silently ignored user."""
        if ip in ("127.0.0.1", "::1", "localhost"):
            return True
        try:
            addr = ipaddress.ip_address(ip.strip("[]").split("%")[0])
            return addr.is_private or addr.is_loopback or addr.is_link_local
        except ValueError:
            return False

    def _log_session(self, conn, ip: str, opened: float, q) -> None:
        """One line per finished connection, so a user reporting "it keeps disconnecting" stops being
        invisible. Sessions that ended badly — frames dropped, an abnormal close code, or a life so
        short it implies a reconnect loop — log at INFO; ordinary ones at DEBUG, or 130 idle sockets
        would bury the journal. Dropped frames are the number to watch: they mean this client could
        not drain what we sent (see _OutQ).

        The short-session rule applies to REMOTE clients only — see _is_internal for why LAN, and
        not just loopback, is the line."""
        try:
            dur = time.time() - opened
            code = getattr(conn, "close_code", None)
            reason = (getattr(conn, "close_reason", "") or "")[:60]
            dropped = getattr(q, "dropped", 0)
            remote = not self._is_internal(ip)
            bad = dropped or (remote and dur < 60) or (code is not None and code not in self._CLEAN_CLOSE)
            logger.log(
                logging.INFO if bad else logging.DEBUG,
                "[nostr-relay] conn closed ip=%s dur=%.1fs sent=%d dropped=%d subs=%d code=%s%s",
                ip, dur, getattr(q, "sent", 0), dropped, self.subs.count(conn),
                code, f" reason={reason!r}" if reason else "",
            )
        except Exception:
            pass   # diagnostics must never break teardown

    def online_count(self) -> int:
        """A closer estimate of *people* online than the raw socket count: distinct client IPs
        among live connections, so one person's multiple tabs / PWA + signer / reconnects collapse
        to one. Each connection with an unknown IP (extraction failed) counts as its own, and the
        whole thing falls back to the raw connection count if no IPs were captured at all."""
        if not self._conn_ips:
            return self._conns   # no IPs captured at all → raw fallback
        _local = {"127.0.0.1", "::1", "localhost"}
        known, unknown = set(), 0
        for ip in self._conn_ips.values():
            if ip and ip not in _local:
                known.add(ip)              # a real remote person
            elif not ip:
                unknown += 1               # IP unknown → count this conn on its own
            # loopback (our own bots / internal) is skipped — not a person online
        return len(known) + unknown

    def active_calls(self) -> int:
        """Distinct people who exchanged call signaling (kind-25050) in the last ~2 minutes — a live
        'in a call' indicator for the sidebar ticker. Prunes stale entries as it counts, so the dict can't
        grow. Not a billing metric: a long, stable call that stops re-signaling ages out, so this reads as
        recent call ACTIVITY, and the UI only shows it when non-zero."""
        cutoff = time.time() - 120
        stale = [pk for pk, t in self._call_seen.items() if t < cutoff]
        for pk in stale:
            self._call_seen.pop(pk, None)
        return len(self._call_seen)

    async def _dispatch(self, conn, raw) -> None:
        if isinstance(raw, (bytes, bytearray)):
            if len(raw) > self.cfg.get("max_message_size", 262144):
                return
            raw = raw.decode("utf-8", "replace")
        elif len(raw) > self.cfg.get("max_message_size", 262144):
            self._send(conn, ["NOTICE", "message too large"])
            return
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            return
        if not isinstance(msg, list) or not msg:
            return
        typ = msg[0]
        if typ == "EVENT" and len(msg) >= 2:
            await self._on_event(conn, msg[1])
        elif typ == "REQ" and len(msg) >= 2:
            await self._on_req(conn, msg[1], msg[2:])
        elif typ == "CLOSE" and len(msg) >= 2:
            self.subs.remove(conn, msg[1])
        elif typ == "COUNT" and len(msg) >= 2:
            await self._on_count(conn, msg[1], msg[2:])
        elif typ == "NEG-OPEN" and len(msg) >= 4:
            await self._on_neg_open(conn, msg[1], msg[2], msg[3])
        elif typ == "NEG-MSG" and len(msg) >= 3:
            await self._on_neg_msg(conn, msg[1], msg[2])
        elif typ == "NEG-CLOSE" and len(msg) >= 2:
            self._neg.get(conn, {}).pop(msg[1], None)
        elif typ == "AUTH":
            pass  # NIP-42 not required (open reads); ignore

    # NIP-04 (kind 4) / NIP-17 gift wrap (1059) / NIP-59 seal (13) DMs. The author of a gift
    # wrap is a random throwaway key, so the WoT gate can't apply — instead we accept a DM only
    # when it's ADDRESSED (p-tag) to one of our own relay users (operator), acting as their inbox.
    _DM_KINDS = (4, 13, 1059)

    def _dm_for_operator(self, ev: dict) -> bool:
        for t in ev.get("tags", []):
            if len(t) >= 2 and t[0] == "p" and self.gate.is_operator(t[1]):
                return True
        return False

    def _dm_for_puppet(self, ev: dict) -> bool:
        """A DM (gift wrap / seal) addressed to a fediverse puppet — a local user replying to a
        bridged DM. Accept it so the bridge can unwrap it (with the puppet's derived key) and post
        the reply back out to the fediverse. The puppet's pubkey is known once its profile is stored."""
        for t in ev.get("tags", []):
            if len(t) >= 2 and t[0] == "p" and t[1] in self._bridge_pubkeys:
                return True
        return False

    async def _collab_for_known_repo(self, ev) -> bool:
        """True if a NIP-34 git collaboration event (patch/issue/reply/status) a-tags a repo whose PUBLIC
        30617 announcement is on THIS relay. Scopes issue/patch acceptance to KNOWN public repos (private
        repos have no 30617, so they never match). Low-volume kinds + a 60s cache on the store lookup, so
        this costs no per-event DB round-trip in steady state."""
        for t in ev.get("tags", []):
            if len(t) >= 2 and t[0] == "a" and isinstance(t[1], str):
                parts = t[1].split(":")
                if len(parts) == 3 and parts[0] in ("30617", "30618"):
                    if await self.store.is_repo_announced(parts[1], parts[2]):
                        return True
        return False

    async def _on_event(self, conn, ev) -> None:
        if not isinstance(ev, dict) or "id" not in ev:
            return
        eid = ev.get("id", "")
        if not verify_event(ev):
            self._send(conn, ["OK", eid, False, "invalid: bad id or signature"])
            return
        kind = int(ev.get("kind", 1))
        # Reject EMPTY text notes (kind-1 with blank/whitespace-only content) — pure spam/noise with
        # nothing to render. Other kinds legitimately have empty content (kind-3 follows, kind-6 reposts,
        # kind-7 reactions, kind-5 deletes), so this is scoped to kind 1 only.
        if kind == 1 and not (ev.get("content") or "").strip():
            self._send(conn, ["OK", eid, False, "invalid: empty note"])
            return
        # NIP-40: reject an event already past its expiration (don't store or relay it).
        exp = _event_expiration(ev)
        if exp is not None and exp <= int(time.time()):
            self._send(conn, ["OK", eid, False, "invalid: event expired"])
            return
        # Reject far-future events (bad client clock): a stored future created_at freezes replaceable
        # updates for that pubkey/kind + evades age retention. Send a distinct 'invalid' (NOT the retryable
        # 'not stored') so a clock-skewed client doesn't retry a doomed write forever. (The store-level
        # guard still covers the sync/firehose path, where a plain False is harmless.)
        try:
            if int(ev.get("created_at", 0)) > int(time.time()) + 900:
                self._send(conn, ["OK", eid, False, "invalid: created_at too far in the future"])
                return
        except (ValueError, TypeError):
            pass
        # Bridge blocklist: an account on a blocked bridge relay (mostr.pub etc.) is denied; learning
        # it from this event's profile/relay-list also bars everything else it posts (via is_member).
        # Our own fediverse-bridge puppets legitimately carry a NIP-48 proxy tag (pointing at the
        # original fedi note) and post mirrored content — they must skip the bridge/proxy denials
        # below, which exist to block OTHER instances' mirror accounts (mostr.pub etc.).
        _is_puppet = self.gate.is_puppet_event(ev)
        # A blocked author stays blocked even when it's a bridge puppet: blocking a puppet npub on
        # the relay (admin / web "Block author") must reject + (on purge) remove its events exactly
        # like a native author — otherwise the puppet exemption below would let it right back in.
        if _is_puppet and self.gate.is_blocked(ev.get("pubkey", "")):
            self._send(conn, ["OK", eid, False, "blocked: author blocked"])
            return
        _br = self.cfg.get("blocked_relays")
        if not _is_puppet and _br and reveals_blocked_bridge(ev, _br):
            if author_on_blocked_bridge(ev, _br):
                self.gate.mark_bridged_identity(ev.get("pubkey", ""))   # kind-0 nip05 → block even members
            else:
                self.gate.mark_bridged(ev.get("pubkey", ""))
            self._send(conn, ["OK", eid, False, "blocked: bridged relay not accepted"])
            return
        # Opt-in "block bridged posts": drop any NIP-48 proxy (fediverse/Bluesky-bridged) event,
        # whatever bridge relayed it. Operators / registered local users are exempt (their own
        # cross-posts are first-party data, never dropped); so are our own bridge puppets.
        if self.cfg.get("block_bridged") and is_bridged_post(ev) and not _is_puppet \
                and not self.gate.is_operator(ev.get("pubkey", "")):
            self._send(conn, ["OK", eid, False, "blocked: bridged (proxy) content not accepted"])
            return
        # WoT publishing gate — skippable. When wot_enabled is off (e.g. a processing node whose
        # relay is internal/localhost-bound and shouldn't run the trust graph), every author is
        # accepted; the bridge/language/word filters above + below still apply.
        _wot = self.cfg.get("wot_enabled", True)
        if kind == 4:
            # NIP-04: the author IS the real sender, so apply the normal WoT gate to them (lets
            # our web-of-trust members DM each other THROUGH this relay) OR accept it as an
            # operator's inbox (a DM addressed to one of our own users).
            if _wot and not (self.gate.is_member(ev.get("pubkey", "")) or self._dm_for_operator(ev)):
                self._send(conn, ["OK", eid, False, "blocked: sender not in web of trust"])
                return
        elif kind in (13, 1059):
            # Gift-wrap / seal (NIP-59 / NIP-17): the outer author is a random throwaway key, so the
            # WoT gate can't apply to it — gate on the RECIPIENT instead. Accept when the wrap p-tags
            # a web-of-trust member (so WoT members can DM each other via NIP-17) OR one of our own
            # relay users (operator inbox). The p-tag is the real recipient per NIP-59, so this is
            # the same routing relays rely on.
            if _wot and not (any(len(t) >= 2 and t[0] == "p" and self.gate.is_member(t[1]) for t in ev.get("tags", []))
                    or self._dm_for_operator(ev) or self._dm_for_puppet(ev)):
                self._send(conn, ["OK", eid, False, "blocked: zap/DM not for a web-of-trust member"])
                return
        elif kind == 9735:
            # NIP-57 zap receipt — authored by the zapper SERVICE (lnurl provider), not the zapper,
            # so the WoT gate can't apply to its author. Accept when it concerns (p-tags) a WoT
            # member, so members' zaps are stored + counted.
            if _wot and not any(len(t) >= 2 and t[0] == "p" and self.gate.is_member(t[1]) for t in ev.get("tags", [])):
                self._send(conn, ["OK", eid, False, "blocked: zap not for a web-of-trust member"])
                return
        elif ev.get("pubkey", "") in self.cfg.get("dvm_allowed", ()) and (
                kind in self.cfg.get("dvm_req_kinds", ()) or kind in self.cfg.get("dvm_res_kinds", ())):
            # DVM compute JOB (5xxx) or RESULT (6xxx) from a SHARE-ALLOWLISTED sender: accepted even if
            # not a WoT member — sharing your GPU (or running a node-agent) is a deliberate per-npub
            # grant, separate from the social web of trust. The DVM worker re-checks the same allowlist
            # (is_trusted / is_agent_trusted) before running anything. Accepting the result kind lets a
            # STANDALONE agent (keyless, no local relay) publish its 6xxx result back to a peer's relay.
            pass
        elif _is_puppet and kind in (0, 1, 3, 5, 6, 7):
            # Fediverse-bridge puppet: the app mirrors the global fediverse timeline through these
            # deterministic per-fedi-user keys. Gate-exempt for the mirrored content kinds only
            # (profile / note / contact-list / repost / reaction / NIP-09 deletion), and ONLY here on
            # the loopback WS
            # publish path — they are not WoT members, so the upstream sync/firehose never accept them.
            # A valid signature is still required (verify_event above) and the event must self-validate
            # as a puppet (fedibridge actor tag → derived pubkey == signer). A kind-5 deletes only the
            # puppet's OWN events (NIP-09), and federates upstream iff bridge broadcast is on (no
            # nofederate tag) — so a fediverse delete propagates everywhere the mirror reached.
            pass
        elif kind == 24133:
            # NIP-46 remote-signer transport (Amber / nsecbunker). Ephemeral — never stored (see below) — and
            # always p-tagged to the specific peer.
            #
            # The web of trust CANNOT gate this: NEITHER party is necessarily a member. The client side is an
            # ephemeral app key by construction, and Amber signs with a per-application signer key rather than
            # the user's identity key — so a WoT check on author-or-recipient rejects Amber's half of every
            # handshake ("blocked: signer traffic not for a web-of-trust member") and bunker login dies on our
            # own relay.
            #
            # ACCEPT IT. Gating this on "is someone subscribed for that pubkey right now" was a mistake I made
            # and it broke signing: the client sends its sign-request the instant you hit Post, and if the
            # signer's socket happens to be down at that moment (phone dozing, Amber backgrounded, or simply
            # connecting a second later) nobody is listening yet — so the relay REJECTED the request, signing
            # failed, and the post was never even created. An identity gate is no better: the client side is an
            # ephemeral app key and Amber signs with a per-app key, so neither party is in the web of trust.
            #
            # A kind-24133 is ephemeral: NOTHING is stored, it is only fanned out to whoever is subscribed. An
            # event addressed to nobody is dropped by the fan-out anyway — refusing it bought us no safety and
            # cost us the ordering hazard above. This is what public signer relays do.
            pass
        elif kind == 25050:
            # Voice/video CALL signaling (WebRTC over Nostr): ephemeral, always p-tagged to the specific
            # peer. Either party may be OUTSIDE the local WoT — a brand-new test account, or a cross-instance
            # caller/callee — so an author-only gate would silently drop every invite/answer to/from them
            # (symptom: "no ring on the other side"). Accept when EITHER party is trusted: the author is a WoT
            # member, OR the p-tagged recipient is a WoT member or one of our own relay users. This is the same
            # recipient-routing DMs/zaps use, so calls work for our users regardless of the peer's WoT status.
            if _wot and not (self.gate.is_member(ev.get("pubkey", "")) or self._dm_for_operator(ev)
                    or any(len(t) >= 2 and t[0] == "p" and self.gate.is_member(t[1]) for t in ev.get("tags", []))):
                self._send(conn, ["OK", eid, False, "blocked: call not for a web-of-trust member"])
                return
            # Tally who's in a call for the live activity ticker: both the caller and each p-tagged peer.
            _t = time.time()
            self._call_seen[ev.get("pubkey", "")] = _t
            for _tag in ev.get("tags", []):
                if len(_tag) >= 2 and _tag[0] == "p" and _tag[1]:
                    self._call_seen[_tag[1]] = _t
        elif kind in (30617, 30618):
            # NIP-34 git repo ANNOUNCEMENT (30617) + repo STATE (30618) are PUBLIC, browsable Discover
            # content — accept from ANY author (the repo owner is a datastore operator key that typically
            # isn't in the social WoT, and a repo hosted on one node must be discoverable on a peer node's
            # relay where the client reads). This mirrors the firehose ingest exemption
            # (nostr_relay/thread.py) + the "announcements stay broadly public" intent. Signature is still
            # verified above and these are kept forever (store._GIT_KINDS). Patches/issues (1617/1621/…)
            # stay WoT-gated until repo-scoped acceptance lands, so this isn't an open spam firehose.
            pass
        elif kind in (1617, 1621, 1622, 1623, 1630, 1631, 1632, 1633):
            # NIP-34 git COLLABORATION: patch (1617), issue (1621), replies (1622/1623), status
            # (1630-1633). Accept from ANY author, but ONLY when the event a-tags a repo whose PUBLIC
            # announcement (30617) is on THIS relay — so issues/patches show up in the client for repos
            # this relay knows about (incl. a repo HOSTED on a peer node, since scoping is by the
            # announcement, not by who hosts it), without opening an unbounded spam firehose. Private
            # repos have no 30617, so they're never matched (no title/content leak). Signature verified above.
            if _wot and not await self._collab_for_known_repo(ev):
                self._send(conn, ["OK", eid, False, "blocked: git patch/issue references an unknown repo"])
                return
        elif _wot and not self.gate.is_member(ev.get("pubkey", "")):
            self._send(conn, ["OK", eid, False, "blocked: not in web of trust"])
            return
        if kind == 1:
            content = ev.get("content", "")
            blocked = self.cfg.get("blocked_langs")
            if blocked:
                lang = blocked_language(content, blocked)
                if lang:
                    self._send(conn, ["OK", eid, False,
                                                f"blocked: language '{lang}' not accepted"])
                    return
            words = self.cfg.get("blocked_words")
            if words and blocked_word(content, words):
                self._send(conn, ["OK", eid, False,
                                            "blocked: contains filtered text"])
                return
        # NIP-01 ephemeral events (20000-29999): deliver to subscribers but NEVER persist.
        # WoT/lang gating above still applies; we just skip storage + the upstream blaster.
        if _is_ephemeral(kind):
            self._send(conn, ["OK", eid, True, ""])
            self.subs.fanout(ev, self._send)
            return
        # origin="direct": a client chose THIS relay as a destination (entrusted data), as
        # opposed to "wot" (a mirror of the public feed we pulled via sync/firehose). Prune
        # keeps direct writes forever and only trims the reconstructable synced feed.
        # origin="bridge": a mirrored fediverse note injected by our own puppet — reconstructable
        # like the synced feed, so the auto-prune ages it out (it is NOT a preserved local write),
        # and the outbox only re-broadcasts it when the operator opted into bridge broadcast (the
        # app omits the `nofederate` tag in that case; see _broadcastable).
        if _is_puppet:
            _origin = "bridge"
        elif kind in (4, 13, 1059) and self._dm_for_puppet(ev) and not self._dm_for_operator(ev):
            # A DM addressed ONLY to a fediverse puppet (a user replying to a bridged DM) is transient:
            # write-back consumes it, then it's federated. origin='bridge' so it's pruned on a short
            # TTL — otherwise (1059 isn't a prunable kind) spamming derivable puppet npubs fills disk.
            _origin = "bridge"
        else:
            _origin = "direct"
        # Only consulted for the private mirror, and only for the handful of kinds it covers — a
        # read per private write is nothing, and it is the difference between a mirror and a loop.
        _was_new = True
        if self.private_cb and _private_mirrorable(ev):
            try:
                _was_new = not await self.store.has_event(eid)
            except Exception as e:
                # Don't mirror when we can't tell — a missed copy beats a loop. But SAY so: silent
                # here means a failing read pool turns the backup off while it still looks on.
                logger.warning("[nostr-relay] private mirror skipped (has_event failed): %s", e)
                _was_new = False
        stored = await self.store.add_event(ev, origin=_origin)
        if not stored:
            # add_event did NOT persist the event — a transient insert/commit error (logged in
            # _add_event_sync) or a replaceable superseded by a newer stored version. Report the truth so
            # the publisher RETRIES instead of trusting a false OK. Sending OK=true here regardless was
            # how a mirrored fediverse reply that failed to store still made the bridge record a delivered
            # row → the personal plane then skipped re-delivery → the user silently missed the reply.
            self._send(conn, ["OK", eid, False, "error: not stored, retry"])
            return
        self._send(conn, ["OK", eid, True, ""])
        if True:
            if _is_puppet and kind == 0:
                self._register_bridge_nip05(ev)   # serve this puppet's <name>@host identity
            self.subs.fanout(ev, self._send)
            # The private mirror is a different list with a different rule, so it is a separate
            # decision — an event is never on both paths.
            #
            # `_was_new` matters here and nowhere else. add_event reports True for a DUPLICATE (the
            # insert is ON CONFLICT DO NOTHING and the publisher is owed an OK either way), so
            # mirroring on `stored` alone means two nodes pointed at each other — the topology this
            # feature's own help text recommends — bounce every private event between them forever:
            # A mirrors to B, B stores it and mirrors back, A stores the duplicate and mirrors again.
            # One perpetual cycle per event, until both 500-slot queues saturate and start dropping
            # the NEWEST writes. The backup stops working silently while both boxes burn CPU.
            if self.private_cb and _was_new and _private_mirrorable(ev):
                try:
                    self.private_cb(ev)
                except Exception as e:
                    logger.debug("[nostr-relay] private mirror enqueue failed: %s", e)
            if self.outbox_cb and _broadcastable(ev, self.cfg) and not self._dm_for_puppet(ev):
                # Blaster: re-broadcast inbound writes to the upstream relays — notes, profile
                # updates, published articles, AND DMs (encrypted, so no content leaks; this is how
                # they reach recipients when a user treats this as their only relay). EXCEPT private/
                # internal events (drafts, the app's own datastore docs) — see _broadcastable. Non-
                # blocking enqueue (drops on overflow) so a post-blasting client can't stall this.
                try:
                    self.outbox_cb(ev)
                except Exception as e:
                    logger.debug("[nostr-relay] outbox enqueue failed: %s", e)

    async def _on_req(self, conn, sub_id, filters) -> None:
        if not isinstance(sub_id, str):
            return
        if self.subs.count(conn) >= self.cfg.get("max_subs_per_conn", 20):
            self._send(conn, ["CLOSED", sub_id, "rate-limited: too many subscriptions"])
            return
        filters = [f for f in filters if isinstance(f, dict)][: self.cfg.get("max_filters_per_req", 10)]
        try:
            events = await self.store.query(filters)
        except Exception as e:
            logger.warning("[nostr-relay] query failed for %s: %s", sub_id, e)
            self._send(conn, ["CLOSED", sub_id, f"error: {e}"])
            return
        # Send oldest-first, newest last (common client expectation). Yield every chunk so the
        # writer drains the shared per-connection queue — otherwise a big response (with other
        # subs also enqueuing) overflows it and this client loses events it asked for. (The EOSE
        # itself is safe either way now: _OutQ never drops a control frame.)
        for n, ev in enumerate(reversed(events)):
            self._send(conn, ["EVENT", sub_id, ev])
            if n % 512 == 511:
                await asyncio.sleep(0)
        self._send(conn, ["EOSE", sub_id])
        self.subs.add(conn, sub_id, filters)

    async def _on_count(self, conn, sub_id, filters) -> None:
        filters = [f for f in filters if isinstance(f, dict)]
        n = await self.store.count_filtered(filters)   # SQL COUNT(*) — don't materialize rows just to len()
        self._send(conn, ["COUNT", sub_id, {"count": n}])

    # --- NIP-77 negentropy --------------------------------------------------

    async def _on_neg_open(self, conn, sub_id, filt, msg_hex) -> None:
        """Start a negentropy session: build our item set for the filter, reconcile against the
        client's initial message, reply NEG-MSG. On any failure, NEG-ERR so the client falls
        back to a normal REQ."""
        if not isinstance(sub_id, str):
            return
        try:
            flts = [filt] if isinstance(filt, dict) else []
            items = await self.store.neg_items(flts, cap=self.cfg.get("neg_max_items", 50000))
            sessions = self._neg.setdefault(conn, {})
            if len(sessions) >= self.cfg.get("max_subs_per_conn", 20):
                self._send(conn, ["NEG-ERR", sub_id, "rate-limited"])
                return
            sessions[sub_id] = items
            resp = negentropy.reconcile(items, bytes.fromhex(msg_hex))
            self._send(conn, ["NEG-MSG", sub_id, resp.hex()])
        except Exception as e:
            logger.debug("[nostr-relay] NEG-OPEN %s fell back: %r", sub_id, e)
            self._neg.get(conn, {}).pop(sub_id, None)
            self._send(conn, ["NEG-ERR", sub_id, "unsupported: negentropy"])

    async def _on_neg_msg(self, conn, sub_id, msg_hex) -> None:
        items = self._neg.get(conn, {}).get(sub_id)
        if items is None:
            self._send(conn, ["NEG-ERR", sub_id, "closed"])
            return
        try:
            resp = negentropy.reconcile(items, bytes.fromhex(msg_hex))
            self._send(conn, ["NEG-MSG", sub_id, resp.hex()])
            if len(resp) <= 1:  # only the version byte → nothing left to reconcile
                self._neg.get(conn, {}).pop(sub_id, None)
        except Exception as e:
            logger.debug("[nostr-relay] NEG-MSG %s error: %r", sub_id, e)
            self._neg.get(conn, {}).pop(sub_id, None)
            self._send(conn, ["NEG-ERR", sub_id, "error"])


_retention_cache: dict = {"key": None, "html": "", "at": 0.0}


def _retention_block() -> str:
    """The pay-to-stay section of the landing page — EMPTY unless an admin enabled the feature, so a
    relay that doesn't charge says nothing about charging.

    The QR encodes the receiving profile as a `nostr:` URI, NOT the lightning address, and that is
    the whole point: a subscription is bought with a ZAP, and only a Nostr client can make one (it
    signs the kind-9734 request that carries the payer's identity). A plain wallet paying the same
    lightning address sends real sats with nothing to attribute them to, so a payment QR here would
    take people's money and credit nobody. The address is still printed as text, as the destination
    to verify a zap against — never as something to scan and pay.

    Rendered once and cached against the settings that produced it: this runs on the relay's event
    loop for every hit on the landing page, and a QR encode per request is real work."""
    try:
        from app.services import paid_retention_service as prs
        from app.services.nostr import nostr_service
        pol = prs.policy()
    except Exception:
        return ""
    if not pol.get("enabled") or not pol.get("free_days"):
        # No free window = nothing is ever deleted = there is nothing to sell. Saying otherwise
        # would be advertising a subscription that buys the visitor exactly what they already have.
        return ""
    key = json.dumps(pol, sort_keys=True)
    if _retention_cache["key"] == key and (time.time() - _retention_cache["at"]) < 300:
        return _retention_cache["html"]
    import html as _html
    npub = ""
    try:
        npub = nostr_service.npub_of(pol["pubkey"]) if pol.get("pubkey") else ""
    except Exception:
        npub = ""
    kept = (f"kept for <b>{pol['paid_days']} days</b>" if pol.get("paid_days")
            else "kept for <b>as long as this relay runs</b>")
    price = pol.get("sats_per_month") or 0
    qr = ""
    if npub:
        try:
            import io
            import segno
            buf = io.BytesIO()
            segno.make(f"nostr:{npub}", error="m").save(buf, kind="svg", scale=4, border=2,
                                                        dark="#000", light="#fff")
            svg = buf.getvalue().decode("utf-8")
            svg = svg[svg.index("<svg"):]          # drop the XML prolog — this is inline HTML
            qr = f'<div class="qr">{svg}</div>'
        except Exception as e:                     # segno missing → the text below still works
            logger.debug("[nostr-relay] retention QR render skipped: %s", e)
    buy = ""
    if price and npub:
        buy = (f'<h2>Keep them longer</h2>'
               f'<div class="pay">{qr}<div>'
               f'<p><b>{price} sats / month.</b> Scan with a Nostr client and <b>zap this '
               f'profile</b> &mdash; the zap is what identifies you, so the storage is credited to '
               f'your key automatically. Renewing early adds to the time you already have.</p>'
               f'<p class="mono">{_html.escape(npub)}</p>'
               + (f'<p class="mono">zaps are paid to {_html.escape(pol["lud16"])}</p>'
                  if pol.get("lud16") else "")
               + '<p class="warn">Pay by <b>zapping the profile above from a Nostr client</b>. A '
                 'plain wallet payment to that lightning address, or a zap on a single <i>post</i>, '
                 'carries no identity for us to credit &mdash; it is treated as an ordinary tip.</p>'
               '</div></div>')
    _retention_cache.update(key=key, at=time.time(), html=(
        f'<h2>How long your posts are kept</h2>'
        f'<ul><li><b>Free:</b> notes you publish here are kept for <b>{pol["free_days"]} days</b>.</li>'
        f'<li><b>Subscribed:</b> {kept}.</li></ul>'
        f'<p class="fine">This applies only to visitors\' own posts. Accounts on this instance, and '
        f'the profiles, contact lists and DMs of everyone, are never auto-deleted.</p>{buy}'))
    return _retention_cache["html"]


_WELCOME_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{NAME}}</title>
<link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@500;700;900&family=Rajdhani:wght@400;500;600&family=Fira+Code&display=swap" rel="stylesheet">
<style>
:root{--bg:#0a0a0f;--bg2:#12121a;--bg3:#1a1a2a;--accent:#00ffff;--accent2:#ff00ff;--accent3:#ff3366;--text:#e0e0e0;--border:#2a2a3a;}
*{box-sizing:border-box;}
body{margin:0;min-height:100vh;background:linear-gradient(135deg,#0a0a0a 0%,#1a0a2e 50%,#0a0a0a 100%);color:var(--text);font-family:'Rajdhani','Segoe UI',sans-serif;display:flex;align-items:center;justify-content:center;padding:2rem;}
.bg-grid{position:fixed;inset:0;background-image:linear-gradient(rgba(0,255,255,.04) 1px,transparent 1px),linear-gradient(90deg,rgba(0,255,255,.04) 1px,transparent 1px);background-size:32px 32px;z-index:0;pointer-events:none;}
.card{position:relative;z-index:1;max-width:780px;width:100%;background:linear-gradient(180deg,var(--bg2),var(--bg));border:1px solid var(--border);border-radius:14px;padding:2.5rem;box-shadow:0 0 40px rgba(0,255,255,.12),inset 0 0 60px rgba(255,0,255,.03);}
h1{font-family:'Orbitron',sans-serif;font-weight:900;font-size:2.2rem;margin:0 0 .3rem;background:linear-gradient(90deg,var(--accent),var(--accent2));-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent;letter-spacing:2px;}
.sub{color:#9aa;letter-spacing:1px;margin:0 0 1.8rem;font-size:1.05rem;}
h2{font-family:'Orbitron',sans-serif;font-size:1.05rem;color:var(--accent);text-transform:uppercase;letter-spacing:2px;margin:2rem 0 .8rem;text-shadow:0 0 10px rgba(0,255,255,.4);}
.url{font-family:'Fira Code',monospace;font-size:1.25rem;color:var(--accent);background:#05050a;border:1px solid var(--accent);border-radius:10px;padding:1rem 1.2rem;display:flex;align-items:center;justify-content:space-between;gap:1rem;box-shadow:0 0 18px rgba(0,255,255,.15);word-break:break-all;}
.copy{font-family:'Orbitron',sans-serif;cursor:pointer;border:none;border-radius:7px;padding:.5rem .9rem;font-size:.8rem;letter-spacing:1px;color:#0a0a0f;background:linear-gradient(135deg,var(--accent),#00cccc);text-transform:uppercase;white-space:nowrap;}
.copy:hover{box-shadow:0 0 14px var(--accent);}
ol{padding-left:1.2rem;line-height:1.9;}ul{line-height:1.8;}
li b{color:#fff;}
.tag{display:inline-block;font-family:'Fira Code',monospace;font-size:.8rem;color:var(--accent2);border:1px solid var(--accent2);border-radius:5px;padding:.1rem .5rem;margin:.1rem;}
.note{border-left:3px solid var(--accent3);background:rgba(255,51,102,.06);padding:.9rem 1.1rem;border-radius:0 8px 8px 0;margin:1.4rem 0;color:#f0c0cc;}
.note b{color:var(--accent3);}
a{color:var(--accent);}
.foot{margin-top:2rem;padding-top:1.2rem;border-top:1px solid var(--border);color:#778;font-size:.85rem;letter-spacing:1px;text-align:center;}
.clients{display:flex;flex-wrap:wrap;gap:.5rem;margin-top:.4rem;}
.pay{display:flex;flex-wrap:wrap;gap:1.2rem;align-items:flex-start;}
.pay>div{flex:1 1 16rem;min-width:0;}
.qr{background:#fff;padding:.5rem;border-radius:8px;line-height:0;flex:0 0 auto;}
.qr svg{width:150px;height:150px;display:block;}
.mono{font-family:'Fira Code',monospace;font-size:.78rem;color:var(--accent2);word-break:break-all;}
.fine{font-size:.85rem;color:#889;}
.warn{font-size:.85rem;color:#f0c0cc;border-left:3px solid var(--accent3);padding-left:.7rem;}
</style></head>
<body>
<div class="bg-grid"></div>
<div class="card">
  <h1>{{NAME}}</h1>
  <p class="sub">{{DESC}} &mdash; a curated, spam-free Nostr feed.</p>

  <h2>Relay URL</h2>
  <div class="url"><span id="u">{{URL}}</span><button class="copy" onclick="navigator.clipboard.writeText(document.getElementById('u').textContent);this.textContent='COPIED'">Copy</button></div>

  <h2>Connect your client</h2>
  <ol>
    <li>Open your Nostr client's <b>Relays</b> / <b>Settings</b> screen.</li>
    <li>Add a new relay and paste the URL above.</li>
    <li>Save &mdash; you'll start pulling this relay's web-of-trust timeline.</li>
  </ol>
  <div class="clients">
    <span class="tag">Amethyst</span><span class="tag">Damus</span><span class="tag">Primal</span>
    <span class="tag">Nos</span><span class="tag">Snort</span><span class="tag">Gossip</span><span class="tag">noStrudel</span>
  </div>

  <h2>What makes this relay different</h2>
  <ul>
    <li><b>Web of Trust:</b> it only stores notes from a trusted set of authors and the people they follow &mdash; no spam, no noise.</li>
    <li><b>Full threads:</b> missing parent notes are fetched automatically, so replies aren't orphaned.</li>
    <li><b>Profiles included:</b> names &amp; avatars for everyone in the feed are synced for you.</li>
  </ul>

  {{RETENTION}}

  <div class="note"><b>Heads up:</b> reading is open to everyone. <b>Publishing</b> is restricted &mdash; only authors inside the web of trust are accepted, so your client can read here freely but should keep your usual relays for posting.</div>

  <div class="foot">Powered by PosterChanAI &middot; self-hosted Nostr relay</div>
</div>
</body></html>"""
