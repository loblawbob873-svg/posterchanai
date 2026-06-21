"""Admin API for managed bots (the merged ~/posterchan framework).

CRUD over the `Bot` table plus runtime actions (On/Off via `enabled`, restart, status). The
actual process lifecycle lives in app/services/bot_manager_service.py; these endpoints just
edit rows and nudge the manager to reconcile. Admin-gated like app/routers/admin.py.
"""

import os
import json
import logging
from typing import List, Optional, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from pydantic import BaseModel

import httpx

from app.database import get_db
from app.models import Bot, User
from app.auth import get_admin_user
from app.services import bot_manager_service, pleroma_service, misskey_service
from app.services.nostr import nostr_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/bots", tags=["bots"])


class BotPayload(BaseModel):
    name: str
    enabled: bool = True
    bot_type: str = "text"          # "text" | "image"
    platform: str = "misskey"       # "misskey" | "pleroma" | "matrix"
    host: Optional[str] = ""        # node hostname; empty = any node
    modes: Optional[str] = ""       # comma-separated main.py flags
    config: Dict[str, Any] = {}     # all other per-bot fields (creds, prompt, feature opts)


class OAuthTokenPayload(BaseModel):
    platform: str = "pleroma"       # "pleroma" (OAuth password grant) | "misskey" (/api/signin)
    server: str                     # instance URL, e.g. https://poster.place
    username: str                   # bot account login (handle, no leading @)
    password: str
    totp: str = ""                  # optional 2FA code (Misskey only)
    scopes: str = "read write follow push"


class BotUpdate(BaseModel):
    name: Optional[str] = None
    enabled: Optional[bool] = None
    bot_type: Optional[str] = None
    platform: Optional[str] = None
    host: Optional[str] = None
    modes: Optional[str] = None
    config: Optional[Dict[str, Any]] = None


def _refresh_wot_for_nostr(bot: Bot):
    """A newly created/updated nostr bot is an operator key — fold it into the relay's WoT now so its
    posts are accepted immediately (rather than waiting for the scheduled rebuild)."""
    if (bot.platform or "") != "nostr":
        return
    try:
        from app.services.nostr_relay.thread import trigger_wot_refresh
        trigger_wot_refresh()
    except Exception:
        pass


def _serialize(bot: Bot) -> dict:
    try:
        cfg = json.loads(bot.config) if bot.config else {}
    except (ValueError, TypeError):
        cfg = {}
    return {
        "id": bot.id,
        "name": bot.name,
        "enabled": bool(bot.enabled),
        "bot_type": bot.bot_type,
        "platform": bot.platform,
        "host": bot.host or "",
        "modes": bot.modes or "",
        "config": cfg,
    }


@router.get("")
def list_bots(db: Session = Depends(get_db), admin: User = Depends(get_admin_user)):
    return [_serialize(b) for b in db.query(Bot).order_by(Bot.name).all()]


@router.get("/status")
def bots_status(admin: User = Depends(get_admin_user)):
    """Live runtime status (running/pid/restarts) merged with DB rows."""
    return bot_manager_service.get_status()


@router.post("/oauth/token")
async def mint_oauth_token(payload: OAuthTokenPayload, admin: User = Depends(get_admin_user)):
    """Mint a fedi access token from the bot account's username/password so an admin can
    connect a bot in the UI without running a script or a browser auth flow. Pleroma uses the
    OAuth password grant; Misskey uses /api/signin. The token is returned for the caller to
    save into the bot's config; nothing is persisted here."""
    server = (payload.server or "").strip()
    username = (payload.username or "").lstrip("@").strip()
    if not server or not username or not payload.password:
        raise HTTPException(status_code=400, detail="Server, username and password are required")
    try:
        if payload.platform == "misskey":
            token = await misskey_service.password_signin(
                server, username, payload.password, token=payload.totp.strip(),
            )
        else:
            token = await pleroma_service.password_grant(
                server, username, payload.password, scopes=payload.scopes,
            )
    except httpx.HTTPStatusError as e:
        body = e.response.text[:300] if e.response is not None else ""
        raise HTTPException(status_code=400,
                            detail=f"Instance rejected the request ({e.response.status_code if e.response is not None else '?'}): {body}")
    except (httpx.HTTPError, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"Could not mint token: {e}")
    return {"access_token": token}


class ProvisionPayload(BaseModel):
    name: str = "ChessBot"
    nip05: Optional[str] = ""          # "chess" → chess@<this-host>, or a full addr
    picture: Optional[str] = ""        # avatar URL (used only if no upload given)
    picture_data: Optional[str] = ""   # uploaded avatar as a data: URL / base64 → stored on Blossom
    about: Optional[str] = ""


@router.post("/provision")
async def provision_nostr_identity(payload: ProvisionPayload, request: Request,
                                   db: Session = Depends(get_db),
                                   admin: User = Depends(get_admin_user)):
    """Mint a fresh Nostr identity for a bot and wire it up so it 'just works' with no manual login:
    generate the nsec, grant it Blossom upload access (built-in server), publish a kind-0 profile
    (name / avatar / nip05), make the OPERATOR follow it + add it to the relay's Web of Trust (so its
    posts are stored/served), and return the nsec+npub for the form to save. WoT note: the bot also
    becomes an operator key once the bot row is saved, but we follow + refresh now so it works
    immediately for testing (the scheduled WoT rebuild is too slow)."""
    from app.services.nostr import bip340, bech32, event as _nevent
    from app.services import settings_store, keystore

    sk = os.urandom(32)
    pub = bip340.pubkey_from_seckey(sk).hex()
    nsec = bech32.encode("nsec", sk)
    npub = nostr_service.npub_of(pub)
    port = int(settings_store.get("nostr_relay_port", "3052") or "3052")
    local = [f"ws://127.0.0.1:{port}"]

    # 1) Blossom upload access for the bot's key (so its board/image uploads are accepted)
    try:
        wl = [x for x in (settings_store.get("blossom_whitelist", "") or "").split() if x]
        if npub not in wl:
            wl.append(npub)
            settings_store.put("blossom_whitelist", "\n".join(wl))
    except Exception as e:
        logger.warning("[provision] blossom grant failed: %s", e)

    # 2) avatar → upload to the built-in Blossom under the bot's (now whitelisted) key
    host = request.url.hostname or ""
    nip05 = (payload.nip05 or "").strip()
    if nip05 and "@" not in nip05:
        nip05 = f"{nip05}@{host}"
    base = str(request.base_url).rstrip("/")
    picture = (payload.picture or "").strip() or f"{base}/static/posterchan-relay.png"
    if (payload.picture_data or "").strip():
        try:
            import base64 as _b64
            from app.services.nostr import media as _media
            raw = payload.picture_data.strip()
            mime = "image/png"
            if raw.startswith("data:"):
                head, _, b64 = raw.partition(",")
                if "image/" in head:
                    mime = head[head.index("image/"):].split(";")[0]
                raw = b64
            data = _b64.b64decode(raw)
            endpoint = f"http://127.0.0.1:{os.getenv('POSTERCHANAI_PORT', '3051')}/blossom"
            info = await _media.upload_blossom(endpoint, sk, data, mime)
            if info.get("url"):
                picture = info["url"]
        except Exception as e:
            logger.warning("[provision] avatar upload failed (falling back to URL/logo): %s", e)

    # 3) TRUST FIRST: operator follows the bot (operator is always WoT) + add the bot to the relay's
    #    WoT now — the relay is WoT-gated, so its kind-0 profile would be REJECTED until it's trusted.
    followed = False
    op_nsec = keystore.get_operator_nsec()
    if op_nsec:
        try:
            op_sk = nostr_service.decode_seckey(op_nsec)
            op_pub = nostr_service.derive_pubkey(op_sk)
            existing = await nostr_service.relay.query(local, [{"authors": [op_pub], "kinds": [3], "limit": 1}]) or []
            existing.sort(key=lambda e: e.get("created_at", 0), reverse=True)
            tags = [t for t in (existing[0].get("tags", []) if existing else []) if t and t[0] == "p"]
            if not any(len(t) >= 2 and t[1] == pub for t in tags):
                tags.append(["p", pub])
            ev3 = _nevent.build_event(op_sk, 3, "", tags=tags)
            await nostr_service.relay.publish(local, ev3)
            followed = True
        except Exception as e:
            logger.warning("[provision] operator follow failed: %s", e)
    try:
        from app.services.nostr_relay.thread import trigger_wot_add, trigger_wot_refresh
        trigger_wot_add([pub])      # immediate in-memory add (control poller, ~1s)
        trigger_wot_refresh()       # full rebuild folds in the operator's new follow
    except Exception as e:
        logger.warning("[provision] wot add/refresh failed: %s", e)

    # 4) publish the kind-0 profile — RETRY until the (now-trusted) relay actually stores it, since
    #    the WoT add is applied asynchronously by the relay's control poller.
    import asyncio
    meta = {"name": payload.name.strip(), "display_name": payload.name.strip(),
            "about": (payload.about or "").strip() or "♟️ #chesstr referee bot", "picture": picture, "bot": True}
    if nip05:
        meta["nip05"] = nip05
    profile_ok = False
    for _ in range(6):
        try:
            ev0 = _nevent.build_event(sk, 0, json.dumps(meta, separators=(",", ":")), tags=[])
            await nostr_service.relay.publish(local, ev0)
            got = await nostr_service.relay.query(local, [{"authors": [pub], "kinds": [0], "limit": 1}]) or []
            if got:
                profile_ok = True
                break
        except Exception as e:
            logger.warning("[provision] profile publish attempt failed: %s", e)
        await asyncio.sleep(1.0)
    if not profile_ok:
        logger.warning("[provision] profile not confirmed stored after retries (npub %s)", npub)

    return {"nsec": nsec, "npub": npub, "nip05": nip05, "picture": picture,
            "followed": followed, "profile_ok": profile_ok}


@router.post("", status_code=status.HTTP_201_CREATED)
def create_bot(payload: BotPayload, db: Session = Depends(get_db),
               admin: User = Depends(get_admin_user)):
    bot = Bot(
        name=payload.name.strip(),
        enabled=payload.enabled,
        bot_type=payload.bot_type,
        platform=payload.platform,
        host=(payload.host or "").strip(),
        modes=(payload.modes or "").strip(),
        config=json.dumps(payload.config or {}),
    )
    db.add(bot)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"A bot named '{payload.name}' already exists")
    db.refresh(bot)
    from app.services import bots_store
    bots_store.sync_bot_blocking(db, bot)
    bot_manager_service.reconcile_now()
    _refresh_wot_for_nostr(bot)
    return _serialize(bot)


@router.put("/{bot_id}")
def update_bot(bot_id: int, payload: BotUpdate, db: Session = Depends(get_db),
               admin: User = Depends(get_admin_user)):
    bot = db.query(Bot).filter(Bot.id == bot_id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")
    old_name = bot.name
    if payload.name is not None:
        bot.name = payload.name.strip()
    if payload.enabled is not None:
        bot.enabled = payload.enabled
    if payload.bot_type is not None:
        bot.bot_type = payload.bot_type
    if payload.platform is not None:
        bot.platform = payload.platform
    if payload.host is not None:
        bot.host = payload.host.strip()
    if payload.modes is not None:
        bot.modes = payload.modes.strip()
    if payload.config is not None:
        bot.config = json.dumps(payload.config)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="Bot name must be unique")
    db.refresh(bot)
    from app.services import bots_store
    if old_name != bot.name:
        bots_store.delete_bot_blocking(db, old_name)   # drop the stale relay doc on rename
    bots_store.sync_bot_blocking(db, bot)
    # config/cred/mode changes need a respawn; nudge a reconcile and restart the running child.
    bot_manager_service.restart_bot(bot.name)
    _refresh_wot_for_nostr(bot)
    return _serialize(bot)


@router.delete("/{bot_id}")
def delete_bot(bot_id: int, db: Session = Depends(get_db),
               admin: User = Depends(get_admin_user)):
    bot = db.query(Bot).filter(Bot.id == bot_id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")
    name = bot.name
    _cleanup_nostr_identity(db, bot)   # remove its account data BEFORE the row is gone
    db.delete(bot)
    db.commit()
    from app.services import bots_store
    bots_store.delete_bot_blocking(db, name)
    bot_manager_service.reconcile_now()  # manager stops the now-absent child
    return {"status": "deleted", "name": name}


def _cleanup_nostr_identity(db: Session, bot: Bot):
    """Tear down a deleted nostr bot's identity: revoke Blossom access + purge its blobs, purge its
    relay events (kind-0 profile/nip05, posts, app-data), and have the operator unfollow it."""
    if (bot.platform or "") != "nostr":
        return
    try:
        nsec = (json.loads(bot.config or "{}")).get("nostr_nsec")
        if not nsec:
            return
        sk = nostr_service.decode_seckey(nsec)
        pub = nostr_service.derive_pubkey(sk)
        npub = nostr_service.npub_of(pub)
    except Exception as e:
        logger.warning("[bot-delete] could not derive identity: %s", e)
        return
    from app.services import settings_store, blossom_service
    # 1) revoke Blossom upload access
    try:
        wl = [x for x in (settings_store.get("blossom_whitelist", "") or "").split() if x and x != npub]
        settings_store.put("blossom_whitelist", "\n".join(wl))
    except Exception as e:
        logger.warning("[bot-delete] whitelist revoke failed: %s", e)
    # 2) purge its Blossom blobs (bytes + index rows)
    try:
        for blob in blossom_service.list_for_pubkey(db, pub):
            try:
                _run_async(blossom_service.delete_blob_bytes(db, blob))
            except Exception:
                pass
            db.delete(blob)
        db.commit()
    except Exception as e:
        db.rollback()
        logger.warning("[bot-delete] blossom purge failed: %s", e)
    # 3) purge its relay events (profile/nip05/posts/app-data) + operator unfollow
    try:
        from app.services.nostr_relay.thread import trigger_delete_author
        trigger_delete_author([pub])
    except Exception as e:
        logger.warning("[bot-delete] relay author purge failed: %s", e)
    _operator_unfollow(pub)


def _operator_unfollow(pub: str):
    """Remove `pub` from the operator's kind-3 contact list (publish the updated list, never wipe)."""
    try:
        from app.services import keystore, settings_store
        from app.services.nostr import event as _nevent
        op_nsec = keystore.get_operator_nsec()
        if not op_nsec:
            return
        op_sk = nostr_service.decode_seckey(op_nsec)
        op_pub = nostr_service.derive_pubkey(op_sk)
        port = int(settings_store.get("nostr_relay_port", "3052") or "3052")
        local = [f"ws://127.0.0.1:{port}"]

        async def _go():
            existing = await nostr_service.relay.query(local, [{"authors": [op_pub], "kinds": [3], "limit": 1}]) or []
            existing.sort(key=lambda e: e.get("created_at", 0), reverse=True)
            if not existing:
                return
            tags = [t for t in existing[0].get("tags", []) if t and t[0] == "p" and not (len(t) >= 2 and t[1] == pub)]
            ev3 = _nevent.build_event(op_sk, 3, "", tags=tags)
            await nostr_service.relay.publish(local, ev3)
        _run_async(_go())
    except Exception as e:
        logger.warning("[bot-delete] operator unfollow failed: %s", e)


def _run_async(coro):
    """Run a coroutine from this sync endpoint (no running loop in the request thread)."""
    import asyncio
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            return ex.submit(lambda: asyncio.run(coro)).result()
    return asyncio.run(coro)


@router.post("/{bot_id}/start")
def start_bot(bot_id: int, db: Session = Depends(get_db),
              admin: User = Depends(get_admin_user)):
    bot = db.query(Bot).filter(Bot.id == bot_id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")
    bot.enabled = True
    db.commit()
    from app.services import bots_store
    bots_store.sync_bot_blocking(db, bot)
    bot_manager_service.reconcile_now()
    return {"status": "started", "name": bot.name}


@router.post("/{bot_id}/stop")
def stop_bot(bot_id: int, db: Session = Depends(get_db),
             admin: User = Depends(get_admin_user)):
    bot = db.query(Bot).filter(Bot.id == bot_id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")
    bot.enabled = False
    db.commit()
    from app.services import bots_store
    bots_store.sync_bot_blocking(db, bot)
    bot_manager_service.reconcile_now()
    return {"status": "stopped", "name": bot.name}


@router.post("/{bot_id}/restart")
def restart_bot(bot_id: int, db: Session = Depends(get_db),
                admin: User = Depends(get_admin_user)):
    bot = db.query(Bot).filter(Bot.id == bot_id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")
    if not bot.enabled:
        bot.enabled = True
        db.commit()
    bot_manager_service.restart_bot(bot.name)
    return {"status": "restarted", "name": bot.name}


@router.post("/{bot_id}/test-post/preview")
async def test_post_preview(bot_id: int, db: Session = Depends(get_db),
                            admin: User = Depends(get_admin_user)):
    """Generate from the bot's SAVED config and return it WITHOUT publishing.
    Text bots → the generated post text; image bots → a generated image (base64)."""
    bot = db.query(Bot).filter(Bot.id == bot_id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")

    if bot.bot_type == "image":
        try:
            cfg = json.loads(bot.config) if bot.config else {}
        except (ValueError, TypeError):
            cfg = {}
        prompt = (cfg.get("prompt") or "").strip()
        if not prompt:
            return {"ok": False, "error": "This image bot has no prompt set."}
        # Mirror imageposter: if random scenes are on, append one so the preview matches posts.
        if cfg.get("random_scenes"):
            scene = bot_manager_service.random_scene()
            if scene:
                prompt = f"{prompt}, {scene}"
        negative = (cfg.get("image_negative") or "").strip()
        from app.services.image_factory import generate_image_with_load_balancing
        img = await generate_image_with_load_balancing(db=db, prompt=prompt, negative_prompt=negative)
        if img:
            return {"ok": True, "image": img}
        return {"ok": False, "error": "Image generation failed (check image servers)."}

    # Text bots: the preview spawns a blocking subprocess — run it off the event loop.
    import asyncio
    return await asyncio.get_event_loop().run_in_executor(None, bot_manager_service.preview_post, bot.name)


@router.post("/{bot_id}/test-post/publish")
def test_post_publish(bot_id: int, db: Session = Depends(get_db),
                      admin: User = Depends(get_admin_user)):
    """Fire one real post now from the bot's SAVED config, bypassing the schedule
    (Test → Publish now in the editor)."""
    bot = db.query(Bot).filter(Bot.id == bot_id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")
    return bot_manager_service.publish_post(bot.name)
