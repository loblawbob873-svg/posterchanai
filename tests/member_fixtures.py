"""Explicit membership fixtures for tests of downstream app behavior.

Membership itself is exercised with signed profiles in test_instance_membership*.
These stand-ins admit only the test's named principals, never arbitrary users.
"""
from fastapi import HTTPException
from app.services import instance_membership


def allow_keys(monkeypatch, *keys):
    async def pubkey(key):
        if key not in keys:
            raise HTTPException(403,'Fixture account is not a member')
        return {'qualified':True,'pubkey':key}
    async def user(value):
        await pubkey(getattr(value,'nostr_npub',''))
        return value
    monkeypatch.setattr(instance_membership,'require_pubkey',pubkey)
    monkeypatch.setattr(instance_membership,'require_user',user)
