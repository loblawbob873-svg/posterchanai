"""The VNC display is password-protected for real, and only ever on loopback — checked against the
TEXT libvirt and QEMU actually print, fed through `VirshBackend`'s subprocess runner seam (there is no
libvirt on the test machine, and a fake backend would agree with whatever the code assumed).

WHY THIS FILE EXISTS. The first version set the console password with
`virsh qemu-monitor-command --hmp "set_password vnc …"` on a display defined WITHOUT `passwd`. QEMU
starts such a display with auth=none and refuses `set_password` — and HMP reports that refusal as
PRINTED TEXT with exit status 0, which the backend read as success. Every console then connected with
no password at all, while the ticket reply still carried a password that protected nothing.

Output formats relied on, verbatim:
  * QMP replies (QEMU docs/interop/qmp-spec.rst, "Success" / "Error" responses):
        {"return": json-value, "id": json-value}
        {"error": {"class": json-string, "desc": json-string}, "id": json-value}
    `virsh qemu-monitor-command DOMAIN '<json>'` (virsh(1), "qemu-monitor-command") passes the command
    through and PRINTS THE REPLY, exiting 0 even when the reply is an error; libvirt stamps its own
    `"id":"libvirt-N"`.
  * QEMU's refusal for a display without password auth (ui/vnc.c `vnc_display_password` → -EINVAL →
    qmp_set_password "Could not set password"); HMP prints the same desc as plain text.
  * `virsh vncdisplay` (tools/virsh-domain.c cmdVNCDisplay): `127.0.0.1:0` for a loopback listen, and
    just `:0` when the listen address is `0.0.0.0` or `::` — i.e. an EMPTY host means "every address".
  * `virsh dumpxml` of a running domain (formatdomain "Graphical framebuffers"):
        <graphics type='vnc' port='5900' autoport='yes' listen='127.0.0.1'>
          <listen type='address' address='127.0.0.1'/>
        </graphics>
"""
import asyncio
import json
import xml.etree.ElementTree as ET

import pytest

from app.services.vmhost import backend as b
from app.services.vmhost import domainxml
from app.services.vmhost.config import VmHostConfig
from app.services.vmhost.service import VmHostService
from app.services.vmhost.storage import Storage
from tests.vmhost_fake import FakeBackend

U1 = "11111111-1111-4111-8111-111111111111"
ADMIN = "1b" * 32
USER = "3d" * 32

QMP_OK = '{"return":{},"id":"libvirt-412"}\n'
QMP_REFUSED = '{"id":"libvirt-413","error":{"class":"GenericError","desc":"Could not set password"}}\n'
HMP_REFUSED = "Could not set password\n"


def dumpxml(listen="127.0.0.1", port="5900", listen_el=None):
    el = listen_el if listen_el is not None else f"<listen type='address' address='{listen}'/>"
    attr = f" listen='{listen}'" if listen is not None else ""
    return (f"<domain type='kvm' id='3'><name>web1</name><uuid>{U1}</uuid><devices>"
            f"<graphics type='vnc' port='{port}' autoport='yes'{attr}>{el}</graphics>"
            f"<video><model type='virtio' heads='1' primary='yes'/></video></devices></domain>\n")


class Runner:
    """Answers `virsh` argv the way the real binary does, recording every call."""

    def __init__(self, monitor=QMP_OK, xml=None, vncdisplay="127.0.0.1:0\n\n"):
        self.monitor, self.xml, self.vncdisplay = monitor, xml or dumpxml(), vncdisplay
        self.calls = []

    async def __call__(self, argv, timeout, stdin=None):
        self.calls.append(argv)
        verb = argv[3]
        if verb == "qemu-monitor-command":
            out = self.monitor(argv) if callable(self.monitor) else self.monitor
            return 0, out, ""                       # exit 0 even for an error reply — that is the trap
        if verb == "dumpxml":
            return 0, self.xml, ""
        if verb == "vncdisplay":
            return 0, self.vncdisplay, ""
        return 0, "", ""


def run(c):
    return asyncio.run(c)


# ------------------------------------------------------------------------------------ H1: the password
def test_every_generated_domain_requires_a_vnc_password_nobody_holds():
    spec = domainxml.DomainSpec(name="a", uuid=U1, guest="linux", firmware="efi", vcpus=1, ram_mib=512,
                                disk_path="/d", nvram_path="/n")
    g1 = ET.fromstring(domainxml.build_domain_xml(spec)).find("devices/graphics")
    g2 = ET.fromstring(domainxml.build_domain_xml(spec)).find("devices/graphics")
    pw = g1.get("passwd")
    assert pw and 1 <= len(pw) <= 8, "without passwd QEMU starts VNC with NO authentication"
    assert pw != g2.get("passwd"), "a fixed password would be a password everybody holds"
    assert g1.get("passwdValidTo") and g1.get("passwdValidTo") < "2000", \
        "the define-time password must already be expired: nobody may use it until a ticket rotates it"


def test_the_password_is_set_over_qmp_and_the_reply_is_read():
    r = Runner()
    run(b.VirshBackend(runner=r).set_vnc_password(U1, "abcd1234", 60))
    mon = [a for a in r.calls if a[3] == "qemu-monitor-command"]
    assert all("--hmp" not in a for a in mon), "HMP errors are printed with exit 0 — unreadable as failure"
    cmds = [json.loads(a[-1]) for a in mon]
    assert cmds[0] == {"execute": "set_password", "arguments": {"protocol": "vnc", "password": "abcd1234"}}
    assert cmds[1] == {"execute": "expire_password", "arguments": {"protocol": "vnc", "time": "+60"}}
    assert all(a[4] == U1 for a in mon)


@pytest.mark.parametrize("label,reply", [
    ("QMP error reply (display defined without passwd)", QMP_REFUSED),
    ("HMP-style plain text", HMP_REFUSED),
    ("empty output", ""),
    ("not an object", '"ok"\n'),
])
def test_a_refused_or_unreadable_monitor_reply_is_a_failure(label, reply):
    r = Runner(monitor=reply)
    with pytest.raises(b.BackendError):
        run(b.VirshBackend(runner=r).set_vnc_password(U1, "abcd1234", 60))


def test_a_refused_expiry_is_a_failure_too():
    r = Runner(monitor=lambda argv: QMP_REFUSED if "expire_password" in argv[-1] else QMP_OK)
    with pytest.raises(b.BackendError):
        run(b.VirshBackend(runner=r).set_vnc_password(U1, "abcd1234", 60))


def host(tmp_path, be):
    root = tmp_path / "vms"
    st = Storage(root)
    st.ensure()
    (root / U1).mkdir()
    cfg = VmHostConfig(enabled=True, storage_dir=str(root), admin_pubkeys=[ADMIN])

    async def admins():
        return set()
    return VmHostService(cfg, be, node_pubkey="0a" * 32, admin_provider=admins, storage=st)


class VirshOverFake(FakeBackend):
    """The fake hypervisor for domain state, the REAL virsh password/endpoint code for the display."""

    def __init__(self, runner):
        super().__init__()
        self.virsh = b.VirshBackend(runner=runner)

    async def set_vnc_password(self, vm_uuid, password, expire_s):
        await self.virsh.set_vnc_password(vm_uuid, password, expire_s)

    async def vnc_endpoint(self, vm_uuid):
        return await self.virsh.vnc_endpoint(vm_uuid)


def test_a_vm_whose_display_refuses_a_password_gets_no_console_ticket(tmp_path):
    """A VM defined before passwd was generated (or by hand, without it) cannot be given a password,
    so it is REFUSED a console rather than handed an unauthenticated one."""
    be = VirshOverFake(Runner(monitor=QMP_REFUSED))
    be.add_domain(U1, "old", state="running", meta=domainxml.VmMeta(owner=ADMIN, assigned=[USER]))
    svc = host(tmp_path, be)
    run(svc.refresh_index())
    res = run(svc.handle(USER, "console.ticket", {"vm": U1}, "t1"))
    assert res["ok"] is False and res["error"]["code"] == "backend_error", res
    assert "ticket" not in json.dumps(res.get("result", {}))
    assert svc.consoles._tickets == {}, "no ticket may exist for a console without authentication"


def test_a_working_rotation_issues_the_ticket(tmp_path):
    be = VirshOverFake(Runner())
    be.add_domain(U1, "new", state="running", meta=domainxml.VmMeta(owner=ADMIN, assigned=[USER]))
    svc = host(tmp_path, be)
    run(svc.refresh_index())
    res = run(svc.handle(USER, "console.ticket", {"vm": U1}, "t1"))
    assert res["ok"] is True and len(svc.consoles._tickets) == 1


# ------------------------------------------------------------------------------------ M2: loopback only
@pytest.mark.parametrize("text,host,port,loop", [
    (":0\n\n", "", 5900, False),            # virsh prints this for listen 0.0.0.0 AND ::
    ("127.0.0.1:0\n\n", "127.0.0.1", 5900, True),
    ("[::]:0\n", "::", 5900, False),
    ("0.0.0.0:0\n", "0.0.0.0", 5900, False),
    ("[::1]:2\n", "::1", 5902, True),
])
def test_vncdisplay_output_and_what_counts_as_loopback(text, host, port, loop):
    assert b.parse_vncdisplay(text) == (host, port)
    assert b.is_loopback(host) is loop


@pytest.mark.parametrize("xml,expect", [
    (dumpxml("127.0.0.1", "5903"), ("127.0.0.1", 5903)),
    (dumpxml("0.0.0.0", "5903"), None),
    (dumpxml("::", "5903"), None),
    (dumpxml(None, "5903", listen_el="<listen type='socket' socket='/run/vnc.sock'/>"), None),
    (dumpxml(None, "5903", listen_el="<listen type='none'/>"), None),
    (dumpxml("127.0.0.1", "-1"), None),       # not running: no port allocated
])
def test_the_endpoint_comes_from_the_domains_own_listen_address(xml, expect):
    r = Runner(xml=xml, vncdisplay=":3\n")
    assert run(b.VirshBackend(runner=r).vnc_endpoint(U1)) == expect


def test_a_display_on_every_address_is_refused_a_console(tmp_path):
    be = VirshOverFake(Runner(xml=dumpxml("0.0.0.0", "5900"), vncdisplay=":0\n"))
    be.add_domain(U1, "pub", state="running", meta=domainxml.VmMeta(owner=ADMIN, assigned=[USER]))
    svc = host(tmp_path, be)
    run(svc.refresh_index())
    res = run(svc.handle(USER, "console.ticket", {"vm": U1}, "t1"))
    assert res["ok"] is False and res["error"]["code"] == "unsupported"
    assert svc.consoles._tickets == {}


# ------------------------------------------------------------------------------------ L2: shared VMs
def shared_host(tmp_path, clock):
    root = tmp_path / "vms"
    st = Storage(root)
    st.ensure()
    (root / U1).mkdir()
    cfg = VmHostConfig(enabled=True, storage_dir=str(root), admin_pubkeys=[ADMIN], ticket_ttl_sec=60)
    be = FakeBackend()
    be.add_domain(U1, "shared", state="running", meta=domainxml.VmMeta(owner=ADMIN, assigned=[USER, OTHER]))

    async def admins():
        return set()
    svc = VmHostService(cfg, be, node_pubkey="0a" * 32, admin_provider=admins, storage=st,
                        now=lambda: clock[0])
    run(svc.refresh_index())
    return svc, be


OTHER = "4e" * 32


def test_two_people_sharing_a_vm_do_not_rotate_each_others_password(tmp_path):
    """User A is issued a ticket and is still logging in when user B asks for one. Rotating the password
    for B would make A's noVNC fail authentication with the password its own ticket carried."""
    clock = [1000.0]
    svc, be = shared_host(tmp_path, clock)
    a = run(svc.handle(USER, "console.ticket", {"vm": U1}, "a"))["result"]
    clock[0] += 20
    b = run(svc.handle(OTHER, "console.ticket", {"vm": U1}, "b"))["result"]
    assert a["vnc_password"] == b["vnc_password"], "B's ticket rotated the password out from under A"
    sets = [c for c in be.calls if c[0] == "set_vnc_password"]
    assert len(sets) == 2 and sets[-1][2] == 60, "the reuse must still EXTEND the expiry to B's ticket"
    clock[0] += 61                              # every ticket for the VM has expired
    c = run(svc.handle(OTHER, "console.ticket", {"vm": U1}, "c"))["result"]
    assert c["vnc_password"] != b["vnc_password"], "with no live ticket the password rotates again"


def test_a_revocation_forces_a_fresh_password(tmp_path):
    clock = [1000.0]
    svc, be = shared_host(tmp_path, clock)
    a = run(svc.handle(USER, "console.ticket", {"vm": U1}, "a"))["result"]
    run(svc.handle(ADMIN, "vm.unassign", {"vm": U1, "pubkey": USER}, "u"))
    b = run(svc.handle(OTHER, "console.ticket", {"vm": U1}, "b"))["result"]
    assert a["vnc_password"] != b["vnc_password"], "an unassigned user must not keep a valid password"
