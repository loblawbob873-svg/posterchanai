"""The Nostr half of the VM host, with REAL signed events and REAL NIP-44 — nothing mocked but the relay.

Each rule here is one a relay or an attacker can exercise without any cooperation from the client:
  * a captured request replayed later (clock window + expiration);
  * the same event delivered twice (relay redelivery → seen-LRU, one answer);
  * an honest retry — a NEW event with the SAME idempotency id — must return the stored result and
    must not power the VM twice;
  * a forged signature, a request addressed to another host, an oversized payload;
  * a stranger, who gets nothing back at all.
And the result event itself: e-tag to the request, p-tag to the requester, nofederate, a short
expiration, signed by the node, readable only by the requester.
"""
import asyncio
import json
import time

import pytest

from app.services.nostr import bip340, nip44
from app.services.nostr.event import build_event, verify_event
from app.services.vmhost import domainxml, kinds, transport
from app.services.vmhost.config import VmHostConfig
from app.services.vmhost.service import VmHostService
from app.services.vmhost.storage import Storage
from tests.vmhost_fake import FakeBackend

NODE_SK = bytes.fromhex("01" * 32)
ADMIN_SK = bytes.fromhex("02" * 32)
USER_SK = bytes.fromhex("03" * 32)
STRANGER_SK = bytes.fromhex("04" * 32)
OTHER_HOST_SK = bytes.fromhex("05" * 32)
pub = lambda sk: bip340.pubkey_from_seckey(sk).hex()  # noqa: E731
NODE, ADMIN, USER, STRANGER, OTHER_HOST = map(pub, (NODE_SK, ADMIN_SK, USER_SK, STRANGER_SK, OTHER_HOST_SK))
U1 = "11111111-1111-4111-8111-111111111111"


def setup(tmp_path):
    root = tmp_path / "vms"
    storage = Storage(root)
    storage.ensure()
    cfg = VmHostConfig(enabled=True, storage_dir=str(root), admin_pubkeys=[ADMIN])
    be = FakeBackend()
    (root / U1).mkdir()
    be.add_domain(U1, "alpha", meta=domainxml.VmMeta(owner=ADMIN, assigned=[USER]))

    async def admins():
        return set()
    svc = VmHostService(cfg, be, node_pubkey=NODE, admin_provider=admins, storage=storage)
    published = []

    async def publish(ev):
        published.append(ev)
        return True
    tr = transport.Transport(svc, NODE_SK, publish)
    return tr, svc, be, published


def read(ev, sk):
    return json.loads(nip44.decrypt_from(sk, bytes.fromhex(ev["pubkey"]), ev["content"]))


def go(tr, svc, *events):
    async def inner():
        await svc.refresh_index()
        return [await tr.on_event(e) for e in events]
    return asyncio.run(inner())


def test_a_request_gets_a_signed_encrypted_result_tagged_back_to_it(tmp_path):
    tr, svc, be, published = setup(tmp_path)
    req = transport.build_request(USER_SK, NODE, "vm.power", {"vm": U1, "action": "start"}, "id-1")
    [res] = go(tr, svc, req)
    assert res is not None and published == [res]
    assert res["kind"] == kinds.RES_KIND and res["pubkey"] == NODE and verify_event(res)
    tags = res["tags"]
    assert ["e", req["id"]] in tags and ["p", USER] in tags and ["nofederate"] in tags
    exp = kinds.expiration_of(res)
    assert exp and time.time() < exp <= time.time() + transport.RESULT_TTL + 5
    body = read(res, USER_SK)
    assert body["ok"] is True and body["id"] == "id-1" and body["result"]["vm"]["state"] == "running"
    with pytest.raises(Exception):
        read(res, STRANGER_SK)          # only the requester can read it


def test_a_stranger_gets_no_reply_and_costs_no_signature_check(tmp_path, monkeypatch):
    tr, svc, be, published = setup(tmp_path)
    verified = []
    monkeypatch.setattr(transport.nostr_event, "verify_event", lambda ev: verified.append(ev) or True)
    req = transport.build_request(STRANGER_SK, NODE, "host.whoami", {}, "x")
    assert go(tr, svc, req) == [None]
    assert published == [] and verified == [] and be.calls == [("list_domains",)]


def test_a_request_for_another_host_is_ignored(tmp_path):
    tr, svc, be, published = setup(tmp_path)
    req = transport.build_request(ADMIN_SK, OTHER_HOST, "host.whoami", {}, "x")
    assert go(tr, svc, req) == [None] and published == []


@pytest.mark.parametrize("skew", [-(transport.REQ_MAX_AGE + 30), transport.REQ_MAX_FUTURE + 30])
def test_a_request_outside_the_clock_window_is_dropped(tmp_path, skew):
    tr, svc, be, published = setup(tmp_path)
    req = transport.build_request(ADMIN_SK, NODE, "host.whoami", {}, "x", created_at=int(time.time()) + skew)
    assert go(tr, svc, req) == [None] and published == []


def _raw_request(sk, tags, created_at=None, content=None):
    ts = int(created_at or time.time())
    body = json.dumps({"v": 1, "id": "raw", "op": "host.whoami", "ts": ts, "args": {}})
    enc = content if content is not None else nip44.encrypt_to(sk, bytes.fromhex(NODE), body)
    return build_event(sk, kinds.REQ_KIND, enc, tags, created_at=ts)


@pytest.mark.parametrize("label,tags", [
    ("no expiration", [["p", NODE], ["nofederate"]]),
    ("expired", [["p", NODE], ["nofederate"], ["expiration", str(int(time.time()) - 5)]]),
    ("expiration too far out", [["p", NODE], ["nofederate"], ["expiration", str(int(time.time()) + 5000)]]),
])
def test_expiration_rules(tmp_path, label, tags):
    tr, svc, be, published = setup(tmp_path)
    assert go(tr, svc, _raw_request(ADMIN_SK, tags)) == [None], label
    assert published == []


def test_the_same_event_twice_is_answered_once(tmp_path):
    tr, svc, be, published = setup(tmp_path)
    req = transport.build_request(ADMIN_SK, NODE, "host.whoami", {}, "x")
    a, b = go(tr, svc, req, dict(req))
    assert a is not None and b is None and len(published) == 1


def test_a_retry_with_the_same_id_returns_the_stored_result_without_repeating_it(tmp_path):
    tr, svc, be, published = setup(tmp_path)
    first = transport.build_request(USER_SK, NODE, "vm.power", {"vm": U1, "action": "start"}, "power-1",
                                    created_at=int(time.time()) - 3)
    retry = transport.build_request(USER_SK, NODE, "vm.power", {"vm": U1, "action": "start"}, "power-1")
    assert first["id"] != retry["id"]
    r1, r2 = go(tr, svc, first, retry)
    assert read(r1, USER_SK) == read(r2, USER_SK)
    assert read(r2, USER_SK)["ok"] is True, "a retry must not come back as 'already running'"
    assert [c for c in be.calls if c[0] == "start"] == [("start", U1)]
    assert ["e", retry["id"]] in r2["tags"], "the retry's answer is tagged to the retry"
    fresh = transport.build_request(USER_SK, NODE, "vm.power", {"vm": U1, "action": "start"}, "power-2")
    [r3] = go(tr, svc, fresh)
    assert read(r3, USER_SK)["error"]["code"] == "conflict", "a NEW id is a new operation"


def test_a_forged_signature_is_dropped(tmp_path):
    tr, svc, be, published = setup(tmp_path)
    req = transport.build_request(ADMIN_SK, NODE, "host.whoami", {}, "x")
    req["sig"] = "00" * 64
    assert go(tr, svc, req) == [None] and published == []
    impostor = transport.build_request(STRANGER_SK, NODE, "host.whoami", {}, "x")
    impostor["pubkey"] = ADMIN            # claims to be the admin, signed by somebody else
    assert go(tr, svc, impostor) == [None] and published == []


def test_the_payload_is_bounded(tmp_path):
    tr, svc, be, published = setup(tmp_path)
    tags = [["p", NODE], ["nofederate"], ["expiration", str(int(time.time()) + 60)]]
    big = _raw_request(ADMIN_SK, tags, content="A" * (kinds.MAX_CONTENT + 1))
    assert go(tr, svc, big) == [None] and published == []


def test_an_unreadable_or_wrong_version_request_from_a_known_user_is_answered(tmp_path):
    tr, svc, be, published = setup(tmp_path)
    tags = [["p", NODE], ["nofederate"], ["expiration", str(int(time.time()) + 60)]]
    garbled = _raw_request(ADMIN_SK, tags, content="not nip44")
    old = build_event(ADMIN_SK, kinds.REQ_KIND, nip44.encrypt_to(ADMIN_SK, bytes.fromhex(NODE),
                      json.dumps({"v": 99, "id": "old", "op": "host.whoami", "args": {}})), tags)
    r1, r2 = go(tr, svc, garbled, old)
    assert read(r1, ADMIN_SK)["error"]["code"] == "bad_request"
    assert read(r2, ADMIN_SK)["error"]["code"] == "version"


def test_create_streams_progress_before_the_result(tmp_path):
    tr, svc, be, published = setup(tmp_path)
    req = transport.build_request(ADMIN_SK, NODE, "vm.create",
                                  {"name": "new", "vcpus": 1, "ram_mib": 512, "disk_gib": 5, "start": True}, "mk")
    [res] = go(tr, svc, req)
    kinds_seen = [e["kind"] for e in published]
    assert kinds_seen[-1] == kinds.RES_KIND and kinds_seen.count(kinds.PROGRESS_KIND) >= 2
    for ev in published[:-1]:
        assert ["e", req["id"]] in ev["tags"] and ["p", ADMIN] in ev["tags"] and verify_event(ev)
        assert read(ev, ADMIN_SK)["progress"]["phase"] in ("disk", "define", "start")
    assert read(res, ADMIN_SK)["ok"] is True


def test_the_announcement_is_public_addressable_and_carries_no_capacity(tmp_path):
    cfg = VmHostConfig(display_name="Box", public_url="https://box.example", public_relay="wss://box.example/relay")
    ev = transport.build_announcement(NODE_SK, cfg)
    assert ev["kind"] == kinds.ANNOUNCE_KIND and verify_event(ev)
    assert ["d", kinds.ANNOUNCE_D] in ev["tags"] and ["relay", "wss://box.example/relay"] in ev["tags"]
    body = json.loads(ev["content"])
    assert body["https"] == "https://box.example" and "ram" not in body and "cpu" not in body
