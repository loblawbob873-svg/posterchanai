#!/usr/bin/env python3
"""Live probe of the VM host against REAL libvirt/QEMU on this machine.

Runs the shipped `VirshBackend` and `VmHostService` directly (no Nostr), plus the real `/ws/vmconsole` route
served by an in-process uvicorn on loopback, and asserts what the unit tests can only assume: what virsh,
qemu-img and QMP really print, whether QEMU really demands the console password, whether a VM the service
defines really starts as the libvirt `qemu` user, and whether a cold-migration rebuild really boots.

    python scripts/vmhost_live_probe.py --storage /var/lib/posterchan/vms-probe \
        [--capture tests/fixtures/vmhost_real] [--steps 1,2,3]

SAFETY — this runs on hosts that have other VMs on them:
  * every VM it makes is named `pcprobe-<random>`; every MUTATING virsh verb goes through one choke point
    (`GuardedRunner`) that refuses a call naming no uuid this probe created, and refuses to define a domain
    that is not named pcprobe-* or whose uuid already belongs to another domain;
  * qemu-img may only create/change files under --storage;
  * everything it creates (domains, disks, nvram, snapshots, ISOs, state) is removed in a `finally`, then the
    probe checks libvirt and the storage directory for leftovers.

--storage must be a probe-ONLY directory (not the host's real VM storage), owned by the user running this and
traversable by libvirt's qemu user (0751). Exit status: 0 all passed, 1 a check failed, 2 could not run.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import secrets
import shutil
import struct
import sys
import time
import traceback
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app.services.vmhost import backend as B  # noqa: E402
from app.services.vmhost import domainxml  # noqa: E402
from app.services.vmhost.config import VmHostConfig  # noqa: E402
from app.services.vmhost.service import VmHostService  # noqa: E402
from app.services.vmhost.storage import Storage  # noqa: E402
import vmhost_rfb as rfb  # noqa: E402

ADMIN = "ad" * 32
NODE = "0e" * 32
READ_ONLY_VIRSH = {"version", "nodeinfo", "nodememstats", "list", "dominfo", "domblklist", "dumpxml", "vncdisplay",
                   "snapshot-list", "snapshot-current", "snapshot-dumpxml", "domstate", "capabilities",
                   "domcapabilities", "net-list", "net-info", "uri", "domiflist", "domifaddr"}
PLACEHOLDER_PW = "PCPROBEX"


class ProbeRefused(Exception):
    pass


# ------------------------------------------------------------------------------------------------ the choke point
class GuardedRunner:
    """Wraps backend._run: records every call (for the fixtures) and refuses anything that could touch a VM or a
    file this probe did not create."""

    def __init__(self, storage_root: Path):
        self.storage_root = storage_root.resolve()
        self.mine: set = set()            # uuids created by this probe
        self.names: set = set()
        self.foreign: set = set()         # uuids that existed before the probe started
        self.calls: list = []

    def _check(self, argv: list) -> None:
        tool = os.path.basename(argv[0])
        if tool == "virsh":
            verb = argv[3] if len(argv) > 3 else ""
            if verb in READ_ONLY_VIRSH or (verb == "metadata" and "--set" not in argv):
                return
            if verb == "qemu-monitor-command" or verb in ("start", "shutdown", "destroy", "reboot", "undefine",
                                                           "autostart", "metadata", "snapshot-create-as",
                                                           "snapshot-create", "snapshot-revert", "snapshot-delete",
                                                           "attach-device", "detach-device"):
                if len(argv) > 4 and (argv[4] in self.mine or argv[4] in self.names):
                    return
                raise ProbeRefused(f"refusing virsh {verb} on {argv[4] if len(argv) > 4 else '?'}: not a probe VM")
            if verb == "define":
                text = Path(argv[4]).read_text()
                name = (re.search(r"<name>([^<]+)</name>", text) or [None, ""])[1]
                u = (re.search(r"<uuid>([^<]+)</uuid>", text) or [None, ""])[1].lower()
                if not name.startswith("pcprobe-"):
                    raise ProbeRefused(f"refusing to define {name!r}: not a probe name")
                if u in self.foreign:
                    raise ProbeRefused(f"refusing to define over an existing domain {u}")
                self.mine.add(u)
                self.names.add(name)
                return
            raise ProbeRefused(f"virsh {verb} is not on the probe's list")
        if tool in ("qemu-img", "qemu-io"):
            verb = argv[1] if len(argv) > 1 else ""
            if verb == "info":
                return
            paths = [a for a in argv[2:] if a.startswith("/")]
            for p in paths:
                rp = Path(p).resolve()
                if not rp.is_relative_to(self.storage_root):
                    raise ProbeRefused(f"refusing {tool} {verb} outside the probe storage: {p}")
            return
        raise ProbeRefused(f"{tool} is not on the probe's list")

    async def __call__(self, argv, timeout, stdin=None):
        argv = list(argv)
        self._check(argv)
        code, out, err = await B._run(argv, timeout, stdin)
        self.calls.append({"argv": argv, "rc": code, "out": out, "err": err})
        return code, out, err

    def last(self, pred) -> dict:
        for c in reversed(self.calls):
            if pred(c["argv"]):
                return c
        raise KeyError("no such captured call")


def virsh_verb(verb, *must):
    return lambda a: os.path.basename(a[0]) == "virsh" and len(a) > 3 and a[3] == verb and all(m in a for m in must)


# ------------------------------------------------------------------------------------------------ fixtures
class Fixtures:
    """Real outputs, saved for tests/test_vmhost_real_captures.py. Probe uuids become stable fake ones and VNC
    passwords a placeholder; nothing else is changed."""

    def __init__(self, directory):
        self.dir = Path(directory) if directory else None
        self.uuids: dict = {}
        self.passwords: set = set()
        self.index: dict = {}

    def _scrub(self, s: str) -> str:
        for real, fake in self.uuids.items():
            s = s.replace(real, fake)
        for pw in self.passwords:
            s = s.replace(pw, PLACEHOLDER_PW)
        s = re.sub(r"passwd=(['\"])[^'\"]*\1", lambda m: f"passwd={m.group(1)}{PLACEHOLDER_PW}{m.group(1)}", s)
        return s

    def alias(self, real_uuid: str) -> None:
        if real_uuid and real_uuid not in self.uuids:
            n = len(self.uuids) + 1
            self.uuids[real_uuid] = f"00000000-0000-4000-8000-{n:012d}"

    def save(self, name: str, call: dict, note: str = "") -> None:
        if self.dir is None:
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / f"{name}.out").write_text(self._scrub(call["out"]))
        self.index[name] = {"argv": [self._scrub(a) for a in call["argv"][3:] if os.path.basename(call["argv"][0]) == "virsh"]
                            or [self._scrub(a) for a in call["argv"][1:]],
                            "tool": os.path.basename(call["argv"][0]), "rc": call["rc"],
                            "stderr": self._scrub(call["err"]).strip()[:2000], "note": note}

    def save_text(self, name: str, text: str, note: str = "") -> None:
        if self.dir is None:
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / f"{name}.out").write_text(self._scrub(text))
        self.index[name] = {"argv": [], "tool": "probe", "rc": 0, "stderr": "", "note": note}

    def finish(self, meta: dict) -> None:
        if self.dir is None:
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "index.json").write_text(json.dumps({"host": meta, "captures": self.index}, indent=1, sort_keys=True))


# ------------------------------------------------------------------------------------------------ helpers
def make_iso(path: Path, volume_id: str = "PCPROBE") -> str:
    """A minimal, valid ISO 9660 image (an empty root directory). Uses xorriso/genisoimage when the host has one,
    and writes the handful of structures by hand when it does not — libvirt and QEMU only need a readable CD."""
    for tool, argv in (("xorriso", ["xorriso", "-as", "mkisofs", "-quiet", "-V", volume_id, "-o", str(path)]),
                       ("genisoimage", ["genisoimage", "-quiet", "-V", volume_id, "-o", str(path)]),
                       ("mkisofs", ["mkisofs", "-quiet", "-V", volume_id, "-o", str(path)])):
        if shutil.which(tool):
            import subprocess
            empty = path.parent / ".iso-src"
            empty.mkdir(exist_ok=True)
            try:
                subprocess.run(argv + [str(empty)], check=True, capture_output=True, timeout=60)
                return tool
            finally:
                shutil.rmtree(empty, True)
    S = 2048

    def both32(n):
        return struct.pack("<I", n) + struct.pack(">I", n)

    def both16(n):
        return struct.pack("<H", n) + struct.pack(">H", n)

    def dirrec(ident: bytes) -> bytes:
        body = (both32(20) + both32(S) + bytes(7) + bytes([2, 0, 0]) + both16(1) + bytes([len(ident)]) + ident)
        rec = bytes([33 + len(ident), 0]) + body
        return rec + (b"\0" if len(rec) % 2 else b"")
    pvd = bytearray(S)
    pvd[0:8] = b"\x01CD001\x01\x00"
    pvd[8:40] = b" " * 32
    pvd[40:72] = volume_id.encode().ljust(32)
    pvd[80:88] = both32(21)
    pvd[120:124] = both16(1)
    pvd[124:128] = both16(1)
    pvd[128:132] = both16(S)
    pvd[132:140] = both32(10)
    pvd[140:144] = struct.pack("<I", 18)
    pvd[148:152] = struct.pack(">I", 19)
    pvd[156:190] = dirrec(b"\0")
    pvd[190:813] = b" " * 623
    for off in (813, 830, 847, 864):
        pvd[off:off + 17] = b"0" * 16 + b"\0"
    pvd[881] = 1
    term = bytearray(S)
    term[0:7] = b"\xffCD001\x01"
    lpt, mpt = bytearray(S), bytearray(S)
    lpt[0:10] = bytes([1, 0]) + struct.pack("<I", 20) + struct.pack("<H", 1) + b"\0\0"
    mpt[0:10] = bytes([1, 0]) + struct.pack(">I", 20) + struct.pack(">H", 1) + b"\0\0"
    root = bytearray(S)
    r = dirrec(b"\0") + dirrec(b"\1")
    root[0:len(r)] = r
    path.write_bytes(bytes(16 * S) + pvd + term + lpt + mpt + root)
    return "python"


class Report:
    def __init__(self):
        self.rows: list = []
        self.findings: list = []

    def add(self, step, status, detail=""):
        self.rows.append((step, status, detail))
        print(f"[{status:5}] {step}: {detail}", flush=True)

    def finding(self, text):
        self.findings.append(text)
        print(f"[FIND ] {text}", flush=True)


class CheckFailed(AssertionError):
    pass


def check(cond, msg):
    if not cond:
        raise CheckFailed(msg)


# ------------------------------------------------------------------------------------------------ the probe
class Probe:
    def __init__(self, storage: Path, capture, report: Report):
        self.root = storage
        self.runner = GuardedRunner(storage)
        self.fx = Fixtures(capture)
        self.r = report
        self.be = B.VirshBackend("qemu:///system", runner=self.runner)
        self.cfg = VmHostConfig(enabled=True, storage_dir=str(storage), admin_pubkeys=[ADMIN], reserve_ram_mib=1024,
                                reserve_disk_gib=5, max_vcpus=4, max_ram_mib=4096, max_disk_gib=8, ticket_ttl_sec=60)
        self.storage = Storage(storage)
        self.svc = VmHostService(self.cfg, self.be, node_pubkey=NODE, admin_provider=lambda: set(),
                                 storage=self.storage)
        self.efi = None           # {"uuid","name"}
        self.bios = None
        self.target_root = storage / "target"
        self.target = None
        self.iso = None
        self.created_dirs: list = []
        self.usb = ""             # vendor:product of a real, unused USB device to hot-plug (--usb)

    async def op(self, op, args, svc=None):
        res = await (svc or self.svc).handle(ADMIN, op, args, secrets.token_hex(8))
        if res is None:
            raise CheckFailed(f"{op}: the service said nothing")
        return res

    async def ok(self, op, args, svc=None):
        res = await self.op(op, args, svc)
        check(res.get("ok"), f"{op} failed: {res.get('error')}")
        return res["result"]

    async def state(self, u):
        d = await self.be.get(u)
        return d.state if d else None

    async def wait_state(self, u, want, seconds):
        t = time.monotonic() + seconds
        while time.monotonic() < t:
            if await self.state(u) == want:
                return True
            await asyncio.sleep(0.5)
        return await self.state(u) == want

    # ---- 1
    async def step1_host(self):
        av = await self.be.available()
        check(av["ok"], f"libvirt not reachable: {av['error']}")
        self.fx.save("virsh-version", self.runner.last(virsh_verb("version")))
        st = await self.be.host_stats(str(self.root))
        self.fx.save("virsh-nodeinfo", self.runner.last(virsh_verb("nodeinfo")))
        self.fx.save("virsh-nodememstats", self.runner.last(virsh_verb("nodememstats")))
        check(st["cores"] == os.cpu_count(), f"cores {st['cores']} != os.cpu_count() {os.cpu_count()}")
        memtotal = int(re.search(r"MemTotal:\s+(\d+)", Path("/proc/meminfo").read_text()).group(1)) // 1024
        check(abs(st["ram_total_mib"] - memtotal) <= memtotal * 0.05,
              f"ram_total_mib {st['ram_total_mib']} vs /proc/meminfo {memtotal}")
        check(0 < st["ram_free_mib"] <= st["ram_total_mib"], f"ram_free_mib {st['ram_free_mib']}")
        check(st["disk_total_gib"] > 0, "disk_total_gib is 0")
        doms = await self.be.list_domains()
        for d in doms:
            self.fx.alias(d.uuid)
        self.fx.save("virsh-list-all-uuid", self.runner.last(virsh_verb("list", "--uuid")))
        self.runner.foreign = {d.uuid for d in doms}
        info = await self.ok("host.info", {})
        check(info["libvirt"] and info["kvm"] == os.path.exists("/dev/kvm"), f"host.info {info}")
        self.r.add("1 host", "PASS", f"cores={st['cores']} ram={st['ram_total_mib']}MiB free={st['ram_free_mib']}MiB "
                   f"disk={st['disk_free_gib']}/{st['disk_total_gib']}GiB kvm={av['kvm']} {av['libvirt']!r}; "
                   f"{len(doms)} pre-existing domain(s) left alone")

    # ---- 2
    async def _create(self, firmware):
        name = f"pcprobe-{secrets.token_hex(3)}"
        res = await self.op("vm.create", {"name": name, "firmware": firmware, "guest": "linux", "vcpus": 1,
                                          "ram_mib": 512, "disk_gib": 2, "start": False})
        if res.get("ok"):
            u = res["result"]["vm"]["uuid"]
            self.fx.alias(u)
            self.created_dirs.append(self.root / u)
            return {"uuid": u, "name": name}, res
        return None, res

    async def step2_create(self):
        self.efi, res = await self._create("efi")
        check(self.efi, f"vm.create (efi) failed: {res.get('error')}")
        u = self.efi["uuid"]
        d = await self.be.get(u)
        self.fx.save("virsh-dominfo-shutoff", self.runner.last(virsh_verb("dominfo", u)))
        self.fx.save("virsh-metadata", self.runner.last(virsh_verb("metadata", u)))
        self.fx.save("virsh-domblklist-details-inactive", self.runner.last(virsh_verb("domblklist", u)))
        check(d and d.state == "shutoff" and d.vcpus == 1 and d.ram_mib == 512, f"dominfo {d}")
        check(d.meta and d.meta.owner == ADMIN and d.meta.firmware == "efi" and d.meta.disk_gib == 2, f"meta {d.meta}")
        check([x["target"] for x in d.disks] == ["vda"], f"disks {d.disks}")
        xml = await self.be.dumpxml(u, inactive=True)
        self.fx.save("virsh-dumpxml-inactive-efi", self.runner.last(virsh_verb("dumpxml", u, "--inactive")))
        root = domainxml.parse_domain(xml)
        nv = root.find("os/nvram")
        check(nv is not None and nv.text == str(self.root / u / "nvram.fd"), "nvram path is not in the VM directory")
        vm_dir = self.root / u
        mode = vm_dir.stat().st_mode & 0o777
        nvp = vm_dir / "nvram.fd"
        check(nvp.exists() and os.access(nvp, os.R_OK), "nvram.fd was not seeded as the app user before the first start")
        self.r.add("2 create efi", "PASS", f"{self.efi['name']} nvram format={nv.get('format')!r} "
                   f"template={nv.get('template')!r} dir mode={oct(mode)} "
                   f"disk mode={oct((vm_dir / 'disk-vda.qcow2').stat().st_mode & 0o777)}")
        self.bios, res = await self._create("bios")
        if self.bios is None:
            self.r.add("2 create bios", "FAIL", str(res.get("error")))
        else:
            bx = domainxml.parse_domain(await self.be.dumpxml(self.bios["uuid"], inactive=True))
            self.fx.save("virsh-dumpxml-inactive-bios", self.runner.last(virsh_verb("dumpxml", self.bios["uuid"])))
            check(bx.find("os/nvram") is None and bx.find("os/loader") is None, "a BIOS VM got firmware variables")
            self.r.add("2 create bios", "PASS", self.bios["name"])

    # ---- 3
    async def step3_start(self):
        for vm in [x for x in (self.efi, self.bios) if x]:
            u = vm["uuid"]
            res = await self.op("vm.power", {"vm": u, "action": "start"})
            check(res.get("ok"), f"start {vm['name']} failed: {res.get('error')}")
            check(await self.state(u) == "running", "not running after start")
        u = self.efi["uuid"]
        self.fx.save("virsh-dominfo-running", self.runner.last(virsh_verb("dominfo", u)))
        ep = await self.be.vnc_endpoint(u)
        self.fx.save("virsh-dumpxml-running", self.runner.last(virsh_verb("dumpxml", u)))
        check(ep and ep[0] == "127.0.0.1" and ep[1] >= 5900, f"vnc endpoint {ep}")
        code, out, err = await self.runner(["virsh", "--connect", "qemu:///system", "dumpxml", u, "--security-info"], 15)
        check(code == 0, err)
        g = domainxml.parse_domain(out).find("devices/graphics[@type='vnc']")
        check(g is not None and g.get("passwd"), "the running display has no passwd")
        check(all(li.get("address") == "127.0.0.1" for li in g.findall("listen")), "VNC listens beyond loopback")
        self.fx.passwords.add(g.get("passwd"))
        code, vout, _ = await self.runner(["virsh", "--connect", "qemu:///system", "vncdisplay", u], 10)
        self.fx.save("virsh-vncdisplay", self.runner.calls[-1])
        check(B.parse_vncdisplay(vout) == ep, f"vncdisplay {vout!r} disagrees with dumpxml {ep}")
        self.r.add("3 start", "PASS", f"running; VNC {ep[0]}:{ep[1]} passwd set, passwdValidTo={g.get('passwdValidTo')}")

    # ---- 4
    async def step4_console(self):
        u = self.efi["uuid"]
        host, port = await self.be.vnc_endpoint(u)
        first = await rfb.try_password(host, port, None)
        check(rfb.SEC_VNC in first["types"] and rfb.SEC_NONE not in first["types"],
              f"(a) security types offered: {first['types']}")
        guess = await rfb.try_password(host, port, domainxml.random_vnc_password())
        check(guess["result"] == "failed", f"a random password before any ticket: {guess}")
        t = await self.ok("console.ticket", {"vm": u})
        self.fx.save("qmp-set-password", self.runner.last(virsh_verb("qemu-monitor-command", u)), "expire_password reply")
        pw = t["vnc_password"]
        self.fx.passwords.add(pw)
        good = await rfb.try_password(host, port, pw)
        check(good["result"] == "ok", f"(b) the ticket's password: {good}")
        bad = await rfb.try_password(host, port, pw[::-1] if pw[::-1] != pw else pw + "x")
        check(bad["result"] == "failed", f"(c) a wrong password: {bad}")
        # A QMP error reply (virsh exits 0 for it) — the parser must turn it into an exception.
        code, out, err = await self.runner(["virsh", "--connect", "qemu:///system", "qemu-monitor-command", u,
                                            '{"execute":"set_password","arguments":{"protocol":"spice","password":"x"}}'], 10)
        self.fx.save("qmp-error-reply", self.runner.calls[-1], "virsh rc for a QMP error reply")
        try:
            B.parse_qmp_reply(out)
            raise CheckFailed(f"a QMP error reply parsed as success (rc={code}): {out!r}")
        except B.BackendError:
            pass
        # (d) expiry
        pw2 = domainxml.random_vnc_password()
        self.fx.passwords.add(pw2)
        await self.be.set_vnc_password(u, pw2, 2)
        now_ok = await rfb.try_password(host, port, pw2)
        check(now_ok["result"] == "ok", f"(d) fresh 2-second password: {now_ok}")
        await asyncio.sleep(3.5)
        expired = await rfb.try_password(host, port, pw2)
        check(expired["result"] == "failed", f"(d) after expiry: {expired}")
        self.r.add("4 console raw", "PASS", f"types={first['types']} ({first['version']}); ticket pw ok; wrong pw "
                   f"failed ({bad['reason']!r}); expired pw failed; QMP error reply rc={code} raises")
        await self._console_ws(u)

    async def _console_ws(self, u):
        try:
            import uvicorn
            from fastapi import FastAPI
            from websockets.asyncio.client import connect
            from app.routers import vmhost as route
            from app.services.vmhost import service as vmsvc
        except Exception as e:
            self.r.add("4 console ws", "SKIP", f"cannot import the route stack: {e}")
            return
        app = FastAPI()
        app.include_router(route.ws_router)
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="off"))
        task = asyncio.create_task(server.serve())
        vmsvc.set_current(self.svc)
        try:
            for _ in range(100):
                if server.started:
                    break
                await asyncio.sleep(0.05)
            port = server.servers[0].sockets[0].getsockname()[1]
            t = await self.ok("console.ticket", {"vm": u})
            self.fx.passwords.add(t["vnc_password"])

            class WsStream:
                def __init__(self, ws):
                    self.ws, self.buf, self.out = ws, b"", []

                async def readexactly(self, n):
                    while len(self.buf) < n:
                        m = await self.ws.recv()
                        if isinstance(m, str):
                            raise rfb.RfbError(f"text frame mid-stream: {m}")
                        self.buf += m
                    b, self.buf = self.buf[:n], self.buf[n:]
                    return b

                def write(self, data):
                    self.out.append(data)

                async def drain(self):
                    if self.out:
                        data, self.out = b"".join(self.out), []
                        await self.ws.send(data)

            async with connect(f"ws://127.0.0.1:{port}/ws/vmconsole") as ws:
                await ws.send(json.dumps({"t": "open", "ticket": t["ticket"]}))
                first = json.loads(await asyncio.wait_for(ws.recv(), 10))
                check(first == {"t": "ok"}, f"open → {first}")
                await ws.send(json.dumps({"t": "go"}))
                s = WsStream(ws)
                hs = await rfb.handshake(s, s, t["vnc_password"])
                check(hs["result"] == "ok", f"RFB over /ws/vmconsole: {hs}")
                check(self.svc.consoles.live_count(u) == 1, "the console is not registered as live")
            async with connect(f"ws://127.0.0.1:{port}/ws/vmconsole") as ws:
                await ws.send(json.dumps({"t": "open", "ticket": t["ticket"]}))
                again = json.loads(await asyncio.wait_for(ws.recv(), 10))
                check(again.get("t") == "err", f"a used ticket was accepted again: {again}")
            self.r.add("4 console ws", "PASS", "ticket → /ws/vmconsole → RFB VNC auth ok through the real route; "
                       "a reused ticket is refused")
        finally:
            vmsvc.set_current(None)
            server.should_exit = True
            await asyncio.wait_for(task, 10)

    # ---- 5
    async def step5_power(self):
        u = self.efi["uuid"]
        res = await self.op("vm.power", {"vm": u, "action": "reboot"})
        self.fx.save("virsh-reboot", self.runner.last(virsh_verb("reboot", u)))
        reboot_note = "ok" if res.get("ok") else f"refused: {res['error']}"
        check(await self.wait_state(u, "running", 5), "not running after reboot")
        res = await self.op("vm.power", {"vm": u, "action": "shutdown"})
        check(res.get("ok"), f"shutdown: {res.get('error')}")
        acpi = await self.wait_state(u, "shutoff", 15)
        if not acpi:
            self.r.finding("ACPI shutdown is ignored while a guest sits in firmware (no OS) — destroy is the fallback")
            res = await self.op("vm.power", {"vm": u, "action": "destroy"})
            check(res.get("ok"), f"destroy: {res.get('error')}")
        check(await self.state(u) == "shutoff", "not shut off")
        res = await self.op("vm.power", {"vm": u, "action": "destroy"})
        check(not res.get("ok") and res["error"]["code"] == "conflict", f"destroy on a stopped VM: {res}")
        if self.bios:
            res = await self.op("vm.power", {"vm": self.bios["uuid"], "action": "destroy"})
            check(res.get("ok"), f"destroy bios: {res.get('error')}")
        self.r.add("5 power", "PASS", f"reboot {reboot_note}; shutdown {'honoured' if acpi else 'ignored → destroy'}; "
                   "destroy on a stopped VM = conflict")

    # ---- 6
    async def step6_update(self):
        u = self.efi["uuid"]
        iso_dir = self.root / "isos"
        iso_dir.mkdir(exist_ok=True)
        self.iso = iso_dir / f"pcprobe-{secrets.token_hex(3)}.iso"
        how = make_iso(self.iso)
        info = await self.be.img_info(str(self.iso))
        self.fx.save("qemu-img-info-iso", self.runner.last(lambda a: "info" in a and str(self.iso) in a))
        check(info["format"] == "raw", f"the ISO probes as {info}")
        v = (await self.ok("vm.update", {"vm": u, "vcpus": 2, "ram_mib": 768, "boot": "cdrom", "add_disk_gib": 1,
                                         "media": {"iso": self.iso.name}, "input": "mouse", "autostart": True}))["vm"]
        d = await self.be.get(u)
        hw = v["hardware"]
        check(d.vcpus == 2 and d.ram_mib == 768 and d.autostart, f"re-read {d}")
        check(hw["boot"] == "cdrom" and hw["media"] == self.iso.name and hw["input"] == "mouse", f"hardware {hw}")
        check(sorted(x["target"] for x in d.disks) == ["sda", "vda", "vdb"], f"disks {d.disks}")
        self.fx.save("virsh-domblklist-with-cdrom", self.runner.last(virsh_verb("domblklist", u)))
        self.fx.save("virsh-dumpxml-inactive-updated", self.runner.last(virsh_verb("dumpxml", u, "--inactive")))
        # QEMU must really open the inserted ISO and the new disk: start it, then stop.
        res = await self.op("vm.power", {"vm": u, "action": "start"})
        check(res.get("ok"), f"start with the ISO inserted: {res.get('error')}")
        await self.op("vm.power", {"vm": u, "action": "destroy"})
        v = (await self.ok("vm.update", {"vm": u, "media": "eject", "boot": "disk", "autostart": False}))["vm"]
        hw = v["hardware"]
        check(hw["media"] == "" and hw["boot"] == "disk" and hw["cdrom"], f"after eject {hw}")
        res = await self.op("vm.update", {"vm": u, "vcpus": 1})
        running_refused = None
        await self.op("vm.power", {"vm": u, "action": "start"})
        r2 = await self.op("vm.update", {"vm": u, "vcpus": 1})
        running_refused = (not r2.get("ok")) and r2["error"]["code"] == "conflict"
        await self.op("vm.power", {"vm": u, "action": "destroy"})
        check(res.get("ok"), f"vcpus back to 1: {res.get('error')}")
        check(running_refused, f"vm.update on a running VM was not refused: {r2}")
        self.r.add("6 update", "PASS", f"vcpus/ram/autostart/boot/add disk/insert ({how} ISO)/eject read back; the VM "
                   "started with the ISO and the new disk; update while running = conflict")

    # ---- 7
    async def _qemu_io(self, path, cmd):
        code, out, err = await self.runner(["qemu-io", "-f", "qcow2", "-c", cmd, str(path)], 30)
        check(code == 0 and "error" not in (out + err).lower(), f"qemu-io {cmd}: {out} {err}")
        return out

    async def step7_snapshots(self, label="7 snapshots"):
        u = self.efi["uuid"]
        vm_dir = self.root / u
        disk = vm_dir / "disk-vda.qcow2"
        nvram = vm_dir / "nvram.fd"
        # Running: refused (offline snapshots only).
        await self.op("vm.power", {"vm": u, "action": "start"})
        res = await self.op("vm.snapshot.create", {"vm": u, "name": "running"})
        running = res
        await self.op("vm.power", {"vm": u, "action": "destroy"})
        res = await self.op("vm.snapshot.create", {"vm": u, "name": "clean", "description": "before the marker"})
        if not res.get("ok"):
            self.r.add(label, "FAIL", f"EFI snapshot while shut off: {res['error']} (running attempt: "
                       f"{running.get('error') or 'accepted'})")
            return False
        check(not running.get("ok") and running["error"]["code"] == "conflict",
              f"a snapshot of a RUNNING VM must be refused: {running}")
        nv_before = nvram.read_bytes() if os.access(nvram, os.R_OK) else None
        await self._qemu_io(disk, "write -P 0x5a 1M 64k")
        if nv_before is not None:
            with open(nvram, "r+b") as f:        # simulate a changed EFI variable store
                f.seek(len(nv_before) - 16)
                f.write(b"PCPROBE-CHANGED!")
        lst = (await self.ok("vm.snapshot.list", {"vm": u}))["snapshots"]
        check([s["name"] for s in lst] == ["clean"], f"list {lst}")
        code, out, err = await self.runner(["qemu-img", "snapshot", "-l", str(disk)], 20)
        self.fx.save("qemu-img-snapshot-l", self.runner.calls[-1])
        await self.be.img_info(str(disk))
        self.fx.save("qemu-img-info-with-snapshot", self.runner.last(lambda a: "info" in a and str(disk) in a))
        res = await self.op("vm.snapshot.revert", {"vm": u, "name": "clean"})
        check(not res.get("ok") and res["error"]["code"] == "bad_request", f"revert without confirm: {res}")
        await self.ok("vm.snapshot.revert", {"vm": u, "name": "clean", "confirm": True})
        out = await self._qemu_io(disk, "read -P 0 1M 64k")
        check("Pattern verification failed" not in out, f"the disk did not revert: {out}")
        if nv_before is not None:
            check(nvram.read_bytes() == nv_before, "the EFI variable store did not revert")
        d = await self.be.get(u)
        check(d.meta and d.meta.owner == ADMIN, f"metadata after revert {d.meta}")
        await self.ok("vm.snapshot.create", {"vm": u, "name": "second"})
        await self.ok("vm.snapshot.delete", {"vm": u, "name": "second"})
        lst = (await self.ok("vm.snapshot.list", {"vm": u}))["snapshots"]
        check([s["name"] for s in lst] == ["clean"], f"after delete {lst}")
        code, out, _ = await self.runner(["qemu-img", "snapshot", "-l", str(disk)], 20)
        check("second" not in out, f"qemu-img still lists the deleted snapshot: {out}")
        # the VM still boots after a revert
        res = await self.op("vm.power", {"vm": u, "action": "start"})
        check(res.get("ok"), f"start after revert: {res.get('error')}")
        await self.op("vm.power", {"vm": u, "action": "destroy"})
        self.r.add(label, "PASS", "running → conflict; offline create/list/revert (disk bytes + nvram restored, "
                   "metadata kept)/delete; boots after revert")
        return True

    # ---- 8
    async def step8_images(self):
        from app.services.vmhost.migrate import Migrator, MigrationAbort
        work = self.root / f"pcprobe-img-{secrets.token_hex(3)}"
        work.mkdir(mode=0o700)
        self.created_dirs.append(work)
        probe = SimpleNamespace(backend=self.be)
        plain, base, over, raw, data = (work / n for n in ("plain.qcow2", "base.qcow2", "overlay.qcow2", "raw.img",
                                                          "datafile.qcow2"))
        for argv in (["qemu-img", "create", "-f", "qcow2", str(plain), "64M"],
                     ["qemu-img", "create", "-f", "qcow2", str(base), "64M"],
                     ["qemu-img", "create", "-f", "qcow2", "-b", str(base), "-F", "qcow2", str(over)],
                     ["qemu-img", "create", "-f", "raw", str(raw), "16M"],
                     ["qemu-img", "create", "-f", "qcow2", "-o", f"data_file={work / 'datafile.raw'}", str(data), "16M"]):
            code, out, err = await self.runner(argv, 30)
            check(code == 0, f"{argv}: {err}")
        results = {}
        for p, fxname in ((plain, "qemu-img-info-qcow2"), (over, "qemu-img-info-backing"), (raw, "qemu-img-info-raw"),
                          (data, "qemu-img-info-datafile")):
            info = await self.be.img_info(str(p))
            self.fx.save(fxname, self.runner.last(lambda a, p=p: "info" in a and str(p) in a))
            try:
                fmt = await Migrator._probe_disk(probe, p, p.name)
                results[p.name] = ("accepted", fmt, info)
            except MigrationAbort as e:
                results[p.name] = ("refused", str(e), info)
        check(results["plain.qcow2"][:2] == ("accepted", "qcow2"), f"plain {results['plain.qcow2']}")
        check(results["raw.img"][:2] == ("accepted", "raw"), f"raw {results['raw.img']}")
        check(results["overlay.qcow2"][0] == "refused" and results["overlay.qcow2"][2]["backing"],
              f"backing {results['overlay.qcow2']}")
        check(results["datafile.qcow2"][0] == "refused" and results["datafile.qcow2"][2]["data_file"],
              f"data file {results['datafile.qcow2']}")
        from app.services.vmhost.migrate import qcow2_has_backing
        check(qcow2_has_backing(over) and not qcow2_has_backing(plain), "the header check disagrees with qemu-img")
        self.r.add("8 qemu-img", "PASS", "qcow2 + raw accepted; backing file and external data file refused")

    # ---- 9
    async def step9_migration(self):
        from app.services.vmhost import migrate as M
        u, name = self.efi["uuid"], self.efi["name"]
        check(await self.state(u) == "shutoff", "the source VM must be shut off")
        mig = M.Migrator(self.svc, secrets.token_bytes(32), M.MigrateConfig(), rpc=SimpleNamespace())
        try:
            xml = await self.be.dumpxml_inactive(u)
            self.fx.save("virsh-dumpxml-inactive-migratable", self.runner.last(virsh_verb("dumpxml", u, "--migratable")))
            info = M.inspect_domain_xml(xml)
            M.Migrator._refusals(info)
            meta = (await self.be.get(u)).meta
            files = await asyncio.to_thread(mig._collect_files, u, info, meta)
            vm_dir = self.root / u
            for f in files:
                f["sha256"] = await asyncio.to_thread(M.sha256_file, vm_dir / f["name"])
            names, current = await self.be.snapshot_names(u)
            self.fx.save("virsh-snapshot-list-names", self.runner.last(virsh_verb("snapshot-list", u, "--name")))
            snaps = []
            for n in names:
                sx = await self.be.snapshot_dumpxml(u, n)
                snaps.append({"name": n, "xml": sx, "current": n == current})
            carried = [sn["name"] for sn in meta.snapshots]
            # the "transfer": copy the files into a second storage root (a second host's layout, same machine)
            self.target_root.mkdir(mode=0o751, exist_ok=True)
            os.chmod(self.target_root, 0o751)
            tstore = Storage(self.target_root)
            tstore.ensure()
            tsvc = VmHostService(self.cfg.__class__(**{**self.cfg.__dict__, "storage_dir": str(self.target_root)}),
                                 self.be, node_pubkey=NODE, admin_provider=lambda: set(), storage=tstore)
            tmig = M.Migrator(tsvc, secrets.token_bytes(32), M.MigrateConfig(), rpc=SimpleNamespace())
            try:
                tdir = self.target_root / u
                self.created_dirs.append(tdir)
                copy_names = [f["name"] for f in files]
                tdir.mkdir(mode=0o751)
                os.chmod(tdir, 0o751)
                for n in copy_names:
                    shutil.copyfile(vm_dir / n, tdir / n)
                    os.chmod(tdir / n, 0o600)
                # hand-off on the source: undefine keeping the nvram
                await self.be.undefine_for_migration(u, keep_nvram=True)
                check(await self.be.get(u) is None, "the source is still defined after undefine_for_migration")
                check((vm_dir / "nvram.fd").exists(), "undefine_for_migration removed the nvram")
                self.runner.mine.discard(u)          # re-added by the define below
                manifest = {"vm": {"uuid": u, "name": name}, "xml": xml, "meta": meta.to_xml(prefixed=True),
                            "snapshots": snaps, "files": files}
                rec = {"id": "probe-" + secrets.token_hex(4), "vm": u, "name": name, "assign_allow": [],
                       "formats": {}}
                await tmig._define_incoming(rec, manifest, tdir, {"id": rec["id"], "state": "incoming", "peer": NODE})
                self.fx.save("virsh-define-rebuilt", self.runner.last(virsh_verb("define")))
                d = await self.be.get(u)
                check(d is not None and d.meta and d.meta.migration.get("id") == rec["id"], f"rebuilt domain {d}")
                txml = await self.be.dumpxml(u, inactive=True)
                self.fx.save("virsh-dumpxml-inactive-rebuilt", self.runner.last(virsh_verb("dumpxml", u, "--inactive")))
                check(str(tdir / "disk-vda.qcow2") in txml and str(tdir / "nvram.fd") in txml, "paths not in target dir")
                tnv = domainxml.parse_domain(txml).find("os/nvram")
                check(tnv is not None and tnv.get("format") == "qcow2", "the rebuilt varstore lost its probed format")
                self.fx.save("qemu-img-info-nvram", self.runner.last(lambda a: "info" in a and a[-1].endswith("nvram.fd")))
                # a migrated (tagged) VM refuses start; clear the tag the way a commit does, then boot it
                d.meta.migration = {}
                await self.be.set_metadata(u, d.meta, live=False)
                res = await self.op("vm.power", {"vm": u, "action": "start"}, svc=tsvc)
                check(res.get("ok"), f"the rebuilt VM did not start: {res.get('error')}")
                t = await self.ok("console.ticket", {"vm": u}, svc=tsvc)
                self.fx.passwords.add(t["vnc_password"])
                host, port = await self.be.vnc_endpoint(u)
                check((await rfb.try_password(host, port, t["vnc_password"]))["result"] == "ok",
                      "console auth on the rebuilt VM")
                check((await rfb.try_password(host, port, None))["types"] == [rfb.SEC_VNC],
                      "the rebuilt VM's display offers more than VNC auth")
                await self.op("vm.power", {"vm": u, "action": "destroy"}, svc=tsvc)
                snap_note = await self._check_carried_snapshots(tsvc, u, carried)
                self.target = {"uuid": u, "name": name, "svc": tsvc}
                self.efi = None          # the source copy is gone (its directory is cleaned up at the end)
                self.r.add("9 migration", "PASS", f"dumpxml --migratable, {len(files)} file(s) hashed, "
                           f"{len(snaps)} libvirt snapshot(s); undefine kept nvram; target REBUILT the domain in a "
                           f"second storage root, it booted and its console needs the password; {snap_note}")
            finally:
                await tmig.close()
        finally:
            await mig.close()

    async def _check_carried_snapshots(self, tsvc, u, want) -> str:
        if not want:
            return "no offline snapshots carried"
        lst = (await self.ok("vm.snapshot.list", {"vm": u}, svc=tsvc))["snapshots"]
        check([(s["name"], s["state"]) for s in lst] == [(n, "ok") for n in want], f"carried snapshots {lst} != {want}")
        await self.ok("vm.snapshot.revert", {"vm": u, "name": want[0], "confirm": True}, svc=tsvc)
        return f"offline snapshots {want} carried and revertable on the target"

    # ---- 10
    async def step11_devices(self):
        """USB hot-plug into the RUNNING probe VM with a real device, and PCI read-only (never attached: the only
        passable card on a real host is somebody's GPU). USB runs only when --usb names a device that is plugged in and
        that the host is not using; otherwise it says so and passes nothing off as tested."""
        from app.services.vmhost import usb as U
        lst = await self.ok("host.devices.list", {"vm": self.efi["uuid"]})
        usb_rows = lst["kinds"]["usb"]["devices"]
        pci_k = lst["kinds"]["pci"]
        self.fx.save_text("devices-host-list", json.dumps(lst, indent=1, sort_keys=True), "host.devices.list")
        busy_pci = [x for x in pci_k["devices"] if x["busy"]]
        pci_note = (f"pci: {len(pci_k['devices'])} listed, checks "
                    + ", ".join(f"{c['id']}={'ok' if c['ok'] else 'NO'}" for c in pci_k["checks"])
                    + f", {len(busy_pci)} in use by the host")
        if pci_k["devices"]:                     # a PCI attach to a RUNNING VM is refused before libvirt is asked
            n = len(self.runner.calls)
            res = await self.op("vm.device.attach", {"vm": self.efi["uuid"], "kind": "pci",
                                                     "address": pci_k["devices"][0]["address"]})
            check(not res.get("ok") and res["error"]["code"] == "conflict", f"pci attach on a running VM: {res}")
            check(not any(c["argv"][3:4] == ["attach-device"] for c in self.runner.calls[n:]), "virsh was asked")
        if not self.usb:
            self.r.add("11 devices", "PASS", "USB NOT exercised (no --usb given); " + pci_note)
            return
        vid, pid = self.usb.split(":")
        usb_bad = [ch for ch in lst["kinds"]["usb"]["checks"] if not ch["ok"]]
        if usb_bad:                              # e.g. Gentoo's default QEMU (USE=-usb) has no usb-host device
            n = len(self.runner.calls)
            res = await self.op("vm.device.attach", {"vm": self.efi["uuid"], "kind": "usb", "vendor": vid, "product": pid})
            check(not res.get("ok") and res["error"]["code"] == "unsupported", f"attach with {usb_bad[0]['label']}: {res}")
            check(not any(c["argv"][3:4] == ["attach-device"] for c in self.runner.calls[n:]), "virsh was asked")
            self.r.add("11 devices", "PASS", f"USB NOT hot-plugged: {usb_bad[0]['label']} — the attach was refused "
                       f"before libvirt was asked; " + pci_note)
            self.r.finding(usb_bad[0]["label"] + " — " + usb_bad[0]["fix"])
            return
        row = next((x for x in usb_rows if x["vendor"] == vid and x["product"] == pid), None)
        check(row is not None, f"--usb {self.usb} is not offered ({[x['label'] for x in usb_rows]})")
        check(not row["busy"] and not row["used_by"], f"{row['label']} is in use: {row['busy'] or row['used_by']}")
        node = f"/dev/bus/usb/{row['bus']:03d}/{row['device']:03d}"
        before = os.stat(node)
        u = self.efi["uuid"]
        args = {"vm": u, "kind": "usb", "vendor": vid, "product": pid, "persist": False}
        r1 = await self.ok("vm.device.attach", args)
        live = await self.be.dumpxml(u, inactive=False)
        self.fx.save("virsh-dumpxml-running-usb-hostdev", self.runner.last(virsh_verb("dumpxml", u)))
        got = U.hostdevs(live)
        check(any(e["vendor"] == vid and e["bus"] == row["bus"] for e in got), f"live XML lacks the device: {got}")
        check(not U.hostdevs(await self.be.dumpxml(u, inactive=True)), "persist:false still wrote the saved definition")
        during = os.stat(node)
        check(any(d["vendor"] == vid and d["live"] and not d["persistent"] for d in r1["vm"]["devices"]),
              f"vm.devices: {r1['vm']['devices']}")
        if self.bios:                            # the same stick for the second probe VM is refused, naming the first
            res = await self.op("vm.device.attach", dict(args, vm=self.bios["uuid"]))
            check(not res.get("ok") and res["error"]["code"] == "conflict" and self.efi["name"] in res["error"]["message"],
                  f"second VM: {res}")
        await self.ok("vm.device.detach", {"vm": u, "kind": "usb", "vendor": vid, "product": pid})
        check(not U.hostdevs(await self.be.dumpxml(u, inactive=False)), "the device is still in the running VM")
        after = None
        for _ in range(20):
            await asyncio.sleep(0.5)
            try:
                after = os.stat(node)
                break
            except FileNotFoundError:
                continue
        stopped = ""
        if self.bios:                            # a STOPPED VM: --config only, read back, and out again
            if await self.state(self.bios["uuid"]) == "running":
                await self.ok("vm.power", {"vm": self.bios["uuid"], "action": "destroy"})
            await self.ok("vm.device.attach", {"vm": self.bios["uuid"], "kind": "usb", "vendor": vid, "product": pid})
            saved = await self.be.dumpxml(self.bios["uuid"], inactive=True)
            self.fx.save("virsh-dumpxml-inactive-usb-hostdev", self.runner.last(virsh_verb("dumpxml", self.bios["uuid"])))
            check(U.hostdevs(saved) == [{"vendor": vid, "product": pid, "bus": None, "device": None}],
                  f"saved hostdevs {U.hostdevs(saved)}")
            await self.ok("vm.device.detach", {"vm": self.bios["uuid"], "kind": "usb", "vendor": vid, "product": pid})
            check(not U.hostdevs(await self.be.dumpxml(self.bios["uuid"], inactive=True)), "still saved after detach")
            stopped = "; stopped VM: saved + removed"
        self.r.add("11 devices", "PASS",
                   f"{row['label']} at {node}: {before.st_uid}:{before.st_gid} {oct(before.st_mode & 0o777)} -> attached "
                   f"{during.st_uid}:{during.st_gid} {oct(during.st_mode & 0o777)} -> detached "
                   f"{(str(after.st_uid) + ':' + str(after.st_gid)) if after else 'node gone'}{stopped}; " + pci_note)

    async def step10_delete(self):
        done = []
        for vm, svc in ((self.bios, self.svc), (self.efi, self.svc),
                        (self.target, self.target and self.target.get("svc"))):
            if not vm:
                continue
            if svc is self.svc and await self.state(vm["uuid"]) == "running":   # a partial --steps run left it on
                await self.ok("vm.power", {"vm": vm["uuid"], "action": "destroy"})
            res = await self.op("vm.delete", {"vm": vm["uuid"], "confirm_name": vm["name"], "delete_disks": True}, svc=svc)
            check(res.get("ok"), f"delete {vm['name']}: {res.get('error')}")
            check(await self.be.get(vm["uuid"]) is None, f"{vm['name']} is still defined")
            check(not (svc.storage.root / vm["uuid"]).exists(), f"{vm['name']}'s directory is still there")
            done.append(vm["name"])
        self.bios = self.efi = self.target = None
        self.r.add("10 delete", "PASS", f"deleted with disks: {', '.join(done)}")

    # ---- cleanup (always)
    async def cleanup(self) -> list:
        left = []
        V = ["virsh", "--connect", "qemu:///system"]
        for u in sorted(self.runner.mine):
            for args in (["destroy", u], ["undefine", u, "--nvram", "--snapshots-metadata"], ["undefine", u]):
                try:
                    await self.runner(V + args, 30)
                except ProbeRefused:
                    pass
        for d in self.created_dirs:
            shutil.rmtree(d, True)
        if self.iso is not None:
            try:
                self.iso.unlink()
            except FileNotFoundError:
                pass
        shutil.rmtree(self.target_root, True)
        shutil.rmtree(self.root / ".state", True)
        code, out, _ = await B._run(V + ["list", "--all", "--name"], 15)
        left += [f"domain {n}" for n in out.split() if n.startswith("pcprobe-")]
        code, out, _ = await B._run(V + ["list", "--all", "--uuid"], 15)
        left += [f"domain uuid {x}" for x in B.parse_uuid_list(out) if x in self.runner.mine]
        for p in self.root.iterdir():
            if p.name == "isos" and p.is_dir() and not any(p.iterdir()):
                continue
            left.append(f"file {p}")
        for nv in Path("/var/lib/libvirt/qemu/nvram").glob("pcprobe-*") if os.access("/var/lib/libvirt/qemu/nvram", os.R_OK) else []:
            left.append(f"libvirt nvram {nv}")
        return left


STEPS = [("1", "step1_host"), ("2", "step2_create"), ("3", "step3_start"), ("4", "step4_console"),
         ("5", "step5_power"), ("6", "step6_update"), ("7", "step7_snapshots"), ("8", "step8_images"),
         ("9", "step9_migration"), ("11", "step11_devices"), ("10", "step10_delete")]


async def amain(args) -> int:
    storage = Path(args.storage).resolve()
    if not storage.is_dir():
        print(f"{storage} does not exist — create it (0751, owned by you, group qemu) first", file=sys.stderr)
        return 2
    if any(p.name not in ("isos",) for p in storage.iterdir()):
        print(f"{storage} is not empty — the probe only runs in a probe-only directory", file=sys.stderr)
        return 2
    if not shutil.which("virsh") or not shutil.which("qemu-img"):
        print("virsh and qemu-img are required", file=sys.stderr)
        return 2
    report = Report()
    probe = Probe(storage, args.capture, report)
    probe.usb = args.usb
    want = set(args.steps.split(",")) if args.steps else None
    failed = False
    try:
        for num, meth in STEPS:
            if want and num not in want:
                continue
            try:
                await getattr(probe, meth)()
            except (CheckFailed, B.BackendError, ProbeRefused) as e:
                failed = True
                report.add(f"{num} {meth.split('_', 1)[1]}", "FAIL", f"{type(e).__name__}: {e}")
            except Exception as e:
                failed = True
                report.add(f"{num} {meth.split('_', 1)[1]}", "FAIL", f"{type(e).__name__}: {e}")
                traceback.print_exc()
    finally:
        left = await probe.cleanup()
        if left:
            failed = True
            report.add("cleanup", "FAIL", "leftovers: " + "; ".join(left))
        else:
            report.add("cleanup", "PASS", "no probe domains, disks, nvram, ISOs or state left")
        code, ver, _ = await B._run(["virsh", "--connect", "qemu:///system", "version"], 10)
        code, qv, _ = await B._run(["qemu-img", "--version"], 10)
        probe.fx.finish({"virsh_version": ver.strip(), "qemu_img": qv.strip().splitlines()[0] if qv else ""})
    print("\n==== summary")
    for step, status, detail in report.rows:
        print(f"{status:5} {step}")
    for f in report.findings:
        print(f"FINDING {f}")
    return 1 if failed else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--storage", required=True)
    ap.add_argument("--capture", default="")
    ap.add_argument("--steps", default="")
    ap.add_argument("--usb", default="", help="vendor:product of a real USB device the host is NOT using, to hot-plug "
                                              "into the probe VM (step 11); without it USB is reported as not exercised")
    args = ap.parse_args()
    sys.exit(asyncio.run(amain(args)))


if __name__ == "__main__":
    main()
