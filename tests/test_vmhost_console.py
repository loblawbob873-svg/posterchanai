"""The console ticket and the /ws/vmconsole route, driven through the REAL WebSocket route to a real
TCP "VNC server" (an echo) — the bytes actually cross the proxy.

The ticket is the only thing standing between "an npub was authorised over Nostr a minute ago" and
"this socket may type into that guest", so each of its properties is asserted on the wire:
single use, a short TTL, bound to the VM it was issued for, revoked when access changes, refused as a
MESSAGE (never an HTTP status), and never accepted from the URL — a query string lands in every proxy
access log between the client and here.
"""
import asyncio
import socketserver
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import vmhost as vmhost_router
from app.services.vmhost import domainxml
from app.services.vmhost import service as service_mod
from app.services.vmhost.config import VmHostConfig
from app.services.vmhost.console import ConsoleRegistry, TICKETS_PER_MINUTE
from app.services.vmhost.service import VmHostService
from app.services.vmhost.storage import Storage
from tests.vmhost_fake import FakeBackend

ADMIN = "1b" * 32
USER = "3d" * 32
STRANGER = "5f" * 32
U1 = "11111111-1111-4111-8111-111111111111"
U2 = "22222222-2222-4222-8222-222222222222"


class Echo(socketserver.BaseRequestHandler):
    def handle(self):
        tag = self.server.tag
        while True:
            data = self.request.recv(65536)
            if not data:
                return
            self.request.sendall(tag + data)


def echo_server(tag: bytes):
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Echo)
    srv.daemon_threads = True
    srv.tag = tag
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture
def host(tmp_path):
    root = tmp_path / "vms"
    storage = Storage(root)
    storage.ensure()
    cfg = VmHostConfig(enabled=True, storage_dir=str(root), admin_pubkeys=[ADMIN], ticket_ttl_sec=60)
    be = FakeBackend()
    for u, name in ((U1, "alpha"), (U2, "beta")):
        (root / u).mkdir()
        be.add_domain(u, name, state="running", meta=domainxml.VmMeta(owner=ADMIN, assigned=[USER]))
    a, b = echo_server(b"A:"), echo_server(b"B:")
    be.vnc = {U1: ("127.0.0.1", a.server_address[1]), U2: ("127.0.0.1", b.server_address[1])}

    async def admins():
        return set()
    svc = VmHostService(cfg, be, node_pubkey="0a" * 32, admin_provider=admins, storage=storage)
    asyncio.run(svc.refresh_index())
    service_mod.set_current(svc)
    app = FastAPI()
    app.include_router(vmhost_router.ws_router)
    client = TestClient(app)
    try:
        yield svc, be, client
    finally:
        service_mod.set_current(None)
        a.shutdown()
        b.shutdown()


def ticket(svc, who=USER, vm=U1, rid="t"):
    res = asyncio.run(svc.handle(who, "console.ticket", {"vm": vm}, rid))
    assert res["ok"], res
    return res["result"]


def open_console(client, tok):
    ws = client.websocket_connect("/ws/vmconsole")
    s = ws.__enter__()
    s.send_json({"t": "open", "ticket": tok})
    return ws, s


def test_a_ticket_opens_a_byte_pipe_to_its_vm_exactly_once(host):
    svc, be, client = host
    t = ticket(svc)
    assert t["ws"] == "/ws/vmconsole" and len(t["vnc_password"]) == 8
    assert be.passwords[U1] == t["vnc_password"], "the VNC password is set on the guest for this ticket"
    ws, s = open_console(client, t["ticket"])
    try:
        assert s.receive_json() == {"t": "ok"}
        s.send_json({"t": "go"})
        s.send_bytes(b"RFB 003.008\n")
        assert s.receive_bytes() == b"A:RFB 003.008\n", "bytes must reach THIS VM's display and come back"
    finally:
        ws.__exit__(None, None, None)
    with client.websocket_connect("/ws/vmconsole") as again:
        again.send_json({"t": "open", "ticket": t["ticket"]})
        msg = again.receive_json()
    assert msg["t"] == "err" and "already used" in msg["m"]


def test_a_ticket_is_bound_to_the_vm_it_was_issued_for(host):
    svc, be, client = host
    t2 = ticket(svc, vm=U2, rid="b")
    with client.websocket_connect("/ws/vmconsole") as s:
        s.send_json({"t": "open", "ticket": t2["ticket"], "vm": U1})     # a client naming another VM is ignored
        assert s.receive_json() == {"t": "ok"}
        s.send_json({"t": "go"})
        s.send_bytes(b"x")
        assert s.receive_bytes() == b"B:x"


def test_an_expired_ticket_is_refused():
    clock = [1000.0]
    reg = ConsoleRegistry(now=lambda: clock[0])
    tok, exp = reg.issue(U1, USER, 60)
    assert exp == 1060
    clock[0] = 1061
    assert reg.consume(tok) is None
    tok2, _ = reg.issue(U1, USER, 60)
    clock[0] = 1100
    assert reg.consume(tok2).vm == U1
    assert reg.consume(tok2) is None


def test_the_ticket_is_never_accepted_from_the_url_and_is_not_spent_by_trying(host):
    svc, be, client = host
    t = ticket(svc)
    with client.websocket_connect("/ws/vmconsole?ticket=" + t["ticket"]) as s:
        msg = s.receive_json()
    assert msg["t"] == "err" and "first frame" in msg["m"]
    with client.websocket_connect("/ws/vmconsole") as s:
        s.send_json({"t": "open", "ticket": t["ticket"]})
        assert s.receive_json() == {"t": "ok"}, "a refused URL attempt must not have consumed it"


def test_refusals_are_messages(host):
    svc, be, client = host
    with client.websocket_connect("/ws/vmconsole") as s:
        s.send_json({"t": "hello"})
        assert s.receive_json()["t"] == "err"
    with client.websocket_connect("/ws/vmconsole") as s:
        s.send_json({"t": "open", "ticket": "made-up"})
        assert s.receive_json()["t"] == "err"
    service_mod.set_current(None)
    with client.websocket_connect("/ws/vmconsole") as s:
        msg = s.receive_json()
    assert msg == {"t": "err", "m": "VM hosting is not running on this node"}


def test_access_is_rechecked_when_the_socket_opens(host):
    svc, be, client = host
    t = ticket(svc)
    # Access removed BEHIND the service's back (virsh by hand): no revoke ran, the ticket is still
    # live, and only the re-check at open time stands between it and the guest.
    be.domains[U1]["meta_xml"] = domainxml.VmMeta(owner=ADMIN, assigned=[]).to_xml(prefixed=False)
    with client.websocket_connect("/ws/vmconsole") as s:
        s.send_json({"t": "open", "ticket": t["ticket"]})
        msg = s.receive_json()
    assert msg["t"] == "err" and "no longer have access" in msg["m"]
    be.domains[U1]["state"] = "shutoff"
    be.domains[U1]["meta_xml"] = domainxml.VmMeta(owner=ADMIN, assigned=[USER]).to_xml(prefixed=False)
    svc.consoles.revoke(U2)
    t2 = svc.consoles.issue(U1, USER, 60)[0]
    with client.websocket_connect("/ws/vmconsole") as s:
        s.send_json({"t": "open", "ticket": t2})
        msg = s.receive_json()
    assert msg["t"] == "err" and "not running" in msg["m"]


def test_unassigning_closes_an_open_console(host):
    svc, be, client = host
    t = ticket(svc)
    ws, s = open_console(client, t["ticket"])
    try:
        assert s.receive_json() == {"t": "ok"}
        s.send_json({"t": "go"})
        s.send_bytes(b"ping")
        assert s.receive_bytes() == b"A:ping"
        assert svc.consoles.live_count(U1) == 1
        asyncio.run(svc.handle(ADMIN, "vm.unassign", {"vm": U1, "pubkey": USER}, "u"))
        got = []
        reader = threading.Thread(target=lambda: got.append(s.receive_json()), daemon=True)
        reader.start()
        reader.join(timeout=10)          # a console that is NOT closed would block here for ever
        assert got, "unassigning did not close the open console"
        msg = got[0]
        assert msg["t"] == "err" and "access" in msg["m"]
    finally:
        ws.__exit__(None, None, None)


def test_stopping_the_vm_revokes_unused_tickets(host):
    svc, be, client = host
    t = ticket(svc)
    asyncio.run(svc.handle(ADMIN, "vm.power", {"vm": U1, "action": "destroy"}, "p"))
    assert svc.consoles.consume(t["ticket"]) is None


def test_ticket_rules_on_the_service(host):
    svc, be, client = host
    be.domains[U2]["state"] = "shutoff"
    off = asyncio.run(svc.handle(USER, "console.ticket", {"vm": U2}, "x"))
    assert off["error"]["code"] == "conflict"
    assert asyncio.run(svc.handle(STRANGER, "console.ticket", {"vm": U1}, "x")) is None
    be.vnc[U1] = ("0.0.0.0", 5900)
    public = asyncio.run(svc.handle(USER, "console.ticket", {"vm": U1}, "y"))
    assert public["error"]["code"] == "unsupported", "a VNC display not on loopback is refused"
    be.vnc[U1] = ("127.0.0.1", 5900)
    results = [asyncio.run(svc.handle(USER, "console.ticket", {"vm": U1}, f"r{i}"))
               for i in range(TICKETS_PER_MINUTE + 1)]
    assert results[-1]["error"]["code"] == "rate_limited"


def test_the_public_url_becomes_a_wss_console_address(host):
    svc, be, client = host
    svc.cfg.public_url = "https://vm.example"
    assert ticket(svc)["ws"] == "wss://vm.example/ws/vmconsole"
