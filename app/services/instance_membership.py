"""App membership: a NIP-05 name this node granted to the key. That is the whole rule.

THE PROFILE IS NOT CONSULTED. It used to be: a member also had to publish that exact address in their
signed kind-0 -- and a kind-0 holds ONE nip05, so anybody with an identity of their own
(bob@nostrplebs.com) had to give it up to use this node ("the entire point was to display both"). The
registry is the authority -- written by this node, never by a profile, which anyone can fill in with
anything -- so it alone decides, and the client shows the profile's address and this node's names side
by side. That also took a relay round trip (and a 503 whenever a relay was slow) out of every app
request. Revoking is what it always really was: an admin removes the name, or the relay blocks the key.
"""
from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import HTTPException

from app.services import settings_store
from app.services.nostr import nostr_service
from app.services.nostr_relay.thread import _parse_nip05


def _configuration():
    if not settings_store.is_hydrated():
        raise HTTPException(503, 'Instance membership settings are not loaded yet')
    return (settings_store.get('nostr_relay_nip05_names', '') or '',
            settings_store.get('nostr_relay_nip05_domain', '') or '',
            settings_store.get('site_url', '') or '')


def _domain(value):
    value = value.strip().lstrip('@').lower().rstrip('.')
    if not value or any(c in value for c in '/:@?#'):
        return ''
    try:
        return value.encode('idna').decode('ascii')
    except UnicodeError:
        return ''


def _address(value):
    if not isinstance(value, str) or value.strip().count('@') != 1:
        return ''
    name, host = value.strip().split('@')
    domain = _domain(host)
    return name + '@' + domain if name and domain else ''


DENIED = 'This app is for members of this server -- ask its admin for a name here'


class MembershipChecker:
    def __init__(self, *, configuration=None):
        self.configuration = configuration or _configuration

    async def status(self, pubkey, *, force=False):
        """`force` is accepted for callers that ask for a fresh answer; every answer is fresh now."""
        pk = nostr_service.to_pubkey_hex(pubkey or '')
        if not pk:
            raise HTTPException(403, 'A signed-in Nostr account is required')
        pk = pk.lower()
        config = tuple(self.configuration())
        names, _ = _parse_nip05(config[0], '')
        domain = _domain(config[1]) or _domain(urlsplit(config[2]).hostname or '')
        aliases = sorted(name for name, owner in names.items() if owner.lower() == pk)
        base = {'pubkey': pk, 'qualified': False, 'address': '', 'addresses': [], 'domain': domain,
                'reason': 'unregistered'}
        # Blocked on the relay = not a member, whatever the registry says.
        from app.services import relay_blocklist
        if relay_blocklist.is_blocked(pk):
            return {**base, 'reason': 'blocked'}
        if not aliases:
            return base
        if not domain:
            raise HTTPException(503, 'Instance NIP-05 domain is not configured')
        addresses = [a + '@' + domain for a in aliases]
        return {**base, 'qualified': True, 'address': addresses[0], 'addresses': addresses, 'reason': 'qualified'}

    async def require_pubkey(self, pubkey):
        result = await self.status(pubkey)
        if not result['qualified']:
            raise HTTPException(403, DENIED)
        return result

    async def require_user(self, user):
        await self.require_pubkey(getattr(user, 'nostr_npub', '') or '')
        return user


_checker = MembershipChecker()


async def status(pubkey, *, force=False):
    """Real membership: a name this node granted to the key (and the key not blocked)."""
    return await _checker.status(pubkey, force=force)


# THE ADMIN CAN OPEN THE APPS TO EVERY SIGNED-IN ACCOUNT ("make it a toggle that we can do in admin").
# `apps_require_nip05` (Admin -> Nostr Relay), ON by default -- a blank row reads as ON, because a blank
# switching the requirement off would open Mail, Files and the rest node-wide with nothing said. Off:
# any signed-in Nostr account passes the APP gates below, except one the relay has blocked. It never
# changes `status()`: real membership still decides the wallets (require_member_*), the NIP-05 grants
# of AI/image/music/Blossom (nip05_access) and the name application on the welcome screen.
def apps_require_nip05() -> bool:
    if not settings_store.is_hydrated():
        return True                      # settings not loaded: the strict answer, never the open one
    v = str(settings_store.get('apps_require_nip05', '') or '').strip().lower()
    return v not in ('false', '0', 'off', 'no')


async def access(pubkey, *, force=False):
    """What the app gates and the client's app list go by: membership, or -- with the requirement
    switched off -- any signed-in account the relay has not blocked."""
    if apps_require_nip05():
        return await _checker.status(pubkey, force=force)
    pk = nostr_service.to_pubkey_hex(pubkey or '')
    if not pk:
        raise HTTPException(403, 'A signed-in Nostr account is required')
    pk = pk.lower()
    base = {'pubkey': pk, 'qualified': False, 'address': '', 'addresses': [], 'domain': '', 'reason': 'blocked'}
    from app.services import relay_blocklist
    if relay_blocklist.is_blocked(pk):
        return base
    return {**base, 'qualified': True, 'reason': 'open'}


async def require_pubkey(pubkey):
    result = await access(pubkey)
    if not result['qualified']:
        raise HTTPException(403, DENIED)
    return result


async def require_user(user):
    await require_pubkey(getattr(user, 'nostr_npub', '') or '')
    return user


async def require_member_pubkey(pubkey):
    """Real membership whatever the switch says (the wallets)."""
    return await _checker.require_pubkey(pubkey)


async def require_member_user(user):
    return await _checker.require_user(user)
