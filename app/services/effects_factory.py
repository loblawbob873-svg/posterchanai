"""Node load balancer for ffmpeg EFFECT commands (AI Chat / Telegram), mirroring image_factory.

The Meme Builder got its own LB in `client.py` (`_meme_lb_forward`), but the effect commands run from
AI Chat go through CommandService → effects_service, which had no cross-node path at all: every glow /
alive / nakedman / meme render ran on whichever node happened to hold the chat websocket, while the
rest of the fleet sat idle.

Same policy as image generation: round-robin over [this node] + the peers in the unified
`chat_server_urls` list. The receiving node runs the command LOCALLY and hands the produced files
back as base64 — so a render peer needs ffmpeg and nothing else (no blob store, no chat session).
Any peer failure falls through to the next candidate, and finally to local, so an offline node can
only cost a round trip, never a render.
"""
import asyncio
from app.utils import lb_auth
import base64
import logging
from typing import List, Optional

logger = logging.getLogger("effects_factory")

_LOCAL = "__local__"
_rr_index = [0]

# An effect is a short ffmpeg job, but `alive`/`nakedman` on a big still can take a while, and the
# peer may be queueing behind another render.
_TIMEOUT_S = 300.0


def _peers(raw: str) -> List[str]:
    """Peer node URLs from the unified node list, EXCLUDING this node (it is `_LOCAL` in the
    rotation — leaving its own address here would forward work to itself)."""
    if not raw:
        return []
    from app.services.load_balancer import parse_server_urls
    return parse_server_urls(raw, exclude_self=True)


def _encode(files: list) -> list:
    """CommandService attachments/files → JSON-safe dicts."""
    out = []
    for f in files or []:
        if isinstance(f, dict):
            name, data, ct = f.get("filename"), f.get("data"), f.get("content_type")
        else:                                   # attachments are (filename, bytes, content_type)
            name, data, ct = f[0], f[1], f[2]
        if not data:
            continue
        out.append({"filename": name or "file", "content_type": ct or "application/octet-stream",
                    "data": base64.b64encode(data).decode()})
    return out


def _decode_files(items: list) -> list:
    """The wire form back into the {filename,data,content_type} dicts a command result carries."""
    out = []
    for it in items or []:
        try:
            out.append({"filename": it.get("filename") or "file",
                        "content_type": it.get("content_type") or "application/octet-stream",
                        "data": base64.b64decode(it.get("data") or "")})
        except Exception:
            continue
    return [f for f in out if f["data"]]


def next_candidate(raw_urls: str) -> Optional[str]:
    """The node whose turn it is: None for LOCAL, else a peer URL. Advances the rotation.

    Deliberately unconditional round-robin rather than an "only when busy" heuristic — the
    Meme Builder LB shipped twice with a busy gate (all slots full, then any slot busy) and both
    times the gate almost never opened, so every job stayed on one node. Rotating is what actually
    spreads the fleet."""
    peers = _peers(raw_urls)
    if not peers:
        return None
    turn = _rr_index[0] % (len(peers) + 1)
    _rr_index[0] += 1
    return None if turn == 0 else peers[turn - 1]


async def run_effect_on_node(node_url: str, command: str, arg: str, attachments: list) -> Optional[dict]:
    """Run one effect command on a peer node. Returns its command-result dict, or None to fall back."""
    import httpx
    payload = {"command": command, "arg": arg or "", "files": _encode(attachments)}
    headers = lb_auth.headers()
    async with httpx.AsyncClient(timeout=httpx.Timeout(_TIMEOUT_S, connect=8.0)) as c:
        r = await c.post("%s/api/effects/run" % node_url.rstrip("/"), json=payload, headers=headers)
    if r.status_code >= 400:
        logger.warning("[EFFECTS] node %s returned HTTP %s", node_url, r.status_code)
        return None
    data = r.json()
    if data.get("error"):
        logger.warning("[EFFECTS] node %s error: %s", node_url, data["error"])
        return None
    if data.get("type") == "files":
        files = _decode_files(data.get("files"))
        if not files:
            return None
        return {"type": "files", "content": data.get("content") or "", "files": files}
    if data.get("type") == "text":
        # The peer ran it and refused with a message ("attach an image", unknown modifier, …) —
        # a real answer, not a failure. Re-running locally would only repeat it.
        return {"type": "text", "content": data.get("content") or ""}
    return None


async def run_effect_balanced(raw_urls: str, command: str, arg: str, attachments: list) -> Optional[dict]:
    """Take this command's turn in the rotation. Returns a command-result dict when a PEER handled
    it, or None meaning 'run it locally' (local's turn, no peers, or every peer failed)."""
    peer = next_candidate(raw_urls)
    if not peer:
        return None
    try:
        logger.info("[EFFECTS] forwarding %s to remote node %s", command, peer)
        result = await run_effect_on_node(peer, command, arg, attachments)
        if result is not None:
            return result
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning("[EFFECTS] node %s failed (%s): %s — running locally", peer, command, e)
    return None
