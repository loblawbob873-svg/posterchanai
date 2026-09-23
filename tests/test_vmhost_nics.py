"""A VM can be given more network adapters, each on its OWN network, and have one taken away.

Needed for the PosterChan Router test rig (a router VM with its WAN card on the LAN bridge and a second
card on an isolated test network): "make sure our VMs can add virtual NICs". The feature existed but an
added adapter FOLLOWED `network`, which also re-points the first adapter -- so the router shape could not
be built -- and there was no way to remove one. `nic_network` puts the new card on its own network and
leaves the first alone; `remove_nic` takes the adapter's MAC (the only stable name an adapter has).
Every change is still admin-only, shut-off-only, and confirmed by reading the definition back.

Runs the shipped service against tests/vmhost_fake.py, which assigns MACs on define as libvirt does.
"""
from app.services.vmhost import domainxml
import itertools

from tests.test_vmhost_phase2 import ADMIN, U1, USER, c as _c, make

_ids = itertools.count(1)


def c(svc, who, op, args=None):
    """A fresh request id per call: the op journal refuses a REUSED id with different arguments."""
    return _c(svc, who, op, args, rid="nic%d" % next(_ids))


def _adapters(be, vm):
    return domainxml.read_hardware(be.domains[vm]["xml"])["adapters"]


def _rig(tmp_path):
    svc, be, _ = make(tmp_path)
    be.bridges = ["br0"]
    be.networks = ["default", "pc-testlan"]
    return svc, be


def test_a_second_adapter_goes_on_its_own_network_and_the_first_stays(tmp_path):
    svc, be = _rig(tmp_path)
    assert c(svc, ADMIN, "vm.update", {"vm": U1, "network": {"type": "bridge", "name": "br0"}})["ok"]
    r = c(svc, ADMIN, "vm.update", {"vm": U1, "add_nic": True,
                                     "nic_network": {"type": "network", "name": "pc-testlan"}})
    assert r["ok"], r
    got = [(a["type"], a["name"]) for a in _adapters(be, U1)]
    assert got == [("bridge", "br0"), ("network", "pc-testlan")], got
    # …and the settings view lists each card with its MAC, which is what Remove names.
    hw = r["result"]["vm"]["hardware"]
    assert [a["name"] for a in hw["adapters"]] == ["br0", "pc-testlan"]
    assert all(domainxml.MAC_RE.match(a["mac"]) for a in hw["adapters"]), hw["adapters"]


def test_an_adapter_is_removed_by_its_mac_and_only_that_one(tmp_path):
    svc, be = _rig(tmp_path)
    assert c(svc, ADMIN, "vm.update", {"vm": U1, "add_nic": True,
                                       "nic_network": {"type": "network", "name": "pc-testlan"}})["ok"]
    before = _adapters(be, U1)
    gone = before[1]["mac"]
    r = c(svc, ADMIN, "vm.update", {"vm": U1, "remove_nic": gone.upper()})
    assert r["ok"], r
    after = _adapters(be, U1)
    assert [a["mac"] for a in after] == [before[0]["mac"]], after


def test_refusals_change_nothing(tmp_path):
    svc, be = _rig(tmp_path)
    before = _adapters(be, U1)
    cases = [
        ({"remove_nic": "52:54:00:de:ad:00"}, "backend_error"),          # no such adapter
        ({"remove_nic": "not-a-mac"}, "bad_request"),
        ({"nic_network": {"type": "network", "name": "pc-testlan"}}, "bad_request"),   # without add_nic
        ({"add_nic": True, "nic_network": {"type": "network", "name": "made-up"}}, "bad_request"),
        ({"add_nic": True, "nic_network": {"type": "bridge", "name": "evil'><x"}}, "bad_request"),
    ]
    for extra, code in cases:
        r = c(svc, ADMIN, "vm.update", dict({"vm": U1}, **extra))
        assert not r["ok"] and r["error"]["code"] == code, (extra, r)
    assert _adapters(be, U1) == before


def test_only_an_admin_changes_adapters(tmp_path):
    svc, be = _rig(tmp_path)
    before = _adapters(be, U1)
    r = c(svc, USER, "vm.update", {"vm": U1, "add_nic": True,
                                    "nic_network": {"type": "network", "name": "pc-testlan"}})
    assert not r["ok"], r
    assert _adapters(be, U1) == before


def test_a_running_vm_is_refused(tmp_path):
    svc, be = _rig(tmp_path)
    be.domains[U1]["state"] = "running"
    r = c(svc, ADMIN, "vm.update", {"vm": U1, "add_nic": True,
                                     "nic_network": {"type": "network", "name": "pc-testlan"}})
    assert not r["ok"], r
