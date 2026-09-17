"""Bugs the FIRST LIVE RUN against real libvirt 12 / QEMU 10.2 found (scripts/vmhost_live_probe.py on nas.lan).

Every one of them was invisible to the fake hypervisor, because each is about something only a real host has: a
second Unix user (libvirt runs QEMU as `qemu`, not as the app), firmware descriptors that pick a qcow2 varstore,
libvirt refusing to undefine a domain that has snapshots, and `dumpxml --migratable` rewriting `<os>`. The
fixtures in tests/fixtures/vmhost_real/ are what the live host printed.
"""
import asyncio
import os
import re
import stat
from pathlib import Path


from app.services.vmhost import domainxml, migrate
from app.services.vmhost.backend import VirshBackend
from app.services.vmhost.config import VmHostConfig
from app.services.vmhost.service import VmHostService
from app.services.vmhost.storage import Storage
from tests.vmhost_fake import FakeBackend

REAL = Path(__file__).parent / "fixtures" / "vmhost_real"
ADMIN = "ad" * 32
U = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"


def run(coro):
    return asyncio.run(coro)


def mode(p) -> int:
    return stat.S_IMODE(os.stat(p).st_mode)


def service(tmp_path, backend=None):
    storage = Storage(tmp_path / "vms")
    storage.ensure()
    cfg = VmHostConfig(enabled=True, storage_dir=str(storage.root), admin_pubkeys=[ADMIN], reserve_disk_gib=1)
    be = backend or FakeBackend()
    return VmHostService(cfg, be, node_pubkey="0e" * 32, admin_provider=lambda: set(), storage=storage), be


async def create(svc, **kw):
    args = {"name": "probe", "vcpus": 1, "ram_mib": 512, "disk_gib": 2, **kw}
    res = await svc.handle(ADMIN, "vm.create", args, os.urandom(6).hex())
    assert res["ok"], res
    return res["result"]["vm"]["uuid"]


# ------------------------------------------------------------------------------ 1. the qemu user could not open the disk
def test_a_vm_directory_is_traversable_by_the_qemu_user(tmp_path):
    """LIVE: `error: Cannot access storage file '…/disk-vda.qcow2' (as uid:77, gid:77): Permission denied` — every VM
    this host created failed its first start. The directory was mkdir'd 0750 by the app user; libvirt's dynamic
    ownership chowns the DISK to qemu, never the directory it has to walk through."""
    svc, _ = service(tmp_path)
    u = run(create(svc))
    m = mode(svc.storage.root / u)
    assert m & stat.S_IXOTH, f"{oct(m)}: qemu cannot traverse the VM directory"
    assert not m & (stat.S_IROTH | stat.S_IWOTH), f"{oct(m)}: other users may list or write the VM directory"


def test_storage_ensure_leaves_the_root_traversable_and_the_library_readable(tmp_path):
    st = Storage(tmp_path / "fresh")
    st.ensure()
    assert mode(st.root) & stat.S_IXOTH and not mode(st.root) & stat.S_IROTH
    assert mode(st.iso_dir) & (stat.S_IROTH | stat.S_IXOTH) == (stat.S_IROTH | stat.S_IXOTH), "qemu reads installers"
    assert not mode(st.state_dir) & 0o077, "the op journal and sessions are the app's alone"


def test_a_created_disk_is_not_readable_by_other_users(tmp_path):
    """qemu-img creates 0644 under the default umask; inside a traversable directory that is every local user
    reading every guest's disk."""
    target = tmp_path / "disk-vda.qcow2"

    async def runner(argv, timeout, stdin=None):
        Path(argv[-2]).write_bytes(b"QFI\xfb")
        os.chmod(argv[-2], 0o644)
        return 0, "", ""
    run(VirshBackend(runner=runner).img_create(str(target), 2))
    assert mode(target) == 0o600


# ------------------------------------------------------------------------------ 2. the app could not read the nvram
def _with_template(xml: str, template: Path) -> str:
    return re.sub(r"<nvram>", f'<nvram template="{template}" templateFormat="qcow2" format="qcow2">', xml, count=1)


class TemplateBackend(FakeBackend):
    """What libvirt 12 reports after firmware auto-selection: the nvram names the template it will copy."""

    def __init__(self, template):
        super().__init__()
        self.template = template

    def _reported_xml(self, vm_uuid):
        return _with_template(super()._reported_xml(vm_uuid), self.template)


def test_the_nvram_is_seeded_by_the_app_before_the_first_start(tmp_path):
    """LIVE: libvirt created nvram.fd from the template at first start as qemu:qemu 0600 and never gave it back, so
    the app could not hash it for a migration (PermissionError) or copy it for a snapshot. Copied from the SAME
    template by the app before the first start, libvirt's remember_owner restores it to the app on every stop."""
    template = tmp_path / "OVMF_VARS_4M.qcow2"
    template.write_bytes(b"QFI\xfb" + os.urandom(64))
    svc, be = service(tmp_path, TemplateBackend(template))
    u = run(create(svc, start=True))
    nv = svc.storage.nvram_path(u)
    assert nv.read_bytes() == template.read_bytes()
    assert mode(nv) == 0o600
    start = next(i for i, c in enumerate(be.calls) if c[0] == "start")
    seeded_before = [c for c in be.calls[:start] if c[0] == "dumpxml"]
    assert seeded_before, "the nvram must exist before libvirt's first start creates it as qemu"


def test_nvram_seed_reads_the_real_libvirt_12_definition():
    xml = (REAL / "virsh-dumpxml-inactive-efi.out").read_text()
    path, template, fmt = domainxml.nvram_seed(xml)
    assert path.endswith("/nvram.fd") and template == "/usr/share/edk2/OvmfX64/OVMF_VARS_4M.qcow2" and fmt == "qcow2"
    assert domainxml.nvram_seed((REAL / "virsh-dumpxml-inactive-bios.out").read_text()) == ("", "", "")


def test_an_existing_nvram_is_never_overwritten(tmp_path):
    template = tmp_path / "vars"
    template.write_bytes(b"template")
    svc, _ = service(tmp_path, TemplateBackend(template))
    u = run(create(svc))
    nv = svc.storage.nvram_path(u)
    nv.write_bytes(b"the guest's boot entries")
    run(svc._ensure_nvram(u))
    assert nv.read_bytes() == b"the guest's boot entries"


# ------------------------------------------------------------------------------ 3. delete refused with snapshots
def test_undefine_drops_snapshot_metadata():
    """LIVE: `error: Requested operation is not valid: cannot delete inactive domain with 2 snapshots` — vm.delete
    of a VM with snapshots failed. Internal snapshots live inside the qcow2 (deleted with it or kept with it)."""
    seen = []

    async def runner(argv, timeout, stdin=None):
        seen.append(argv)
        if argv[3] == "undefine" and "--snapshots-metadata" not in argv and "--tpm" not in argv:
            return 1, "", ("error: Failed to undefine domain '%s'\nerror: Requested operation is not valid: cannot "
                           "delete inactive domain with 2 snapshots" % U)
        return 0, "", ""
    be = VirshBackend(runner=runner)
    run(be.undefine(U, keep_nvram=False))
    run(be.undefine(U, keep_nvram=True))
    assert all("--snapshots-metadata" in a for a in seen if a[3] == "undefine" and "--tpm" not in a)


# ------------------------------------------------------------------------------ 4/5. migration against real XML
REAL_MIGRATABLE = (REAL / "virsh-dumpxml-inactive-migratable.out").read_text()


def test_the_real_migratable_definition_hides_firmware_autoselection():
    """What `dumpxml --inactive --migratable` really prints on libvirt 12: `<os>` WITHOUT firmware='efi', with the
    auto-selected loader path spelled out."""
    info = migrate.inspect_domain_xml(REAL_MIGRATABLE)
    assert info["loader"].startswith("/usr/share/edk2/") and not info["firmware_auto"]


def _real_world(tmp_path, monkeypatch, nvram_bytes: bytes):
    from tests.vmhost_migration_fake import VM, World, seed_vm
    real_exists = os.path.exists
    monkeypatch.setattr(migrate.os.path, "exists",
                        lambda p: False if str(p).startswith("/usr/share/edk2") else real_exists(p))
    w = World(tmp_path)
    seed_vm(w.S, state="shutoff", snapshots=False, name=migrate.inspect_domain_xml(REAL_MIGRATABLE)["name"])
    vm_dir = w.S.root / VM
    (vm_dir / "nvram.fd").write_bytes(nvram_bytes)
    real = REAL_MIGRATABLE.replace("00000000-0000-4000-8000-000000000002", VM)
    real = real.replace("/var/lib/posterchan/vms-probe/", str(w.S.root) + "/")
    # the probe added a second disk in step 6 (and the snapshot record names it); this seeded VM has only vda
    real = re.sub(r"<disk type='file' device='disk'>(?:(?!</disk>).)*disk-vdb\.qcow2.*?</disk>", "", real, flags=re.S)
    real = re.sub(r"<pc:snapshot .*?</pc:snapshot>", "", real, flags=re.S)
    real = re.sub(r"<disk type='file' device='cdrom'>.*?</disk>", "", real, flags=re.S)
    meta = domainxml.parse_meta(w.S.backend.domains[VM]["meta_xml"])
    w.S.backend.domains[VM]["xml"] = real
    w.S.backend.domains[VM]["meta_xml"] = meta.to_xml(prefixed=False)
    return w, VM


def _migrate(w, vm):
    from tests.vmhost_migration_fake import T, until

    async def go():
        await w.S.svc.refresh_index()
        res = await w.call(w.S, "vm.migrate", {"vm": vm, "target": T, "authz": w.authz(vm=vm)})
        assert res["ok"], res
        mig = res["result"]["migration"]["id"]
        await until(lambda: (w.T.rec(mig) or {}).get("state") in ("done", "aborted")
                    and (w.S.rec(mig) or {}).get("state") in ("done", "aborted"), what="settled")
        return w.T.rec(mig), w.S.rec(mig)
    return go()


def test_a_target_without_the_sources_firmware_path_still_accepts_an_efi_vm(tmp_path, monkeypatch):
    """LIVE capture + fix: the source sends the loader it read from `--migratable` XML (a Gentoo edk2 path) and
    `firmware_auto` False, and a target without that exact file (every Debian host: /usr/share/OVMF) refused the
    precheck — although the target never uses that path: it REBUILDS with `firmware="efi"` auto-selection."""
    qcow2_vars = b"QFI\xfb\x00\x00\x00\x03" + b"\x00" * 8 + os.urandom(4080)

    async def go():
        w, vm = _real_world(tmp_path, monkeypatch, qcow2_vars)
        try:
            trec, srec = await _migrate(w, vm)
            assert trec["state"] == "done", (trec.get("error"), trec.get("history"))
            import xml.etree.ElementTree as ET
            nv = ET.fromstring(w.T.backend.domains[vm]["xml"]).find("os/nvram")
            assert nv.get("format") == "qcow2", "the varstore's PROBED format picks a matching firmware on the target"
            assert mode(w.T.root / vm) & stat.S_IXOTH and mode(w.T.root / vm / "disk-vda.qcow2") == 0o600
        finally:
            await w.close()
    run(go())


def test_a_hostile_nvram_naming_a_backing_file_is_refused(tmp_path, monkeypatch):
    """LIVE-derived: libvirt 12 runs qcow2 varstores, and the target never probed nvram.fd — a qcow2 "varstore" whose
    backing file is a host path would hand the guest's firmware a read-through view of that file."""
    backed = b"QFI\xfb\x00\x00\x00\x03" + (1024).to_bytes(8, "big") + os.urandom(4080)

    async def go():
        w, vm = _real_world(tmp_path, monkeypatch, backed)
        monkeypatch.setattr(migrate, "qcow2_has_backing", lambda p: False)     # the source's own check skipped
        try:
            trec, _ = await _migrate(w, vm)
            assert trec["state"] == "aborted" and "nvram" in (trec.get("error") or ""), trec.get("error")
            assert vm not in w.T.backend.domains
        finally:
            await w.close()
    run(go())
