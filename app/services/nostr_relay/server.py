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

from websockets.datastructures import Headers
from websockets.http11 import Response

from app.services.nostr.event import verify_event
from .langfilter import blocked_language, blocked_word
from .bridges import reveals_blocked_bridge, author_on_blocked_bridge, is_bridged_post


# pcai: CONFIG d-tags eligible for the opt-in DR backup to upstream (small + critical). Bulky /
# per-item docs (chat conversations/messages, upload refs, drafts, ai-requests) are NEVER broadcast.
_BACKUP_NS = ("pcai:setting:", "pcai:user:", "pcai:usercfg:", "pcai:bot:")


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

    def fanout(self, ev: dict, send) -> None:
        """Enqueue `ev` to every matching open subscription via `send(conn, obj)` (a
        non-blocking, drop-on-slow enqueue) — so one slow client can't stall the firehose."""
        for conn, subs in list(self._subs.items()):
            for sub_id, filters in list(subs.items()):
                if _matches(filters, ev):
                    send(conn, ["EVENT", sub_id, ev])


class RelayServer:
    def __init__(self, store, gate, config: dict, outbox_cb=None):
        self.store = store
        self.gate = gate                 # .is_member(pubkey) -> bool
        self.cfg = config
        self.outbox_cb = outbox_cb       # async fn(event) | None (Phase 4)
        self.subs = SubscriptionManager()
        self._conns = 0
        self._neg: dict = {}   # conn -> {sub_id: negentropy item set} (NIP-77 sessions)
        self._outq: dict = {}  # conn -> bounded outbound queue (decouples slow clients)
        self._conn_ips: dict = {}  # conn -> client IP (for the deduped "online people" estimate)
        # Fediverse-bridge NIP-05: local-part -> puppet pubkey, populated as puppet kind-0 profiles
        # are stored (and warmed from the store at startup). Resolved on ?name= lookups only; never
        # enumerated in the no-name nostr.json dump (there can be tens of thousands).
        self._bridge_nip05: dict = {}
        self._bridge_pubkeys: set = set()   # puppet pubkeys (values of _bridge_nip05) — DM inbox set

    def _send(self, conn, obj) -> None:
        """Enqueue a message to a client WITHOUT blocking. If the client is too slow and its
        queue is full, drop the message (live events are re-pullable) — a slow consumer must
        never stall the firehose/fanout or other clients."""
        q = self._outq.get(conn)
        if q is None:
            return
        try:
            q.put_nowait(json.dumps(obj))
        except asyncio.QueueFull:
            pass

    async def _writer(self, conn, q) -> None:
        """Drain one connection's outbound queue at the client's own pace."""
        try:
            while True:
                msg = await q.get()
                await conn.send(msg)
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
        return json.dumps(doc).encode("utf-8")

    def _register_bridge_nip05(self, ev: dict) -> None:
        """Record a fediverse puppet's NIP-05 local-part → pubkey from its just-stored kind-0, in the
        live map (served immediately) and the persistent table (warmed on the next start). The
        local-part is the part of the profile's `nip05` before '@' (we only serve OUR domain)."""
        try:
            meta = json.loads(ev.get("content") or "{}")
        except (ValueError, TypeError):
            return
        nip05 = (meta.get("nip05") or "").strip().lower()
        local = nip05.split("@", 1)[0] if nip05 else ""
        pk = ev.get("pubkey", "")
        if not local or not pk:
            return
        if self._bridge_nip05.get(local) == pk:
            return
        self._bridge_nip05[local] = pk
        self._bridge_pubkeys.add(pk)
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
        self._bridge_pubkeys = set(self._bridge_nip05.values())
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
                # Capture the real client IP for the deduped "online" count. We sit behind nginx, so
                # remote_address is the proxy — prefer the forwarded client IP (first XFF hop).
                try:
                    xff = hdrs.get("X-Forwarded-For", "") or hdrs.get("X-Real-IP", "")
                    ip = xff.split(",")[0].strip() if xff else ""
                    if not ip:
                        ra = getattr(connection, "remote_address", None)
                        ip = ra[0] if ra else ""
                    setattr(connection, "_pcai_ip", ip)
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
            "{{DESC}}", desc).encode("utf-8")

    # --- connection handling ------------------------------------------------

    async def handle(self, conn) -> None:
        if self._conns >= self.cfg.get("max_connections", 5000):
            await conn.close(code=1013, reason="overloaded")
            return
        self._conns += 1
        self._conn_ips[conn] = getattr(conn, "_pcai_ip", "") or ""
        # Bigger than the query hard_cap (5000) so a full-page REQ response + its EOSE always
        # fit without the synchronous send loop overflowing the queue and dropping the EOSE.
        q = asyncio.Queue(maxsize=self.cfg.get("outq_size", 8192))
        self._outq[conn] = q
        writer = asyncio.create_task(self._writer(conn, q))
        try:
            async for raw in conn:
                await self._dispatch(conn, raw)
        except Exception as e:
            if "ConnectionClosed" not in type(e).__name__:
                logger.debug("[nostr-relay] connection handler error: %r", e)
        finally:
            writer.cancel()
            self._outq.pop(conn, None)
            self._conn_ips.pop(conn, None)
            self.subs.remove_conn(conn)
            self._neg.pop(conn, None)
            self._conns -= 1

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

    async def _on_event(self, conn, ev) -> None:
        if not isinstance(ev, dict) or "id" not in ev:
            return
        eid = ev.get("id", "")
        if not verify_event(ev):
            self._send(conn, ["OK", eid, False, "invalid: bad id or signature"])
            return
        kind = int(ev.get("kind", 1))
        # NIP-40: reject an event already past its expiration (don't store or relay it).
        exp = _event_expiration(ev)
        if exp is not None and exp <= int(time.time()):
            self._send(conn, ["OK", eid, False, "invalid: event expired"])
            return
        # Bridge blocklist: an account on a blocked bridge relay (mostr.pub etc.) is denied; learning
        # it from this event's profile/relay-list also bars everything else it posts (via is_member).
        # Our own fediverse-bridge puppets legitimately carry a NIP-48 proxy tag (pointing at the
        # original fedi note) and post mirrored content — they must skip the bridge/proxy denials
        # below, which exist to block OTHER instances' mirror accounts (mostr.pub etc.).
        _is_puppet = self.gate.is_puppet_event(ev)
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
        elif kind in self.cfg.get("dvm_req_kinds", ()) and ev.get("pubkey", "") in self.cfg.get("dvm_allowed", ()):
            # DVM compute job from a SHARE-ALLOWLISTED sender: accepted even if not a WoT member —
            # sharing your GPU is a deliberate per-npub grant, separate from the social web of trust.
            # The DVM worker re-checks the same allowlist (is_trusted) before running anything.
            pass
        elif _is_puppet and kind in (0, 1, 5, 6, 7):
            # Fediverse-bridge puppet: the app mirrors the global fediverse timeline through these
            # deterministic per-fedi-user keys. Gate-exempt for the mirrored content kinds only
            # (profile / note / repost / reaction / NIP-09 deletion), and ONLY here on the loopback WS
            # publish path — they are not WoT members, so the upstream sync/firehose never accept them.
            # A valid signature is still required (verify_event above) and the event must self-validate
            # as a puppet (fedibridge actor tag → derived pubkey == signer). A kind-5 deletes only the
            # puppet's OWN events (NIP-09), and federates upstream iff bridge broadcast is on (no
            # nofederate tag) — so a fediverse delete propagates everywhere the mirror reached.
            pass
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
        _origin = "bridge" if _is_puppet else "direct"
        stored = await self.store.add_event(ev, origin=_origin)
        self._send(conn, ["OK", eid, True, ""])
        if stored:
            if _is_puppet and kind == 0:
                self._register_bridge_nip05(ev)   # serve this puppet's <name>@host identity
            self.subs.fanout(ev, self._send)
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
        # subs also enqueuing) could overflow the queue and drop the trailing EOSE.
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

  <div class="note"><b>Heads up:</b> reading is open to everyone. <b>Publishing</b> is restricted &mdash; only authors inside the web of trust are accepted, so your client can read here freely but should keep your usual relays for posting.</div>

  <div class="foot">Powered by PosterChanAI &middot; self-hosted Nostr relay</div>
</div>
</body></html>"""
