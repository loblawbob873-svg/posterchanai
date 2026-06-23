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
  * Each node has a DEDICATED worker key (`keystore.get_worker_seckey`); its npub is its address.
  * ONE setting `nostr_dvm_worker_npubs` = the cluster's worker npubs (the LB list). Peers = list−self.
    The same list is the trusted-requester allowlist (cluster nodes serve each other).
  * Job request kinds are per task (NIP-90 5xxx range); result kind = request + 1000. Payload is
    NIP-44-encrypted to the recipient and the result is signature-verified before use.
"""

import asyncio
import base64
import collections
import json
import logging
import time
from typing import Optional

from app.services import keystore, settings_store
from app.services.nostr import bip340
from app.services.nostr import event as nostr_event
from app.services.nostr import nip44
from app.services.nostr import nostr_service
from app.services.nostr import relay as nostr_relay

logger = logging.getLogger(__name__)

# task -> request kind (NIP-90 5xxx range). 5050/6050 are the NIP-90 text-gen kinds; 5100/6100 image;
# 5201/5202 are cluster-local DVM kinds for music/video (no NIP-90 standard). Result = request + 1000.
_REQ_KIND = {"chat": 5050, "image": 5100, "music": 5201, "video": 5202}
_TASK_BY_REQ = {v: k for k, v in _REQ_KIND.items()}
DVM_REQ_KINDS = frozenset(_REQ_KIND.values())
DVM_KINDS = frozenset(list(_REQ_KIND.values()) + [k + 1000 for k in _REQ_KIND.values()])

# Per-task wait budget (seconds) — the coordinator gives up and falls back to local/next after this.
_JOB_TIMEOUT = {"chat": 180, "image": 300, "music": 600, "video": 900}


# ---------------------------------------------------------------- identity
def node_seckey() -> bytes:
    return keystore.get_worker_seckey()


def node_pubkey() -> str:
    return bip340.pubkey_from_seckey(node_seckey()).hex()


def node_npub() -> str:
    return nostr_service.npub_of(node_pubkey())


# ---------------------------------------------------------------- settings
def _settings(settings: Optional[dict] = None) -> dict:
    return settings if settings is not None else settings_store.all_settings()


def _truthy(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def is_enabled(settings: Optional[dict] = None) -> bool:
    return _truthy(_settings(settings).get("nostr_dvm_enabled", False))


def worker_pubkeys(settings: Optional[dict] = None) -> list:
    """Cluster worker pubkeys (hex) parsed from the `nostr_dvm_worker_npubs` list."""
    raw = _settings(settings).get("nostr_dvm_worker_npubs", "") or ""
    out: list = []
    for tok in raw.replace(",", "\n").split("\n"):
        tok = tok.strip()
        if not tok:
            continue
        pk = nostr_service.to_pubkey_hex(tok)
        if pk and pk not in out:
            out.append(pk)
    return out


def peers(settings: Optional[dict] = None) -> list:
    """Worker pubkeys other than this node — the remote candidates for dispatch."""
    me = node_pubkey()
    return [pk for pk in worker_pubkeys(settings) if pk != me]


def is_trusted(pubkey_hex: str, settings: Optional[dict] = None) -> bool:
    """A worker only serves jobs from npubs in the cluster list (anti-abuse allowlist)."""
    return pubkey_hex in worker_pubkeys(settings)


def relay_url(settings: Optional[dict] = None) -> str:
    s = _settings(settings)
    url = (s.get("nostr_dvm_relay", "") or "").strip()
    if url:
        return url
    port = s.get("nostr_relay_port", 3052) or 3052
    return f"ws://127.0.0.1:{port}/relay"


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
                     timeout: Optional[float] = None) -> Optional[dict]:
    """Dispatch a job to a peer worker over Nostr and return the decrypted result dict, or None on
    failure/timeout (the caller then falls back to local or the next candidate). The result shape
    mirrors the worker's `_run_local` return: {"image":b64} / {"audio":b64,"format":ext} /
    {"video":b64} / {"completion": <openai-response>}."""
    s = _settings(settings)
    req_kind = _REQ_KIND.get(task)
    if req_kind is None:
        return None
    if worker_pubkey is None:
        worker_pubkey = _pick_peer(peers(s))
    if not worker_pubkey:
        return None
    sk = node_seckey()
    relay = relay_url(s)
    budget = float(timeout or _JOB_TIMEOUT.get(task, 300))
    try:
        peer_bytes = bytes.fromhex(worker_pubkey)
        enc = nip44.encrypt_to(sk, peer_bytes, json.dumps(params))
        tags = [["p", worker_pubkey], ["t", task],
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
            logger.warning("[dvm] %s job %s timed out (%ss)", task, ev["id"][:12], int(budget))
            return None
        if not nostr_event.verify_event(res):
            logger.warning("[dvm] %s result failed signature verify", task)
            return None
        out = json.loads(nip44.decrypt_from(sk, peer_bytes, res.get("content", "")))
        if out.get("error"):
            logger.warning("[dvm] worker %s error: %s", task, out["error"])
            return None
        return out
    except Exception as e:
        logger.warning("[dvm] run_remote(%s) failed: %s", task, e)
        return None


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


def _job_meta(ev: dict):
    """(task, author, jid) for a signature-verified job addressed to THIS worker, else None."""
    if not nostr_event.verify_event(ev):
        return None
    task = _TASK_BY_REQ.get(int(ev.get("kind", 0)))
    if not task:
        return None
    me = node_pubkey()
    if not any(len(t) >= 2 and t[0] == "p" and t[1] == me for t in ev.get("tags", [])):
        return None
    return task, ev.get("pubkey", ""), ev.get("id", "")


async def _publish_result(task: str, jid: str, author: str, result: dict) -> None:
    sk = node_seckey()
    enc = nip44.encrypt_to(sk, bytes.fromhex(author), json.dumps(result))
    # Short NIP-40 expiration: the coordinator reads the result within seconds, so 6xxx results don't
    # linger / accumulate in the relay.
    res_ev = nostr_event.build_event(sk, _REQ_KIND[task] + 1000, enc,
        [["e", jid], ["p", author], ["t", task], ["expiration", str(int(time.time()) + 3600)]])
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
    so the cap is race-free."""
    global _inflight
    meta = _job_meta(ev)
    if not meta:
        return
    task, author, jid = meta
    if not is_trusted(author):
        logger.info("[dvm] ignoring %s job from untrusted %s", task, author[:12])
        return
    if jid in _seen_jobs:
        return   # idempotency: never reprocess a job we've already handled
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
    from app.services.locks import gpu_busy
    if _inflight >= _MAX_INFLIGHT or (_inflight >= 1 and gpu_busy()):
        logger.info("[dvm] busy (in-flight=%d gpu_busy=%s) → bouncing %s job %s to other nodes",
                    _inflight, gpu_busy(), task, jid[:12])
        _track(asyncio.create_task(_publish_result(task, jid, author, {"error": "busy", "busy": True})))
        return
    _inflight += 1
    _track(asyncio.create_task(_handle_job(task, author, jid, ev)))


async def _run_local(task: str, params: dict) -> dict:
    """Run a job on THIS node's GPU via the existing local-execution paths (local_only=True so they
    never bounce back out to the network). Opens a fresh DB session (worker runs outside requests)."""
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        if task == "image":
            from app.services.image_factory import generate_image_with_load_balancing
            b64 = await generate_image_with_load_balancing(
                db, params.get("prompt", ""), params.get("negative_prompt", ""),
                params.get("width"), params.get("height"), params.get("steps"),
                params.get("cfg"), local_only=True)
            return {"image": b64} if b64 else {"error": "image generation returned nothing"}
        if task == "music":
            from app.services.music_factory import generate_music_for_user
            audio, ext = await generate_music_for_user(
                db, params.get("prompt", ""), params.get("lyrics", ""),
                params.get("duration"), params.get("steps"), local_only=True)
            return {"audio": base64.b64encode(audio).decode(), "format": ext}
        if task == "video":
            from app.services.video_factory import generate_video_for_user
            mp4 = await generate_video_for_user(
                db, params.get("prompt", ""), params.get("negative_prompt", ""), local_only=True)
            return {"video": base64.b64encode(mp4).decode()}
        if task == "chat":
            from app.services.inference_factory import get_inference_service
            from app.services.vram_manager import prepare_vram_for_llm
            prepare_vram_for_llm(db)
            service = get_inference_service(db)
            kwargs = {k: params[k] for k in
                      ("temperature", "top_p", "max_tokens", "stop", "model", "tools", "tool_choice")
                      if params.get(k) is not None}
            result = await service.chat_completion(messages=params.get("messages", []),
                                                   stream=False, **kwargs)
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
        params = json.loads(nip44.decrypt_from(node_seckey(), bytes.fromhex(author), ev.get("content", "")))
        logger.info("[dvm] running %s job %s from %s (%d in flight)", task, jid[:12],
                    nostr_service.npub_of(author)[:16], _inflight)
        try:
            result = await _run_local(task, params)
        except Exception as e:
            logger.warning("[dvm] local %s job %s failed: %s", task, jid[:12], e)
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
    if not is_enabled(s):
        return
    _worker_stop = asyncio.Event()
    relay = relay_url(s)
    # since is stamped per-(re)connect by subscribe(since_now=True) so a reconnect never replays old jobs.
    filters = [{"kinds": list(DVM_REQ_KINDS), "#p": [node_pubkey()]}]
    logger.info("[dvm] worker listening on %s as %s (trusted: %d npubs, max %d in-flight)",
                relay, node_npub(), len(worker_pubkeys(s)), _MAX_INFLIGHT)
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
