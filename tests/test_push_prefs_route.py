"""POST /api/push/prefs — the only way the toggles ever reach the push watcher.

The preferences live in a kind-30078 document encrypted to the user's own key, so this node cannot
read them; the client mirrors them onto its own subscription rows through here. Two things must
hold, and each is a real loss if it does not:

  * IT IS AUTHENTICATED, and bound to its purpose. Without that, knowing somebody's npub is enough
    to switch their notifications off — and a silenced alert is invisible to the person it belonged
    to, so nobody would ever report it.
  * IT SCOPES TO ONE DEVICE. These are per-device on purpose: a phone set to mentions-only must not
    silence the same events on a desktop. An unscoped write puts every device back under one list,
    which is the behaviour this whole feature exists to undo.
"""
import base64
import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.models import PushSubscription
from app.routers import push as push_router
from app.services.nostr import bip340, event as nostr_event

SK = bytes(range(1, 33))
PK = bip340.pubkey_from_seckey(SK).hex()


def _b64(ev):
    return base64.b64encode(json.dumps(ev).encode()).decode()


def _auth(content="push-prefs", created_at=None, seckey=SK):
    ev = nostr_event.build_event(seckey, 27235, content, [["p", PK]],
                                 int(created_at) if created_at else None)
    return _b64(ev)


@pytest.fixture
def client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[PushSubscription.__table__])
    db = Session(engine)
    db.add(PushSubscription(pubkey=PK, endpoint="direct:%s:phone-device-0001" % PK,
                            transport="direct", device_id="phone-device-0001"))
    db.add(PushSubscription(pubkey=PK, endpoint="https://push.example/desktop",
                            transport="webpush", p256dh="p", auth="a"))
    db.commit()
    api = FastAPI()
    api.include_router(push_router.router)
    api.dependency_overrides[get_db] = lambda: db
    yield TestClient(api), db
    db.close()


def _rows(db):
    return {r.endpoint: (json.loads(r.prefs) if r.prefs else None)
            for r in db.query(PushSubscription).all()}


def test_a_named_device_is_the_only_one_that_changes(client):
    api, db = client
    r = api.post("/api/push/prefs", json={"pubkey": PK, "auth": _auth(),
                                          "device_id": "phone-device-0001",
                                          "prefs": {"likes": False, "zaps": False}})
    assert r.json() == {"ok": True, "devices": 1}
    rows = _rows(db)
    assert rows["direct:%s:phone-device-0001" % PK] == {"likes": False, "zaps": False}
    assert rows["https://push.example/desktop"] is None, \
        "a phone's preferences must never reach the desktop's row"


def test_a_web_push_device_scopes_by_its_endpoint(client):
    """Web Push rows carry no device id — the endpoint IS the device."""
    api, db = client
    r = api.post("/api/push/prefs", json={"pubkey": PK, "auth": _auth(),
                                          "endpoint": "https://push.example/desktop",
                                          "prefs": {"likes": False}})
    assert r.json()["devices"] == 1
    rows = _rows(db)
    assert rows["https://push.example/desktop"] == {"likes": False}
    assert rows["direct:%s:phone-device-0001" % PK] is None


def test_without_a_device_it_is_an_explicit_all_of_mine(client):
    api, db = client
    r = api.post("/api/push/prefs", json={"pubkey": PK, "auth": _auth(),
                                          "prefs": {"likes": False}})
    assert r.json()["devices"] == 2
    assert all(v == {"likes": False} for v in _rows(db).values())


def test_it_refuses_an_unsigned_call(client):
    api, db = client
    r = api.post("/api/push/prefs", json={"pubkey": PK, "prefs": {"likes": False}})
    assert r.json() == {"ok": False, "error": "auth required"}
    assert all(v is None for v in _rows(db).values())


def test_a_proof_for_something_else_cannot_silence_you(client):
    """`verify_self_auth` binds the content to the purpose, so a proof captured from the SUBSCRIBE
    call is not a licence to rewrite what that device is allowed to tell you about."""
    api, db = client
    r = api.post("/api/push/prefs", json={"pubkey": PK, "auth": _auth("push-subscribe"),
                                          "prefs": {"likes": False}})
    assert r.json()["ok"] is False
    assert all(v is None for v in _rows(db).values())


def test_a_stale_proof_is_refused(client):
    api, db = client
    r = api.post("/api/push/prefs", json={"pubkey": PK,
                                          "auth": _auth(created_at=time.time() - 3600),
                                          "prefs": {"likes": False}})
    assert r.json()["ok"] is False


def test_somebody_elses_key_cannot_sign_for_you(client):
    api, db = client
    other = bytes(range(2, 34))
    r = api.post("/api/push/prefs",
                 json={"pubkey": PK, "auth": _auth(seckey=other), "prefs": {"likes": False}})
    assert r.json()["ok"] is False
    assert all(v is None for v in _rows(db).values())


def test_only_real_toggles_are_stored(client):
    """An unknown key stored now is a permanent silent mute for a type nothing displays later."""
    api, db = client
    api.post("/api/push/prefs", json={"pubkey": PK, "auth": _auth(),
                                      "device_id": "phone-device-0001",
                                      "prefs": {"likes": False, "made_up": False, "zaps": "no"}})
    assert _rows(db)["direct:%s:phone-device-0001" % PK] == {"likes": False}


def test_a_bad_device_id_is_refused_rather_than_widened(client):
    """Falling through to 'every device' on a malformed id would silence the lot."""
    api, db = client
    r = api.post("/api/push/prefs", json={"pubkey": PK, "auth": _auth(),
                                          "device_id": "../../etc", "prefs": {"likes": False}})
    assert r.json() == {"ok": False, "error": "invalid device_id"}
    assert all(v is None for v in _rows(db).values())
