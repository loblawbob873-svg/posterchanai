"""Instance-name welcome flow, authenticated with a purpose-bound Nostr proof."""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.nostr import event, nostr_service
from app.services import instance_welcome as service, settings_store

router = APIRouter(prefix='/api/instance-welcome', tags=['instance welcome'])


class Proof(BaseModel):
    pubkey: str = Field(max_length=100)
    auth: str = Field(max_length=8192)


def verify(data, purpose):
    pk = nostr_service.to_pubkey_hex(data.pubkey)
    if not pk or not event.verify_self_auth(data.auth, pk, purpose):
        raise HTTPException(403, 'A recent signed proof for this action is required')
    return pk


def address_for(db, request, pk):
    from app.routers.client import _nip05_domain
    from app.services.nostr_relay.thread import _parse_nip05
    names, _ = _parse_nip05(settings_store.get('nostr_relay_nip05_names', '') or '', '')
    name = next((name for name, owner in names.items() if owner == pk), '')
    return f'{name}@{_nip05_domain(request, db)}' if name else ''


@router.post('/status')
async def status(data: Proof, request: Request, db: Session = Depends(get_db)):
    pk = verify(data, 'instance-welcome-status')
    address = address_for(db, request, pk)
    row = service.application(db, pk)
    if row:
        if address:
            await service.notify_approval(db, pk, address)
        else:
            await service.notify_admins(db, row)
    return {'eligible': not bool(address), 'address': address,
            'pending': bool(row and not address),
            'site_name': settings_store.get('site_name', '') or 'this instance'}


@router.post('/apply')
async def apply(data: Proof, request: Request, db: Session = Depends(get_db)):
    pk = verify(data, 'instance-welcome-apply')
    address = address_for(db, request, pk)
    if address:
        return {'ok': True, 'already': True, 'address': address}
    row, created = service.apply(db, pk)
    notified = await service.notify_admins(db, row)
    return {'ok': True, 'pending': True, 'created': created, 'admin_notified': notified}
