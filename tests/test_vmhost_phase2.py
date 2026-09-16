"""VM hosting, phase 2, through the SHIPPED service/transport/route against a fake hypervisor.

What each block pins:
  * vm.update — shut-off only, validated and capacity-checked BEFORE a write, one define, and CONFIRMED
    by reading the definition back (a Save libvirt accepted but did not keep is an error, not success);
    the cdrom is REPLACED, never given a second source (change-media --update semantics); a failed
    define leaves no orphan disk file.
  * snapshots — admin-only, revert demands confirm, a migrating VM refuses.
  * ISO fetch — the SSRF guard runs on EVERY hop (a redirect to 169.254.169.254 is refused and never
    requested), the size cap is enforced on the header AND on the stream, nothing half-downloaded stays.
  * ISO upload — the ticket is single use, admin only, size-bound, and re-checks admin at PUT time.
  * host.access — invalid keys refuse the WHOLE request; the write is durable before the host changes.
  * session keys — a session acts as its owner for use ops only; anything else is step_up_required; an
    ended session is ANSWERED session_expired (silence would look like an offline host).
"""
import asyncio
import hashlib
import json
import os
import time

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import vmhost as vmhost_router
from app.services import rss_service
from app.services.nostr import bip340, nip44, nostr_service
from app.services.nostr.event import build_event
from app.services.vmhost import access, domainxml, isolib, sessions, transport
from app.services.vmhost import service as service_mod
from app.services.vmhost.backend import BackendError, parse_snapshot_list
from app.services.vmhost.config import VmHostConfig
from app.services.vmhost.service import OPS, VmHostService
from app.services.vmhost.storage import Storage
from tests.vmhost_fake import FakeBackend

NODE_SK = bytes.fromhex("01" * 32)
ADMIN_SK = bytes.fromhex("02" * 32)
USER_SK = bytes.fromhex("03" * 32)
SESS_SK = bytes.fromhex("06" * 32)
pub = lambda sk: bip340.pubkey_from_seckey(sk).hex()  # noqa: E731
NODE, ADMIN, USER, SESS = map(pub, (NODE_SK, ADMIN_SK, USER_SK, SESS_SK))
NEWUSER = "7a" * 32
U1 = "11111111-1111-4111-8111-111111111111"
U2 = "22222222-2222-4222-8222-222222222222"


def run(coro):
    return asyncio.run(coro)


def make(tmp_path, **cfgkw):
    root = tmp_path / "vms"
    storage = Storage(root)
    storage.ensure()
    cfg = VmHostConfig(enabled=True, storage_dir=str(root), admin_pubkeys=[ADMIN], **cfgkw)
    be = FakeBackend()
    for u, name, st in ((U1, "alpha", "shutoff"), (U2, "beta", "running")):
        (root / u).mkdir()
        be.add_domain(u, name, state=st, meta=domainxml.VmMeta(owner=ADMIN, created=1, disk_gib=20, assigned=[USER]))

    async def admins():
        return set()
    svc = VmHostService(cfg, be, node_pubkey=NODE, admin_provider=admins, storage=storage)
    run(svc.refresh_index())
    return svc, be, root


async def call(svc, who, op, args=None, rid="r1", session=None):
    return await svc.handle(who, op, args or {}, rid, session=session)


def c(svc, who, op, args=None, rid="r1", session=None):
    return run(call(svc, who, op, args, rid, session))


def cdrom_sources(xml):
    root = domainxml.parse_domain(xml)
    cds = [d for d in root.findall("devices/disk") if d.get("device") == "cdrom"]
    return [[s.get("file") for s in d.findall("source")] for d in cds]


# ================================================================================ vm.update
def test_update_changes_everything_asked_and_confirms_it_by_reading_back(tmp_path):
    svc, be, root = make(tmp_path)
    (root / "isos" / "debian.iso").write_bytes(b"ISO")
    res = c(svc, ADMIN, "vm.update", {"vm": U1, "vcpus": 4, "ram_mib": 4096, "autostart": True, "boot": "cdrom",
                                       "input": "mouse", "add_nic": True, "add_disk_gib": 10,
                                       "media": {"iso": "debian.iso"}})
    assert res["ok"], res
    vm = res["result"]["vm"]
    assert (vm["vcpus"], vm["ram_mib"], vm["autostart"], vm["disk_gib"]) == (4, 4096, True, 30)
    hw = vm["hardware"]
    assert hw["boot"] == "cdrom" and hw["input"] == "mouse" and hw["nics"] == 2 and hw["media"] == "debian.iso"
    assert (root / U1 / "disk-vdb.qcow2").is_file()
    assert any(d["target"] == "vdb" for d in hw["disks"])
    # the definition libvirt holds, and the metadata inside it
    xml = be.domains[U1]["xml"]
    assert str(root / "isos" / "debian.iso") in xml
    assert domainxml.parse_meta(be.domains[U1]["meta_xml"]).iso == "debian.iso"
    # admins see the same hardware from vm.get
    got = c(svc, ADMIN, "vm.get", {"vm": U1}, "g")["result"]["vm"]["hardware"]
    assert got == hw


def test_changing_media_replaces_the_source_never_adds_one_and_eject_empties_it(tmp_path):
    svc, be, root = make(tmp_path)
    for n in ("a.iso", "b.iso"):
        (root / "isos" / n).write_bytes(b"ISO")
    assert c(svc, ADMIN, "vm.update", {"vm": U1, "media": {"iso": "a.iso"}}, "1")["ok"]
    assert cdrom_sources(be.domains[U1]["xml"]) == [[str(root / "isos" / "a.iso")]]
    assert c(svc, ADMIN, "vm.update", {"vm": U1, "media": {"iso": "b.iso"}}, "2")["ok"]
    assert cdrom_sources(be.domains[U1]["xml"]) == [[str(root / "isos" / "b.iso")]], \
        "a second <source> in one cdrom is the --insert-over-a-filled-tray bug"
    ej = c(svc, ADMIN, "vm.update", {"vm": U1, "media": "eject"}, "3")
    assert ej["ok"] and ej["result"]["vm"]["hardware"]["media"] == ""
    assert cdrom_sources(be.domains[U1]["xml"]) == [[]]


@pytest.mark.parametrize("args,code", [
    ({"vcpus": 2}, "conflict"),                                 # U2 is running
])
def test_update_needs_the_vm_shut_off(tmp_path, args, code):
    svc, be, root = make(tmp_path)
    before = be.domains[U2]["xml"]
    res = c(svc, ADMIN, "vm.update", {"vm": U2, **args})
    assert res["error"]["code"] == code
    assert be.domains[U2]["xml"] == before


@pytest.mark.parametrize("args,code", [
    ({}, "bad_request"),
    ({"vcpus": "lots"}, "bad_request"),
    ({"boot": "floppy"}, "bad_request"),
    ({"input": "joystick"}, "bad_request"),
    ({"autostart": "yes"}, "bad_request"),
    ({"add_nic": 3}, "bad_request"),
    ({"media": {"iso": "../etc/x.iso"}}, "bad_request"),
    ({"media": {"iso": "missing.iso"}}, "not_found"),
    ({"media": "/etc/passwd"}, "bad_request"),
    ({"xml": "<domain/>"}, "bad_request"),
    ({"vcpus": 17}, "insufficient_capacity"),          # over the per-VM limit
    ({"vcpus": 12}, "insufficient_capacity"),          # more than the host's 8 cores
    ({"ram_mib": 31000}, "insufficient_capacity"),     # more than uncommitted after the reserve
    ({"add_disk_gib": 390}, "insufficient_capacity"),  # more than free after the reserve
])
def test_update_refuses_bad_input_and_capacity_before_writing(tmp_path, args, code):
    svc, be, root = make(tmp_path)
    before = dict(be.domains[U1])
    res = c(svc, ADMIN, "vm.update", {"vm": U1, **args})
    assert res["ok"] is False and res["error"]["code"] == code, (args, res)
    assert be.domains[U1] == before
    assert not any(n.startswith("disk-vd") and n != "disk-vda.qcow2" for n in os.listdir(root / U1))
    assert not [call_ for call_ in be.calls if call_[0] == "define"]


def test_update_refuses_a_vm_posterchan_did_not_make(tmp_path):
    svc, be, root = make(tmp_path)
    u = "33333333-3333-4333-8333-333333333333"
    be.add_domain(u, "foreign")
    res = c(svc, ADMIN, "vm.update", {"vm": u, "vcpus": 1})
    assert res["error"]["code"] == "unsupported"


def test_a_failed_define_leaves_no_orphan_disk(tmp_path):
    svc, be, root = make(tmp_path)
    be.fail["define"] = BackendError("XML error")
    res = c(svc, ADMIN, "vm.update", {"vm": U1, "add_disk_gib": 5})
    assert res["ok"] is False
    assert not (root / U1 / "disk-vdb.qcow2").exists()
    assert domainxml.parse_meta(be.domains[U1]["meta_xml"]).disk_gib == 20


def test_a_save_the_host_did_not_keep_is_an_error_not_success(tmp_path, monkeypatch):
    svc, be, root = make(tmp_path)
    real = be.define

    async def lossy(xml, workdir):
        await real(xml.replace("<vcpu>4</vcpu>", "<vcpu>2</vcpu>"), workdir)
    monkeypatch.setattr(be, "define", lossy)
    res = c(svc, ADMIN, "vm.update", {"vm": U1, "vcpus": 4})
    assert res["ok"] is False and res["error"]["code"] == "backend_error" and "vCPUs" in res["error"]["message"]


# ================================================================================ admin-only, host-enforced
PHASE2_ADMIN_OPS = ["vm.update", "vm.snapshot.list", "vm.snapshot.create", "vm.snapshot.revert",
                    "vm.snapshot.delete", "iso.fetch", "iso.upload_ticket", "iso.delete",
                    "host.access.get", "host.access.set"]


def test_every_phase2_management_op_is_admin_in_the_table():
    for op in PHASE2_ADMIN_OPS:
        assert OPS[op][0] == "admin", op
    assert len(PHASE2_ADMIN_OPS) == 10


@pytest.mark.parametrize("op", PHASE2_ADMIN_OPS)
def test_a_user_cannot_run_management_ops(tmp_path, op):
    svc, be, root = make(tmp_path)
    (root / "isos" / "a.iso").write_bytes(b"ISO")
    be.calls.clear()
    res = c(svc, USER, op, {"vm": U1, "vcpus": 1, "name": "s1", "confirm": True, "url": "https://x.example/a.iso",
                            "iso": "a.iso", "size": 3, "allowed": [USER]})
    assert res["ok"] is False and res["error"]["code"] == "forbidden", res
    assert be.calls == [] and (root / "isos" / "a.iso").exists()


# ================================================================================ snapshots
def test_snapshot_create_list_revert_delete(tmp_path):
    svc, be, root = make(tmp_path)
    assert c(svc, ADMIN, "vm.snapshot.create", {"vm": U1, "name": "clean"}, "1")["ok"]
    assert c(svc, ADMIN, "vm.snapshot.create", {"vm": U1, "name": "clean"}, "2")["error"]["code"] == "conflict"
    assert c(svc, ADMIN, "vm.snapshot.create", {"vm": U1, "name": "bad name"}, "3")["error"]["code"] == "bad_request"
    lst = c(svc, ADMIN, "vm.snapshot.list", {"vm": U1}, "4")["result"]["snapshots"]
    assert [s["name"] for s in lst] == ["clean"]
    assert c(svc, ADMIN, "vm.update", {"vm": U1, "vcpus": 6}, "5")["ok"]
    no = c(svc, ADMIN, "vm.snapshot.revert", {"vm": U1, "name": "clean"}, "6")
    assert no["error"]["code"] == "bad_request" and be.domains[U1]["vcpus"] == 6, "revert without confirm"
    yes = c(svc, ADMIN, "vm.snapshot.revert", {"vm": U1, "name": "clean", "confirm": True}, "7")
    assert yes["ok"] and yes["result"]["vm"]["vcpus"] == 2
    assert c(svc, ADMIN, "vm.snapshot.revert", {"vm": U1, "name": "nope", "confirm": True}, "8")["error"]["code"] == "not_found"
    assert c(svc, ADMIN, "vm.snapshot.delete", {"vm": U1, "name": "clean"}, "9")["result"]["snapshots"] == []


def test_a_migrating_vm_refuses_snapshots_and_edits(tmp_path, monkeypatch):
    svc, be, root = make(tmp_path)
    real_info = be._info

    def migrating(u):
        d = real_info(u)
        if d.meta is not None:
            d.meta.migration = {"id": "m1", "state": "exporting"}
        return d
    monkeypatch.setattr(be, "_info", migrating)
    for op, args in (("vm.snapshot.create", {"name": "x"}), ("vm.snapshot.list", {}),
                     ("vm.snapshot.revert", {"name": "x", "confirm": True}), ("vm.snapshot.delete", {"name": "x"}),
                     ("vm.update", {"vcpus": 1})):
        res = c(svc, ADMIN, op, {"vm": U1, **args}, op.replace(".", "-"))
        assert res["error"]["code"] == "migrating", (op, res)
    assert be.snapshots == {}


def test_virsh_snapshot_list_parser_reads_real_output():
    text = (" Name         Creation Time               State\n"
            "-------------------------------------------------------\n"
            " clean        2026-09-16 12:00:00 +0000   shutoff\n"
            " after-apt    2026-09-16 13:10:02 +0000   running\n\n")
    assert parse_snapshot_list(text) == [
        {"name": "clean", "created": "2026-09-16 12:00:00 +0000", "state": "shutoff"},
        {"name": "after-apt", "created": "2026-09-16 13:10:02 +0000", "state": "running"}]


def test_virsh_snapshot_argv_is_validated():
    from app.services.vmhost.backend import VirshBackend
    seen = []

    async def runner(argv, timeout, stdin=None):
        seen.append(argv)
        return 0, "", ""
    v = VirshBackend("qemu:///system", runner=runner)
    run(v.snapshot_create(U1, "clean", "desc"))
    assert seen[-1] == ["virsh", "--connect", "qemu:///system", "snapshot-create-as", U1, "--name", "clean",
                        "--atomic", "--description", "desc"]
    run(v.snapshot_revert(U1, "clean"))
    assert seen[-1][3:] == ["snapshot-revert", U1, "--snapshotname", "clean"]
    with pytest.raises(BackendError):
        run(v.snapshot_delete(U1, "--all"))


# ================================================================================ ISO fetch (SSRF)
def _guard():
    """The REAL guard, except that `*.example` names (which cannot resolve offline) count as public."""
    def safe(url):
        host = httpx.URL(url).host
        return True if host.endswith(".example") else rss_service.is_safe_host(url)
    return rss_service.looks_fetchable, safe


def fetch_setup(tmp_path, handler, **cfg):
    svc, be, root = make(tmp_path, **cfg)
    seen = []

    def h(request):
        seen.append(str(request.url))
        return handler(request)
    svc.fetch_client = httpx.AsyncClient(transport=httpx.MockTransport(h), follow_redirects=False)
    svc.fetch_guard = _guard()
    return svc, be, root, seen


def no_parts(root):
    inc = root / "isos" / ".incoming"
    return not inc.exists() or not os.listdir(inc)


def test_fetch_streams_hashes_and_adds_to_the_library(tmp_path):
    body = b"\x01" * (3 << 20)
    svc, be, root, seen = fetch_setup(tmp_path, lambda r: httpx.Response(200, content=body))
    progress = []

    async def prog(p):
        progress.append(p)
    res = run(svc.handle(ADMIN, "iso.fetch", {"url": "https://mirror.example/pub/debian-12.iso"}, "f", prog))
    assert res["ok"], res
    iso = res["result"]["iso"]
    assert iso["id"] == "debian-12.iso" and iso["size"] == len(body)
    assert iso["sha256"] == hashlib.sha256(body).hexdigest()
    assert (root / "isos" / "debian-12.iso").read_bytes() == body and no_parts(root)
    assert [i["id"] for i in c(svc, ADMIN, "iso.list", {}, "l")["result"]["isos"]] == ["debian-12.iso"]
    again = c(svc, ADMIN, "iso.fetch", {"url": "https://mirror.example/pub/debian-12.iso"}, "f2")
    assert again["error"]["code"] == "conflict" and no_parts(root)


@pytest.mark.parametrize("url", ["http://127.0.0.1/x.iso", "http://10.0.0.5/x.iso", "http://169.254.169.254/x.iso",
                                 "file:///etc/passwd", "http://localhost/x.iso", "http://nas.lan/x.iso",
                                 "ftp://mirror.example/x.iso"])
def test_fetch_refuses_private_and_non_http_targets_without_a_request(tmp_path, url):
    svc, be, root, seen = fetch_setup(tmp_path, lambda r: httpx.Response(200, content=b"x"))
    res = c(svc, ADMIN, "iso.fetch", {"url": url})
    assert res["ok"] is False and res["error"]["code"] == "forbidden", res
    assert seen == [] and no_parts(root)


def test_a_redirect_to_the_metadata_service_is_refused_and_never_requested(tmp_path):
    def handler(r):
        if r.url.host == "mirror.example":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/x.iso"})
        return httpx.Response(200, content=b"SECRET")
    svc, be, root, seen = fetch_setup(tmp_path, handler)
    res = c(svc, ADMIN, "iso.fetch", {"url": "https://mirror.example/x.iso"})
    assert res["ok"] is False and res["error"]["code"] == "forbidden"
    assert seen == ["https://mirror.example/x.iso"], "the redirect target must never be requested"
    assert no_parts(root) and os.listdir(root / "isos") in ([], [".incoming"])


def test_a_public_redirect_is_followed(tmp_path):
    def handler(r):
        if r.url.host == "a.example":
            return httpx.Response(301, headers={"location": "https://b.example/real/alpine.iso"})
        return httpx.Response(200, content=b"ISO!")
    svc, be, root, seen = fetch_setup(tmp_path, handler)
    res = c(svc, ADMIN, "iso.fetch", {"url": "https://a.example/latest"})
    assert res["ok"] and res["result"]["iso"]["id"] == "alpine.iso"


def test_oversize_is_refused_on_the_header_and_on_the_stream(tmp_path, monkeypatch):
    monkeypatch.setattr(isolib, "ISO_MAX_GIB", 1)
    big = 2 << 30
    svc, be, root, seen = fetch_setup(tmp_path, lambda r: httpx.Response(200, headers={"content-length": str(big)},
                                                                          content=b""))
    res = c(svc, ADMIN, "iso.fetch", {"url": "https://mirror.example/big.iso"})
    assert res["error"]["code"] == "insufficient_capacity" and no_parts(root)

    def endless(r):
        async def gen():
            for _ in range(1100):
                yield b"\x00" * (1 << 20)
        return httpx.Response(200, content=gen())
    svc2, be2, root2, _ = fetch_setup(tmp_path / "two", endless)
    res2 = c(svc2, ADMIN, "iso.fetch", {"url": "https://mirror.example/liar.iso"})
    assert res2["error"]["code"] == "insufficient_capacity"
    assert no_parts(root2) and not (root2 / "isos" / "liar.iso").exists()


def test_fetch_can_be_turned_off(tmp_path):
    svc, be, root, seen = fetch_setup(tmp_path, lambda r: httpx.Response(200, content=b"x"), iso_fetch_enabled=False)
    assert c(svc, ADMIN, "iso.fetch", {"url": "https://mirror.example/x.iso"})["error"]["code"] == "forbidden"
    assert seen == []


# ================================================================================ ISO upload
@pytest.fixture
def upload_host(tmp_path):
    svc, be, root = make(tmp_path)
    service_mod.set_current(svc)
    app = FastAPI()
    app.include_router(vmhost_router.router)
    try:
        yield svc, be, root, TestClient(app)
    finally:
        service_mod.set_current(None)


def test_an_upload_ticket_is_single_use_and_the_file_lands_whole(upload_host):
    svc, be, root, client = upload_host
    body = os.urandom(4096)
    t = c(svc, ADMIN, "iso.upload_ticket", {"name": "my install.iso", "size": len(body)})["result"]
    assert t["name"] == "my-install.iso" and t["url"].endswith("/api/vmhost/iso/" + t["ticket"])
    pre = client.options("/api/vmhost/iso/" + t["ticket"])
    assert pre.headers.get("access-control-allow-origin") == "*"
    r = client.put("/api/vmhost/iso/" + t["ticket"], content=body)
    assert r.status_code == 200, r.text
    assert r.json()["result"]["iso"]["sha256"] == hashlib.sha256(body).hexdigest()
    assert (root / "isos" / "my-install.iso").read_bytes() == body
    again = client.put("/api/vmhost/iso/" + t["ticket"], content=body)
    assert again.status_code == 403, "a ticket is spent by its first use"


def test_an_upload_longer_or_shorter_than_declared_is_discarded(upload_host):
    svc, be, root, client = upload_host
    t = c(svc, ADMIN, "iso.upload_ticket", {"name": "a.iso", "size": 10}, "1")["result"]
    assert client.put("/api/vmhost/iso/" + t["ticket"], content=b"x" * 11).status_code == 400
    t2 = c(svc, ADMIN, "iso.upload_ticket", {"name": "a.iso", "size": 10}, "2")["result"]
    assert client.put("/api/vmhost/iso/" + t2["ticket"], content=b"x" * 9).status_code == 400
    assert not (root / "isos" / "a.iso").exists() and no_parts(root)


def test_an_oversized_upload_stops_reading_at_the_declared_size(tmp_path):
    """Refusing at the END (size != declared) is not enough: a client that declared 10 bytes must not get
    to write gigabytes to this host's disk first. Reading must stop as soon as the body passes the size."""
    svc, be, root = make(tmp_path)
    t = c(svc, ADMIN, "iso.upload_ticket", {"name": "c.iso", "size": 10})["result"]
    consumed = []

    async def body():
        for i in range(1000):
            consumed.append(i)
            yield b"x" * 8
    with pytest.raises(service_mod.VmHostError):
        run(svc.receive_upload(t["ticket"], body()))
    assert len(consumed) <= 2, f"kept reading {len(consumed)} chunks past the declared size"
    assert no_parts(root) and not (root / "isos" / "c.iso").exists()


def test_upload_refuses_oversize_tickets_and_a_revoked_admin(upload_host, monkeypatch):
    svc, be, root, client = upload_host
    assert c(svc, ADMIN, "iso.upload_ticket", {"name": "huge.iso", "size": 900 << 30}, "1")["error"]["code"] \
        == "insufficient_capacity"
    t = c(svc, ADMIN, "iso.upload_ticket", {"name": "b.iso", "size": 3}, "2")["result"]
    svc.cfg.admin_pubkeys = []                     # the admin was removed between ticket and PUT
    svc._admin_cache = (0.0, set())
    r = client.put("/api/vmhost/iso/" + t["ticket"], content=b"abc")
    assert r.status_code == 403 and not (root / "isos" / "b.iso").exists()
    assert client.put("/api/vmhost/iso/not-a-ticket", content=b"abc").status_code == 403


def test_iso_delete_refuses_one_a_vm_still_uses(tmp_path):
    svc, be, root = make(tmp_path)
    for n in ("used.iso", "attached.iso", "free.iso"):
        (root / "isos" / n).write_bytes(b"ISO")
    meta = domainxml.parse_meta(be.domains[U2]["meta_xml"])
    meta.iso = "used.iso"
    be.domains[U2]["meta_xml"] = meta.to_xml(prefixed=False)
    assert c(svc, ADMIN, "vm.update", {"vm": U1, "media": {"iso": "attached.iso"}}, "m")["ok"]
    assert c(svc, ADMIN, "iso.delete", {"iso": "used.iso"}, "1")["error"]["code"] == "conflict"
    assert c(svc, ADMIN, "iso.delete", {"iso": "attached.iso"}, "2")["error"]["code"] == "conflict"
    assert c(svc, ADMIN, "iso.delete", {"iso": "../x.iso"}, "3")["error"]["code"] == "bad_request"
    assert c(svc, ADMIN, "iso.delete", {"iso": "free.iso"}, "4")["ok"]
    assert sorted(os.listdir(root / "isos")) == ["attached.iso", "used.iso"]


# ================================================================================ host.access
def test_access_set_normalizes_writes_durably_then_applies(tmp_path):
    svc, be, root = make(tmp_path)
    writes = []

    async def writer(value):
        writes.append(value)
        return True
    svc.access_writer = writer
    npub_new = nostr_service.npub_of(NEWUSER)
    assert run(svc.role_of(NEWUSER)) is None
    res = c(svc, ADMIN, "host.access.set", {"allowed": [npub_new, NEWUSER.upper(), "  " + USER + " "]})
    assert res["ok"], res
    assert [a["pubkey"] for a in res["result"]["allowed"]] == [NEWUSER, USER]
    assert writes == [npub_new + "\n" + nostr_service.npub_of(USER)]
    assert run(svc.role_of(NEWUSER)) == "user"
    got = c(svc, ADMIN, "host.access.get", {}, "g")["result"]
    assert [a["pubkey"] for a in got["allowed"]] == [NEWUSER, USER]
    assert [a["pubkey"] for a in got["admins"]] == [ADMIN] and got["admins_editable"] is False


@pytest.mark.parametrize("entries", [["npub1notakey"], [NEWUSER, "hello"], ["nsec1" + "q" * 58], "npub1x", [123]])
def test_access_set_refuses_the_whole_list_on_any_bad_key(tmp_path, entries):
    svc, be, root = make(tmp_path)
    writes = []

    async def writer(value):
        writes.append(value)
        return True
    svc.access_writer = writer
    before = list(svc.cfg.allowed_pubkeys)
    res = c(svc, ADMIN, "host.access.set", {"allowed": entries})
    assert res["error"]["code"] == "bad_request"
    assert writes == [] and svc.cfg.allowed_pubkeys == before
    assert "nsec1" not in res["error"]["message"]


def test_a_write_that_did_not_land_changes_nothing(tmp_path):
    svc, be, root = make(tmp_path)

    async def writer(value):
        return False
    svc.access_writer = writer
    res = c(svc, ADMIN, "host.access.set", {"allowed": [NEWUSER]})
    assert res["error"]["code"] == "backend_error"
    assert run(svc.role_of(NEWUSER)) is None


def test_the_default_writer_goes_through_write_through_before_the_cache(monkeypatch):
    from app.services import settings_store
    order = []

    async def wt(db, changes, _answer=[0]):
        order.append(("relay", dict(changes)))
        return _answer[0]
    monkeypatch.setattr(settings_store, "write_through", wt)
    monkeypatch.setattr(settings_store, "put", lambda k, v, write_relay=True: order.append(("cache", k, v, write_relay)))
    assert run(access.default_writer("npub1abc")) is False
    assert order == [("relay", {"vmhost_allowed_npubs": "npub1abc"})], "a failed durable write must not touch the cache"
    order.clear()
    wt.__defaults__[0][0] = 1
    assert run(access.default_writer("npub1abc")) is True
    assert order == [("relay", {"vmhost_allowed_npubs": "npub1abc"}), ("cache", "vmhost_allowed_npubs", "npub1abc", False)]


# ================================================================================ session keys
def req(sk, op, args, rid, ts=None):
    return transport.build_request(sk, NODE, op, args, rid, created_at=ts)


def session_args(owner=USER, spk_sk=SESS_SK, exp=None, content=None):
    exp = exp or int(time.time()) + 3600
    proof = build_event(spk_sk, sessions.PROOF_KIND, content or sessions.proof_content(owner, exp, NODE), [])
    return {"pk": pub(spk_sk), "exp": exp, "scope": "use", "proof": proof}


def tsetup(tmp_path):
    svc, be, root = make(tmp_path)
    published = []

    async def publish(ev):
        published.append(ev)
        return True
    return transport.Transport(svc, NODE_SK, publish), svc, be, root


def answer(tr, ev, sk):
    out = run(tr.on_event(ev))
    return None if out is None else json.loads(nip44.decrypt_from(sk, bytes.fromhex(out["pubkey"]), out["content"]))


def test_a_session_key_acts_as_its_owner_for_use_ops_only(tmp_path):
    tr, svc, be, root = tsetup(tmp_path)
    assert answer(tr, req(SESS_SK, "vm.list", {}, "pre"), SESS_SK) is None, "an unknown key is dropped"
    opened = answer(tr, req(USER_SK, "session.open", session_args(), "open"), USER_SK)
    assert opened["ok"], opened
    lst = answer(tr, req(SESS_SK, "vm.list", {}, "l"), SESS_SK)
    assert lst["ok"] and [v["uuid"] for v in lst["result"]["vms"]] == [U1, U2]
    power = answer(tr, req(SESS_SK, "vm.power", {"vm": U1, "action": "start"}, "p"), SESS_SK)
    assert power["ok"] and be.domains[U1]["state"] == "running"
    again = answer(tr, req(SESS_SK, "session.open", session_args(spk_sk=bytes.fromhex("08" * 32)), "o2"), SESS_SK)
    assert again["error"]["code"] == "step_up_required", "a session cannot open a session"


def test_an_admins_session_cannot_manage_anything(tmp_path):
    tr, svc, be, root = tsetup(tmp_path)
    assert answer(tr, req(ADMIN_SK, "session.open", session_args(owner=ADMIN), "open"), ADMIN_SK)["ok"]
    assert answer(tr, req(SESS_SK, "host.info", {}, "i"), SESS_SK)["result"]["cpu"]["cores"] == 8
    for op, args in (("vm.update", {"vm": U1, "vcpus": 1}), ("vm.delete", {"vm": U1, "confirm_name": "alpha"}),
                     ("vm.assign", {"vm": U1, "pubkey": NEWUSER}), ("host.access.set", {"allowed": []}),
                     ("iso.list", {}), ("vm.snapshot.create", {"vm": U1, "name": "x"}), ("vm.create", {"name": "z"})):
        res = answer(tr, req(SESS_SK, op, args, "x-" + op.replace(".", "-")), SESS_SK)
        assert res["ok"] is False and res["error"]["code"] == "step_up_required", (op, res)
    assert len(be.domains) == 2 and be.domains[U1]["vcpus"] == 2 and be.snapshots == {}


def test_an_ended_session_is_answered_not_dropped(tmp_path):
    tr, svc, be, root = tsetup(tmp_path)
    assert answer(tr, req(USER_SK, "session.open", session_args(), "open"), USER_SK)["ok"]
    assert answer(tr, req(SESS_SK, "session.close", {}, "close"), SESS_SK)["result"]["closed"] == 1
    res = answer(tr, req(SESS_SK, "vm.list", {}, "after"), SESS_SK)
    assert res is not None and res["error"]["code"] == "session_expired"


def test_sessions_survive_a_restart_and_expire(tmp_path):
    tr, svc, be, root = tsetup(tmp_path)
    assert answer(tr, req(USER_SK, "session.open", session_args(), "open"), USER_SK)["ok"]
    assert oct(os.stat(root / ".state" / "sessions.json").st_mode & 0o777) == "0o600"
    svc2 = VmHostService(svc.cfg, be, node_pubkey=NODE, admin_provider=svc._admin_provider, storage=svc.storage)
    run(svc2.refresh_index())
    tr2 = transport.Transport(svc2, NODE_SK, tr.publish)
    assert answer(tr2, req(SESS_SK, "vm.get", {"vm": U1}, "g"), SESS_SK)["ok"]
    svc2._sessions().now = lambda: time.time() + 7200
    res = answer(tr2, req(SESS_SK, "vm.get", {"vm": U1}, "g2"), SESS_SK)
    assert res["error"]["code"] == "session_expired"


@pytest.mark.parametrize("mutate,why", [
    (lambda a: a.update(exp=int(time.time()) + 13 * 3600), "longer than vmhost_session_max_hours"),
    (lambda a: a.update(scope="admin"), "a scope other than use"),
    (lambda a: a.update(proof=build_event(bytes.fromhex("09" * 32), sessions.PROOF_KIND, a["proof"]["content"], [])),
     "a proof signed by some other key"),
    (lambda a: a.update(proof=dict(build_event(bytes.fromhex("09" * 32), sessions.PROOF_KIND, a["proof"]["content"], []),
                                   pubkey=a["pk"])), "a proof wearing the session key but signed by another"),
    (lambda a: a.update(proof=dict(a["proof"], content="posterchan-vmhost-session:" + ADMIN + ":1:x")),
     "a proof over somebody else's session"),
    (lambda a: a.update(pk=ADMIN), "a session key that is a real identity on the host"),
    (lambda a: a.pop("proof"), "no proof"),
])
def test_session_open_refusals(tmp_path, mutate, why):
    tr, svc, be, root = tsetup(tmp_path)
    args = session_args()
    mutate(args)
    res = answer(tr, req(USER_SK, "session.open", args, "open"), USER_SK)
    assert res["ok"] is False and res["error"]["code"] == "bad_request", (why, res)
    assert svc._sessions().lookup(pub(SESS_SK)) == (None, None)
