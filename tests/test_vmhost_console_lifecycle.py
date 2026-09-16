"""A LIVE console is a live grant, so it must end when the grant does — through the real /ws/vmconsole
route, with bytes flowing, not just an unused ticket being refused.

Three ways access goes away while somebody is typing into a guest, each of which used to leave the
socket open for the rest of a four-hour session:
  * access removed behind the service's back (an admin edits the domain with virsh, a VM is stopped
    from the host) — no `revoke` runs, so only a periodic re-check can notice;
  * access removed in the gap between the open-time check and the console being registered — the
    revoke that ran in that gap had nothing to close yet;
  * a settings Save restarts the host service (`transport.stop` → a NEW service with a NEW registry):
    the old registry, which is the only thing that can close those sockets, was simply dropped.
"""
import asyncio
import threading

import pytest

from app.routers import vmhost as vmhost_router
from app.services.vmhost import domainxml, transport
from tests import test_vmhost_console as _console
from tests.test_vmhost_console import ADMIN, U1, USER, open_console, ticket


@pytest.fixture
def host(tmp_path):
    """The console suite's fixture: a real ws route, a fake hypervisor, echo servers as VNC displays."""
    yield from _console.host.__wrapped__(tmp_path)


def closes_within(s, seconds):
    got = []
    t = threading.Thread(target=lambda: got.append(s.receive_json()), daemon=True)
    t.start()
    t.join(timeout=seconds)
    return got[0] if got else None


def live(client, svc):
    t = ticket(svc)
    ws, s = open_console(client, t["ticket"])
    assert s.receive_json() == {"t": "ok"}
    s.send_json({"t": "go"})
    s.send_bytes(b"ping")
    assert s.receive_bytes() == b"A:ping"
    return ws, s


def test_access_removed_behind_the_services_back_closes_a_live_console(host, monkeypatch):
    svc, be, client = host
    monkeypatch.setattr(vmhost_router, "RECHECK_EVERY", 0.2, raising=False)
    ws, s = live(client, svc)
    try:
        be.domains[U1]["meta_xml"] = domainxml.VmMeta(owner=ADMIN, assigned=[]).to_xml(prefixed=False)
        msg = closes_within(s, 10)
        assert msg is not None, "the console stayed open after its user lost access"
        assert msg["t"] == "err" and "access" in msg["m"]
    finally:
        ws.__exit__(None, None, None)


def test_a_vm_stopped_from_the_host_closes_a_live_console(host, monkeypatch):
    svc, be, client = host
    monkeypatch.setattr(vmhost_router, "RECHECK_EVERY", 0.2, raising=False)
    ws, s = live(client, svc)
    try:
        be.domains[U1]["state"] = "shutoff"
        msg = closes_within(s, 10)
        assert msg is not None and msg["t"] == "err" and "not running" in msg["m"]
    finally:
        ws.__exit__(None, None, None)


def test_access_lost_between_the_open_check_and_registration_is_caught_at_once(host, monkeypatch):
    """The periodic re-check is set to an hour: only the re-check right after attach can close this."""
    svc, be, client = host
    monkeypatch.setattr(vmhost_router, "RECHECK_EVERY", 3600, raising=False)
    real = be.vnc_endpoint
    calls = []

    async def endpoint_then_unassign(vm):
        ep = await real(vm)
        calls.append(vm)
        if len(calls) == 1:     # the open-time check has just passed; access goes away now
            be.domains[U1]["meta_xml"] = domainxml.VmMeta(owner=ADMIN, assigned=[]).to_xml(prefixed=False)
        return ep
    t = ticket(svc)
    be.vnc_endpoint = endpoint_then_unassign
    ws, s = open_console(client, t["ticket"])
    try:
        assert s.receive_json() == {"t": "ok"}
        s.send_json({"t": "go"})
        msg = closes_within(s, 10)
        assert msg is not None, "a console registered after its revocation stayed open"
        assert msg["t"] == "err" and "access" in msg["m"]
    finally:
        ws.__exit__(None, None, None)


def test_a_settings_save_restart_closes_every_live_console(host, monkeypatch):
    svc, be, client = host
    monkeypatch.setattr(vmhost_router, "RECHECK_EVERY", 3600, raising=False)
    ws, s = live(client, svc)
    try:
        assert svc.consoles.live_count() == 1
        asyncio.run(transport.stop())            # what request_reload → reload() runs first
        msg = closes_within(s, 10)
        assert msg is not None, "the old service's consoles outlived the restart"
        assert msg["t"] == "err"
    finally:
        ws.__exit__(None, None, None)


@pytest.mark.parametrize("n", [3])
def test_close_all_closes_every_console_on_every_vm(n):
    from app.services.vmhost.console import ConsoleRegistry
    reg = ConsoleRegistry()
    closed = []
    for i in range(n):
        reg.attach(f"vm{i}", USER, lambda i=i: closed.append(i))
    assert reg.close_all() == n and sorted(closed) == list(range(n))
    assert reg.live_count() == 0
