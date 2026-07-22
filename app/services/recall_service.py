"""Semantic recall over your own words — `recall <question>`.

"What did I say about the Arc VAE fix?" answered across your chat history and your own Nostr notes,
by MEANING rather than keyword.

Built for a GPU-poor node: the expensive half runs on the CPU. Embeddings come from
all-MiniLM-L6-v2 (22M params, 384 dims, ~90MB, already in the HF cache) pinned to CPU, so indexing
and search never touch the GPU or queue behind a chat generation. The only GPU work is ONE short
grounded answer at the end — the same cost as any brief reply.

No vector database either: 384 float32s is 1536 bytes, so a user's whole index loads in one query
and similarity is a numpy dot product. At tens of thousands of messages that is still milliseconds,
and it keeps the deployment story at "no new services".
"""
import logging
import re
import struct
import time

from sqlalchemy.orm import Session

from app.models import Conversation, Message, RecallVector, User

logger = logging.getLogger(__name__)

MODEL_NAME = "all-MiniLM-L6-v2"
DIM = 384
MAX_TEXT = 1200        # per row; longer messages are truncated for the embedding AND the snippet
TOP_K = 6
NOSTR_LIMIT = 500      # how many of your own notes to pull from the relay per index pass
MIN_CHARS = 12         # "ok", "thanks" etc. carry no meaning worth indexing

_model = None


class RecallUnavailable(RuntimeError):
    """sentence-transformers isn't installed. Deliberately OPTIONAL: it pulls torch, which is NOT a
    base dependency here (each GPU stack installs its own), so making it required would add ~2GB to
    every lean/Nostr-only install for a feature they may never use."""


def _get_model():
    """Load the sentence-transformer once, on CPU. Imported lazily: pulling in torch at module import
    would add seconds to every app start for a feature most requests never touch."""
    global _model
    if _model is None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as e:
            raise RecallUnavailable(
                "recall needs the sentence-transformers package, which isn't installed on this "
                "server. Enable it with `./install.sh --recall` (adds ~90MB model, CPU-only)."
            ) from e
        _model = SentenceTransformer(MODEL_NAME, device="cpu")
    return _model


def _encode(texts: list[str]):
    import numpy as np
    m = _get_model()
    vecs = m.encode(texts, batch_size=32, show_progress_bar=False,
                    convert_to_numpy=True, normalize_embeddings=True)   # normalized → dot == cosine
    return np.asarray(vecs, dtype="float32")


def _pack(v) -> bytes:
    return v.astype("float32").tobytes()


def _unpack_matrix(rows):
    import numpy as np
    if not rows:
        return None
    return np.frombuffer(b"".join(r.vec for r in rows), dtype="float32").reshape(len(rows), DIM)


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def _worth_indexing(txt: str) -> bool:
    return len(txt) >= MIN_CHARS


async def _nostr_notes(db: Session, user: User) -> list[dict]:
    """The user's OWN kind-1 notes from the local relay ([] when they have no linked npub)."""
    npub = getattr(user, "nostr_npub", None)
    if not npub:
        return []
    try:
        from app.services import settings_store
        from app.services.nostr import nostr_service
        from app.services.nostr_store import _ws_query
        pk = nostr_service.to_pubkey_hex(npub)
        if not pk:
            return []
        port = settings_store.get_int("nostr_relay_port", 3052)
        evs = await _ws_query(port, [{"authors": [pk], "kinds": [1], "limit": NOSTR_LIMIT}], timeout=8.0)
        return evs or []
    except Exception as e:
        logger.debug("[recall] nostr fetch failed: %s", e)
        return []


async def index_user(db: Session, user: User) -> int:
    """Embed anything of the user's that isn't indexed yet. Incremental — returns how many were added."""
    have = {(r.source, r.ref_id) for r in
            db.query(RecallVector.source, RecallVector.ref_id).filter(RecallVector.user_id == user.id).all()}

    pending: list[dict] = []

    rows = (db.query(Message, Conversation.id)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .filter(Conversation.user_id == user.id).all())
    for msg, conv_id in rows:
        if ("chat", str(msg.id)) in have:
            continue
        txt = _clean(msg.content)[:MAX_TEXT]
        if not _worth_indexing(txt):
            continue
        ts = int(msg.created_at.timestamp()) if msg.created_at else 0
        pending.append({"source": "chat", "ref_id": str(msg.id), "conversation_id": conv_id,
                        "text": txt, "ts": ts})

    for ev in await _nostr_notes(db, user):
        eid = ev.get("id") or ""
        if not eid or ("nostr", eid) in have:
            continue
        txt = _clean(ev.get("content"))[:MAX_TEXT]
        if not _worth_indexing(txt):
            continue
        pending.append({"source": "nostr", "ref_id": eid, "conversation_id": None,
                        "text": txt, "ts": int(ev.get("created_at") or 0)})

    if not pending:
        return 0
    import asyncio
    vecs = await asyncio.to_thread(_encode, [p["text"] for p in pending])   # CPU work off the loop
    for p, v in zip(pending, vecs):
        db.add(RecallVector(user_id=user.id, vec=_pack(v), **p))
    db.commit()
    logger.info("[recall] indexed %d new items for user %s", len(pending), user.id)
    return len(pending)


async def search(db: Session, user: User, question: str, k: int = TOP_K) -> list[dict]:
    """Top-k of the user's own items by cosine similarity to the question."""
    import asyncio
    import numpy as np
    rows = db.query(RecallVector).filter(RecallVector.user_id == user.id).all()
    mat = _unpack_matrix(rows)
    if mat is None:
        return []
    qv = (await asyncio.to_thread(_encode, [question]))[0]
    sims = mat @ qv                                     # both sides normalized → cosine
    order = np.argsort(-sims)[:k]
    out = []
    for i in order:
        r = rows[int(i)]
        out.append({"score": float(sims[int(i)]), "source": r.source, "ref_id": r.ref_id,
                    "conversation_id": r.conversation_id, "text": r.text, "ts": r.ts})
    return out


def _when(ts: int) -> str:
    if not ts:
        return ""
    d = time.time() - ts
    if d < 3600:
        return f"{int(d // 60)}m ago"
    if d < 86400:
        return f"{int(d // 3600)}h ago"
    return f"{int(d // 86400)}d ago"


def format_hits(hits: list[dict]) -> str:
    lines = []
    for h in hits:
        where = "note" if h["source"] == "nostr" else "chat"
        lines.append(f"• [{where}, {_when(h['ts'])}] {h['text'][:220]}")
    return "\n".join(lines)
