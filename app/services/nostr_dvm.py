"""Nostr-native distributed load balancer — a NIP-90-style Data Vending Machine.

Replaces the IP/HTTP round-robin LB. Instead of forwarding a job to a peer over HTTP, the COORDINATOR
publishes an encrypted job-request event to a shared relay, `p`-tagged to a chosen WORKER node's npub.
That worker — listening for jobs addressed to it FROM A TRUSTED npub — runs the job on its GPU through
the EXISTING local-execution paths (`generate_image_with_load_balancing(local_only=True)`,
`get_inference_service().chat_completion()`, …) and publishes an encrypted RESULT event back, which the
coordinator awaits. Full-response (no token streaming) for now.

This is the groundwork for open distributed AI over Nostr: the same protocol serves the OpenAI-API
path, the ai.poster.place web client, or any third-party DVM client/worker later.

Design (deliberately simple — "listen for events from a trusted npub, process, return"):
  * A node's DVM identity IS its INSTALLATION key — the operator/relay nsec already in the keystore,
    which is distinct per node and already Blossom-authorized. No separate worker key to generate/set.
  * The cluster = the relay's Web-of-Trust SEED npubs (Admin → Relay). Peers = seeds − self; the same
    seeds are the trusted-requester allowlist, and the relay's WoT gate already accepts their events.
  * Job request kinds are per task (NIP-90 5xxx range); result kind = request + 1000. Payload is
    NIP-44-encrypted to the recipient and the result is signature-verified before use.
"""

import asyncio
import base64
import collections
import hashlib
import json
import logging
import os
import time
from typing import Optional

import httpx

from app.services import keystore, settings_store
from app.services.nostr import bip340
from app.services.nostr import event as nostr_event
from app.services.nostr import nip44
from app.services.nostr import nostr_service
from app.services.nostr import relay as nostr_relay

logger = logging.getLogger(__name__)

# task -> request kind (NIP-90 5xxx range). 5050/6050 are the NIP-90 text-gen kinds; 5100/6100 image;
# 5201/5202 are cluster-local DVM kinds for music/video (no NIP-90 standard). Result = request + 1000.
_REQ_KIND = {"chat": 5050, "image": 5100, "music": 5201, "video": 5202, "agent": 5300}
_TASK_BY_REQ = {v: k for k, v in _REQ_KIND.items()}
DVM_REQ_KINDS = frozenset(_REQ_KIND.values())
DVM_KINDS = frozenset(list(_REQ_KIND.values()) + [k + 1000 for k in _REQ_KIND.values()])

# Per-task wait budget (seconds) — the coordinator gives up and falls back to local/next after this.
_JOB_TIMEOUT = {"chat": 180, "image": 300, "music": 600, "video": 900, "agent": 900}

# node/agent tasks (kind 5300) run OS commands / Claude — a bigger grant than GPU offload, so they use a
# DEDICATED allowlist (node_exec_trusted_npubs) and run ONE-AT-A-TIME (queued, never bounced to a peer:
# the command must run on the addressed node). Serialized by this lock, created lazily in the loop.
_agent_lock: "Optional[asyncio.Lock]" = None


# ---------------------------------------------------------------- identity
def node_seckey() -> Optional[bytes]:
    """This node's DVM identity = its RELAY IDENTITY: the operator nsec in the keystore (the same key
    shown in Admin → Relay). It's distinct per install and already Blossom-authorized, so there's no
    separate worker key to generate or set. Returns None only if the relay identity isn't set yet."""
    op = keystore.get_operator_nsec()
    return nostr_service.decode_seckey(op) if op else None


def node_pubkey() -> Optional[str]:
    sk = node_seckey()
    return bip340.pubkey_from_seckey(sk).hex() if sk else None


def node_npub() -> Optional[str]:
    pk = node_pubkey()
    return nostr_service.npub_of(pk) if pk else None


# ---------------------------------------------------------------- settings
def _settings(settings: Optional[dict] = None) -> dict:
    return settings if settings is not None else settings_store.all_settings()


def _truthy(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def is_enabled(settings: Optional[dict] = None) -> bool:
    return _truthy(_settings(settings).get("nostr_dvm_enabled", False))


def peers_list(settings: Optional[dict] = None) -> list:
    """The SHARED CLUSTER — peers you share compute with. Each non-blank line of `nostr_dvm_peers` is a
    connection card `npub relay`: a peer's npub and the relay to reach it on. Sharing is MUTUAL — if you
    list a peer, they may send jobs to THIS node (trusted) AND this node may offload to them. This is a
    deliberate grant, separate from the social Web of Trust (a follow does NOT grant GPU access). Blossom
    is self-describing (each result ref carries the uploader's URL), so a card needs only npub + relay.
    Returns [{pubkey, npub, relay}] (relay '' if a line gives just an npub). Empty → no sharing."""
    out: list = []
    seen: set = set()
    for line in (_settings(settings).get("nostr_dvm_peers", "") or "").replace(",", "\n").splitlines():
        parts = line.split()
        if not parts:
            continue
        pk = nostr_service.to_pubkey_hex(parts[0])
        if not pk or pk in seen:
            continue
        seen.add(pk)
        out.append({"pubkey": pk, "npub": parts[0].strip(), "relay": parts[1].strip() if len(parts) > 1 else ""})
    return out


def peer_pubkeys(settings: Optional[dict] = None) -> list:
    """Pubkeys of all shared-cluster peers — the TRUST set (who may send jobs to this node)."""
    return [p["pubkey"] for p in peers_list(settings)]


def is_trusted(pubkey_hex: str, settings: Optional[dict] = None) -> bool:
    """A node serves jobs ONLY from peers in its shared cluster — you listed them, so it's a deliberate
    grant, never a side effect of a social follow. No peers → serve no one."""
    return pubkey_hex in peer_pubkeys(settings)


# ---- node/agent-over-Nostr trust + config (DEDICATED allowlist, NOT the DVM peer cluster above) ----
def agent_worker_enabled(settings: Optional[dict] = None) -> bool:
    """True if THIS node accepts node/agent commands over Nostr (runs them locally for trusted controllers)."""
    return _truthy(_settings(settings).get("node_exec_nostr_enabled", False))


def agent_trusted_pubkeys(settings: Optional[dict] = None) -> list:
    """Controllers allowed to run commands on THIS node — `node_exec_trusted_npubs`, one npub/hex per line
    or comma-separated. A bigger grant than GPU offload, so it's a separate list from the DVM peers."""
    out = []
    for line in (_settings(settings).get("node_exec_trusted_npubs", "") or "").replace(",", "\n").splitlines():
        line = line.strip()
        if line:
            pk = nostr_service.to_pubkey_hex(line)
            if pk:
                out.append(pk)
    return out


def is_agent_trusted(pubkey_hex: str, settings: Optional[dict] = None) -> bool:
    return pubkey_hex in agent_trusted_pubkeys(settings)


def agent_node_map(settings: Optional[dict] = None) -> dict:
    """Controller side: node name -> worker pubkey hex, from `node_exec_node_npubs` (`name npub1… [relay]`)."""
    m: dict = {}
    for line in (_settings(settings).get("node_exec_node_npubs", "") or "").splitlines():
        parts = line.split()
        if len(parts) >= 2:
            pk = nostr_service.to_pubkey_hex(parts[1])
            if pk:
                m[parts[0].strip()] = pk
    return m


def agent_node_relay(pubkey_hex: str, settings: Optional[dict] = None) -> Optional[str]:
    """Controller side: the relay a WORKER listens on — the OPTIONAL 3rd field of a `node_exec_node_npubs`
    line (`name npub1… ws://host:3052/relay`). A full node subscribes ONLY to its own local relay, so the
    controller must publish the job event THERE (not to its own relay — the two relays don't cross-deliver
    a `nofederate` cluster event). Returns None when no relay is given: a relay-less STANDALONE agent
    connects out to the controllers' relays, so the default local relay reaches it."""
    for line in (_settings(settings).get("node_exec_node_npubs", "") or "").splitlines():
        parts = line.split()
        if len(parts) >= 3 and nostr_service.to_pubkey_hex(parts[1]) == pubkey_hex and parts[2].strip():
            return parts[2].strip()
    return None


def providers(settings: Optional[dict] = None) -> list:
    """Peers this node can OFFLOAD to — the same mutual cluster, limited to peers that gave a relay (so
    we can reach them). Sharing is symmetric: the trust set and the offload set are one list. Gated by
    the master toggle: when shared compute is OFF this node neither serves (no worker) nor offloads."""
    if not is_enabled(settings):
        return []
    return [p for p in peers_list(settings) if p["relay"]]


def relay_url(settings: Optional[dict] = None) -> str:
    """Jobs/results always ride THIS node's local relay. Cross-node delivery is the relay graph's
    job: when DVM is on, each node's firehose streams the cluster job/result kinds addressed to it
    from its WoT upstream(s) (the paired nodes), so a publish to the local relay reaches the peer's
    local relay via the existing pairing — no separate shared job relay to configure or keep in sync."""
    s = _settings(settings)
    port = s.get("nostr_relay_port", 3052) or 3052
    return f"ws://127.0.0.1:{port}/relay"


# ---------------------------------------------------------------- media transport (Blossom)
# Image/music/video results are far too big to inline in a Nostr event (the relay caps messages at
# 256KB), so the worker uploads the bytes to a SHARED Blossom server and sends only the sha256 over
# Nostr; the coordinator fetches + verifies the blob. Chat (small text) stays inline.
def _blossom_base(settings: Optional[dict] = None) -> str:
    """Base URL of the shared Blossom server the cluster ships media through. Defaults to this node's
    configured public Blossom URL; every node must point at ONE Blossom server reachable by all."""
    s = _settings(settings)
    return ((s.get("nostr_dvm_blossom_url", "") or s.get("blossom_public_url", "")) or "").rstrip("/")


def _blossom_auth(sk: bytes, verb: str, sha256: str) -> str:
    """BUD-01 Authorization header: a kind-24242 event (t=verb, x=sha256, future expiration) signed by
    this node's relay identity (which is Blossom-authorized)."""
    ev = nostr_event.build_event(sk, 24242, "",
        [["t", verb], ["x", sha256], ["expiration", str(int(time.time()) + 300)]])
    return "Nostr " + base64.b64encode(json.dumps(ev).encode()).decode()


async def upload_media(data: bytes, mime: str, settings: Optional[dict] = None) -> Optional[str]:
    """Upload result bytes to the shared Blossom server; return the sha256, or None on failure."""
    base, sk = _blossom_base(settings), node_seckey()
    if not base or not sk:
        logger.warning("[dvm] no Blossom URL or relay identity — cannot ship media")
        return None
    sha = hashlib.sha256(data).hexdigest()
    try:
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.put(base + "/upload", content=data,
                            headers={"Authorization": _blossom_auth(sk, "upload", sha),
                                     "Content-Type": mime, "X-No-Mirror": "true"})
        if r.status_code >= 400:
            logger.warning("[dvm] blossom upload failed %s: %s", r.status_code, r.text[:200])
            return None
        return sha
    except Exception as e:
        logger.warning("[dvm] blossom upload error: %s", e)
        return None


async def download_media(sha256: str, settings: Optional[dict] = None, base: Optional[str] = None) -> Optional[bytes]:
    """Fetch result bytes from a Blossom server and verify the hash; None on failure. `base` overrides
    the server (the ref's own `url` when fetching a remote provider's result); else this node's."""
    base = (base or _blossom_base(settings) or "").rstrip("/")
    if not base:
        return None
    try:
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as c:
            r = await c.get(base + "/" + sha256)
        if r.status_code >= 400:
            logger.warning("[dvm] blossom download %s failed: %s", sha256[:12], r.status_code)
            return None
        if hashlib.sha256(r.content).hexdigest() != sha256:
            logger.warning("[dvm] blossom blob %s hash mismatch — dropping", sha256[:12])
            return None
        return r.content
    except Exception as e:
        logger.warning("[dvm] blossom download error: %s", e)
        return None


# Media at rest on Blossom is ALWAYS AES-256-GCM ciphertext — the per-job key/iv ride inside the
# NIP-44-encrypted result event, so the blob is unreadable to anyone who only has the Blossom URL.
async def put_media(data: bytes, settings: Optional[dict] = None) -> Optional[dict]:
    """Encrypt the media and upload the CIPHERTEXT; return a ref {blob, key, iv} or None."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    key, nonce = os.urandom(32), os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, data, None)
    sha = await upload_media(ct, "application/octet-stream", settings)
    if not sha:
        return None
    ref = {"blob": sha, "key": key.hex(), "iv": nonce.hex()}
    base = _blossom_base(settings)
    if base:
        ref["url"] = base   # self-describing: a remote fetcher (another owner's node) uses THIS URL,
                            # so a provider connection card needs only npub + relay, not a Blossom URL
    return ref


async def get_media(ref: dict, settings: Optional[dict] = None) -> Optional[bytes]:
    """Fetch the ciphertext by its hash and AES-256-GCM-decrypt it with the ref's key/iv; None on fail."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    ct = await download_media(ref.get("blob", ""), settings, base=ref.get("url"))
    if ct is None:
        return None
    try:
        return AESGCM(bytes.fromhex(ref["key"])).decrypt(bytes.fromhex(ref["iv"]), ct, None)
    except Exception as e:
        logger.warning("[dvm] media decrypt failed: %s", e)
        return None


# ---------------------------------------------------------------- payload pack/spill
# A job's params and a worker's result normally ride INLINE as NIP-44 ciphertext in the event content.
# But the relay caps a message at 256KB (max_message_size), and a full agentic/long-chat context —
# accumulated messages + tool outputs + file bodies — easily exceeds that (an OpenAI-API/opencode turn
# at our 65k ctx is ~250-400KB). So when the plaintext is large, SPILL it: AES-encrypt + upload to the
# shared Blossom (exactly like media) and inline only the small encrypted ref. The receiver re-fetches
# transparently. This makes the DVM size-agnostic for chat in BOTH directions, which is what lets
# agentic traffic ride Nostr at all. Small payloads (image params, chat deltas, media refs) stay inline.
_SPILL_OVER = 150 * 1024   # plaintext bytes; NIP-44 base64 (~1.4x) + the event envelope must clear 256KB


async def _pack_content(peer_hex: str, obj: dict, settings: Optional[dict] = None) -> Optional[str]:
    """NIP-44-encrypt a job/result payload for the event content; spill oversized payloads to Blossom
    (encrypting only the small ref inline). Returns None if a needed spill upload fails."""
    sk = node_seckey()
    if not sk:
        return None
    raw = json.dumps(obj).encode()
    if len(raw) > _SPILL_OVER:
        ref = await put_media(raw, settings)
        if ref is None:
            return None
        payload = json.dumps({"__spill__": ref})
    else:
        payload = raw.decode()
    return nip44.encrypt_to(sk, bytes.fromhex(peer_hex), payload)


async def _unpack_content(peer_hex: str, content: str, settings: Optional[dict] = None) -> dict:
    """Inverse of _pack_content: NIP-44-decrypt, then re-hydrate a Blossom-spilled payload if present."""
    obj = json.loads(nip44.decrypt_from(node_seckey(), bytes.fromhex(peer_hex), content))
    if isinstance(obj, dict) and "__spill__" in obj:
        raw = await get_media(obj["__spill__"], settings)
        if raw is None:
            raise ValueError("spill blob fetch/decrypt failed")
        obj = json.loads(raw)
    return obj


# ---------------------------------------------------------------- coordinator
_rr = {"i": 0}


def _pick_peer(pk_list: list) -> Optional[str]:
    if not pk_list:
        return None
    i = _rr["i"] % len(pk_list)
    _rr["i"] = i + 1
    return pk_list[i]


async def run_remote(task: str, params: dict, settings: Optional[dict] = None,
                     worker_pubkey: Optional[str] = None,
                     timeout: Optional[float] = None,
                     relay: Optional[str] = None) -> Optional[dict]:
    """Dispatch a job to a peer worker over Nostr and return the decrypted result dict, or None on
    failure/timeout (the caller then falls back to local or the next candidate). The result shape
    mirrors the worker's `_run_local` return: {"image":b64} / {"audio":b64,"format":ext} /
    {"video":b64} / {"completion": <openai-response>}."""
    s = _settings(settings)
    req_kind = _REQ_KIND.get(task)
    if req_kind is None:
        return None
    if worker_pubkey is None:
        worker_pubkey = _pick_peer([p["pubkey"] for p in providers(s)])
    if not worker_pubkey:
        return None
    sk = node_seckey()
    if not sk:
        return None
    # Where to publish + await: for a node-agent job, the WORKER's own relay (full nodes subscribe only
    # to their local relay, so the controller must publish THERE) — falls back to this node's local relay,
    # which reaches a relay-less standalone agent (it connects to the controllers' relays). GPU-offload
    # jobs pass the provider's relay explicitly, so the caller-supplied `relay` still wins.
    relay = relay or (agent_node_relay(worker_pubkey, s) if task == "agent" else None) or relay_url(s)
    budget = float(timeout or _JOB_TIMEOUT.get(task, 300))
    try:
        enc = await _pack_content(worker_pubkey, params, s)   # inline, or Blossom-spilled if oversized
        if enc is None:
            logger.warning("[dvm] %s job payload pack/upload failed — not dispatched", task)
            return None
        # nofederate: a DVM job is cluster-internal — the relay must NEVER broadcast it to public
        # upstream relays (see nostr_relay.server._broadcastable). Keeps job traffic off the network.
        tags = [["p", worker_pubkey], ["t", task], ["nofederate"],
                ["expiration", str(int(time.time()) + int(budget) + 30)]]
        ev = nostr_event.build_event(sk, req_kind, enc, tags)
        # direct=True: the cluster job relay is the app's OWN relay — connect straight to it, never
        # through the outbound Tor proxy (which would add seconds of latency to every job).
        if not await nostr_relay.publish(relay, ev, direct=True):
            logger.warning("[dvm] %s job %s reached no relay (%s)", task, ev["id"][:12], relay)
            return None
        logger.info("[dvm] dispatched %s job %s → %s", task, ev["id"][:12],
                    nostr_service.npub_of(worker_pubkey)[:16])
        res = await nostr_relay.await_one(
            relay,
            [{"kinds": [req_kind + 1000], "#e": [ev["id"]], "authors": [worker_pubkey]}],
            timeout=budget, direct=True)
        if not res:
            _hint = ("" if task != "agent" else
                     " — if this worker is a FULL node, list its relay in node_exec_node_npubs "
                     "(`name npub ws://host:3052/relay`); a full node only listens on its own relay")
            logger.warning("[dvm] %s job %s timed out (%ss)%s", task, ev["id"][:12], int(budget), _hint)
            return None
        if not nostr_event.verify_event(res):
            logger.warning("[dvm] %s result failed signature verify", task)
            return None
        out = await _unpack_content(worker_pubkey, res.get("content", ""), s)
        if out.get("error"):
            logger.warning("[dvm] worker %s error: %s", task, out["error"])
            return None
        # Media (image/music/video) comes back as a Blossom sha256 ref — fetch the bytes and rebuild
        # the dict shape the factory expects ({"image":b64} / {"audio":b64,"format"} / {"video":b64}).
        if out.get("blob"):
            data = await get_media(out, s)
            if data is None:
                logger.warning("[dvm] %s result blob %s fetch/decrypt failed", task, out["blob"][:12])
                return None
            b64 = base64.b64encode(data).decode()
            if task == "image":
                return {"image": b64}
            if task == "music":
                return {"audio": b64, "format": out.get("format", "ogg")}
            if task == "video":
                return {"video": b64}
            return None
        return out   # chat: inline {"completion": ...}
    except Exception as e:
        logger.warning("[dvm] run_remote(%s) failed: %s", task, e)
        return None


def pick_provider(settings: Optional[dict] = None) -> Optional[dict]:
    """CONSUMER round-robin: pick a remote provider (shared machine) to offload to, or None to run the
    job on THIS node. `[self]+providers` rotation, so we still use our own GPU instead of always
    offloading. Returns a provider dict {pubkey, npub, relay} or None."""
    p = providers(settings)
    if not p:
        return None
    cand = _pick_peer(["__LOCAL__"] + p)
    return None if cand == "__LOCAL__" else cand


async def offload_chat(messages: list, provider: dict, settings: Optional[dict] = None, **kw) -> Optional[dict]:
    """CONSUMER: run a chat completion on a remote PROVIDER over Nostr (its relay). Returns the OpenAI
    completion dict, or None to fall back locally (the provider failed/timed out). kw passes through
    temperature/top_p/max_tokens/stop (None dropped)."""
    params = {"messages": messages}
    params.update({k: v for k, v in kw.items() if v is not None})
    r = await run_remote("chat", params, settings, worker_pubkey=provider["pubkey"], relay=provider["relay"])
    return r["completion"] if (r and r.get("completion")) else None


# ---------------------------------------------------------------- worker
_worker_stop: Optional[asyncio.Event] = None
_worker_task: Optional[asyncio.Task] = None
_seen_jobs: "collections.OrderedDict" = collections.OrderedDict()   # job ids already handled (idempotency)
_running_jobs: set = set()   # strong refs so spawned tasks aren't GC'd mid-run
# Max DVM jobs this worker holds (running + queued). Beyond it → fast busy-reject so the coordinator
# fails over to an idle node instead of piling up behind this GPU (the GPU lock serializes the work).
_MAX_INFLIGHT = 2
_inflight = 0


def _event_expired(ev: dict) -> bool:
    """True if the request's NIP-40 expiration has passed — the coordinator already gave up, so
    running it would only waste GPU on a result nobody is waiting for."""
    for t in ev.get("tags", []):
        if len(t) >= 2 and t[0] == "expiration":
            try:
                return int(t[1]) < int(time.time())
            except (ValueError, TypeError):
                return False
    return False


async def _publish_result(task: str, jid: str, author: str, result: dict) -> None:
    sk = node_seckey()
    enc = await _pack_content(author, result, None)   # inline, or Blossom-spilled if oversized
    if enc is None:
        # Spill upload failed for an oversized result. Send a tiny inline error instead of nothing, so
        # the coordinator fails over fast rather than waiting out the whole job budget for a dead result.
        logger.warning("[dvm] result pack/upload failed for %s job %s — sending error so coordinator fails over fast", task, jid[:12])
        enc = nip44.encrypt_to(sk, bytes.fromhex(author), json.dumps({"error": "result too large or blossom upload failed"}))
    # Short NIP-40 expiration: the coordinator reads the result within seconds, so 6xxx results don't
    # linger / accumulate in the relay.
    # nofederate: like the request, a DVM RESULT is cluster-internal — the coordinator reads it off the
    # local/paired relay. Without this the result (encrypted, but its existence + which peer served whom
    # + task type) was broadcast to every public upstream relay. Keep all DVM traffic off the open net.
    res_ev = nostr_event.build_event(sk, _REQ_KIND[task] + 1000, enc,
        [["e", jid], ["p", author], ["t", task], ["nofederate"], ["expiration", str(int(time.time()) + 3600)]])
    await nostr_relay.publish(relay_url(), res_ev, direct=True)


def _track(t: "asyncio.Task") -> None:
    _running_jobs.add(t)
    t.add_done_callback(_running_jobs.discard)


async def _spawn_job(ev: dict) -> None:
    """Listener callback (runs IN the recv loop — must stay fast, no GPU work here). Validates,
    DEDUPS (already-processed jobs are skipped — e.g. a relay redelivery on reconnect), drops EXPIRED
    jobs, enforces the in-flight cap (fast busy-reject so the coordinator tries another node), then
    runs the job as a BACKGROUND task so the recv loop keeps reading + ping-keepalive during long jobs.
    The check+increment of `_inflight` is synchronous (the recv loop awaits this one event at a time),
    so the cap is race-free.

    CPU: the listener is event-driven (idle on recv between messages) and the relay filters server-side
    (kinds + #p), so jobs are the only thing delivered. The expensive BIP340 signature verify runs LAST
    — only after the cheap kind/addressed/trusted/dedup filters — so bogus traffic can't peg the CPU."""
    global _inflight
    # --- cheap filters first (no crypto) ---
    task = _TASK_BY_REQ.get(int(ev.get("kind", 0)))
    if not task:
        return
    me = node_pubkey()
    if not me or not any(len(t) >= 2 and t[0] == "p" and t[1] == me for t in ev.get("tags", [])):
        return   # not addressed to this node
    author = ev.get("pubkey", "")
    # Trust: node/agent commands use the DEDICATED allowlist; GPU-offload tasks use the DVM peer cluster.
    if task == "agent":
        if not is_agent_trusted(author):
            return   # not a whitelisted controller — reject before any signature work
    elif not is_trusted(author):
        return   # not a cluster node — reject before any signature work
    jid = ev.get("id", "")
    if jid in _seen_jobs:
        return   # idempotency: never reprocess a job we've already handled
    # --- expensive verify only for an addressed, trusted, unseen job ---
    if not nostr_event.verify_event(ev):
        return
    _seen_jobs[jid] = 1
    while len(_seen_jobs) > 1000:
        _seen_jobs.popitem(last=False)
    if _event_expired(ev):
        logger.info("[dvm] dropping expired %s job %s (coordinator gave up)", task, jid[:12])
        return
    # Busy-reject so the coordinator fails over to an idle node, when EITHER: the DVM in-flight cap is
    # hit, OR the GPU is already busy AND a DVM job is queued. The second clause counts LOCAL load too
    # (gpu_busy() is held by a local web/Telegram/opencode generation), so a node running a long
    # agentic session sheds distributed jobs to idle peers instead of queueing them behind the agent
    # (where their coordinators would time out).
    # node/agent tasks are NEVER bounced (the command must run on THIS addressed node) — they QUEUE on
    # _agent_lock inside _run_local and run one-at-a-time. Only GPU-offload tasks busy-reject to a peer.
    if task != "agent":
        from app.services.locks import gpu_busy
        if _inflight >= _MAX_INFLIGHT or (_inflight >= 1 and gpu_busy()):
            logger.info("[dvm] busy (in-flight=%d gpu_busy=%s) → bouncing %s job %s to other nodes",
                        _inflight, gpu_busy(), task, jid[:12])
            _track(asyncio.create_task(_publish_result(task, jid, author, {"error": "busy", "busy": True})))
            return
    _inflight += 1
    _track(asyncio.create_task(_handle_job(task, author, jid, ev)))


async def _run_claude(s: dict, goal: str, dangerous: bool) -> dict:
    """Run the Claude Code CLI on this worker. Double-locked: node_exec_claude_enabled must be on, and
    dangerous (--dangerously-skip-permissions) needs node_exec_claude_dangerous too."""
    if not _truthy(s.get("node_exec_claude_enabled", False)):
        return {"error": "Claude Code agent is not enabled on this worker"}
    argv = (s.get("node_exec_claude_cmd") or "claude").split() + ["-p", goal]
    if dangerous:
        if not _truthy(s.get("node_exec_claude_dangerous", False)):
            return {"error": "CI/CD dangerous mode is not allowed on this worker"}
        argv.append("--dangerously-skip-permissions")
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=True)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=_JOB_TIMEOUT["agent"])
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return {"error": f"claude timed out after {_JOB_TIMEOUT['agent']}s"}
    text = (out or b"").decode("utf-8", "replace")[-200_000:]
    return {"status": "done" if proc.returncode == 0 else "error",
            "summary": text[-1500:] or f"claude exit {proc.returncode}", "output": text, "exit": proc.returncode}


async def _exec_agent(db, params: dict) -> dict:
    """Run a node/agent command LOCALLY on this worker (target 'local'). mode: shell (default) | agent
    (LLM-driven, uses this node's model) | claude (Claude Code CLI). Serialized by _agent_lock."""
    from app.services import node_service
    s = settings_store.all_settings()
    mode = (params.get("mode") or "shell").lower()
    dangerous = bool(params.get("dangerous"))
    if mode == "claude":
        return await _run_claude(s, params.get("goal") or params.get("command") or "", dangerous)
    # Sandbox load-balancing: a `sandbox_uid` means the CONTROLLER placed this user's container on THIS
    # node — run the task inside `pcai-sbx-<uid>` here, not on the host. Requires the sandbox enabled +
    # Docker reachable on this worker; otherwise the container ops fail and we report a clear error.
    _sbx_uid = params.get("sandbox_uid")
    if _sbx_uid:
        from app.services import sandbox_service
        if not (sandbox_service.enabled() and await sandbox_service.available()):
            return {"error": "sandbox not available on this worker (enable it + install Docker)"}
    _node = "sandbox" if _sbx_uid else "local"
    _target = f"sandbox:{_sbx_uid}" if _sbx_uid else "local"
    if mode == "agent":
        from app.models import User
        u = db.query(User).filter(User.id == 1).first()   # admin owns worker-run jobs
        # report=True → the model's final summary only (no ## header) — used by the health report.
        try:
            summary = await node_service.run_agent(db, u, _node, _target, params.get("goal") or "", None,
                                                   notify=None, report_mode=bool(params.get("report")))
            return {"status": "done", "summary": summary, "output": summary, "exit": 0}
        finally:
            # A PLACED agent run tears down its container here, matching the local panel's immediate reap
            # (bug #5). NB: the controller deleting the chat can't stop THIS remote run — it runs to its
            # step limit and this reap fires then; the idle reaper is the backstop (bug #4, gated).
            if _sbx_uid:
                try:
                    from app.services import sandbox_service
                    await sandbox_service.reap(_sbx_uid, force=False)
                except Exception:
                    pass
    cmd = params.get("command") or ""
    if not cmd:
        return {"error": "empty command"}
    job = await node_service.run_to_completion(db, _node, _target, cmd, user_id=1)
    out = (job.output or "").strip()[:200_000]
    return {"status": "done" if job.exit_code == 0 else "error",
            "summary": f"exit {job.exit_code}", "output": out, "exit": job.exit_code}


async def _run_local(task: str, params: dict) -> dict:
    """Serve a job by handing it to THIS node's own IP load balancer (chat_server_urls) + local GPU —
    `dvm_offload=False` so it spreads across this node's cluster (e.g. server1 + nas) WITHOUT
    re-dispatching back out over Nostr (no loop). The peer that picks up an HTTP-forwarded request sees
    a plain local request. Opens a fresh DB session (the worker runs outside requests)."""
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        if task == "agent":
            global _agent_lock
            if _agent_lock is None:
                _agent_lock = asyncio.Lock()
            async with _agent_lock:          # ONE agent command at a time on this worker (queue)
                try:
                    return await _exec_agent(db, params)
                except Exception as e:
                    return {"error": str(e)[:300]}
        if task == "image":
            from app.services.image_factory import generate_image_with_load_balancing
            b64 = await generate_image_with_load_balancing(
                db, params.get("prompt", ""), params.get("negative_prompt", ""),
                params.get("width"), params.get("height"), params.get("steps"),
                params.get("cfg"), local_only=False, dvm_offload=False)
            if not b64:
                return {"error": "image generation returned nothing"}
            ref = await put_media(base64.b64decode(b64))
            return ref or {"error": "blossom upload failed"}
        if task == "music":
            from app.services.music_factory import generate_music_for_user
            audio, ext = await generate_music_for_user(
                db, params.get("prompt", ""), params.get("lyrics", ""),
                params.get("duration"), params.get("steps"), local_only=False, dvm_offload=False)
            ref = await put_media(audio)
            if not ref:
                return {"error": "blossom upload failed"}
            ref["format"] = ext
            return ref
        if task == "video":
            from app.services.video_factory import generate_video_for_user
            mp4 = await generate_video_for_user(
                db, params.get("prompt", ""), params.get("negative_prompt", ""),
                local_only=False, dvm_offload=False)
            ref = await put_media(mp4)
            return ref or {"error": "blossom upload failed"}
        if task == "chat":
            messages = params.get("messages", [])
            kwargs = {k: params[k] for k in
                      ("temperature", "top_p", "max_tokens", "stop", "model", "tools", "tool_choice")
                      if params.get(k) is not None}
            # Hand to this node's IP LB (chat_server_urls) so it spreads across the cluster, exactly
            # like the media tasks — NOT the DVM consumer path (which would re-offload over Nostr → loop).
            import os as _os
            from app.services.load_balancer import LoadBalancer, parse_server_urls
            s = settings_store.all_settings()
            servers = parse_server_urls(s.get("chat_server_urls", ""), exclude_self=False)
            if servers:
                try:
                    _lp = s.get("llm_model_path", "")
                    model = kwargs.get("model") or (_os.path.basename(_lp) if _lp else "default")
                    timeout = int(s.get("ollama_timeout", "300000") or "300000") / 1000
                    lb_kw = {k: kwargs[k] for k in ("temperature", "top_p", "max_tokens", "stop", "tools", "tool_choice") if k in kwargs}
                    lbres = await LoadBalancer(servers, timeout=timeout, model=model).chat(messages=messages, **lb_kw)
                    if isinstance(lbres, dict) and "error" not in lbres:
                        return {"completion": lbres}
                except Exception as e:
                    logger.info("[dvm] chat LB hop failed (%s) → local", e)
            # Local fallback (no LB configured, or it failed)
            from app.services.inference_factory import get_inference_service, prepare_vram_for_llm
            prepare_vram_for_llm(db)
            service = get_inference_service(db)
            result = await service.chat_completion(messages=messages, stream=False, **kwargs)
            if "error" in result:
                return {"error": result["error"].get("message", "inference error")}
            return {"completion": result}
        return {"error": f"unknown task {task}"}
    finally:
        db.close()


async def _handle_job(task: str, author: str, jid: str, ev: dict) -> None:
    """Run ONE validated job on the GPU (serialized by the shared GPUResourceLock, exactly like a local
    web/Telegram request) and publish the encrypted result. Always decrements the in-flight count."""
    global _inflight
    try:
        # Unpack INSIDE the result-bearing try: a failed spill fetch (or any decode error) must become
        # a published error result, not a silent drop — else the coordinator waits out its full budget.
        try:
            params = await _unpack_content(author, ev.get("content", ""), None)
            logger.info("[dvm] running %s job %s from %s (%d in flight)", task, jid[:12],
                        nostr_service.npub_of(author)[:16], _inflight)
            result = await _run_local(task, params)
        except Exception as e:
            logger.warning("[dvm] %s job %s failed: %s", task, jid[:12], e)
            result = {"error": str(e)[:300]}
        await _publish_result(task, jid, author, result)
        logger.info("[dvm] returned %s result for job %s%s", task, jid[:12],
                    " (error)" if result.get("error") else "")
    except Exception as e:
        logger.warning("[dvm] handle_job error: %s", e)
    finally:
        _inflight -= 1


def start_worker() -> None:
    """Start the persistent worker loop (idempotent). No-op when DVM is disabled."""
    global _worker_stop, _worker_task
    if _worker_task and not _worker_task.done():
        return
    s = settings_store.all_settings()
    if not (is_enabled(s) or agent_worker_enabled(s)):   # DVM (GPU offload) OR node/agent-over-Nostr
        return
    me = node_pubkey()
    if not me:
        logger.warning("[dvm] enabled but no relay identity (operator nsec) — worker not started")
        return
    _worker_stop = asyncio.Event()
    relay = relay_url(s)
    # since is stamped per-(re)connect by subscribe(since_now=True) so a reconnect never replays old jobs.
    filters = [{"kinds": list(DVM_REQ_KINDS), "#p": [me]}]
    _npeers = len(peer_pubkeys(s))
    logger.info("[dvm] worker listening on %s as %s (%d shared peer%s, max %d in-flight)%s",
                relay, node_npub(), _npeers, "" if _npeers == 1 else "s", _MAX_INFLIGHT,
                "" if _npeers else " — no peers, so it will serve NO ONE until peers are added")
    _worker_task = asyncio.create_task(
        nostr_relay.subscribe(relay, filters, _spawn_job, _worker_stop, direct=True, since_now=True))


async def stop_worker() -> None:
    global _worker_stop, _worker_task
    if _worker_stop:
        _worker_stop.set()
    task = _worker_task
    _worker_task = None
    if task:
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=3)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            pass
