"""NIP-01 relay server: WebSocket EVENT/REQ/CLOSE/COUNT + live subscription fan-out,
plus the NIP-11 relay info document served over a plain HTTP GET.

Runs on the relay thread's own asyncio loop (see thread.py). All persistence goes through
`RelayStore` (off-loop executors), and every write is gated by the WoT (`gate.is_member`)
so only web-of-trust pubkeys are ever accepted.
"""

import json
import asyncio
import logging

from websockets.datastructures import Headers
from websockets.http11 import Response

from app.services.nostr.event import verify_event
from .langfilter import blocked_language, blocked_word

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

    async def fanout(self, ev: dict) -> None:
        for conn, subs in list(self._subs.items()):
            for sub_id, filters in list(subs.items()):
                if _matches(filters, ev):
                    try:
                        await conn.send(json.dumps(["EVENT", sub_id, ev]))
                    except Exception:
                        self.remove_conn(conn)
                        break


class RelayServer:
    def __init__(self, store, gate, config: dict, outbox_cb=None):
        self.store = store
        self.gate = gate                 # .is_member(pubkey) -> bool
        self.cfg = config
        self.outbox_cb = outbox_cb       # async fn(event) | None (Phase 4)
        self.subs = SubscriptionManager()
        self._conns = 0

    # --- NIP-11 -------------------------------------------------------------

    def nip11_doc(self) -> bytes:
        c = self.cfg
        doc = {
            "name": c.get("name") or "PosterChanAI Relay",
            "description": c.get("description") or "Web-of-trust relay",
            "software": "https://github.com/loblawbob873-svg/posterchanai",
            "supported_nips": [1, 9, 11],
            "limitation": {
                "max_message_length": c.get("max_message_size", 262144),
                "max_subscriptions": c.get("max_subs_per_conn", 20),
                "max_filters": c.get("max_filters_per_req", 10),
                "restricted_writes": True,
            },
        }
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
            return Response(200, "OK", headers, self.nip11_doc())
        headers = Headers({"Content-Type": "text/html; charset=utf-8"})
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
        try:
            async for raw in conn:
                await self._dispatch(conn, raw)
        except Exception:
            pass
        finally:
            self.subs.remove_conn(conn)
            self._conns -= 1

    async def _dispatch(self, conn, raw) -> None:
        if isinstance(raw, (bytes, bytearray)):
            if len(raw) > self.cfg.get("max_message_size", 262144):
                return
            raw = raw.decode("utf-8", "replace")
        elif len(raw) > self.cfg.get("max_message_size", 262144):
            await conn.send(json.dumps(["NOTICE", "message too large"]))
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

    async def _on_event(self, conn, ev) -> None:
        if not isinstance(ev, dict) or "id" not in ev:
            return
        eid = ev.get("id", "")
        if not verify_event(ev):
            await conn.send(json.dumps(["OK", eid, False, "invalid: bad id or signature"]))
            return
        if not self.gate.is_member(ev.get("pubkey", "")):
            await conn.send(json.dumps(["OK", eid, False,
                                        "blocked: not in web of trust"]))
            return
        if int(ev.get("kind", 1)) == 1:
            content = ev.get("content", "")
            blocked = self.cfg.get("blocked_langs")
            if blocked:
                lang = blocked_language(content, blocked)
                if lang:
                    await conn.send(json.dumps(["OK", eid, False,
                                                f"blocked: language '{lang}' not accepted"]))
                    return
            words = self.cfg.get("blocked_words")
            if words and blocked_word(content, words):
                await conn.send(json.dumps(["OK", eid, False,
                                            "blocked: contains filtered text"]))
                return
        stored = await self.store.add_event(ev, origin="wot")
        await conn.send(json.dumps(["OK", eid, True, ""]))
        if stored:
            await self.subs.fanout(ev)
            if self.outbox_cb:
                # Non-blocking enqueue onto the paced outbox queue (drops on overflow) — a
                # post-blasting client can't stall this connection or flood upstream relays.
                try:
                    self.outbox_cb(ev)
                except Exception as e:
                    logger.debug("[nostr-relay] outbox enqueue failed: %s", e)

    async def _on_req(self, conn, sub_id, filters) -> None:
        if not isinstance(sub_id, str):
            return
        if self.subs.count(conn) >= self.cfg.get("max_subs_per_conn", 20):
            await conn.send(json.dumps(["CLOSED", sub_id, "rate-limited: too many subscriptions"]))
            return
        filters = [f for f in filters if isinstance(f, dict)][: self.cfg.get("max_filters_per_req", 10)]
        events = await self.store.query(filters)
        for ev in reversed(events):  # send oldest-first, newest last (common client expectation)
            await conn.send(json.dumps(["EVENT", sub_id, ev]))
        await conn.send(json.dumps(["EOSE", sub_id]))
        self.subs.add(conn, sub_id, filters)

    async def _on_count(self, conn, sub_id, filters) -> None:
        filters = [f for f in filters if isinstance(f, dict)]
        events = await self.store.query(filters)
        await conn.send(json.dumps(["COUNT", sub_id, {"count": len(events)}]))


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
