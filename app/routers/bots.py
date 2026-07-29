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
from app.services import bot_manager_service, pleroma_service
from app.services.nostr import nostr_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/bots", tags=["bots"])


class BotPayload(BaseModel):
    name: str
    enabled: bool = True
    bot_type: str = "text"          # "text" | "image"
    platform: str = "pleroma"       # "pleroma"
    host: Optional[str] = ""        # node hostname; empty = any node
    modes: Optional[str] = ""       # comma-separated main.py flags
    config: Dict[str, Any] = {}     # all other per-bot fields (creds, prompt, feature opts)


class OAuthTokenPayload(BaseModel):
    platform: str = "pleroma"       # "pleroma" (OAuth password grant)
    server: str                     # instance URL, e.g. https://poster.place
    username: str                   # bot account login (handle, no leading @)
    password: str
    scopes: str = "read write follow push"


class BotUpdate(BaseModel):
    name: Optional[str] = None
    enabled: Optional[bool] = None
    bot_type: Optional[str] = None
    platform: Optional[str] = None
    host: Optional[str] = None
    modes: Optional[str] = None
    config: Optional[Dict[str, Any]] = None


def _publish_bot_profile(cfg: dict):
    """Publish the bot's kind-0 NOW, directly from the app, using its saved name/nip05/avatar. This
    makes Save update the on-relay profile IMMEDIATELY instead of relying on the bot restarting and
    re-running ensure_profile (which only fires on startup — the reason the nip05 'never showed up').
    A bare nip05 ('chess') is expanded to 'chess@<instance host>' the same way the bot's env is."""
    nsec = cfg.get("nostr_nsec")
    if not nsec:
        return
    name = (cfg.get("nostr_profile_name") or "").strip()
    nip05 = (cfg.get("nostr_profile_nip05") or "").strip()
    picture = (cfg.get("nostr_profile_picture") or "").strip()
    if nip05 and "@" not in nip05:
        try:
            nip05 = bot_manager_service._nip05_full(nip05)
        except Exception:
            pass
    if not (name or nip05 or picture):
        return
    meta = {"bot": True}
    if name:
        meta["name"] = name
        meta["display_name"] = name
    if nip05:
        meta["nip05"] = nip05
    if picture:
        meta["picture"] = picture
    import asyncio
    from app.services.nostr import event as _ev, relay as _relay
    seckey = nostr_service.decode_seckey(nsec)
    ev = _ev.build_event(seckey, 0, json.dumps(meta, separators=(",", ":")), tags=[])
    # update_bot is a sync endpoint (threadpool) → no running loop; a short asyncio.run is fine.
    asyncio.run(_relay.publish(["ws://127.0.0.1:3052"], ev))


def _refresh_wot_for_nostr(bot: Bot):
    """A newly created/updated nostr bot is an operator key — fold it into the relay's WoT now so its
    posts are accepted immediately (rather than waiting for the scheduled rebuild). Also (re)register
    its NIP-05 name in the relay's served list so name@host resolves, and republish its kind-0 NOW so
    the profile (name/nip05/avatar) updates on Save."""
    if (bot.platform or "") != "nostr":
        return
    try:
        from app.services.nostr_relay.thread import trigger_wot_refresh
        trigger_wot_refresh()
    except Exception:
        pass
    try:
        cfg = json.loads(bot.config or "{}")
        nip = (cfg.get("nostr_profile_nip05") or "").strip()
        nsec = cfg.get("nostr_nsec")
        if nip and nsec:
            pub = nostr_service.derive_pubkey(nostr_service.decode_seckey(nsec))
            _nip05_set(nip.split("@", 1)[0], pub)
    except Exception as e:
        logger.warning("[bots] nip05 register on save failed: %s", e)
    # Publish the kind-0 immediately so the profile shows the new name/nip05/avatar without waiting for
    # the bot to restart (independent of the registration above so one failing can't block the other).
    try:
        _publish_bot_profile(json.loads(bot.config or "{}"))
    except Exception as e:
        logger.warning("[bots] publish kind-0 profile on save failed: %s", e)


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
    connect a bot in the UI without running a script or a browser auth flow, via the Pleroma
    OAuth password grant. The token is returned for the caller to save into the bot's config;
    nothing is persisted here."""
    server = (payload.server or "").strip()
    username = (payload.username or "").lstrip("@").strip()
    if not server or not username or not payload.password:
        raise HTTPException(status_code=400, detail="Server, username and password are required")
    try:
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


@router.post("/provision")
async def provision_nostr_identity(payload: ProvisionPayload, request: Request,
                                   db: Session = Depends(get_db),
                                   admin: User = Depends(get_admin_user)):
    """Mint a fresh Nostr identity for a bot and wire up the parts only the SERVER can do: generate
    the nsec, grant it Blossom upload access, make the OPERATOR follow it + add it to the relay's Web
    of Trust. The profile (name/nip05/avatar) is saved in the bot's config and published by the bot
    itself on startup (when it's an operator key → always accepted), which is far more reliable than
    racing the WoT here. Returns the nsec+npub (+ resolved nip05) for the form to save."""
    from app.services.nostr import bip340, bech32, event as _nevent
    from app.services import settings_store, keystore

    sk = os.urandom(32)
    pub = bip340.pubkey_from_seckey(sk).hex()
    nsec = bech32.encode("nsec", sk)
    npub = nostr_service.npub_of(pub)
    port = int(settings_store.get("nostr_relay_port", "3052") or "3052")
    local = [f"ws://127.0.0.1:{port}"]

    # 1) Blossom upload access: a bot's key is already an OPERATOR key (blossom_service._operator_pubkeys
    #    scans every Bot row), so is_pubkey_allowed() accepts its uploads WITHOUT a whitelist entry.
    #    Adding it here was redundant — and worse, it read-modify-wrote the SHARED blossom_whitelist from
    #    a possibly-empty/stale in-process cache (pre-hydration or clobbered by another node), silently
    #    dropping human grants (e.g. the admin) from the list. Don't touch the whitelist for bots.

    # 2) resolve the nip05 (local part → name@thishost) — the actual registration happens on Save
    host = request.url.hostname or ""
    nip05 = (payload.nip05 or "").strip()
    if nip05 and "@" not in nip05:
        nip05 = f"{nip05}@{host}"

    # 3) operator follows the bot + add it to the relay's WoT now (so its posts/profile are accepted)
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
        trigger_wot_add([pub])
        trigger_wot_refresh()
    except Exception as e:
        logger.warning("[provision] wot add/refresh failed: %s", e)

    return {"nsec": nsec, "npub": npub, "nip05": nip05, "followed": followed}


class AvatarPayload(BaseModel):
    bot_id: Optional[int] = None     # existing bot → sign with its stored nsec
    nsec: Optional[str] = ""         # new bot being created → the just-minted nsec
    picture_data: str = ""           # data: URL / base64


@router.post("/upload-avatar")
async def upload_bot_avatar(payload: AvatarPayload, db: Session = Depends(get_db),
                            admin: User = Depends(get_admin_user)):
    """Upload an avatar image to the built-in Blossom (signed by the bot's key) and return its public
    URL, for the form's Avatar field. Works for a new bot (pass the minted `nsec`) or an existing one
    (pass `bot_id` → its stored nsec)."""
    import base64 as _b64
    from app.services.nostr import media as _media
    nsec = (payload.nsec or "").strip()
    if not nsec and payload.bot_id is not None:
        bot = db.query(Bot).filter(Bot.id == payload.bot_id).first()
        if bot:
            try:
                nsec = (json.loads(bot.config or "{}")).get("nostr_nsec", "")
            except (ValueError, TypeError):
                nsec = ""
    if not nsec:
        raise HTTPException(status_code=400, detail="no bot key (generate an identity or save the bot first)")
    if not (payload.picture_data or "").strip():
        raise HTTPException(status_code=400, detail="no image provided")
    try:
        sk = nostr_service.decode_seckey(nsec)
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
        if not info.get("url"):
            raise RuntimeError("no url returned")
        return {"url": info["url"]}
    except Exception as e:
        logger.warning("[upload-avatar] failed: %s", e)
        raise HTTPException(status_code=500, detail=f"upload failed: {e}")


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
    # Bot operator key may be new/changed → refresh the Blossom operator auth set so its uploads are
    # accepted immediately (bots are authorized via the operator set, not the whitelist), rather than
    # waiting up to the operator-cache TTL.
    try:
        from app.services import blossom_service
        blossom_service.invalidate_operator_cache()
    except Exception:
        pass
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
    # Bot operator key may be new/changed → refresh the Blossom operator auth set so its uploads are
    # accepted immediately (bots are authorized via the operator set, not the whitelist), rather than
    # waiting up to the operator-cache TTL.
    try:
        from app.services import blossom_service
        blossom_service.invalidate_operator_cache()
    except Exception:
        pass
    from app.services import bots_store
    if old_name != bot.name:
        bots_store.delete_bot_blocking(db, old_name)   # drop the stale relay doc on rename
    bots_store.sync_bot_blocking(db, bot)
    # Register the NIP-05 name + WoT FIRST, then restart — so when the bot republishes its kind-0 on
    # startup the name already resolves in /.well-known/nostr.json (was: restart raced the register).
    _refresh_wot_for_nostr(bot)
    # config/cred/mode changes need a respawn; nudge a reconcile and restart the running child.
    bot_manager_service.restart_bot(bot.name)
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
    # Deleted operator key → drop it from the Blossom operator auth set now, not up to a TTL later.
    try:
        from app.services import blossom_service
        blossom_service.invalidate_operator_cache()
    except Exception:
        pass
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
    except Exception as e:
        logger.warning("[bot-delete] could not derive identity: %s", e)
        return
    from app.services import blossom_service
    # 1) Blossom access needs no revoke: the bot was allowed as an OPERATOR key (a live Bot row), not via
    #    the whitelist, so deleting the Bot row already ends its upload access once _operator_pubkeys
    #    re-scans. We deliberately DON'T rewrite the shared blossom_whitelist here — that read-modify-write
    #    from a possibly-empty/stale cache was silently dropping human grants (e.g. the admin).
    # 1b) remove its NIP-05 name from the relay's served list
    _nip05_remove_pubkey(pub)
    # 2) purge its Blossom blobs (bytes + index rows)
    try:
        for blob in blossom_service.list_for_pubkey(db, pub):
            try:
                _run_async(blossom_service.delete_blob_bytes(db, blob, fresh_client=True))   # off-main-loop (_run_async) → own httpx client, else the proxy delete silently fails and orphans the bytes
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


def _nip05_set(name: str, pubkey_hex: str):
    """Add/replace this bot's NIP-05 mapping in the relay's served names list (one 'name hex' per
    line), then reload so /.well-known/nostr.json resolves it. Drops any prior line for the same
    name or pubkey first."""
    try:
        from app.services import settings_store
        # Read-modify-write of the SHARED names list — if the cache isn't synced with the relay yet,
        # get() returns "" (not loaded, not empty) and we'd wipe every other bot's name. Skip until
        # hydrated (this is a post-startup admin action, so it's normally already true).
        if not settings_store.is_hydrated():
            logger.warning("[provision] nip05 register skipped — settings not hydrated yet")
            return
        raw = settings_store.get("nostr_relay_nip05_names", "") or ""
        kept = []
        for ln in raw.split("\n"):
            s = ln.strip()
            if s and not s.startswith("#"):
                toks = s.replace("=", " ").replace(",", " ").split()
                if len(toks) >= 2 and (toks[0] == name or nostr_service.to_pubkey_hex(toks[1]) == pubkey_hex):
                    continue
            kept.append(ln)
        kept.append(f"{name} {pubkey_hex}")
        settings_store.put("nostr_relay_nip05_names", "\n".join(l for l in kept if l.strip()))
        from app.services.nostr_relay.thread import trigger_nip05_reload
        trigger_nip05_reload()
    except Exception as e:
        logger.warning("[provision] nip05 register failed: %s", e)


def _nip05_remove_pubkey(pubkey_hex: str):
    """Remove any NIP-05 name lines that map to this pubkey (bot deleted), then reload."""
    try:
        from app.services import settings_store
        # Same guard as _nip05_set: don't rewrite the shared names list from an unhydrated (empty) cache,
        # or we'd wipe every other bot's NIP-05 name. Skipping the removal just leaves a harmless stale
        # line until the next hydrated write.
        if not settings_store.is_hydrated():
            logger.warning("[bot-delete] nip05 remove skipped — settings not hydrated yet")
            return
        raw = settings_store.get("nostr_relay_nip05_names", "") or ""
        kept = []
        for ln in raw.split("\n"):
            s = ln.strip()
            if s and not s.startswith("#"):
                toks = s.replace("=", " ").replace(",", " ").split()
                if len(toks) >= 2 and nostr_service.to_pubkey_hex(toks[1]) == pubkey_hex:
                    continue
            kept.append(ln)
        settings_store.put("nostr_relay_nip05_names", "\n".join(l for l in kept if l.strip()))
        from app.services.nostr_relay.thread import trigger_nip05_reload
        trigger_nip05_reload()
    except Exception as e:
        logger.warning("[bot-delete] nip05 remove failed: %s", e)


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


@router.post("/{bot_id}/delete-posts")
async def delete_bot_posts(bot_id: int, db: Session = Depends(get_db),
                           admin: User = Depends(get_admin_user)):
    """Delete ALL of a nostr bot's kind-1 posts via NIP-09 (signed kind-5 deletions). Unlike a raw
    DB wipe, this propagates: the relay drops them AND broadcasts the deletion upstream, and clients
    honour it. Profile (kind-0) and game state (kind-30078) are left alone."""
    from app.services import settings_store
    from app.services.nostr import event as _nevent
    bot = db.query(Bot).filter(Bot.id == bot_id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")
    if (bot.platform or "") != "nostr":
        raise HTTPException(status_code=400, detail="Only nostr bots have posts to delete")
    try:
        cfg = json.loads(bot.config or "{}")
    except (ValueError, TypeError):
        cfg = {}
    nsec = cfg.get("nostr_nsec")
    if not nsec:
        raise HTTPException(status_code=400, detail="Bot has no nostr key")
    sk = nostr_service.decode_seckey(nsec)
    pub = nostr_service.derive_pubkey(sk)
    port = int(settings_store.get("nostr_relay_port", "3052") or "3052")
    local = [f"ws://127.0.0.1:{port}"]
    try:
        evs = await nostr_service.relay.query(local, [{"authors": [pub], "kinds": [1], "limit": 5000}]) or []
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"relay query failed: {e}")
    ids = [e.get("id") for e in evs if e.get("id")]
    # NIP-09: e-tag the ids to delete, batched so a single kind-5 isn't enormous.
    for i in range(0, len(ids), 400):
        chunk = ids[i:i + 400]
        ev = _nevent.build_event(sk, 5, "bulk delete (admin)", tags=[["e", _id] for _id in chunk])
        try:
            await nostr_service.relay.publish(local, ev)
        except Exception as e:
            logger.warning("[bots] delete-posts publish failed: %s", e)
    return {"status": "ok", "deleted": len(ids), "bot": bot.name}


@router.post("/{bot_id}/restart")
def restart_bot(bot_id: int, db: Session = Depends(get_db),
                admin: User = Depends(get_admin_user)):
    bot = db.query(Bot).filter(Bot.id == bot_id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")
    if not bot.enabled:
        bot.enabled = True
        db.commit()
        from app.services import bots_store   # (module isn't imported at file scope; every call site imports locally)
        bots_store.sync_bot_blocking(db, bot)   # write-through to the relay-authoritative store, else
                                                # hydrate reverts enabled→False on the next startup
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
