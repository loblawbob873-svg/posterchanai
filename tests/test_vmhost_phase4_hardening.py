"""Phase 4 hardening: an audit line per op, no setting that does nothing, and an announcement that follows the settings.

  * AUDIT — every answered request leaves one `[vmhost-audit]` line: who (pubkey, session or not), what (op + the
    ids it named) and the result code. Built from an allowlist, so a console ticket, a VNC password, an ISO URL
    (which can carry credentials) or a description never reaches the log.
  * `vmhost_backend` offered "auto (virsh)" and "virsh", which did the same thing — a choice that changes nothing
    is removed rather than shown.
  * The kind-31310 host announcement is republished the moment a Save restarts the host (not up to six hours
    later), carries the SAME feature list host.whoami answers, and is RETRACTED (NIP-09 `a` deletion) when an admin
    turns announcing or hosting off — an addressable event otherwise stays on every relay that took it.
"""
import asyncio
import json
import logging
import re
from pathlib import Path

from app.schemas import SettingsResponse
from app.services.vmhost import config as vmconfig
from app.services.vmhost import kinds, transport
from app.services.vmhost import service as service_mod
from app.services.vmhost.backend import VirshBackend, make_backend
from app.services.vmhost.config import VmHostConfig
from app.services.vmhost.service import VmHostService
from app.services.vmhost.storage import Storage
from tests.vmhost_fake import FakeBackend

ROOT = Path(__file__).resolve().parent.parent
ADMIN = "ad" * 32
USER = "ee" * 32
STRANGER = "5a" * 32


def run(coro):
    return asyncio.run(coro)


def audit_lines(caplog):
    return [r for r in caplog.records if r.getMessage().startswith("[vmhost-audit]")]


def test_every_answered_op_leaves_one_audit_line_and_no_secret_does(tmp_path, caplog):
    storage = Storage(tmp_path / "vms")
    storage.ensure()
    cfg = VmHostConfig(enabled=True, storage_dir=str(storage.root), admin_pubkeys=[ADMIN], reserve_disk_gib=1)
    be = FakeBackend()
    svc = VmHostService(cfg, be, node_pubkey="0e" * 32, admin_provider=lambda: set(), storage=storage)
    caplog.set_level(logging.DEBUG, logger="app.services.vmhost.service")

    async def go():
        c = lambda who, op, args, rid, **kw: svc.handle(who, op, args, rid, **kw)  # noqa: E731
        created = await c(ADMIN, "vm.create", {"name": "web", "vcpus": 1, "ram_mib": 512, "disk_gib": 2,
                                               "description": "hunter2-in-a-field"}, "c1")
        vm = created["result"]["vm"]["uuid"]
        await c(ADMIN, "vm.assign", {"vm": vm, "pubkey": USER}, "a1")
        await c(ADMIN, "vm.power", {"vm": vm, "action": "start"}, "p1")
        ticket = await c(USER, "console.ticket", {"vm": vm}, "t1")
        await c(USER, "vm.delete", {"vm": vm, "confirm_name": "web"}, "d1")
        await c(ADMIN, "iso.fetch", {"url": "https://user:s3cretpw@example.com/x.iso"}, "f1")
        await c(ADMIN, "vm.list", {}, "l1")
        assert await c(STRANGER, "vm.list", {}, "s1") is None
        await c(ADMIN, "vm.power", {"vm": vm, "action": "start"}, "p1")         # a retry: the journal answers
        return vm, ticket["result"]
    vm, ticket = run(go())
    lines = [r.getMessage() for r in audit_lines(caplog)]
    by_op = {re.search(r"op=(\S+)", m).group(1): m for m in lines}
    assert f"op=vm.create by={ADMIN} role=admin vm={vm} result=ok" in by_op["vm.create"]
    assert f"target={USER}" in by_op["vm.assign"]
    assert "action=start" in by_op["vm.power"] and f"vm={vm}" in by_op["vm.power"]
    assert f"op=console.ticket by={USER} role=user vm={vm} result=ok" in by_op["console.ticket"]
    assert "result=forbidden" in by_op["vm.delete"], "a refusal is audited too"
    assert len([m for m in lines if "op=vm.power" in m]) == 2, "a journal replay is still an answered request"
    assert not any(STRANGER in m for m in lines), "a stranger is dropped without a trace in the audit log"
    levels = {re.search(r"op=(\S+)", r.getMessage()).group(1): r.levelno for r in audit_lines(caplog)}
    assert levels["vm.list"] == logging.DEBUG and levels["console.ticket"] == logging.INFO
    assert levels["vm.create"] == logging.INFO and levels["vm.delete"] == logging.INFO
    everything = "\n".join(r.getMessage() for r in caplog.records)
    for secret in (ticket["ticket"], ticket["vnc_password"], "s3cretpw", "hunter2-in-a-field"):
        assert secret not in everything, f"{secret[:6]}… reached the log"


def test_the_backend_setting_that_changed_nothing_is_gone():
    assert "vmhost_backend" not in vmconfig.DEFAULTS
    assert "vmhost_backend" not in SettingsResponse.model_fields
    assert "vmhost_backend" not in (ROOT / "templates/admin/tabs/vmhost.html").read_text()
    assert isinstance(make_backend(VmHostConfig()), VirshBackend)
    assert not hasattr(VmHostConfig(), "backend")


def test_the_status_panel_says_whether_qemu_can_walk_into_the_storage():
    src = (ROOT / "app/routers/vmhost.py").read_text()
    assert '"storage_traversable": bool(os.path.isdir(cfg.storage_dir) and os.stat(cfg.storage_dir).st_mode & 0o001)' in src
    assert "storage_traversable===false" in (ROOT / "templates/admin/tabs/vmhost.html").read_text()


# ------------------------------------------------------------------------------ announcement lifecycle
class Relay:
    def __init__(self):
        self.published = []

    async def publish(self, url, ev, direct=True):
        self.published.append(ev)
        return True

    async def subscribe(self, url, filters, handler, stop, direct=True):
        await stop.wait()

    def announcements(self):
        return [json.loads(e["content"]) for e in self.published if e["kind"] == kinds.ANNOUNCE_KIND]

    def retractions(self):
        return [e for e in self.published if e["kind"] == 5]


async def until(pred, what, timeout=10.0):
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


def test_the_announcement_follows_every_save_and_is_retracted_when_turned_off(tmp_path, monkeypatch):
    from app.services import nostr_dvm, settings_store
    from app.services.nostr import bip340
    from app.services.nostr import relay as nostr_relay
    from app.services.vmhost import backend as backend_mod
    sk = bytes.fromhex("42" * 32)
    pk = bip340.pubkey_from_seckey(sk).hex()
    relay = Relay()
    settings = {"vmhost_enabled": "true", "vmhost_storage_dir": str(tmp_path / "vms"), "vmhost_display_name": "One",
                "vmhost_announce": "true"}
    monkeypatch.setattr(nostr_dvm, "node_seckey", lambda: sk)
    monkeypatch.setattr(nostr_dvm, "node_pubkey", lambda: pk)
    monkeypatch.setattr(nostr_dvm, "relay_url", lambda: "ws://127.0.0.1:1/relay")
    monkeypatch.setattr(nostr_relay, "publish", relay.publish)
    monkeypatch.setattr(nostr_relay, "subscribe", relay.subscribe)
    monkeypatch.setattr(vmconfig, "current", lambda: VmHostConfig.from_settings(settings))
    monkeypatch.setattr(settings_store, "all_settings", lambda: dict(settings))
    monkeypatch.setattr(backend_mod, "make_backend", lambda cfg: FakeBackend())
    transport._state["announced"] = False

    async def go():
        try:
            transport.start()
            await until(lambda: any(a["name"] == "One" for a in relay.announcements()), "the first announcement")
            first = relay.announcements()[-1]
            assert first["features"] == service_mod.FEATURES, "discovery must advertise what whoami answers"
            assert "access" in first["features"] and "iso-upload" in first["features"]

            settings["vmhost_display_name"] = "Two"                 # an admin Save → request_reload → reload()
            await transport.reload()
            await until(lambda: any(a["name"] == "Two" for a in relay.announcements()), "the renamed announcement")

            settings["vmhost_announce"] = "false"
            n = len(relay.announcements())
            await transport.reload()
            await until(lambda: relay.retractions(), "a retraction when announcing is turned off")
            ev = relay.retractions()[-1]
            assert ["a", f"{kinds.ANNOUNCE_KIND}:{pk}:{kinds.ANNOUNCE_D}"] in ev["tags"] and ev["pubkey"] == pk
            await asyncio.sleep(0.2)
            assert len(relay.announcements()) == n, "announcing off must not announce"

            settings["vmhost_announce"] = "true"
            await transport.reload()
            await until(lambda: len(relay.announcements()) > n, "announcing again")
            r = len(relay.retractions())
            settings["vmhost_enabled"] = "false"                    # hosting off altogether
            await transport.reload()
            await until(lambda: len(relay.retractions()) > r, "a retraction when hosting is switched off")
        finally:
            await transport.stop()
            service_mod.set_current(None)
    run(go())
