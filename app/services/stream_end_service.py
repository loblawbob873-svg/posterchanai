"""End a live stream's NIP-53 announcement when the stream really stops — even if the streamer's tab is gone.

The ghost-LIVE problem: the kind-30311 is signed in the BROWSER (a nip07 extension / nip46 signer never hands
its secret key to the server), so only the client could ever mark a stream ended. Close the tab mid-stream and
the event sits `status: live` forever, pointing at a dead HLS URL.

The fix, without the server ever holding a signing key: at go-live the client also signs the "ended" twin of
its live event and parks it here (the *sentinel*). The server never authors anything — it just PUBLISHES that
pre-signed event once the stream is really over. Two triggers, layered:

- MediaMTX's `runOnUnpublish` hook (POST /api/streams/unpublish) fires the instant the publisher drops;
- a reaper sweep catches what the hook can't (app restarted mid-stream, MediaMTX killed, hook lost).

Both re-probe MediaMTX's local HLS endpoint before firing, so an OBS reconnect blip never ends a live stream —
we only publish "ended" for a stream whose feed is actually gone.

The sentinel carries no `ends` tag: its signature is fixed at go-live, so any end time it named would be a lie.
The normal in-browser end path still stamps an accurate `ends`, and because that event is newer it wins the
replaceable-event race — an older sentinel landing afterwards is simply dropped by the relay.
"""
from __future__ import annotations

import asyncio
import json
import re
import logging
import time
from typing import Optional

from app.database import SessionLocal
from app.models import UserSetting
from app.services import nostr_store, settings_store

logger = logging.getLogger(__name__)

SENTINEL_KEY = "stream_end_sentinel"   # per-user UserSetting holding the parked, pre-signed "ended" event
TOKEN_KEY = "stream_token"             # per-user publish token (== the MediaMTX path == the 30311 `d` tag)

_HOOK_GRACE = 20        # seconds after runOnUnpublish before we believe the publisher is really gone
_SWEEP_INTERVAL = 30    # reaper period
_DOWN_STRIKES = 2       # consecutive dead-HLS sweeps before the reaper ends a stream
_NEVER_LIVE_TTL = 900   # announced but never actually published (OBS never started) → reap after 15 min
_SENTINEL_MAX_AGE = 86400   # give up on a sentinel the relay has refused for a full day

_reaper: Optional[asyncio.Task] = None
_pending: set = set()          # strong refs to in-flight grace tasks (a bare create_task can be GC'd)
_strikes: dict = {}            # token -> consecutive sweeps seen with no HLS feed


# ---------------------------------------------------------------- sentinel storage (UserSetting-backed)

def _row(db, user_id: int) -> Optional[UserSetting]:
    return db.query(UserSetting).filter(UserSetting.user_id == user_id,
                                        UserSetting.key == SENTINEL_KEY).first()


def _data(row: Optional[UserSetting]) -> dict:
    if not row or not row.value:
        return {}
    try:
        d = json.loads(row.value)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _dump(data: dict) -> str:
    return json.dumps(data, separators=(",", ":"))


def _tag_of(data: dict, name: str) -> str:
    for t in ((data.get("event") or {}).get("tags") or []):
        if isinstance(t, list) and len(t) >= 2 and t[0] == name:
            return str(t[1])
    return ""


def token_of(data: dict) -> str:
    """The stream's MediaMTX publish token, recovered from the parked event's `d` tag.

    The `d` is NOT the token any more. It used to be, and that was the bug: the token is stable for a
    user's whole life (it's the MediaMTX path in their OBS config), so kind 30311 being a parameterized
    replaceable event meant every broadcast REPLACED the previous one at `30311:<pubkey>:<token>` —
    last week's stream silently overwritten by this week's. A broadcast now gets `<token>-<starts>`.

    This must keep returning the TOKEN, because that is what `is_publishing()` probes MediaMTX with:
    hand it the full `d` and the probe finds no such path, reads it as "feed gone", and the reaper ends
    every live stream it sweeps. A token is hex (never contains `-`) and `starts` is a 10-digit unix
    time, so stripping one trailing `-<digits>` recovers it — and leaves an OLD parked event, whose `d`
    really is the bare token, untouched.
    """
    return re.sub(r"-\d{9,}$", "", _tag_of(data, "d"))


def _session_of(data: dict) -> str:
    """Identifies one BROADCAST, not one user. The publish token is stable for life (it's the MediaMTX path),
    so it can't tell this go-live apart from the last one — `starts` can."""
    return _tag_of(data, "starts")


def user_by_token(db, token: str) -> Optional[int]:
    row = db.query(UserSetting).filter(UserSetting.key == TOKEN_KEY, UserSetting.value == token).first()
    return row.user_id if row else None


def save_sentinel(db, user_id: int, event: dict) -> None:
    """Park (or refresh) the user's pre-signed "ended" event.

    `seen_live` is carried over only when re-parking the SAME broadcast (a client re-adopting its own live
    stream after a reload): resetting it there would make the reaper treat a running stream as one that never
    started. It must NOT carry across broadcasts — keyed on the publish token it would, since that token
    never rotates, and a stale `seen_live` from a previous stream would skip the "never went live" grace and
    let the reaper end the NEXT stream 60s after they announce it but before they've started OBS.
    """
    row = _row(db, user_id)
    prev = _data(row)
    fresh = {"event": event}
    seen = bool(prev.get("seen_live")) and _session_of(prev) == _session_of(fresh) \
        and token_of(prev) == token_of(fresh)
    blob = _dump({"event": event, "seen_live": seen, "ts": int(time.time())})
    if row:
        row.value = blob
    else:
        db.add(UserSetting(user_id=user_id, key=SENTINEL_KEY, value=blob))
    db.commit()


def clear_sentinel(db, user_id: int) -> None:
    """The client ended the stream itself (with an accurate `ends`) — drop the fallback."""
    row = _row(db, user_id)
    if row:
        _strikes.pop(token_of(_data(row)), None)
        db.delete(row)
        db.commit()


def mark_publishing(db, user_id: int) -> None:
    """MediaMTX just authorized a publish for this user — their stream really went live."""
    row = _row(db, user_id)
    data = _data(row)
    if not row or not data or data.get("seen_live"):
        return
    data["seen_live"] = True
    row.value = _dump(data)
    db.commit()


# ---------------------------------------------------------------- liveness + publishing

async def is_publishing(token: str) -> Optional[bool]:
    """Is a source publishing this path? True / False / None = can't tell. (Public: stream_vod_service and
    the HLS proxy's clamp resolver both ask this, so it is the one MediaMTX liveness probe in the codebase.)

    Asks MediaMTX's local control API, NOT the HLS playlist. Two reasons the playlist is the wrong signal:
    a WebRTC/WHIP (phone) ingest 404s while it warms up into HLS — the client disables its own HLS heartbeat
    on that path for exactly this reason (see _phoneGoLive) — and a 404 from a *dead* MediaMTX is
    indistinguishable from a 404 for a stopped stream. Both would end a perfectly healthy stream.

    None ("MediaMTX is down / unreachable") is the important case: we genuinely don't know, so the caller
    must NOT score a strike. Ending a live stream is unrecoverable — the browser only signs a `live` event at
    Go Live, so nothing can put it back.
    """
    port = (settings_store.get("stream_api_port", "") or "9997").strip()
    import httpx
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=2.0)) as client:
            r = await client.get(f"http://127.0.0.1:{port}/v3/paths/get/{token}")
    except Exception:
        return None                       # MediaMTX unreachable — unknown, never "gone"
    if r.status_code == 404:
        return False                      # MediaMTX is up and has no such path — definitively not publishing
    if r.status_code != 200:
        return None
    try:
        return bool((r.json() or {}).get("ready"))
    except Exception:
        return None


async def _publish_end(user_id: int, data: dict) -> bool:
    ev = (data.get("event") or {})
    if not ev.get("id") or not ev.get("sig"):
        return False
    port = settings_store.get_int("nostr_relay_port", 3052)
    ok, msg = await nostr_store._ws_publish(port, ev)
    if not ok:
        logger.info("[stream-end] relay did not accept the ended event for user %s: %s", user_id, msg)
    return ok


async def _end_now(user_id: int, reason: str) -> None:
    """Publish the parked "ended" event. Opens its own session — this runs long after the request is gone."""
    db = SessionLocal()
    try:
        row = _row(db, user_id)
        data = _data(row)
        if not row or not data:
            return
        if await _publish_end(user_id, data):
            _strikes.pop(token_of(data), None)
            db.delete(row)
            db.commit()
            logger.info("[stream-end] marked user %s's stream ended (%s)", user_id, reason)
            return
        # The publish failed (the local relay restarts on deploys and is watchdog-respawned, so this is a
        # normal transient). KEEP the sentinel and retry on later sweeps — dropping it would throw away the
        # only signed "ended" event in existence and strand the stream ● LIVE forever, i.e. re-create the
        # exact bug this service exists to fix. Only a sentinel that's been failing for a whole day is
        # abandoned as genuinely unpublishable.
        tries = int(data.get("tries", 0)) + 1
        data["tries"] = tries
        row.value = _dump(data)
        db.commit()
        if int(time.time()) - int(data.get("ts", 0)) > _SENTINEL_MAX_AGE:
            _strikes.pop(token_of(data), None)
            db.delete(row)
            db.commit()
            logger.warning("[stream-end] abandoning user %s's ended event — still unpublishable after %d "
                           "tries over 24h", user_id, tries)
        elif tries in (1, 5, 20):
            logger.warning("[stream-end] user %s's ended event has failed to publish %d time(s) — will keep "
                           "retrying (%s)", user_id, tries, reason)
    except Exception as e:
        logger.warning("[stream-end] could not end user %s's stream: %s", user_id, e)
    finally:
        db.close()


# ---------------------------------------------------------------- triggers

# NOTE: saving the tmpfs recording to Blossom is NOT triggered from here. It's driven independently by
# stream_vod_service.process_pending() (run by the reaper below), which detects an ended session by probing
# MediaMTX and claims the recording — so graceful/ungraceful/lost-hook/inconclusive ends are all handled
# uniformly, without this path (which only runs when a sentinel exists) having to fire it.

async def _end_after_grace(token: str, user_id: int) -> None:
    try:
        await asyncio.sleep(_HOOK_GRACE)
        # Only a definitive "not publishing" ends it here. Came back (a reconnect blip) or can't tell
        # (MediaMTX went down with it — its own shutdown fires this hook for every live path) → leave it to
        # the reaper, which re-probes until it gets a real answer.
        if await is_publishing(token) is not False:
            logger.info("[stream-end] %s… is publishing again or unverifiable — leaving it to the reaper",
                        token[:8])
            return
        await _end_now(user_id, "publisher disconnected")
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning("[stream-end] grace check failed for %s…: %s", token[:8], e)


def schedule_end(token: str, user_id: int) -> None:
    """MediaMTX says the publisher dropped — end the stream after a grace period (unless it comes back)."""
    try:
        task = asyncio.get_running_loop().create_task(_end_after_grace(token, user_id))
    except RuntimeError:      # no running loop (shouldn't happen from a request) — the reaper still covers us
        return
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def _sweep() -> None:
    """Safety net for the ends the hook can't deliver: app restarted mid-stream, MediaMTX killed, hook lost."""
    db = SessionLocal()
    try:
        pending = [(r.user_id, _data(r)) for r in db.query(UserSetting).filter(
            UserSetting.key == SENTINEL_KEY).all()]
    finally:
        db.close()
    if not pending:
        return
    # Streaming turned off ⇒ MediaMTX is stopped for good ⇒ nothing can still be publishing. Don't bail out
    # here: that would strand an announced stream ● LIVE forever with MediaMTX gone and the hook unable to
    # fire. It still goes through the strike counter below, so a transient false read (e.g. settings not yet
    # hydrated at startup) can't end a live stream on its own.
    enabled = (settings_store.get("stream_enabled", "false") or "").strip().lower() == "true"
    now = int(time.time())
    for user_id, data in pending:
        token = token_of(data)
        if not token:
            continue
        live = await is_publishing(token) if enabled else False
        if live is None:
            continue            # MediaMTX unreachable — we can't tell, so we don't guess (a wrong "ended"
                                # is unrecoverable: only the browser can sign a stream back to "live")
        if live:
            _strikes.pop(token, None)
            if not data.get("seen_live"):     # the probe proves it — no need to wait for a publish hook
                db = SessionLocal()
                try:
                    mark_publishing(db, user_id)
                finally:
                    db.close()
            continue
        if enabled and not data.get("seen_live"):
            # Announced but the feed never appeared (the Go Live modal tells them to start OBS *after*
            # announcing, so an empty stream is normal for a while). Reap it once it's clearly abandoned.
            if now - int(data.get("ts", now)) > _NEVER_LIVE_TTL:
                await _end_now(user_id, "announced but never went live")
            continue
        strikes = _strikes.get(token, 0) + 1
        _strikes[token] = strikes
        if strikes >= _DOWN_STRIKES:
            await _end_now(user_id, "feed gone" if enabled else "streaming disabled on this server")


async def _reaper_loop() -> None:
    while True:
        try:
            await asyncio.sleep(_SWEEP_INTERVAL)
            await _sweep()
            # Drive the save-to-Blossom worker: detect ended recordings + retry staged uploads. SPAWNED
            # (not awaited) so its per-token MediaMTX liveness probes can't stall the reaper; it's
            # re-entrancy-guarded, so a still-running pass is a no-op. Isolated from the reaper's own work.
            try:
                from app.services import stream_vod_service
                _vt = asyncio.get_running_loop().create_task(stream_vod_service.process_pending())
                _pending.add(_vt)
                _vt.add_done_callback(_pending.discard)
            except Exception as e:
                logger.debug("[stream-end] VOD process_pending error: %s", e)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("[stream-end] sweep error: %s", e)


def start_stream_end_reaper() -> None:
    """Idempotently start the reaper (no-op work unless streaming is enabled and a stream is announced)."""
    global _reaper
    if _reaper is not None and not _reaper.done():
        return
    try:
        _reaper = asyncio.get_running_loop().create_task(_reaper_loop())
    except RuntimeError:
        _reaper = None
        return
    logger.info("[stream-end] reaper started")


def stop_stream_end_reaper() -> None:
    global _reaper
    if _reaper is not None:
        _reaper.cancel()
        _reaper = None
    for t in list(_pending):
        t.cancel()
    _pending.clear()
    logger.info("[stream-end] reaper stopped")
