"""Which wire a VM is on, and what a host tells an admin about where its VMs can be attached.

  * host.info (admin) lists the libvirt networks and Linux bridges this host has RIGHT NOW;
  * vm.create / vm.update accept `network: {"type": "network"|"bridge", "name": …}` and put ONLY a name
    from that live list into the definition — an unknown name, or a string shaped like markup, is refused
    before anything is written (a bridge name lands in XML, so "validate the characters" is not enough:
    `br0` is a valid name that may simply not exist here);
  * vm.update retargets the FIRST adapter (keeping its MAC/model) and confirms it by reading back;
  * the views carry `net` (the primary adapter) and, for a RUNNING VM, `ips` and `uptime_s`, and an
    address lookup is cached so the client's 10-second poll does not become a virsh call per VM per poll;
  * the parsers read what a real host (nas.lan, libvirt 12) printed.
"""
from pathlib import Path

import pytest

from app.services.vmhost import backend as B
from app.services.vmhost import domainxml
from tests.test_vmhost_phase2 import ADMIN, U1, U2, USER, c, make

REAL = Path(__file__).parent / "fixtures" / "vmhost_real"


def primary(xml):
    return domainxml.read_hardware(xml)["net"]


def test_host_info_lists_networks_and_bridges_for_admins_only(tmp_path):
    svc, be, _ = make(tmp_path)
    be.networks = ["default", "isolated"]
    be.bridges = ["br0"]
    info = c(svc, ADMIN, "host.info")["result"]
    assert info["networks"] == ["default", "isolated"] and info["bridges"] == ["br0"]
    assert info["default_network"] == "default" and info["bridge"] == ""
    assert "bridges" not in c(svc, USER, "host.info")["result"]


def test_a_backend_without_the_calls_still_answers(tmp_path):
    svc, be, _ = make(tmp_path)

    class Bare:                    # an older backend (or a test fake) with no networking calls at all
        def __getattr__(self, n):
            if n in ("list_networks", "list_bridges", "guest_addresses", "uptimes"):
                raise AttributeError(n)
            return getattr(be, n)
    svc.backend = Bare()
    info = c(svc, ADMIN, "host.info")["result"]
    assert info["networks"] == [] and info["bridges"] == []
    assert c(svc, ADMIN, "vm.list")["ok"]


def test_create_on_a_bridge_writes_a_bridge_interface(tmp_path):
    svc, be, _ = make(tmp_path)
    be.bridges = ["br0"]
    res = c(svc, ADMIN, "vm.create", {"name": "web", "network": {"type": "bridge", "name": "br0"}})
    assert res["ok"], res
    u = res["result"]["vm"]["uuid"]
    assert primary(be.domains[u]["xml"]) == {"type": "bridge", "name": "br0"}
    assert res["result"]["vm"]["net"] == {"type": "bridge", "name": "br0"}


def test_create_without_a_choice_uses_the_host_default(tmp_path):
    svc, be, _ = make(tmp_path)
    res = c(svc, ADMIN, "vm.create", {"name": "web"})
    assert primary(be.domains[res["result"]["vm"]["uuid"]]["xml"]) == {"type": "network", "name": "default"}


@pytest.mark.parametrize("net", [
    {"type": "bridge", "name": "br9"},                         # a valid name this host does not have
    {"type": "network", "name": "nosuch"},
    {"type": "bridge", "name": 'br0"/><hostdev'},              # markup
    {"type": "network", "name": "default'><x"},
    {"type": "direct", "name": "enp1s0"},                      # macvtap/ethernet is not offered
    {"type": "bridge"},
    "br0",
])
def test_create_refuses_a_network_the_host_does_not_have(tmp_path, net):
    svc, be, _ = make(tmp_path)
    be.bridges = ["br0"]
    be.networks = ["default", 'default\'><x']                  # even a hostile name in the live list is refused
    before = dict(be.domains)
    res = c(svc, ADMIN, "vm.create", {"name": "web", "network": net})
    assert not res["ok"] and res["error"]["code"] == "bad_request", res
    assert be.domains == before
    assert not any(x[0] == "img_create" for x in be.calls), "refused before any disk was made"


def test_update_moves_the_first_adapter_and_keeps_its_mac(tmp_path):
    svc, be, _ = make(tmp_path)
    be.bridges = ["br0"]
    x = be.domains[U1]["xml"].replace('<interface type="network">',
                                      '<interface type="network"><mac address="52:54:00:aa:bb:cc"/>')
    be.domains[U1]["xml"] = x
    res = c(svc, ADMIN, "vm.update", {"vm": U1, "network": {"type": "bridge", "name": "br0"}})
    assert res["ok"], res
    root = domainxml.parse_domain(be.domains[U1]["xml"])
    ifs = root.findall("devices/interface")
    assert len(ifs) == 1 and ifs[0].get("type") == "bridge"
    assert ifs[0].find("source").attrib == {"bridge": "br0"}
    assert ifs[0].find("mac").get("address") == "52:54:00:aa:bb:cc"
    assert res["result"]["vm"]["hardware"]["net"] == {"type": "bridge", "name": "br0"}
    back = c(svc, ADMIN, "vm.update", {"vm": U1, "network": {"type": "network", "name": "default"}}, "2")
    assert back["ok"] and primary(be.domains[U1]["xml"]) == {"type": "network", "name": "default"}


def test_update_refuses_an_unknown_bridge_before_writing(tmp_path):
    svc, be, _ = make(tmp_path)
    x = be.domains[U1]["xml"]
    res = c(svc, ADMIN, "vm.update", {"vm": U1, "network": {"type": "bridge", "name": "br0"}})
    assert not res["ok"] and res["error"]["code"] == "bad_request"
    assert be.domains[U1]["xml"] == x


def test_add_nic_follows_the_network_chosen_in_the_same_save(tmp_path):
    svc, be, _ = make(tmp_path)
    be.bridges = ["br0"]
    assert c(svc, ADMIN, "vm.update", {"vm": U1, "network": {"type": "bridge", "name": "br0"}, "add_nic": True})["ok"]
    ifs = domainxml.parse_domain(be.domains[U1]["xml"]).findall("devices/interface")
    assert [i.get("type") for i in ifs] == ["bridge", "bridge"]


def test_set_primary_nic_adds_one_to_a_vm_without_any():
    root = domainxml.parse_domain('<domain><name>x</name><devices></devices></domain>')
    domainxml.set_primary_nic(root, "", "br0", False)
    assert domainxml.read_hardware(domainxml.to_text(root))["net"] == {"type": "bridge", "name": "br0"}


def test_views_carry_the_wire_and_for_running_vms_address_and_uptime(tmp_path):
    svc, be, _ = make(tmp_path)
    be.addrs = {U2: ["192.168.122.32"], U1: ["10.9.9.9"]}
    be.ups = {U2: 3725, U1: 5}
    vms = {v["uuid"]: v for v in c(svc, ADMIN, "vm.list")["result"]["vms"]}
    assert vms[U2]["net"] == {"type": "network", "name": "default"}
    assert vms[U2]["ips"] == ["192.168.122.32"] and vms[U2]["uptime_s"] == 3725
    assert "ips" not in vms[U1] and "uptime_s" not in vms[U1], "a shut-off VM has no address or uptime"
    got = c(svc, USER, "vm.get", {"vm": U2})["result"]["vm"]
    assert got["ips"] == ["192.168.122.32"]


def test_the_address_lookup_is_cached_across_polls(tmp_path):
    svc, be, _ = make(tmp_path)
    be.addrs = {U2: ["192.168.122.32"]}
    for i in range(4):
        c(svc, ADMIN, "vm.list", rid=f"p{i}")
    assert sum(1 for x in be.calls if x[0] == "guest_addresses") == 1


def test_a_failing_address_or_uptime_read_is_absent_not_an_error(tmp_path):
    svc, be, _ = make(tmp_path)
    be.fail["guest_addresses"] = B.BackendError("boom")
    be.fail["uptimes"] = B.BackendError("boom")
    res = c(svc, ADMIN, "vm.list")
    assert res["ok"]
    v = {v["uuid"]: v for v in res["result"]["vms"]}[U2]
    assert v["ips"] == [] and "uptime_s" not in v


# ------------------------------------------------------------------ parsers, fed a real host's output
def test_domiflist_as_a_real_host_prints_it():
    nics = B.parse_domiflist((REAL / "virsh-domiflist-inactive.out").read_text())
    assert nics == [{"type": "network", "source": "default", "model": "virtio", "mac": "52:54:00:22:b0:28"}]


@pytest.mark.parametrize("name", ["virsh-domifaddr-lease", "virsh-domifaddr-arp"])
def test_domifaddr_as_a_real_host_prints_it(name):
    assert B.parse_domifaddr((REAL / f"{name}.out").read_text()) == ["192.168.122.32"]


def test_net_list_as_a_real_host_prints_it():
    assert B.parse_name_list((REAL / "virsh-net-list-names.out").read_text()) == ["default"]


def test_host_bridges_reads_sysfs(tmp_path):
    for n, br in (("br0", True), ("enp1s0", False), ("virbr0", True), ("bad name", True)):
        (tmp_path / n).mkdir()
        if br:
            (tmp_path / n / "bridge").mkdir()
    assert B.host_bridges(str(tmp_path)) == ["br0", "virbr0"]


def test_qemu_uptimes_reads_proc(tmp_path):
    (tmp_path / "stat").write_text("cpu 1 2 3\nbtime 1000\n")
    u = "553cd647-f495-4f7e-a0c1-73a1f83abec4"

    def proc(pid, argv, start_ticks):
        d = tmp_path / str(pid)
        d.mkdir()
        (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
        # comm may contain spaces and parens; field 22 counts from the LAST ')'
        rest = ["S"] + ["0"] * 18 + [str(start_ticks)] + ["0"] * 5
        (d / "stat").write_text(f"{pid} (qemu (x) y) " + " ".join(rest) + "\n")
    proc(10, ["/usr/bin/qemu-system-x86_64", "-name", "guest=a", "-uuid", u], 100 * 20)
    proc(11, ["/bin/bash", "-uuid", "00000000-0000-4000-8000-000000000000"], 0)
    got = B.qemu_uptimes(str(tmp_path), now=1000 + 20 + 3600)
    import os
    hz = os.sysconf("SC_CLK_TCK")
    assert got == {u: int(1000 + 20 + 3600 - (1000 + 2000 / hz))}
    assert B.qemu_uptimes(str(tmp_path / "nope")) == {}


def test_virsh_backend_reads_nics_with_domiflist():
    import asyncio
    seen = []

    async def runner(argv, timeout, stdin=None):
        seen.append(argv[3])
        if argv[3] == "dominfo":
            return 0, (REAL / "virsh-dominfo-running.out").read_text(), ""
        if argv[3] == "domiflist":
            return 0, (REAL / "virsh-domiflist-inactive.out").read_text(), ""
        return 1, "", "no"
    be = B.VirshBackend(runner=runner)
    d = asyncio.run(be.get("00000000-0000-4000-8000-000000000002"))
    assert "domiflist" in seen and d.nics[0]["source"] == "default"
