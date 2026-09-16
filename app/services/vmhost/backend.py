"""The hypervisor behind a VM host: a small async Protocol, and `VirshBackend` which speaks it through
the `virsh` CLI.

WHY A CLI AND NOT libvirt-python: it is not in requirements.txt, it needs libvirt headers to build,
and the desktop app has driven virsh successfully for a year (desktop/vm.js). The costs are a
subprocess per call and parsing text, both contained here and both covered by tests that feed real
virsh output through the parsers.

Three rules every call obeys:
  * STRICT ARGV. `create_subprocess_exec`, never a shell, and every value a client influenced is a
    validated uuid, a clamped integer or a path built by `storage.Storage` — so nothing can be
    smuggled in as an option or a shell word.
  * A TIMEOUT ON EVERYTHING. A wedged libvirtd must cost one request an error, never the event loop
    (this runs inside the single uvicorn worker).
  * NEVER BLOCK THE LOOP. Subprocesses are awaited; filesystem stat of the storage root goes through
    `asyncio.to_thread`.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
from dataclasses import dataclass, field
from typing import Optional, Protocol

from . import domainxml


class BackendError(Exception):
    def __init__(self, message: str, code: str = "backend_error"):
        super().__init__(message)
        self.code = code


@dataclass
class DomainInfo:
    uuid: str
    name: str
    state: str                    # running | shutoff | paused | stopping | other
    vcpus: int = 0
    ram_mib: int = 0
    autostart: bool = False
    meta: Optional[domainxml.VmMeta] = None
    disks: list = field(default_factory=list)


def normalize_state(raw: str) -> str:
    s = str(raw or "").strip().lower()
    if s in ("running", "idle", "blocked", "no state"):
        return "running"
    if s in ("shut off", "shutoff", "inactive"):
        return "shutoff"
    if s == "paused":
        return "paused"
    if s in ("in shutdown", "shutdown"):
        return "stopping"
    return "other"


class Backend(Protocol):
    async def available(self) -> dict: ...
    async def host_stats(self, storage_root: str) -> dict: ...
    async def list_domains(self) -> list: ...
    async def get(self, vm_uuid: str) -> Optional[DomainInfo]: ...
    async def define(self, xml: str, workdir: str) -> None: ...
    async def undefine(self, vm_uuid: str, keep_nvram: bool = False) -> None: ...
    async def start(self, vm_uuid: str) -> None: ...
    async def shutdown(self, vm_uuid: str) -> None: ...
    async def destroy(self, vm_uuid: str) -> None: ...
    async def reboot(self, vm_uuid: str) -> None: ...
    async def set_autostart(self, vm_uuid: str, on: bool) -> None: ...
    async def set_metadata(self, vm_uuid: str, meta: domainxml.VmMeta, live: bool) -> None: ...
    async def vnc_endpoint(self, vm_uuid: str) -> Optional[tuple]: ...
    async def set_vnc_password(self, vm_uuid: str, password: str, expire_s: int) -> None: ...
    async def img_create(self, path: str, size_gib: int) -> None: ...


# ---------------------------------------------------------------------------------------- parsers
def parse_kv(text: str) -> dict:
    """`virsh dominfo` / `nodeinfo` style `Key:   value` lines."""
    out = {}
    for line in str(text or "").splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip().lower()] = v.strip()
    return out


def parse_dominfo(text: str) -> dict:
    kv = parse_kv(text)
    mem = re.search(r"\d+", kv.get("max memory", "") or "")
    try:
        vcpus = int(kv.get("cpu(s)", "0") or 0)
    except ValueError:
        vcpus = 0
    return {"uuid": kv.get("uuid", "").lower(), "name": kv.get("name", ""),
            "state": normalize_state(kv.get("state", "")), "vcpus": vcpus,
            "ram_mib": (int(mem.group(0)) // 1024) if mem else 0,
            "autostart": kv.get("autostart", "").lower().startswith("enable")}


def parse_uuid_list(text: str) -> list:
    return [ln.strip().lower() for ln in str(text or "").splitlines()
            if re.fullmatch(r"[0-9a-fA-F-]{36}", ln.strip())]


def parse_vncdisplay(text: str) -> Optional[tuple]:
    """`virsh vncdisplay` → `127.0.0.1:0` (display, not port) → ('127.0.0.1', 5900)."""
    m = re.search(r"^\s*(\[[^\]]+\]|[^:\s]*):(\d+)\s*$", str(text or ""), re.M)
    if not m:
        return None
    host = m.group(1).strip("[]") or "127.0.0.1"
    return host, 5900 + int(m.group(2))


def parse_nodeinfo(text: str) -> dict:
    kv = parse_kv(text)
    mem = re.search(r"\d+", kv.get("memory size", "") or "")
    try:
        cores = int(kv.get("cpu(s)", "0") or 0)
    except ValueError:
        cores = 0
    return {"cores": cores, "ram_total_mib": (int(mem.group(0)) // 1024) if mem else 0}


def parse_nodememstats(text: str) -> int:
    """Free-ish memory in MiB: free + buffers + cached, which is what a new guest can actually take."""
    kv = parse_kv(text)
    total = 0
    for k in ("free", "buffers", "cached"):
        m = re.search(r"\d+", kv.get(k, "") or "")
        if m:
            total += int(m.group(0))
    return total // 1024


def parse_domblklist(text: str) -> list:
    out = []
    for line in str(text or "").splitlines()[2:]:
        parts = line.split()
        if len(parts) >= 4:
            out.append({"type": parts[0], "device": parts[1], "target": parts[2],
                        "source": " ".join(parts[3:])})
    return out


# ---------------------------------------------------------------------------------------- virsh
async def _run(argv: list, timeout: float, stdin: bytes | None = None) -> tuple:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except FileNotFoundError:
        raise BackendError(f"{argv[0]} is not installed on this host", "unsupported")
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await proc.wait()
        except Exception:
            pass
        raise BackendError(f"{os.path.basename(argv[0])} {argv[3] if len(argv) > 3 else ''} timed out", "timeout")
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


class VirshBackend:
    """Every call is `virsh --connect <uri> <verb> …` with a timeout. `runner` is injectable so the
    argv shapes can be tested without libvirt."""

    def __init__(self, uri: str = "qemu:///system", runner=None, virsh: str = "virsh",
                 qemu_img: str = "qemu-img"):
        if not re.fullmatch(r"[a-z+]+://[A-Za-z0-9._/:@-]*(\?[A-Za-z0-9._=&/-]*)?", uri or ""):
            raise BackendError("invalid libvirt URI", "bad_request")
        self.uri = uri
        self._run = runner or _run
        self.virsh = virsh
        self.qemu_img = qemu_img

    async def _v(self, *args, timeout: float = 20.0, ok_codes=(0,)) -> str:
        code, out, err = await self._run([self.virsh, "--connect", self.uri, *args], timeout)
        if code not in ok_codes:
            msg = (err or out).strip().splitlines()
            raise BackendError((msg[-1] if msg else f"virsh {args[0]} failed")[:300])
        return out

    async def available(self) -> dict:
        kvm = os.path.exists("/dev/kvm")
        try:
            out = await self._v("version", timeout=10)
            return {"ok": True, "kvm": kvm, "libvirt": out.strip().splitlines()[0] if out.strip() else "",
                    "uri": self.uri, "error": ""}
        except BackendError as e:
            return {"ok": False, "kvm": kvm, "libvirt": "", "uri": self.uri, "error": str(e)}

    async def host_stats(self, storage_root: str) -> dict:
        info = parse_nodeinfo(await self._v("nodeinfo", timeout=10))
        try:
            free = parse_nodememstats(await self._v("nodememstats", timeout=10))
        except BackendError:
            free = 0
        try:
            du = await asyncio.to_thread(shutil.disk_usage, storage_root)
            dtot, dfree = du.total // (1 << 30), du.free // (1 << 30)
        except OSError:
            dtot = dfree = 0
        try:
            load1 = os.getloadavg()[0]
        except OSError:
            load1 = 0.0
        return {"cores": info["cores"], "load1": round(load1, 2), "ram_total_mib": info["ram_total_mib"],
                "ram_free_mib": free, "disk_total_gib": dtot, "disk_free_gib": dfree}

    async def list_domains(self) -> list:
        out = []
        for u in parse_uuid_list(await self._v("list", "--all", "--uuid", timeout=15)):
            d = await self.get(u)
            if d:
                out.append(d)
        return out

    async def get(self, vm_uuid: str) -> Optional[DomainInfo]:
        code, out, err = await self._run([self.virsh, "--connect", self.uri, "dominfo", vm_uuid], 15)
        if code != 0:
            if re.search(r"failed to get domain|domain not found|no domain", err + out, re.I):
                return None
            raise BackendError((err or out).strip()[:300] or "dominfo failed")
        d = parse_dominfo(out)
        meta = None
        code, mout, _ = await self._run([self.virsh, "--connect", self.uri, "metadata", vm_uuid,
                                         "--uri", domainxml.PC_NS, "--config"], 15)
        if code == 0:
            meta = domainxml.parse_meta(mout)
        disks = []
        code, bout, _ = await self._run([self.virsh, "--connect", self.uri, "domblklist", vm_uuid,
                                         "--details", "--inactive"], 15)
        if code == 0:
            disks = parse_domblklist(bout)
        return DomainInfo(uuid=d["uuid"] or vm_uuid, name=d["name"], state=d["state"], vcpus=d["vcpus"],
                          ram_mib=d["ram_mib"], autostart=d["autostart"], meta=meta, disks=disks)

    async def define(self, xml: str, workdir: str) -> None:
        path = os.path.join(workdir, "domain.xml")

        def _write():
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(xml)
        await asyncio.to_thread(_write)
        await self._v("define", path, timeout=30)

    async def undefine(self, vm_uuid: str, keep_nvram: bool = False) -> None:
        flag = "--keep-nvram" if keep_nvram else "--nvram"
        code, out, err = await self._run([self.virsh, "--connect", self.uri, "undefine", vm_uuid, flag], 30)
        if code != 0 and re.search(r"nvram", err, re.I):
            # A BIOS guest has no NVRAM, and older libvirt refuses the flag outright for one.
            await self._v("undefine", vm_uuid, timeout=30)
        elif code != 0:
            raise BackendError((err or out).strip()[:300] or "undefine failed")
        # Swtpm state lives outside the VM directory; `--tpm` removes it where supported, and a host
        # without the flag simply keeps it (left for the admin, never guessed at).
        if not keep_nvram:
            await self._run([self.virsh, "--connect", self.uri, "undefine", vm_uuid, "--tpm"], 10)

    async def start(self, vm_uuid: str) -> None:
        await self._v("start", vm_uuid, timeout=60)

    async def shutdown(self, vm_uuid: str) -> None:
        await self._v("shutdown", vm_uuid, timeout=30)

    async def destroy(self, vm_uuid: str) -> None:
        await self._v("destroy", vm_uuid, timeout=30)

    async def reboot(self, vm_uuid: str) -> None:
        await self._v("reboot", vm_uuid, timeout=30)

    async def set_autostart(self, vm_uuid: str, on: bool) -> None:
        args = ["autostart", vm_uuid] + ([] if on else ["--disable"])
        await self._v(*args, timeout=15)

    async def set_metadata(self, vm_uuid: str, meta: domainxml.VmMeta, live: bool) -> None:
        args = ["metadata", vm_uuid, "--uri", domainxml.PC_NS, "--key", domainxml.PC_KEY,
                "--set", meta.to_xml(prefixed=False), "--config"]
        if live:
            args.append("--live")
        await self._v(*args, timeout=15)

    async def vnc_endpoint(self, vm_uuid: str) -> Optional[tuple]:
        try:
            return parse_vncdisplay(await self._v("vncdisplay", vm_uuid, timeout=10))
        except BackendError:
            return None

    async def set_vnc_password(self, vm_uuid: str, password: str, expire_s: int) -> None:
        if not re.fullmatch(r"[A-Za-z0-9]{1,8}", password or ""):
            raise BackendError("invalid VNC password", "internal")
        await self._v("qemu-monitor-command", vm_uuid, "--hmp", f"set_password vnc {password}", timeout=10)
        await self._v("qemu-monitor-command", vm_uuid, "--hmp", f"expire_password vnc +{int(expire_s)}",
                      timeout=10)

    async def img_create(self, path: str, size_gib: int) -> None:
        code, out, err = await self._run([self.qemu_img, "create", "-f", "qcow2", "--", path,
                                          f"{int(size_gib)}G"], 60)
        if code != 0:
            raise BackendError((err or out).strip()[:300] or "qemu-img create failed")


    # ==================================================================================================
    # PHASE 3 — cold-migration primitives (app/services/vmhost/migrate.py is the only caller).
    # Kept in one block, below everything else, so the phase-2 edits above this line never collide.
    # ==================================================================================================
    async def dumpxml_inactive(self, vm_uuid: str) -> str:
        """The persistent definition in the form another host can define: `--migratable` drops the
        host-specific runtime bits (live device aliases, seclabels libvirt generated here)."""
        return await self._v("dumpxml", vm_uuid, "--inactive", "--migratable", timeout=20)

    async def snapshot_names(self, vm_uuid: str) -> tuple:
        """(names parents-first, current name or ""). Parents first because a redefine of a child whose
        parent is not yet known is refused."""
        out = await self._v("snapshot-list", vm_uuid, "--name", "--topological", timeout=20)
        names = [ln.strip() for ln in out.splitlines() if ln.strip()]
        for n in names:
            if not valid_snapshot_name(n):
                raise BackendError(f"unsupported snapshot name {n[:40]!r}", "unsupported")
        code, cur, _ = await self._run([self.virsh, "--connect", self.uri, "snapshot-current", vm_uuid,
                                        "--name"], 15)
        return names, (cur.strip() if code == 0 and cur.strip() in names else "")

    async def snapshot_dumpxml(self, vm_uuid: str, name: str) -> str:
        if not valid_snapshot_name(name):
            raise BackendError("invalid snapshot name", "bad_request")
        return await self._v("snapshot-dumpxml", vm_uuid, "--snapshotname", name, timeout=20)

    async def snapshot_redefine(self, vm_uuid: str, xml: str, workdir: str, current: bool = False) -> None:
        """Re-create a snapshot's METADATA from its XML. Internal qcow2 snapshots travel inside the disk
        file itself; this is the half libvirt keeps outside it."""
        path = os.path.join(workdir, ".snapshot-redefine.xml")

        def _write():
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(xml)
        await asyncio.to_thread(_write)
        try:
            args = ["snapshot-create", vm_uuid, path, "--redefine"] + (["--current"] if current else [])
            await self._v(*args, timeout=30)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    async def undefine_for_migration(self, vm_uuid: str, keep_nvram: bool) -> None:
        """Undefine without touching the disks. `--snapshots-metadata` because libvirt refuses to
        undefine a domain that still has snapshots, and their data lives on inside the qcow2 file."""
        flag = "--keep-nvram" if keep_nvram else "--nvram"
        code, out, err = await self._run([self.virsh, "--connect", self.uri, "undefine", vm_uuid,
                                          "--snapshots-metadata", flag], 30)
        if code != 0 and re.search(r"nvram", err, re.I):
            await self._v("undefine", vm_uuid, "--snapshots-metadata", timeout=30)
        elif code != 0 and not re.search(r"failed to get domain|domain not found|no domain", err + out, re.I):
            raise BackendError((err or out).strip()[:300] or "undefine failed")


def valid_snapshot_name(name) -> bool:
    s = str(name or "")
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._:+-]{0,127}", s))

def make_backend(cfg) -> Backend:
    """`vmhost_backend`: auto | virsh. (libvirt-python is a phase-2 option; auto = virsh today.)"""
    if cfg.backend not in ("auto", "virsh"):
        raise BackendError(f"unknown vmhost_backend {cfg.backend!r}", "unsupported")
    return VirshBackend(cfg.libvirt_uri)
