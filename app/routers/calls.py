"""Voice/video call support endpoints.

The heavy lifting (WebRTC media, signaling) is peer-to-peer + over Nostr; the server's only job is to
hand each authenticated user a short-lived ICE configuration:

- STUN + TURN pointing at the built-in Pion relay (app/services/turn_service.py), when it's enabled.
- Short-lived TURN REST credentials minted with HMAC-SHA1 over the same `turn_shared_secret` the Pion
  server validates — so there's no shared state and no static passwords, and creds expire on their own.

P2P-first: most calls connect directly and never touch the relay; TURN is only the NAT fallback.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time

from fastapi import APIRouter, Depends

from app.auth import get_current_user
from app.models import User
from app.services import settings_store

router = APIRouter(prefix="/api/calls", tags=["calls"])

_CRED_TTL = 3600  # seconds a minted TURN credential stays valid


def _split_urls(raw: str) -> list[str]:
    return [u.strip() for u in (raw or "").replace("\n", ",").split(",") if u.strip()]


@router.get("/turn-credentials")
def turn_credentials(current_user: User = Depends(get_current_user)):
    """Return an ICE-server list (RTCConfiguration.iceServers shape) for this user.

    When the built-in TURN relay is enabled, includes STUN + TURN (udp/tcp, and turns:// on the TLS port
    if configured) with fresh REST credentials. Otherwise falls back to any configured public STUN so
    P2P can still gather server-reflexive candidates.
    """
    cfg = settings_store.all_settings()
    ice: list[dict] = []

    calls_on = (cfg.get("calls_enabled", "true") or "").strip().lower() == "true"
    enabled = (cfg.get("turn_enabled", "false") or "").strip().lower() == "true"
    secret = (cfg.get("turn_shared_secret", "") or "").strip()
    public_ip = (cfg.get("turn_public_ip", "") or "").strip()
    domain = (cfg.get("turn_domain", "") or "").strip()
    host = domain or public_ip
    port = (cfg.get("turn_port", "") or "3478").strip()
    tls_port = (cfg.get("turn_tls_port", "") or "").strip()
    tls_cert = (cfg.get("turn_tls_cert", "") or "").strip()
    tls_key = (cfg.get("turn_tls_key", "") or "").strip()

    # Only mint relay credentials when calls are enabled AND the relay is actually runnable. turn_service
    # requires turn_public_ip (a domain alone isn't enough), so mirror that here — otherwise we'd advertise
    # a relay that isn't running and, worse, hand any logged-in user an OPEN UDP relay while calls are off.
    relay_up = calls_on and enabled and bool(secret) and bool(public_ip)
    if relay_up:
        # STUN on the same relay (server-reflexive candidates for P2P).
        # A TURN domain is frequently also the instance's Blossom hostname. If that DNS record is
        # Cloudflare-proxied, HTTPS works perfectly while TURN UDP/TCP can never reach the relay.
        # `turn_public_ip` is already required to launch Pion and is the authoritative bypass. Offer
        # both values (deduped) so normal direct DNS stays readable and split-DNS/proxy deployments
        # still have a usable ICE candidate. These are both Admin UI values; the client hard-codes
        # neither address.
        turn_hosts = list(dict.fromkeys(h for h in (domain, public_ip) if h))
        ice.append({"urls": [f"stun:{h}:{port}" for h in turn_hosts]})
        # Short-lived TURN REST credential (coturn use-auth-secret scheme; Pion validates the same HMAC).
        expiry = int(time.time()) + _CRED_TTL
        username = f"{expiry}:{current_user.id}"
        credential = base64.b64encode(
            hmac.new(secret.encode(), username.encode(), hashlib.sha1).digest()
        ).decode()
        turn_urls = [url for h in turn_hosts for url in (
            f"turn:{h}:{port}?transport=udp",
            f"turn:{h}:{port}?transport=tcp",
        )]
        # Advertise turns:// ONLY when the relay actually opens a TLS listener (needs port + cert + key,
        # matching turn_service._build_env / the Go server) — else clients waste ICE on a closed port.
        if tls_port and tls_cert and tls_key:
            # TLS certificates name the domain, not its numeric address.
            turn_urls.append(f"turns:{host}:{tls_port}?transport=tcp")
        ice.append({"urls": turn_urls, "username": username, "credential": credential})
    elif calls_on:
        # No self-hosted relay: offer any configured public STUN so P2P still works across simple NATs.
        stun = _split_urls(cfg.get("stun_fallback_urls", ""))
        if stun:
            ice.append({"urls": [s if "://" in s or s.startswith("stun:") else f"stun:{s}" for s in stun]})

    return {
        "iceServers": ice,
        "ttl": _CRED_TTL,
        "callsEnabled": calls_on,
        "defaultVideo": (cfg.get("calls_default_video", "false") or "").strip().lower() == "true",
        "relay": relay_up,
    }
