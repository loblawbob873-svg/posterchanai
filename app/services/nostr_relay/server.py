"""NIP-01 relay server: WebSocket EVENT/REQ/CLOSE/COUNT + live subscription fan-out,
plus the NIP-11 relay info document served over a plain HTTP GET.

Runs on the relay thread's own asyncio loop (see thread.py). All persistence goes through
`RelayStore` (off-loop executors), and every write is gated by the WoT (`gate.is_member`)
so only web-of-trust pubkeys are ever accepted.
"""

import re
import json
import asyncio
import logging

from websockets.datastructures import Headers
from websockets.http11 import Response

from app.services.nostr.event import verify_event
from .langfilter import blocked_language, blocked_word
from . import negentropy

logger = logging.getLogger(__name__)


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
            # 44 encryption, 45 COUNT, 50 search, 59 gift wrap, 65 relay-list
            "supported_nips": [1, 2, 9, 11, 17, 22, 23, 44, 45, 50, 59, 65],
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

    def process_request(self, connection, request):
        """Route the opening handshake: WebSocket upgrade → proceed (None); a NIP-11
        request (`Accept: application/nostr+json`) → the info doc; any other browser GET →
        the cyberpunk welcome page explaining how to connect a client."""
        try:
            hdrs = request.headers
            if hdrs.get("Upgrade", "").lower() == "websocket":
                return None  # let the WebSocket handshake proceed
            accept = hdrs.get("Accept", "")
            host = hdrs.get("Host", "") or f"{self.cfg.get('bind','')}:{self.cfg.get('port','')}"
            # The reverse proxy preserves the public path (e.g. /relay), so the welcome page
            # advertises the exact wss URL clients should use, not a guessed root.
            path = (getattr(request, "path", "") or "/relay").split("?", 1)[0]
        except Exception:
            return None
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
            self.subs.remove_conn(conn)
            self._neg.pop(conn, None)
            self._conns -= 1

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

    async def _on_event(self, conn, ev) -> None:
        if not isinstance(ev, dict) or "id" not in ev:
            return
        eid = ev.get("id", "")
        if not verify_event(ev):
            self._send(conn, ["OK", eid, False, "invalid: bad id or signature"])
            return
        kind = int(ev.get("kind", 1))
        is_dm = kind in self._DM_KINDS
        if is_dm:
            if not self._dm_for_operator(ev):
                self._send(conn, ["OK", eid, False, "blocked: not a DM to a relay user"])
                return
        elif not self.gate.is_member(ev.get("pubkey", "")):
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
        # origin="direct": a client chose THIS relay as a destination (entrusted data), as
        # opposed to "wot" (a mirror of the public feed we pulled via sync/firehose). Prune
        # keeps direct writes forever and only trims the reconstructable synced feed.
        stored = await self.store.add_event(ev, origin="direct")
        self._send(conn, ["OK", eid, True, ""])
        if stored:
            self.subs.fanout(ev, self._send)
            if self.outbox_cb:
                # Blaster: re-broadcast EVERY inbound write to the upstream relays — notes,
                # profile updates, articles, AND DMs. DMs are encrypted (gift wraps / NIP-04),
                # so broadcasting leaks no content and is what delivers them to recipients when
                # a user treats this as their only relay. Non-blocking enqueue (drops on
                # overflow) so a post-blasting client can't stall this connection.
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
        events = await self.store.query(filters)
        self._send(conn, ["COUNT", sub_id, {"count": len(events)}])

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
