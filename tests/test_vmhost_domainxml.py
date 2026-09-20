"""The domain XML a hosted VM is defined with — parsed back, never string-matched alone.

A server VM differs from the desktop's SPICE+virgl session VM in ways that each fail silently: a GL
GPU with no GL context boots to black; VNC on a public address is an unauthenticated keyboard; a
Windows installer with only virtio devices sees no disk. These parse the shipped output to prove the
headless shape, and feed hostile values through to prove they are escaped rather than injected.
"""
import xml.etree.ElementTree as ET

import pytest

from app.services.vmhost import domainxml

U = "11111111-1111-4111-8111-111111111111"
PK = "ab" * 32


def spec(**kw):
    base = dict(name="web1", uuid=U, guest="linux", firmware="efi", vcpus=4, ram_mib=4096,
                disk_path=f"/var/lib/posterchan/vms/{U}/disk-vda.qcow2",
                nvram_path=f"/var/lib/posterchan/vms/{U}/nvram.fd",
                iso_path="/var/lib/posterchan/vms/isos/debian.iso", network="default",
                meta=domainxml.VmMeta(owner=PK, created=1700000000, disk_gib=40, assigned=[PK]))
    base.update(kw)
    return domainxml.DomainSpec(**base)


def parse(s):
    return ET.fromstring(domainxml.build_domain_xml(s))


def test_it_is_well_formed_and_carries_the_sizes():
    d = parse(spec())
    assert d.tag == "domain" and d.get("type") == "kvm"
    assert d.findtext("name") == "web1" and d.findtext("uuid") == U
    assert d.find("memory").text == "4096" and d.find("memory").get("unit") == "MiB"
    assert d.findtext("vcpu") == "4"
    assert d.find("devices/emulator") is None, "libvirt picks the host's own emulator"


def test_the_display_is_headless_vnc_on_loopback_only():
    d = parse(spec())
    g = d.findall("devices/graphics")
    assert len(g) == 1 and g[0].get("type") == "vnc"
    assert g[0].get("listen") == "127.0.0.1"
    assert [l.get("address") for l in g[0].findall("listen")] == ["127.0.0.1"]
    assert d.find("devices/graphics[@type='spice']") is None
    model = d.find("devices/video/model")
    assert model.get("type") == "vga"   # a plain VGA framebuffer renders from firmware POST
    # through GRUB, the kernel and the OS installer; virtio-gpu showed "Display output is not active"
    # (no scanout) whenever the guest was not actively driving it — a black console during boot/install.
    assert model.find("acceleration") is None and d.find(".//gl") is None, \
        "no accel3d/gl: a headless host has no GL context and the guest boots to black"


def test_linux_gets_virtio_and_a_real_network():
    d = parse(spec())
    disk = d.find("devices/disk[@device='disk']")
    assert disk.find("target").get("bus") == "virtio"
    assert disk.find("source").get("file").endswith("disk-vda.qcow2")
    nic = d.find("devices/interface")
    assert nic.get("type") == "network" and nic.find("source").get("network") == "default"
    assert nic.find("model").get("type") == "virtio"
    assert d.find("devices/interface[@type='user']") is None
    assert d.find("devices/tpm") is None
    assert d.find("devices/channel/target").get("name") == "org.qemu.guest_agent.0"


def test_windows_gets_devices_its_installer_can_see_plus_tpm_and_secure_boot():
    d = parse(spec(guest="windows"))
    assert d.find("devices/disk[@device='disk']/target").get("bus") == "sata"
    assert d.find("devices/interface/model").get("type") == "e1000e"
    assert d.find("devices/tpm/backend").get("version") == "2.0"
    assert d.find("os/firmware/feature[@name='secure-boot']").get("enabled") == "yes"
    assert d.find("features/smm").get("state") == "on"
    assert d.find("clock").get("offset") == "localtime"


def test_efi_names_its_nvram_inside_the_vm_directory_and_bios_has_none():
    efi = parse(spec())
    assert efi.find("os").get("firmware") == "efi"
    assert efi.findtext("os/nvram") == f"/var/lib/posterchan/vms/{U}/nvram.fd"
    bios = parse(spec(firmware="bios"))
    assert bios.find("os").get("firmware") is None and bios.find("os/nvram") is None


def test_boot_order_follows_whether_there_is_installer_media():
    assert [b.get("dev") for b in parse(spec()).findall("os/boot")] == ["cdrom", "hd"]
    no_iso = parse(spec(iso_path=""))
    assert [b.get("dev") for b in no_iso.findall("os/boot")] == ["hd"]
    assert no_iso.find("devices/disk[@device='cdrom']") is None


def test_a_bridge_replaces_the_network():
    nic = parse(spec(bridge="br0")).find("devices/interface")
    assert nic.get("type") == "bridge" and nic.find("source").get("bridge") == "br0"


def test_metadata_is_embedded_in_the_pc_namespace_and_reads_back():
    xml = domainxml.build_domain_xml(spec())
    d = ET.fromstring(xml)
    vm = d.find("metadata/{%s}vm" % domainxml.PC_NS)
    assert vm is not None and vm.get("owner") == PK
    assert domainxml.parse_meta(xml).assigned == [PK]


@pytest.mark.parametrize("hostile", ['"/><graphics type="vnc" listen="0.0.0.0"/><x a="',
                                     "</source><interface type='user'/>", "a&b<c>d'e\"f"])
def test_hostile_values_are_escaped_not_injected(hostile):
    s = spec(disk_path="/v/" + hostile, iso_path="/i/" + hostile, network=hostile, bridge="",
             meta=domainxml.VmMeta(owner=PK, labels=[hostile]))
    d = parse(s)                                        # still well-formed
    assert len(d.findall("devices/graphics")) == 1
    assert d.find("devices/graphics").get("listen") == "127.0.0.1"
    assert d.find("devices/disk[@device='disk']/source").get("file") == "/v/" + hostile
    assert d.find("devices/interface/source").get("network") == hostile
    assert domainxml.parse_meta(domainxml.build_domain_xml(s)).labels == [hostile]
