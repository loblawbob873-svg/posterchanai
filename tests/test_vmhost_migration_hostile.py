"""A HOSTILE SOURCE HOST, end to end through the shipped transport, transfer route and target migrator.

The trust boundary of a migration runs between the two hosts: `vmhost_peer_hosts` pairs them, but pairing is
not handing the other host root. A domain definition IS root on the host that defines it — a `qemu:commandline`
runs anything, a serial port of `type=file` writes any path the qemu user can, a `<kernel>` boots a host file, a
static `<seclabel>` turns off confinement. So the TARGET must never define what the source sent: it rebuilds the
domain from a small validated field set and refuses what would change the machine's meaning.

How the source is made hostile: its own export-time checks (`_refusals`, `_collect_files`) are bypassed exactly as
a malicious host would bypass them, and its libvirt answers with the hostile definition. Everything the target
does is the shipped code.
"""
import asyncio
import re
import xml.etree.ElementTree as ET

import pytest

from app.services.vmhost import domainxml, migrate
from tests.vmhost_migration_fake import T, USER, VM, World, seed_vm, sha, until

VICTIM = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"
QEMU_NS = "http://libvirt.org/schemas/domain/qemu/1.0"
MAC = "52:54:00:ab:cd:ef"


def run(coro):
    return asyncio.run(coro)


def state(host, mig):
    r = host.rec(mig)
    return r and r["state"]


def make_hostile(w, mutate_xml=None, mutate_snaps=None):
    """The source host lies from here on: its libvirt reports `mutate_xml(clean)`, its checks are skipped."""
    be, m = w.S.backend, w.S.migrator
    real_dump = be.dumpxml_inactive

    async def dump(u):
        xml = await real_dump(u)
        return mutate_xml(xml) if mutate_xml else xml
    be.dumpxml_inactive = dump
    m._refusals = lambda info: None
    real_collect = m._collect_files

    def collect(u, info, meta=None):
        return real_collect(u, migrate.inspect_domain_xml(be._reported_xml(u)), meta)
    m._collect_files = collect
    if mutate_snaps:
        for s in be.snapshots.get(VM, []):
            s["xml"] = mutate_snaps(s["xml"])


def add_mac(host):
    d = host.backend.domains[VM]
    d["xml"] = d["xml"].replace('<interface type="network">', f'<interface type="network"><mac address="{MAC}"/>', 1)


def devices(extra):
    return lambda xml: xml.replace("</devices>", extra + "</devices>", 1)


async def migrate_to_end(w):
    res = await w.call(w.S, "vm.migrate", {"vm": VM, "target": T, "authz": w.authz(), "start_after": False})
    assert res and res["ok"], res
    mig = res["result"]["migration"]["id"]
    await until(lambda: state(w.T, mig) in ("done", "aborted") and state(w.S, mig) in ("done", "aborted"),
                what="the migration to finish either way")
    return mig


def defined_texts(w):
    """Everything the target handed its hypervisor: the domain and every snapshot definition."""
    out = [w.T.backend.domains[VM]["xml"]] if VM in w.T.backend.domains else []
    out += [s["xml"] for s in w.T.backend.snapshots.get(VM, [])]
    return out


# ------------------------------------------------------------------------------------------ dropped constructs
DROPPED = {
    "static root seclabel": (
        lambda x: x.replace("</devices>", "</devices><seclabel type='static' model='dac' relabel='no'>"
                                          "<label>+0:+0</label></seclabel>", 1),
        ["seclabel", "+0:+0"]),
    "qemu:commandline": (
        lambda x: x.replace('<domain type="kvm">', f'<domain type="kvm" xmlns:qemu="{QEMU_NS}">', 1)
                   .replace("</devices>", "</devices><qemu:commandline><qemu:arg value='-drive'/>"
                                          "<qemu:arg value='file=/etc/shadow'/></qemu:commandline>", 1),
        ["commandline", "/etc/shadow", QEMU_NS]),
    "qemu:commandline under another prefix": (
        lambda x: x.replace('<domain type="kvm">', f'<domain type="kvm" xmlns:zz="{QEMU_NS}">', 1)
                   .replace("</devices>", "</devices><zz:commandline><zz:arg value='-chardev'/>"
                                          "<zz:arg value='file,path=/root/.ssh/authorized_keys'/></zz:commandline>", 1),
        ["commandline", "authorized_keys", QEMU_NS]),
    "serial/console/channel on host paths": (
        devices("<serial type='file'><source path='/etc/cron.d/pwn'/><target port='0'/></serial>"
                "<console type='dev'><source path='/dev/sda'/><target type='serial'/></console>"
                "<channel type='unix'><source mode='bind' path='/run/libvirt/libvirt-sock'/>"
                "<target type='virtio' name='x.0'/></channel>"
                "<serial type='pipe'><source path='/tmp/pipe'/><target port='1'/></serial>"),
        ["/etc/cron.d/pwn", "/dev/sda", "libvirt-sock", "/tmp/pipe", "<serial", "<console"]),
    "ethernet interface with a script": (
        devices("<interface type='ethernet'><mac address='52:54:00:00:00:99'/><script path='/tmp/evil.sh'/>"
                "<target dev='tapx'/></interface>"),
        ["/tmp/evil.sh", "ethernet", "<script"]),
    "spice and vnc on every address and on a socket": (
        devices("<graphics type='spice' autoport='yes' listen='0.0.0.0'><listen type='address' address='0.0.0.0'/>"
                "</graphics><graphics type='vnc' socket='/tmp/vnc.sock'><listen type='socket' socket='/tmp/vnc.sock'/>"
                "</graphics><graphics type='vnc' port='5999' listen='0.0.0.0'/>"),
        ["spice", "0.0.0.0", "/tmp/vnc.sock", "5999"]),
    "kernel, initrd, dtb, cmdline": (
        lambda x: x.replace("<nvram>", "<kernel>/boot/vmlinuz-host</kernel><initrd>/boot/initrd-host</initrd>"
                                       "<dtb>/boot/host.dtb</dtb><cmdline>init=/bin/sh</cmdline><nvram>", 1),
        ["vmlinuz-host", "initrd-host", "host.dtb", "init=/bin/sh"]),
    "nvram template and a foreign nvram path": (
        lambda x: re.sub(r"<nvram>[^<]*</nvram>", "<nvram template='/etc/shadow'>/etc/passwd</nvram>", x),
        ["/etc/shadow", "/etc/passwd", "template"]),
    "a custom emulator": (
        devices("<emulator>/tmp/evil-qemu</emulator>"),
        ["/tmp/evil-qemu", "<emulator"]),
    "a snapshot that embeds a hostile domain": (
        None,
        ["/etc/shadow", "commandline"]),
}


@pytest.mark.parametrize("case", sorted(DROPPED))
def test_a_hostile_construct_never_reaches_the_targets_hypervisor(tmp_path, case):
    mutate, needles = DROPPED[case]

    async def go():
        w = World(tmp_path)
        try:
            seed_vm(w.S, state="shutoff")
            add_mac(w.S)
            await w.S.svc.refresh_index()

            def hostile_snap(sx):
                return sx.replace("<domainsnapshot>", f"<domainsnapshot xmlns:qemu='{QEMU_NS}'>", 1).replace(
                    "</devices>", "</devices><qemu:commandline><qemu:arg value='/etc/shadow'/>"
                                  "</qemu:commandline>", 1)
            make_hostile(w, mutate, hostile_snap if case == "a snapshot that embeds a hostile domain" else None)
            mig = await migrate_to_end(w)
            assert state(w.T, mig) == "done", (case, w.T.rec(mig).get("error"))
            texts = defined_texts(w)
            assert texts, "the VM was defined"
            for t in texts:
                for n in needles:
                    assert n not in t, f"{case}: {n!r} reached the target's libvirt:\n{t}"
            root = ET.fromstring(w.T.backend.domains[VM]["xml"])
            vnc = root.findall("devices/graphics")
            assert len(vnc) == 1 and vnc[0].get("type") == "vnc" and vnc[0].get("listen") == "127.0.0.1"
            assert [li.get("address") for li in vnc[0].findall("listen")] == ["127.0.0.1"]
            assert vnc[0].get("passwd"), "a display with no password auth"
            assert root.find("devices/emulator") is None
            assert [i.get("type") for i in root.findall("devices/interface")] == ["network"]
            assert root.find("devices/interface/mac").get("address") == MAC
        finally:
            await w.close()
    run(go())


# ------------------------------------------------------------------------------------------ refused constructs
REFUSED = {
    "a shared host filesystem": devices("<filesystem type='mount' accessmode='passthrough'><source dir='/'/>"
                                        "<target dir='hostroot'/></filesystem>"),
    "a host device": devices("<hostdev mode='subsystem' type='usb'><source><vendor id='0x1234'/>"
                             "<product id='0xbeef'/></source></hostdev>"),
    "a passthrough tpm": devices("<tpm model='tpm-tis'><backend type='passthrough'><device path='/dev/tpm0'/>"
                                 "</backend></tpm>"),
    "a block disk": devices("<disk type='block' device='disk'><driver name='qemu' type='raw'/>"
                            "<source dev='/dev/sda'/><target dev='vdb' bus='virtio'/></disk>"),
    "a file disk that also names a device": lambda x: x.replace(
        '<source file="', '<source dev="/dev/sda" file="', 1),
    "a volume disk": devices("<disk type='volume' device='disk'><source pool='default' volume='host.img'/>"
                             "<target dev='vdb' bus='virtio'/></disk>"),
    "a network disk": devices("<disk type='network' device='disk'><source protocol='nbd' name='x'>"
                              "<host name='127.0.0.1' port='10809'/></source><target dev='vdb' bus='virtio'/></disk>"),
    "a backing chain": lambda x: x.replace('<target dev="vda" bus="virtio"/>',
                                           '<target dev="vda" bus="virtio"/><backingStore type="file">'
                                           '<format type="qcow2"/><source file="/var/lib/base.qcow2"/>'
                                           '</backingStore>', 1),
    "a disk outside the transfer": devices("<disk type='file' device='disk'><driver name='qemu' type='qcow2'/>"
                                           "<source file='/var/lib/libvirt/images/other.qcow2'/>"
                                           "<target dev='vdc' bus='virtio'/></disk>"),
    "a foreign architecture": lambda x: x.replace('arch="x86_64"', 'arch="aarch64"', 1),
}


@pytest.mark.parametrize("case", sorted(REFUSED))
def test_a_dangerous_construct_refuses_the_migration_and_the_vm_stays_on_the_source(tmp_path, case):
    async def go():
        w = World(tmp_path)
        try:
            disk = seed_vm(w.S, state="shutoff")
            want = sha(disk)
            await w.S.svc.refresh_index()
            make_hostile(w, REFUSED[case])
            mig = await migrate_to_end(w)
            assert state(w.T, mig) == "aborted" and state(w.S, mig) == "aborted", case
            assert VM not in w.T.backend.domains and not (w.T.root / VM).exists(), case
            assert VM in w.S.backend.domains and sha(disk) == want
        finally:
            await w.close()
    run(go())


@pytest.mark.parametrize("snap", ["external disk", "external memory", "bad name", "unknown parent"])
def test_a_hostile_snapshot_refuses_the_migration(tmp_path, snap, monkeypatch):
    # The source's own external-snapshot check is skipped too: the target must not rely on it.
    monkeypatch.setattr(migrate, "snapshot_is_external", lambda xml: False)

    async def go():
        w = World(tmp_path)
        try:
            seed_vm(w.S, state="shutoff")
            await w.S.svc.refresh_index()
            be = w.S.backend
            s0 = be.snapshots[VM][0]
            if snap == "external disk":
                s0["xml"] = s0["xml"].replace("snapshot='internal'", "snapshot='external'")
            elif snap == "external memory":
                s0["xml"] = s0["xml"].replace("<disks>", "<memory snapshot='external' file='/etc/shadow'/><disks>")
            elif snap == "bad name":
                s0["name"] = "--all"
                s0["xml"] = s0["xml"].replace("<name>clean-install</name>", "<name>--all</name>", 1)
            else:
                s1 = be.snapshots[VM][1]
                s1["xml"] = s1["xml"].replace("<parent><name>clean-install</name></parent>",
                                              "<parent><name>nowhere</name></parent>")
            make_hostile(w)
            real_names = be.snapshot_names

            async def names(u):                         # the source's own name check skipped as well
                return [s["name"] for s in be.snapshots[u]], be.snapshots[u][-1]["name"]
            be.snapshot_names = names
            mig = await migrate_to_end(w)
            assert state(w.T, mig) == "aborted", (snap, w.T.rec(mig))
            assert VM not in w.T.backend.domains and real_names
        finally:
            await w.close()
    run(go())


# ------------------------------------------------------------------------------------------ identity (HIGH 3)
@pytest.mark.parametrize("what", ["uuid", "name"])
def test_a_definition_wearing_another_vms_identity_is_refused_and_the_victim_is_untouched(tmp_path, what):
    async def go():
        w = World(tmp_path)
        try:
            seed_vm(w.S, state="shutoff")
            await w.S.svc.refresh_index()
            # The target already runs a VM: "victim".
            vdir = w.T.root / VICTIM
            vdir.mkdir(parents=True)
            (vdir / "disk-vda.qcow2").write_bytes(b"QFI\xfb" + b"victim-data" * 1000)
            victim_sha = sha(vdir / "disk-vda.qcow2")
            w.T.backend.add_domain(VICTIM, "victim", state="running",
                                   meta=domainxml.VmMeta(owner="ab" * 32, created=1, assigned=["cd" * 32]))
            victim_xml = w.T.backend.domains[VICTIM]["xml"]
            await w.T.svc.refresh_index()
            if what == "uuid":
                mutate = lambda x: x.replace(f"<uuid>{VM}</uuid>", f"<uuid>{VICTIM}</uuid>", 1)  # noqa: E731
            else:
                mutate = lambda x: x.replace("<name>alpha</name>", "<name>victim</name>", 1)  # noqa: E731
            make_hostile(w, mutate)
            mig = await migrate_to_end(w)
            assert state(w.T, mig) == "aborted", w.T.rec(mig)
            assert w.T.backend.domains[VICTIM]["xml"] == victim_xml and w.T.backend.domains[VICTIM]["state"] == "running"
            assert sha(vdir / "disk-vda.qcow2") == victim_sha
            assert VM not in w.T.backend.domains
        finally:
            await w.close()
    run(go())


# ------------------------------------------------------------------------------------------ positive round trip
def test_an_ordinary_vm_keeps_what_makes_it_that_vm_through_the_rebuild(tmp_path):
    async def go():
        w = World(tmp_path)
        try:
            seed_vm(w.S, state="shutoff")
            add_mac(w.S)
            vdb = w.S.root / VM / "disk-vdb.qcow2"
            vdb.write_bytes(b"QFI\xfb" + b"\x00\x00\x00\x03" + b"\x00" * 8 + b"second disk" * 5000)
            d = w.S.backend.domains[VM]
            d["xml"] = d["xml"].replace("</disk>", "</disk><disk type=\"file\" device=\"disk\"><driver name=\"qemu\" "
                                        f"type=\"qcow2\"/><source file=\"{vdb}\"/><target dev=\"vdb\" bus=\"virtio\"/>"
                                        "</disk>", 1)
            d["vcpus"], d["ram_mib"] = 3, 3072
            d["xml"] = d["xml"].replace("<vcpu>2</vcpu>", "<vcpu>3</vcpu>").replace(">2048<", ">3072<")
            await w.S.svc.refresh_index()
            mig = await migrate_to_end(w)
            assert state(w.T, mig) == "done", w.T.rec(mig)
            tdir = w.T.root / VM
            root = ET.fromstring(w.T.backend.domains[VM]["xml"])
            assert root.findtext("uuid") == VM and root.findtext("name") == "alpha"
            assert root.findtext("vcpu") == "3" and root.find("memory").text == "3072"
            disks = [(x.find("target").get("dev"), x.find("source").get("file"), x.find("driver").get("type"))
                     for x in root.findall("devices/disk") if x.get("device") == "disk"]
            assert disks == [("vda", str(tdir / "disk-vda.qcow2"), "qcow2"), ("vdb", str(tdir / "disk-vdb.qcow2"), "qcow2")]
            assert sha(tdir / "disk-vdb.qcow2") == sha(w.S.root / ".retained" / w.S.rec(mig)["retained"] / "disk-vdb.qcow2")
            assert root.find("os").get("firmware") == "efi" and root.findtext("os/nvram") == str(tdir / "nvram.fd")
            nic = root.find("devices/interface")
            assert nic.get("type") == "network" and nic.find("source").get("network") == "default"
            assert nic.find("mac").get("address") == MAC
            meta = domainxml.parse_meta(w.T.backend.domains[VM]["meta_xml"])
            assert meta.assigned == [USER] and meta.labels == ["web"] and meta.guest == "linux"
            snaps = w.T.backend.snapshots[VM]
            assert [(s["name"], s["current"]) for s in snaps] == [("clean-install", False), ("before-upgrade", True)]
            assert "<parent><name>clean-install</name></parent>" in snaps[1]["xml"]
        finally:
            await w.close()
    run(go())
