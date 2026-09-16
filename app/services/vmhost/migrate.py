"""Cold (offline) migration of a VM between two PosterChan VM hosts — phase 3.

    client ──vm.migrate──▶ SOURCE ──peer.migrate.precheck──▶ TARGET        (5310/6310 between node keys)
                           SOURCE ◀──GET /api/vmhost/transfer/{mig}/{i}── TARGET   (HTTPS, NIP-98, Range)
                           SOURCE ◀──peer.migrate.commit── TARGET ──peer.migrate.ack──▶ SOURCE

THE STATE MACHINE (every transition is journaled to `.state/migrations/<id>.json` with tmp+fsync+rename
BEFORE the side effect it announces, so a crash anywhere resumes from the journal):

  source  planned → quiescing → exporting → transferring → handed_off → done (acked)
                         └───────────┴────────────┴──▶ aborted          (VM back on the source)
                                                        handed_off ──▶ locked ──force_reclaim──▶ reclaimed | released
  target  prechecked → receiving → defining → defined → committed → done
                  └──────────┴──────────┴────────┴──▶ aborted            (.incoming and the pending define removed)
                                                  defined ──▶ locked ──force_reclaim──▶ done | released

THE ONE RULE EVERYTHING ELSE SERVES: exactly one host may run the VM, and it is decided by ONE
journaled fact — the source writing `handed_off`. Before it the source owns the VM (any failure →
the target throws its copy away and the source restores and, if it was running, restarts it). After
it the target owns it. The target learns the fact from the source's answer to `peer.migrate.commit`;
if that answer never arrives neither side can know which side of the line the other is on, so both
LOCK and only an admin (`vm.migrate.force_reclaim`) decides, with an explicit split-brain warning.
If contact comes back first, the lock resolves itself.

WHO MAY: a migration is requested by an ADMIN of the source, and the target independently verifies
that the same person is an admin THERE. The client signs a second, unpublished request —
`vm.migrate.authorize {source, target, vm}` NIP-44-encrypted to the TARGET — and the source carries it
inside `peer.migrate.precheck`. The source cannot forge it (it is signed by the admin) and cannot read
it; the target decrypts it and checks the signer against its own admin list. Peer hosts are only
ever the hosts named in `vmhost_peer_hosts` (`npub relay https`, one per line) on BOTH sides.

v1 REFUSES: a VM with a TPM (swtpm state is root-owned and outside the VM directory — every Windows VM
built here has one), host devices / shared filesystems, disks outside the VM's own directory, and
qcow2 disks with a backing file or external snapshots. Each refusal names itself.

Not a live migration: the VM is shut down first (ACPI, `vmhost_shutdown_timeout_sec`, then refuse
unless the admin asked for a forced power-off).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import math
import os
import re
import shutil
import time
import uuid as _uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from app.services.nostr import bip340, nip44
from app.services.nostr import event as nostr_event

from . import domainxml, kinds
from .backend import BackendError
from .storage import PathEscape, valid_uuid

logger = logging.getLogger(__name__)

GIB = 1 << 30
AUTHZ_OP = "vm.migrate.authorize"
AUTHZ_MAX_AGE = 600
PRECHECK_TTL = 3600
TRANSFER_PATH = "/api/vmhost/transfer/{mig}/{index}"
NIP98_KIND = 27235
NIP98_SKEW = 60

_MIG_RE = re.compile(r"^[0-9a-f]{32}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

SOURCE_PRE_HANDOFF = ("planned", "quiescing", "exporting", "transferring")
TARGET_PRE_COMMIT = ("prechecked", "receiving", "defining", "defined")
FINAL = ("done", "aborted", "reclaimed", "released")


# ====================================================================================== configuration
@dataclass
class Peer:
    pubkey: str
    relay: str
    https: str


def parse_peer_hosts(raw) -> tuple:
    """`vmhost_peer_hosts` → ([Peer], [problem lines]). One host per line: `npub relay https`.
    A line that does not parse is REPORTED, never guessed at: a half-read peer entry is a host that
    silently cannot migrate, or worse, a transfer URL pointing somewhere unintended."""
    from .service import _to_hex
    peers, bad, seen = [], [], set()
    for line in str(raw or "").splitlines():
        s = line.split("#", 1)[0].strip()
        if not s:
            continue
        parts = s.replace(",", " ").split()
        if len(parts) != 3:
            bad.append(line.strip())
            continue
        pk = _to_hex(parts[0])
        relay, https = parts[1], parts[2].rstrip("/")
        if (not pk or not re.fullmatch(r"wss?://[^\s/]+(/[^\s]*)?", relay)
                or not re.fullmatch(r"https://[A-Za-z0-9.-]+(:\d{1,5})?", https)):
            bad.append(line.strip())
            continue
        if pk in seen:
            continue
        seen.add(pk)
        peers.append(Peer(pk, relay, https))
    return peers, bad


def _cfg_int(s: dict, key: str, default: int, lo: int, hi: int) -> int:
    try:
        n = int(str(s.get(key) or "").strip() or default)
    except (TypeError, ValueError):
        n = default
    return max(lo, min(hi, n))


@dataclass
class MigrateConfig:
    peers: list = field(default_factory=list)
    peer_errors: list = field(default_factory=list)
    shutdown_timeout_sec: int = 120
    keep_source_hours: int = 72
    transfer_max_mbps: int = 0

    @classmethod
    def from_settings(cls, s: dict) -> "MigrateConfig":
        peers, bad = parse_peer_hosts(s.get("vmhost_peer_hosts", ""))
        return cls(peers=peers, peer_errors=bad,
                   shutdown_timeout_sec=_cfg_int(s, "vmhost_shutdown_timeout_sec", 120, 5, 3600),
                   keep_source_hours=_cfg_int(s, "vmhost_migration_keep_source_hours", 72, 0, 24 * 365),
                   transfer_max_mbps=_cfg_int(s, "vmhost_transfer_max_mbps", 0, 0, 100_000))


@dataclass
class Timing:
    """Every wait in the state machine, in one place, so the tests can run it in milliseconds."""
    rpc_timeout: float = 20.0
    rpc_retries: int = 2
    shutdown_poll: float = 2.0
    watch_poll: float = 15.0
    commit_retry: float = 5.0
    commit_retry_max: float = 60.0
    contact_deadline: float = 600.0      # no answer this long after the commit point → locked
    transfer_attempts: int = 8
    transfer_backoff: float = 2.0
    progress_every: float = 2.0
    chunk: int = 1 << 20


class MigrationAbort(Exception):
    """A failure before the commit point: roll back."""


class Superseded(Exception):
    """Somebody else (a cancel, an abort from the peer) already moved this migration on: stop quietly."""


class MigrationError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ====================================================================================== journal
class MigrationStore:
    """One JSON file per migration, written tmp → fsync → rename → fsync(dir). A torn write leaves the
    previous version, never half of one."""

    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self._recs: dict = {}

    def load(self) -> None:
        self._recs = {}
        try:
            names = sorted(os.listdir(self.dir))
        except OSError:
            return
        for n in names:
            if not n.endswith(".json") or n.endswith(".manifest.json"):
                continue
            try:
                with open(self.dir / n, encoding="utf-8") as f:
                    rec = json.load(f)
                if isinstance(rec, dict) and _MIG_RE.match(str(rec.get("id", ""))):
                    self._recs[rec["id"]] = rec
            except (OSError, ValueError):
                logger.warning("[vmhost] unreadable migration journal %s", n)

    def get(self, mig) -> Optional[dict]:
        return self._recs.get(mig) if isinstance(mig, str) else None

    def all(self) -> list:
        return list(self._recs.values())

    def manifest_path(self, mig: str) -> Path:
        return self.dir / f"{mig}.manifest.json"

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
        try:
            dfd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass

    def write(self, rec: dict) -> None:
        """Disk only — safe in a worker thread. The in-memory map is only ever touched on the loop."""
        self._atomic_write(self.dir / f"{rec['id']}.json", json.dumps(rec, sort_keys=True).encode())

    def put(self, rec: dict) -> None:
        self._recs[rec["id"]] = rec

    def save(self, rec: dict) -> None:
        rec["updated"] = int(time.time())
        self.write(rec)
        self.put(rec)

    def read(self, mig: str) -> Optional[dict]:
        """A fresh read of one journal from disk, without touching the in-memory map."""
        try:
            with open(self.dir / f"{mig}.json", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def write_manifest(self, mig: str, raw: bytes) -> None:
        self._atomic_write(self.manifest_path(mig), raw)


# ====================================================================================== domain XML
def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def inspect_domain_xml(xml: str) -> dict:
    """What a migration needs to know about a definition, and what it must refuse."""
    root = ET.fromstring(xml)
    dev = root.find("devices")
    out = {"name": (root.findtext("name") or "").strip(), "uuid": (root.findtext("uuid") or "").strip().lower(),
           "tpm": False, "unsupported": [], "disks": [], "nvram": "", "loader": "", "firmware_auto": False,
           "emulator": "", "vcpus": 0, "ram_mib": 0, "cdrom": ""}
    try:
        out["vcpus"] = int((root.findtext("vcpu") or "0").strip())
    except ValueError:
        pass
    mem = root.find("memory")
    if mem is not None:
        try:
            n = int((mem.text or "0").strip())
            unit = (mem.get("unit") or "KiB").lower()
            out["ram_mib"] = n if unit == "mib" else n // 1024 if unit in ("kib", "k") else n * 1024 if unit == "gib" else n
        except ValueError:
            pass
    os_el = root.find("os")
    if os_el is not None:
        out["firmware_auto"] = os_el.get("firmware") in ("efi", "bios")
        out["nvram"] = (os_el.findtext("nvram") or "").strip()
        out["loader"] = (os_el.findtext("loader") or "").strip()
    if dev is not None:
        out["emulator"] = (dev.findtext("emulator") or "").strip()
        if dev.find("tpm") is not None:
            out["tpm"] = True
        for tag, why in (("hostdev", "a host device passed through"), ("filesystem", "a shared host filesystem")):
            if dev.find(tag) is not None:
                out["unsupported"].append(why)
        for disk in dev.findall("disk"):
            src = disk.find("source")
            path = ""
            if src is not None:
                path = src.get("file") or src.get("dev") or src.get("name") or src.get("volume") or ""
            drv = disk.find("driver")
            bs = disk.find("backingStore")
            out["disks"].append({"type": disk.get("type", "file"), "device": disk.get("device", "disk"),
                                 "source": path, "format": drv.get("type", "") if drv is not None else "",
                                 "backing": bs is not None and bs.find("source") is not None})
            if disk.get("device") == "cdrom" and path:
                out["cdrom"] = os.path.basename(path)
    return out


def _rewrite_domain_el(dom: ET.Element, vm_dir: Path, names: set, iso_path_for) -> None:
    """In place: point every managed path at `vm_dir/<basename>`, BY NAME ONLY. The source host's
    directory layout is never trusted — only the relative file names in the verified manifest."""
    for md in dom.findall("metadata"):
        dom.remove(md)
    dev = dom.find("devices")
    if dev is not None:
        emu = dev.find("emulator")
        if emu is not None and not os.path.exists((emu.text or "").strip()):
            dev.remove(emu)          # libvirt picks this host's own emulator
        for disk in dev.findall("disk"):
            src = disk.find("source")
            if src is None:
                continue
            if disk.get("device") == "cdrom":
                iso = iso_path_for(os.path.basename(src.get("file") or ""))
                if iso:
                    src.set("file", iso)
                else:
                    disk.remove(src)  # ejected: the installer is not in this host's library
                continue
            base = os.path.basename(src.get("file") or "")
            if base not in names:
                raise MigrationAbort(f"the definition names a disk that was not transferred ({base})")
            src.set("file", str(vm_dir / base))
    # `dumpxml --migratable` omits the VNC passwd; defined without one, QEMU would run the display with NO
    # authentication on this host (and refuse every console ticket). Same rule as vm.update.
    domainxml.secure_vnc(dom, force_loopback=True)
    os_el = dom.find("os")
    if os_el is not None:
        nv = os_el.find("nvram")
        if nv is not None and (nv.text or "").strip():
            base = os.path.basename(nv.text.strip())
            nv.text = str(vm_dir / base)
            if base in names and "template" in nv.attrib:
                del nv.attrib["template"]
        ld = os_el.find("loader")
        if ld is not None and not os.path.exists((ld.text or "").strip()):
            if os_el.get("firmware") in ("efi", "bios"):
                os_el.remove(ld)     # re-resolved by firmware auto-selection on this host
                if nv is not None and "template" in nv.attrib:
                    del nv.attrib["template"]
            else:
                raise MigrationAbort(f"this host has no firmware at {(ld.text or '').strip()}")


def rewrite_domain_xml(xml: str, vm_dir: Path, names: set, meta: domainxml.VmMeta, iso_path_for) -> str:
    root = ET.fromstring(xml)
    _rewrite_domain_el(root, vm_dir, names, iso_path_for)
    out = ET.tostring(root, encoding="unicode")
    md = f"<metadata>{meta.to_xml(prefixed=True)}</metadata>"
    i = out.find("</uuid>")
    if i < 0:
        raise MigrationAbort("the transferred definition has no uuid")
    i += len("</uuid>")
    return out[:i] + md + out[i:]


def rewrite_snapshot_xml(xml: str, vm_dir: Path, names: set, iso_path_for) -> str:
    root = ET.fromstring(xml)
    for tag in ("domain", "inactiveDomain"):
        dom = root.find(tag)
        if dom is not None:
            _rewrite_domain_el(dom, vm_dir, names, iso_path_for)
    return ET.tostring(root, encoding="unicode")


# ====================================================================================== TARGET: rebuild
# THE TARGET NEVER DEFINES WHAT THE SOURCE SENT. A domain definition is root on the host that defines it — a
# `qemu:commandline` (under any prefix) runs anything, a `<serial type="file">` writes any path qemu can, a
# `<kernel>` boots a host file, a static `<seclabel>` turns confinement off, an `<interface type="ethernet">`
# runs a script. A paired host is a peer, not an administrator of this one. So the definition is REBUILT here
# with this host's own generator (domainxml.build_domain_xml) from a whitelisted field set, and everything
# else is dropped — except constructs whose silent loss would change what the machine IS, which are refused.
_MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
_DISK_FILE_RE = re.compile(r"^disk-(vd|sd)[a-z]\.qcow2$")
_NIC_MODELS = ("virtio", "e1000e", "e1000", "rtl8139")
_SNAP_STATES = ("nostate", "running", "blocked", "paused", "shutdown", "shutoff", "crashed", "pmsuspended",
                "disk-snapshot")
_MEM_UNITS = {"b": 1 / 1048576, "bytes": 1 / 1048576, "k": 1 / 1024, "kib": 1 / 1024, "kb": 1000 / 1048576,
              "m": 1, "mib": 1, "mb": 1000 ** 2 / 1048576, "g": 1024, "gib": 1024, "gb": 1000 ** 3 / 1048576,
              "t": 1048576, "tib": 1048576, "tb": 1000 ** 4 / 1048576}
MAX_LABELS = 16
MAX_ASSIGNED = 256


def _mem_mib(el) -> int:
    if el is None:
        return 0
    try:
        n = int((el.text or "0").strip())
    except ValueError:
        return 0
    f = _MEM_UNITS.get((el.get("unit") or "KiB").strip().lower())
    return int(n * f) if f else 0


def validate_incoming_domain(xml: str, *, vm_uuid: str, name: str, disk_names: set, cfg) -> dict:
    """The source's definition → the few facts this host will build from, or MigrationAbort. Nothing in the
    returned dict is a path, a namespace, or anything the source chose beyond the whitelisted values."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        raise MigrationAbort("the transferred definition is not readable XML")
    if _local(root.tag) != "domain":
        raise MigrationAbort("the transferred definition is not a domain")
    info = inspect_domain_xml(xml)
    try:
        Migrator._refusals(info)                      # the SAME refusals the source runs, run again here
    except MigrationError as e:
        raise MigrationAbort(e.message)
    if info["uuid"] != vm_uuid:
        raise MigrationAbort("the transferred definition names a different VM id than the migration")
    if info["name"] != name:
        raise MigrationAbort("the transferred definition names a different VM than the migration")
    os_el = root.find("os")
    typ = os_el.find("type") if os_el is not None else None
    if typ is None or (typ.text or "").strip() != "hvm" or (typ.get("arch") or "x86_64") != "x86_64":
        raise MigrationAbort("only x86_64 hardware-virtualized VMs can be migrated here")
    machine = (typ.get("machine") or "").strip().lower()
    chipset = "pc" if (machine == "pc" or machine.startswith("pc-i440fx")) else "q35"
    vcpus, ram = info["vcpus"], _mem_mib(root.find("memory"))
    if not 1 <= vcpus <= cfg.max_vcpus:
        raise MigrationAbort(f"the VM has {vcpus} vCPUs; this host allows 1-{cfg.max_vcpus}")
    if not 256 <= ram <= cfg.max_ram_mib:
        raise MigrationAbort(f"the VM has {ram} MiB of memory; this host allows 256-{cfg.max_ram_mib}")
    loader = os_el.find("loader")
    efi = (os_el.get("firmware") == "efi" or os_el.find("nvram") is not None
           or (loader is not None and (loader.get("type") == "pflash" or (loader.text or "").strip())))
    dev = root.find("devices")
    disks, seen, cdrom_target = [], set(), ""
    for d in (dev.findall("disk") if dev is not None else []):
        device = d.get("device", "disk")
        tgt = d.find("target")
        target = tgt.get("dev", "") if tgt is not None else ""
        if device == "cdrom":
            if target.startswith("sd") and domainxml._DEV_RE.match(target):
                cdrom_target = cdrom_target or target
            continue                                      # detached: installer media is this host's business
        if device != "disk":
            continue                                      # floppy and friends are dropped with their sources
        src = d.find("source")
        if d.get("type", "file") != "file" or src is None or set(src.attrib) != {"file"}:
            raise MigrationAbort("a disk that is not a plain file cannot be migrated")
        if d.find("backingStore/source") is not None or d.find("mirror") is not None \
                or d.get("snapshot") == "external":
            raise MigrationAbort("a disk with a backing chain or external overlay cannot be migrated")
        base = os.path.basename(src.get("file") or "")
        if base not in disk_names or base in seen:
            raise MigrationAbort(f"the definition names a disk that was not transferred ({base[:64]})")
        if not domainxml._DEV_RE.match(target) or any(x["target"] == target for x in disks):
            raise MigrationAbort("a disk has an unsupported or duplicate target")
        seen.add(base)
        disks.append({"name": base, "target": target})
    if not disks:
        raise MigrationAbort("the transferred definition has no disks")
    nics = []
    for i in (dev.findall("interface") if dev is not None else []):
        if i.get("type") not in ("network", "bridge"):
            continue                                      # ethernet/direct/vhostuser/…: host plumbing, dropped
        mac_el = i.find("mac")
        mac = (mac_el.get("address") or "").strip().lower() if mac_el is not None else ""
        if mac and (not _MAC_RE.match(mac) or int(mac[:2], 16) & 1):
            mac = ""                                      # not a unicast MAC: libvirt assigns a fresh one
        model_el = i.find("model")
        model = model_el.get("type") if model_el is not None else ""
        nics.append({"mac": mac, "model": model if model in _NIC_MODELS else ""})
        if len(nics) >= 8:
            break
    inputs = [x.get("type") for x in (dev.findall("input") if dev is not None else [])]
    return {"uuid": vm_uuid, "name": name, "vcpus": vcpus, "ram_mib": ram, "chipset": chipset,
            "firmware": "efi" if efi else "bios",
            "disks": disks, "cdrom_target": cdrom_target, "nics": nics,
            "input": "tablet" if "tablet" in inputs or not inputs else "mouse"}


def clean_incoming_meta(meta_xml, *, cfg, assign_allow=None) -> domainxml.VmMeta:
    """The source's `pc:vm` → a VmMeta of validated values only. `assign_allow`: when a set, only those
    pubkeys may be carried (see the authorization)."""
    m = domainxml.parse_meta(meta_xml or "") or domainxml.VmMeta()
    owner = str(m.owner or "").lower()
    assigned = [pk for pk in m.assigned if assign_allow is None or pk in assign_allow][:MAX_ASSIGNED]
    labels = []
    for lb in m.labels:
        s = re.sub(r"[\x00-\x1f\x7f]", "", str(lb)).strip()[:64]
        if s and s not in labels:
            labels.append(s)
    return domainxml.VmMeta(owner=owner if _HEX64.match(owner) else "", created=max(0, int(m.created or 0)),
                            guest=m.guest if m.guest in ("linux", "windows") else "linux",
                            firmware=m.firmware if m.firmware in ("efi", "bios") else "efi",
                            disk_gib=max(0, min(int(m.disk_gib or 0), cfg.max_disk_gib * 32)), iso="",
                            assigned=assigned, labels=labels[:MAX_LABELS])


def build_incoming_domain(plan: dict, vm_dir: Path, meta: domainxml.VmMeta, cfg, formats: dict,
                          nvram: bool) -> str:
    """This host's own definition for a validated plan. Every path is `vm_dir/<manifest name>`; the network is
    this host's; the display is one loopback VNC with an expired password; no emulator, no seclabel."""
    meta.firmware = plan["firmware"]
    windows = meta.guest == "windows"
    spec = domainxml.DomainSpec(
        name=plan["name"], uuid=plan["uuid"], guest=meta.guest, firmware=plan["firmware"], vcpus=plan["vcpus"],
        ram_mib=plan["ram_mib"], disk_path=str(vm_dir / plan["disks"][0]["name"]),
        nvram_path=str(vm_dir / "nvram.fd"), iso_path="", network=cfg.default_network, bridge=cfg.bridge,
        meta=meta)
    root = ET.fromstring(domainxml.build_domain_xml(spec))
    if plan["chipset"] == "pc":
        root.find("os/type").set("machine", "pc")
    # No nvram file travelled (`nvram` False): the path still points into vm_dir, and libvirt creates it from
    # this host's own firmware template on first start.
    dev = root.find("devices")
    for el in list(dev.findall("disk")) + list(dev.findall("interface")) + list(dev.findall("tpm")):
        dev.remove(el)
    at = 0
    for dk in plan["disks"]:
        d = ET.Element("disk", {"type": "file", "device": "disk"})
        ET.SubElement(d, "driver", {"name": "qemu", "type": formats[dk["name"]]})
        ET.SubElement(d, "source", {"file": str(vm_dir / dk["name"])})
        ET.SubElement(d, "target", {"dev": dk["target"], "bus": "sata" if dk["target"].startswith("sd") else "virtio"})
        dev.insert(at, d)
        at += 1
    if plan["cdrom_target"] and plan["cdrom_target"] not in {dk["target"] for dk in plan["disks"]}:
        cd = ET.Element("disk", {"type": "file", "device": "cdrom"})
        ET.SubElement(cd, "driver", {"name": "qemu", "type": "raw"})
        ET.SubElement(cd, "target", {"dev": plan["cdrom_target"], "bus": "sata"})
        ET.SubElement(cd, "readonly")
        dev.insert(at, cd)
        at += 1
    for nic in plan["nics"]:
        n = ET.Element("interface", {"type": "bridge" if cfg.bridge else "network"})
        if nic["mac"]:
            ET.SubElement(n, "mac", {"address": nic["mac"]})
        if cfg.bridge:
            ET.SubElement(n, "source", {"bridge": cfg.bridge})
        else:
            ET.SubElement(n, "source", {"network": cfg.default_network or "default"})
        ET.SubElement(n, "model", {"type": nic["model"] or ("e1000e" if windows else "virtio")})
        dev.insert(at, n)
        at += 1
    # The cdrom arrives EMPTY (installer media is this host's library, not the source's path), so the machine
    # boots from its disk; build_domain_xml already wrote exactly that.
    domainxml.set_input(root, plan["input"])
    return domainxml.to_text(root)


def validate_incoming_snapshots(snaps, plan: dict) -> list:
    """The source's snapshot list → [{name, description, state, created, parent, current, disks, memory}] of
    validated values, parents first; MigrationAbort on anything external or malformed."""
    from .backend import valid_snapshot_name
    if not isinstance(snaps, list) or len(snaps) > 256:
        raise MigrationAbort("the manifest's snapshot list is malformed")
    targets = {dk["target"] for dk in plan["disks"]}
    out, names = [], set()
    for s in snaps:
        if not isinstance(s, dict) or not isinstance(s.get("xml"), str):
            raise MigrationAbort("the manifest's snapshot list is malformed")
        try:
            el = ET.fromstring(s["xml"])
        except ET.ParseError:
            raise MigrationAbort("a snapshot definition is not readable XML")
        name = (el.findtext("name") or "").strip()
        if _local(el.tag) != "domainsnapshot" or not valid_snapshot_name(name) or name in names \
                or name != s.get("name"):
            raise MigrationAbort("a snapshot has an invalid or duplicate name")
        mem = el.find("memory")
        if mem is not None and mem.get("snapshot") not in (None, "internal", "no"):
            raise MigrationAbort(f"snapshot {name} keeps its memory outside the disk and cannot be migrated")
        disks = []
        for d in el.findall("disks/disk"):
            mode = d.get("snapshot") or "internal"
            if mode not in ("internal", "no") or d.find("source") is not None:
                raise MigrationAbort(f"snapshot {name} is external (a separate overlay file) and cannot be migrated")
            if d.get("name") in targets and mode == "internal":
                disks.append(d.get("name"))
        parent = (el.findtext("parent/name") or "").strip()
        if parent and parent not in names:
            raise MigrationAbort(f"snapshot {name} names a parent that does not come before it")
        state = (el.findtext("state") or "shutoff").strip()
        if state not in _SNAP_STATES:
            raise MigrationAbort(f"snapshot {name} has an unknown state")
        try:
            created = max(0, int((el.findtext("creationTime") or "0").strip()))
        except ValueError:
            created = 0
        desc = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", el.findtext("description") or "")[:200]
        names.add(name)
        out.append({"name": name, "description": desc, "state": state, "created": created, "parent": parent,
                    "current": bool(s.get("current")), "disks": disks,
                    "memory": "internal" if state in ("running", "paused", "blocked") else "no"})
    return out


def build_incoming_snapshot(snap: dict, domain_xml: str) -> str:
    """Snapshot METADATA only, from validated fields, around this host's own rebuilt definition — never the
    `<domain>` the source embedded."""
    from xml.sax.saxutils import escape
    parts = [f"<domainsnapshot><name>{escape(snap['name'])}</name>"]
    if snap["description"]:
        parts.append(f"<description>{escape(snap['description'])}</description>")
    parts.append(f"<state>{snap['state']}</state><creationTime>{int(snap['created'])}</creationTime>")
    if snap["parent"]:
        parts.append(f"<parent><name>{escape(snap['parent'])}</name></parent>")
    parts.append(f"<memory snapshot='{snap['memory']}'/><disks>")
    parts += [f"<disk name='{d}' snapshot='internal'/>" for d in snap["disks"]]
    parts.append("</disks>" + domain_xml + "</domainsnapshot>")
    return "".join(parts)


def snapshot_is_external(xml: str) -> bool:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return True
    return any(d.get("snapshot") == "external" for d in root.iter("disk")) or \
        root.find("memory") is not None and root.find("memory").get("snapshot") == "external"


# ====================================================================================== files
def sha256_file(path, chunk: int = 4 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def qcow2_has_backing(path) -> bool:
    """Read the qcow2 header directly (magic, version, backing_file_offset). A backing file is a second
    file somewhere on the source host that the copy would silently not carry."""
    try:
        with open(path, "rb") as f:
            hdr = f.read(16)
    except OSError:
        return False
    if len(hdr) < 16 or hdr[:4] != b"QFI\xfb":
        return False
    return int.from_bytes(hdr[8:16], "big") != 0


class Pacer:
    """Keeps a byte stream under `mbps` megabits per second (0 = unlimited)."""

    def __init__(self, mbps: int, clock=time.monotonic):
        self.rate = (mbps * 1_000_000 / 8) if mbps and mbps > 0 else 0
        self.clock = clock
        self.t0 = None
        self.sent = 0

    async def tick(self, n: int) -> None:
        if not self.rate:
            return
        if self.t0 is None:
            self.t0 = self.clock()
        self.sent += n
        ahead = self.sent / self.rate - (self.clock() - self.t0)
        if ahead > 0.002:
            await asyncio.sleep(ahead)


# ====================================================================================== NIP-98
def nip98_header(sk: bytes, url: str, method: str = "GET") -> str:
    ev = nostr_event.build_event(sk, NIP98_KIND, "", [["u", url], ["method", method]])
    return "Nostr " + base64.b64encode(json.dumps(ev).encode()).decode()


def check_transfer_auth(header: Optional[str], expected_path: str, allowed_pubkey: str,
                        public_url: str = "") -> Optional[str]:
    """The repo's own verify_nip98 (signature, method GET, ±60s, signer allowlisted) PLUS an exact URL
    binding: verify_nip98 only checks that the path CONTAINS a needle, and `/…/{mig}/1` contains
    `/…/{mig}/1` as a prefix of `/…/{mig}/10` — a header for one file must not fetch another."""
    from app.services.git_auth import verify_nip98
    from urllib.parse import urlparse
    signer = verify_nip98(header, "GET", expected_path, {allowed_pubkey}, max_skew=NIP98_SKEW)
    if not signer:
        return None
    try:
        ev = json.loads(base64.b64decode(header.strip().split(None, 1)[1], validate=True))
        u = next(t[1] for t in ev.get("tags") or [] if isinstance(t, list) and len(t) >= 2 and t[0] == "u")
        pu = urlparse(u)
    except (StopIteration, ValueError, TypeError, IndexError, AttributeError):
        return None
    if pu.path != expected_path or pu.query or pu.fragment or pu.scheme not in ("https", "http"):
        return None
    if public_url:
        want = urlparse(public_url)
        if (pu.scheme, pu.netloc.lower()) != (want.scheme, want.netloc.lower()):
            return None
    return signer


# ====================================================================================== peer RPC
class RelayRoundtrip:
    """Subscribe FIRST, then publish, then wait — the same order as vmrpc.js, for the same reason."""

    async def roundtrip(self, url: str, event: dict, filt: dict, accept, timeout: float):
        from app.services.nostr import relay as nostr_relay
        sub = _uuid.uuid4().hex[:16]
        try:
            async with nostr_relay._connect(url, True) as ws:
                await ws.send(json.dumps(["REQ", sub, filt]))
                await ws.send(json.dumps(["EVENT", event]))
                loop = asyncio.get_running_loop()
                deadline = loop.time() + timeout
                while True:
                    left = deadline - loop.time()
                    if left <= 0:
                        return None
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=left)
                        msg = json.loads(raw)
                    except asyncio.TimeoutError:
                        return None
                    except ValueError:
                        continue
                    if isinstance(msg, list) and len(msg) >= 3 and msg[0] == "EVENT" and msg[1] == sub:
                        got = accept(msg[2])
                        if got is not None:
                            return got
                    elif isinstance(msg, list) and len(msg) >= 3 and msg[0] == "OK" and msg[1] == event["id"] \
                            and msg[2] is False:
                        logger.info("[vmhost] peer relay %s refused a request: %s", url, msg[3] if len(msg) > 3 else "")
                        return None
        except Exception as e:
            logger.info("[vmhost] peer relay %s unreachable: %s", url, e)
            return None


class PeerRpc:
    """Host → host requests, signed by this node's key. Returns the decrypted response body, or None
    when nobody answered (never conflated with a refusal)."""

    def __init__(self, node_sk: bytes, io):
        self.node_sk = node_sk
        self.io = io

    async def call(self, peer: Peer, op: str, args: dict, *, timeout: float = 20.0, retries: int = 1,
                   req_id: Optional[str] = None) -> Optional[dict]:
        from .transport import build_request
        rid = req_id or _uuid.uuid4().hex
        for _ in range(1 + max(0, retries)):
            ev = build_request(self.node_sk, peer.pubkey, op, args, rid)

            def accept(rev, ev=ev):
                try:
                    if rev.get("pubkey") != peer.pubkey or rev.get("kind") != kinds.RES_KIND:
                        return None
                    if ev["id"] not in kinds.tag_values(rev, "e") or not nostr_event.verify_event(rev):
                        return None
                    body = json.loads(nip44.decrypt_from(self.node_sk, bytes.fromhex(peer.pubkey), rev["content"]))
                    return body if isinstance(body, dict) and body.get("id") == rid else None
                except Exception:
                    return None
            got = await self.io.roundtrip(peer.relay, ev, {"kinds": [kinds.RES_KIND], "authors": [peer.pubkey],
                                                           "#e": [ev["id"]]}, accept, timeout)
            if got is not None:
                return got
        return None


# ====================================================================================== the migrator
def _now_i() -> int:
    return int(time.time())


class Migrator:
    def __init__(self, svc, node_sk: bytes, mcfg: MigrateConfig, *, rpc=None, publish=None,
                 http_client=None, timing: Optional[Timing] = None):
        self.svc = svc
        self.backend = svc.backend
        self.storage = svc.storage
        self.node_sk = node_sk
        self.node_pk = bip340.pubkey_from_seckey(node_sk).hex()
        self.mcfg = mcfg
        self.rpc = rpc or PeerRpc(node_sk, RelayRoundtrip())
        self.publish = publish
        self.http_client = http_client or self._default_http
        self.t = timing or Timing()
        self.store = MigrationStore(self.storage.state_dir / "migrations")
        self.store.load()
        self._locks: dict = {}
        self._tasks: set = set()
        self._live: dict = {}            # mig -> {"phase","msg","bytes","total","at"}
        self._last_notify: dict = {}
        self.closed = False

    @staticmethod
    def _default_http():
        import httpx
        return httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=15.0), trust_env=False,
                                 follow_redirects=False)

    # ------------------------------------------------------------------ small helpers
    def peer(self, pk) -> Optional[Peer]:
        pk = str(pk or "").lower()
        return next((p for p in self.mcfg.peers if p.pubkey == pk), None)

    def is_peer(self, pk) -> bool:
        return self.peer(pk) is not None

    def _lock(self, mig: str) -> asyncio.Lock:
        lk = self._locks.get(mig)
        if lk is None:
            lk = self._locks[mig] = asyncio.Lock()
        return lk

    def _spawn(self, coro) -> None:
        if self.closed:
            coro.close()
            return
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def close(self) -> None:
        self.closed = True
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _save(self, rec: dict) -> None:
        rec["updated"] = _now_i()
        snapshot = json.loads(json.dumps(rec))      # what reaches disk is what this moment decided
        await asyncio.to_thread(self.store.write, snapshot)
        self.store.put(rec)

    def active_for(self, vm_uuid: str) -> Optional[dict]:
        for r in self.store.all():
            if r.get("vm") == vm_uuid and r.get("state") not in FINAL:
                return r
        return None

    def migrated_away(self, vm_uuid: str) -> Optional[dict]:
        """The LATEST migration of this VM, when it says the VM left this host: a source record that reached the
        commit point and was not taken back. A VM that moved away and later came back has a newer TARGET record,
        which is what makes it startable here again."""
        recs = [r for r in self.store.all() if r.get("vm") == vm_uuid]
        if not recs:
            return None
        latest = max(recs, key=lambda r: (r.get("created", 0), r.get("updated", 0)))
        if latest["role"] == "source" and latest["state"] in ("handed_off", "locked", "done", "released"):
            return latest
        return None

    def blocks_start(self, vm_uuid: str) -> Optional[str]:
        r = self.active_for(vm_uuid)
        if r is None:
            if self.migrated_away(vm_uuid):
                return "this VM was migrated to another host — it cannot be started here"
            return None
        if r["role"] == "source":
            return "this VM is being migrated to another host — it cannot be started here"
        return "this VM is arriving from another host and is not committed yet — it cannot be started"

    def summary(self, rec: dict) -> dict:
        live = self._live.get(rec["id"], {})
        total = int(rec.get("bytes_total") or 0)
        done = int(live.get("bytes", rec.get("bytes_done", 0)) or 0)
        return {"id": rec["id"], "role": rec["role"], "state": rec["state"], "vm": rec.get("vm"),
                "name": rec.get("name"), "source": rec.get("source"), "target": rec.get("target"),
                "start_after": bool(rec.get("start_after")), "bytes_total": total, "bytes_done": done,
                "pct": (round(100 * done / total, 1) if total else (100.0 if rec["state"] == "done" else 0.0)),
                "phase": live.get("phase", rec["state"]), "msg": live.get("msg", ""),
                "error": rec.get("error", ""), "created": rec.get("created", 0), "updated": rec.get("updated", 0),
                "locked_at": rec.get("locked_at", 0), "retained": bool(rec.get("retained")) and not rec.get("retained_reaped"),
                "acked": bool(rec.get("acked_at")), "warning": rec.get("warning", "")}

    async def _notify(self, rec: dict, phase: str, msg: str = "", *, bytes_done=None, force=False) -> None:
        live = self._live.setdefault(rec["id"], {})
        live.update({"phase": phase, "msg": msg, "at": time.time()})
        if bytes_done is not None:
            live["bytes"] = bytes_done
        if not self.publish or not rec.get("requester") or not rec.get("authz_id"):
            return
        now = time.monotonic()
        if not force and now - self._last_notify.get(rec["id"], 0) < self.t.progress_every:
            return
        self._last_notify[rec["id"]] = now
        from .transport import build_reply
        payload = {"v": 1, "id": rec["id"], "progress": dict(self.summary(rec), side=rec["role"],
                                                             phase=phase, msg=msg)}
        try:
            ev = build_reply(self.node_sk, rec["authz_id"], rec["requester"], payload, kind=kinds.PROGRESS_KIND)
            await self.publish(ev)
        except Exception as e:
            logger.debug("[vmhost] migration progress not published: %s", e)

    async def _peer_call(self, pk: str, op: str, args: dict, retries=None, timeout=None) -> Optional[dict]:
        p = self.peer(pk)
        if p is None:
            return None
        return await self.rpc.call(p, op, args, timeout=timeout or self.t.rpc_timeout,
                                   retries=self.t.rpc_retries if retries is None else retries)

    # ------------------------------------------------------------------ op dispatch
    async def handle_op(self, op: str, pk: str, role: str, args: dict, progress) -> dict:
        name = "_op_" + op.replace(".", "_")
        fn = getattr(self, name, None)
        if fn is None:
            raise MigrationError("unsupported", f"unknown operation {op}")
        return await fn(pk, args)

    def _rec_arg(self, args: dict, role: Optional[str] = None) -> dict:
        mig = args.get("migration")
        rec = self.store.get(mig) if isinstance(mig, str) and _MIG_RE.match(mig) else None
        if rec is None or (role and rec["role"] != role):
            raise MigrationError("not_found", "no such migration")
        return rec

    # ================================================================== SOURCE: client ops
    async def _plan(self, pk: str, args: dict) -> tuple:
        from .service import _to_hex
        target = _to_hex(args.get("target"))
        if not target:
            raise MigrationError("bad_request", "target must be the target host's npub")
        if target == self.node_pk:
            raise MigrationError("bad_request", "a VM cannot be migrated to the host it is on")
        peer = self.peer(target)
        if peer is None:
            raise MigrationError("forbidden", "that host is not paired with this one (vmhost_peer_hosts on both hosts)")
        d = await self.svc._domain(pk, "admin", args)
        if self.active_for(d.uuid):
            raise MigrationError("migrating", "this VM already has a migration in progress")
        if self.migrated_away(d.uuid):
            raise MigrationError("migrating", "this VM was migrated to another host")
        if d.meta is None or not self.storage.is_managed_dir(d.uuid):
            raise MigrationError("unsupported", "only VMs created by PosterChan can be migrated")
        authz = args.get("authz")
        if not isinstance(authz, dict) or str(authz.get("pubkey", "")).lower() != pk \
                or authz.get("kind") != kinds.REQ_KIND or target not in kinds.tag_values(authz, "p") \
                or not nostr_event.verify_event(authz):
            raise MigrationError("bad_request", "the migration needs your signed authorization for the target host")
        xml = await self.backend.dumpxml_inactive(d.uuid)
        info = inspect_domain_xml(xml)
        self._refusals(info)
        files = await asyncio.to_thread(self._collect_files, d.uuid, info)
        return d, peer, info, files, authz

    @staticmethod
    def _refusals(info: dict) -> None:
        if info["tpm"]:
            raise MigrationError("unsupported",
                                 "VMs with a TPM (every Windows VM) cannot be migrated yet: the TPM state is "
                                 "root-owned and lives outside the VM's directory, so it would not travel")
        if info["unsupported"]:
            raise MigrationError("unsupported", "VMs with " + " and ".join(info["unsupported"]) + " cannot be migrated")
        for dk in info["disks"]:
            if dk["device"] == "cdrom":
                continue
            if dk["type"] != "file":
                raise MigrationError("unsupported", f"a {dk['type']} disk cannot be migrated — only file disks")
            if dk["backing"]:
                raise MigrationError("unsupported", "a disk with a backing chain cannot be migrated")

    def _collect_files(self, vm_uuid: str, info: dict) -> list:
        vm_dir = self.storage.vm_dir(vm_uuid)
        real = vm_dir.resolve()
        files, seen = [], set()

        def add(path: str, role: str, required: bool):
            if not path:
                return
            p = Path(path)
            if p.parent.resolve() != real or not _FILE_RE.match(p.name):
                raise MigrationError("unsupported", f"{p.name} is not inside the VM's own directory, so it would not travel")
            full = vm_dir / p.name
            try:
                st = os.lstat(full)
            except FileNotFoundError:
                if required:
                    raise MigrationError("conflict", f"{p.name} is missing on this host")
                return
            import stat as _st
            if not _st.S_ISREG(st.st_mode):
                raise MigrationError("unsupported", f"{p.name} is not a regular file")
            if role == "disk" and qcow2_has_backing(full):
                raise MigrationError("unsupported", f"{p.name} has a qcow2 backing file, which would not travel")
            if p.name in seen:
                return
            seen.add(p.name)
            files.append({"i": len(files), "name": p.name, "size": st.st_size, "role": role})

        for dk in info["disks"]:
            if dk["device"] != "cdrom":
                add(dk["source"], "disk", True)
        add(info["nvram"], "nvram", False)
        if not files:
            raise MigrationError("unsupported", "this VM has no disks to migrate")
        return files

    def _precheck_args(self, mig, d, info, files, authz, start_after, dry_run) -> dict:
        return {"migration": mig, "dry_run": bool(dry_run), "authz": authz, "start_after": bool(start_after),
                "vm": {"uuid": d.uuid, "name": d.name, "vcpus": d.vcpus, "ram_mib": d.ram_mib,
                       "total_bytes": sum(f["size"] for f in files), "loader": info["loader"],
                       "firmware_auto": info["firmware_auto"], "iso": info["cdrom"]}}

    async def _op_vm_migrate_precheck(self, pk, args):
        d, peer, info, files, authz = await self._plan(pk, args)
        res = await self.rpc.call(peer, "peer.migrate.precheck",
                                  self._precheck_args("0" * 32, d, info, files, authz, args.get("start_after"), True),
                                  timeout=self.t.rpc_timeout, retries=self.t.rpc_retries)
        if res is None:
            raise MigrationError("timeout", "the target host did not answer")
        if not res.get("ok"):
            e = res.get("error") or {}
            raise MigrationError(e.get("code", "internal"), "the target host refused: " + str(e.get("message", "")))
        return {"source": {"vm": d.uuid, "name": d.name, "state": d.state, "files": len(files),
                           "total_bytes": sum(f["size"] for f in files)},
                "target": res.get("result", {})}

    async def _op_vm_migrate(self, pk, args):
        d, peer, info, files, authz = await self._plan(pk, args)
        mig = _uuid.uuid4().hex
        lock = self.svc._vm_lock(d.uuid)
        await self.svc._acquire(lock, "this VM")
        try:
            if self.active_for(d.uuid):
                raise MigrationError("migrating", "this VM already has a migration in progress")
            res = await self.rpc.call(peer, "peer.migrate.precheck",
                                      self._precheck_args(mig, d, info, files, authz, args.get("start_after"), False),
                                      timeout=self.t.rpc_timeout, retries=self.t.rpc_retries)
            if res is None:
                raise MigrationError("timeout", "the target host did not answer the precheck — nothing was changed")
            if not res.get("ok"):
                e = res.get("error") or {}
                raise MigrationError(e.get("code", "internal"), "the target host refused: " + str(e.get("message", "")))
            rec = {"id": mig, "role": "source", "state": "planned", "vm": d.uuid, "name": d.name,
                   "source": self.node_pk, "target": peer.pubkey, "requester": pk, "authz_id": authz["id"],
                   "start_after": bool(args.get("start_after")), "force_shutdown": bool(args.get("force_shutdown")),
                   "bytes_total": sum(f["size"] for f in files), "created": _now_i(), "history": []}
            await self._save(rec)
        finally:
            lock.release()
        logger.info("[vmhost] migration %s: %s (%s) → %s by %s", mig, d.name, d.uuid, peer.pubkey[:12], pk[:12])
        self._spawn(self._run_source(mig))
        return {"migration": self.summary(rec), "precheck": res.get("result", {})}

    async def _op_vm_migrate_status(self, pk, args):
        recs = self.store.all()
        if args.get("migration"):
            recs = [self._rec_arg(args)]
        elif args.get("vm"):
            recs = [r for r in recs if r.get("vm") == valid_uuid(args.get("vm"))]
        else:
            cutoff = _now_i() - 7 * 86400
            recs = [r for r in recs if r["state"] not in FINAL or r.get("updated", 0) >= cutoff]
        recs.sort(key=lambda r: r.get("created", 0), reverse=True)
        return {"migrations": [self.summary(r) for r in recs[:50]],
                "peers": [{"pubkey": p.pubkey, "relay": p.relay, "https": p.https} for p in self.mcfg.peers]}

    async def _op_vm_migrate_cancel(self, pk, args):
        rec = self._rec_arg(args)
        if rec["role"] != "source":
            raise MigrationError("unsupported", "cancel a migration on its SOURCE host")
        if rec["state"] not in SOURCE_PRE_HANDOFF:
            if rec["state"] in ("handed_off", "locked"):
                raise MigrationError("conflict", "the VM was already handed off to the target — it can no longer be cancelled")
            raise MigrationError("conflict", f"the migration is already {rec['state']}")
        await self._abort_source(rec["id"], "cancelled by an admin", tell_target=True)
        rec = self.store.get(rec["id"])
        if rec["state"] != "aborted":
            raise MigrationError("conflict", f"the migration could not be cancelled (it is {rec['state']})")
        return {"migration": self.summary(rec)}

    async def _op_vm_migrate_force_reclaim(self, pk, args):
        rec = self._rec_arg(args)
        side = args.get("side")
        if side not in ("source", "target"):
            raise MigrationError("bad_request", "side must be source or target — the host that KEEPS the VM")
        if args.get("confirm") != "split-brain":
            raise MigrationError("bad_request", "force reclaim must be confirmed (confirm: split-brain)")
        async with self._lock(rec["id"]):
            rec = self.store.get(rec["id"])
            if rec["state"] != "locked":
                raise MigrationError("conflict", "force reclaim is only for a LOCKED migration (the other host "
                                                 f"could not be reached); this one is {rec['state']}")
            warning = ("SPLIT-BRAIN RISK: this decision was made without the other host. If the other host "
                       "also keeps or starts this VM, two copies will run with the same identity and "
                       "diverging disks. Make the matching choice on the other host.")
            rec["forced"] = {"side": side, "by": pk, "at": _now_i()}
            rec["warning"] = warning
            if rec["role"] == "source":
                if side == "source":
                    await self._reclaim_on_source(rec)
                    rec["state"] = "reclaimed"
                else:
                    rec["state"] = "released"      # the retained copy stays until an admin removes it
            else:
                if side == "target":
                    rec["state"] = "committed"
                    await self._save(rec)
                    await self._finalize_target_locked(rec, forced=True)
                    rec = self.store.get(rec["id"])
                else:
                    await self._drop_target_copy(rec)
                    rec["state"] = "released"
            await self._save(rec)
        logger.warning("[vmhost] migration %s force-reclaimed (%s keeps the VM) by %s on the %s", rec["id"],
                       side, pk[:12], rec["role"])
        return {"migration": self.summary(rec), "warning": warning}

    # ================================================================== SOURCE: the run
    async def _transition(self, mig: str, frm: tuple, to: str, **kw) -> dict:
        async with self._lock(mig):
            rec = self.store.get(mig)
            if rec is None or rec["state"] not in frm:
                if rec is not None and rec["state"] in FINAL:
                    raise Superseded()
                raise MigrationAbort(f"unexpected state {rec and rec['state']}")
            rec["state"] = to
            rec.update(kw)
            rec.setdefault("history", []).append([_now_i(), to])
            await self._save(rec)
            return rec

    async def _run_source(self, mig: str) -> None:
        try:
            await self._quiesce(mig)
            await self._export(mig)
            await self._begin(mig)
        except Superseded:
            return
        except Exception as e:
            msg = getattr(e, "message", None) or str(e) or type(e).__name__
            logger.warning("[vmhost] migration %s failed before handoff: %s", mig, msg,
                           exc_info=not isinstance(e, (MigrationAbort, MigrationError)))
            await self._abort_source(mig, msg, tell_target=True)
            return
        await self._watch_source(mig)

    async def _quiesce(self, mig: str) -> None:
        rec = await self._transition(mig, ("planned",), "quiescing")
        d = await self.backend.get(rec["vm"])
        if d is None:
            raise MigrationAbort("the VM disappeared")
        was_running = d.state in ("running", "paused", "stopping")
        rec.update(was_running=was_running, autostart=bool(d.autostart), quiesced=True)
        await self._save(rec)
        meta = d.meta or domainxml.VmMeta()
        meta.migration = {"id": mig, "state": "outgoing", "peer": rec["target"]}
        await self.backend.set_metadata(d.uuid, meta, live=was_running)
        if d.autostart:
            await self.backend.set_autostart(d.uuid, False)
        self.svc.consoles.revoke(d.uuid)
        if not was_running:
            return
        await self._notify(rec, "shutdown", "shutting the VM down", force=True)
        try:
            await self.backend.shutdown(d.uuid)
        except BackendError:
            pass
        deadline = time.monotonic() + self.mcfg.shutdown_timeout_sec
        while time.monotonic() < deadline:
            cur = await self.backend.get(d.uuid)
            if cur is None or cur.state == "shutoff":
                return
            await asyncio.sleep(min(self.t.shutdown_poll, max(0.01, deadline - time.monotonic())))
        cur = await self.backend.get(d.uuid)
        if cur is not None and cur.state != "shutoff":
            if not rec.get("force_shutdown"):
                raise MigrationAbort(f"the VM did not shut down within {self.mcfg.shutdown_timeout_sec}s — "
                                     "shut it down from inside, or migrate with a forced power-off")
            await self._notify(rec, "shutdown", "forcing the VM off", force=True)
            await self.backend.destroy(d.uuid)

    async def _export(self, mig: str) -> None:
        rec = await self._transition(mig, ("quiescing",), "exporting")
        d = await self.backend.get(rec["vm"])
        if d is None:
            raise MigrationAbort("the VM disappeared")
        if d.state != "shutoff":
            raise MigrationAbort("the VM is running again — refusing to copy a live disk")
        xml = await self.backend.dumpxml_inactive(d.uuid)
        info = inspect_domain_xml(xml)
        try:
            self._refusals(info)
            files = await asyncio.to_thread(self._collect_files, d.uuid, info)
        except MigrationError as e:
            raise MigrationAbort(e.message)
        vm_dir = self.storage.vm_dir(d.uuid)
        total = sum(f["size"] for f in files)
        hashed = 0
        for f in files:
            await self._notify(rec, "export", f"checksumming {f['name']}", bytes_done=hashed)
            f["sha256"] = await asyncio.to_thread(sha256_file, vm_dir / f["name"])
            hashed += f["size"]
        names, current = await self.backend.snapshot_names(d.uuid)
        snaps = []
        for n in names:
            sx = await self.backend.snapshot_dumpxml(d.uuid, n)
            if snapshot_is_external(sx):
                raise MigrationAbort(f"snapshot {n} is external (a separate overlay file) and cannot be migrated")
            snaps.append({"name": n, "xml": sx, "current": n == current})
        meta = d.meta or domainxml.VmMeta()
        meta.migration = {}
        manifest = {"v": 1, "migration": mig, "source": self.node_pk, "target": rec["target"],
                    "vm": {"uuid": d.uuid, "name": d.name}, "xml": xml, "meta": meta.to_xml(prefixed=True),
                    "snapshots": snaps, "autostart": bool(rec.get("autostart")),
                    "start_after": bool(rec.get("start_after")), "files": files, "created": _now_i()}
        raw = json.dumps(manifest, sort_keys=True).encode()
        sha = hashlib.sha256(raw).digest()
        await asyncio.to_thread(self.store.write_manifest, mig, raw)
        async with self._lock(mig):
            rec = self.store.get(mig)
            rec.update(files=files, bytes_total=total, manifest_sha256=sha.hex(),
                       manifest_sig=bip340.sign(sha, self.node_sk).hex())
            await self._save(rec)
        await self._notify(rec, "export", "exported", bytes_done=0, force=True)

    async def _begin(self, mig: str) -> None:
        rec = await self._transition(mig, ("exporting",), "transferring")
        res = await self._peer_call(rec["target"], "peer.migrate.begin",
                                    {"migration": mig, "manifest_sha256": rec["manifest_sha256"],
                                     "manifest_sig": rec["manifest_sig"]})
        if res is None:
            raise MigrationAbort("the target host did not answer — the VM stays here")
        if not res.get("ok"):
            raise MigrationAbort("the target host refused to start the transfer: " +
                                 str((res.get("error") or {}).get("message", "")))
        await self._notify(rec, "transfer", "the target is copying the disks", force=True)

    async def _watch_source(self, mig: str) -> None:
        """Until the migration is final: before handoff, notice a target that gave up; after it, learn
        that the target finished (the ack), or lock when it cannot be reached."""
        while not self.closed:
            rec = self.store.get(mig)
            if rec is None or rec["state"] in FINAL:
                return
            st = rec["state"]
            if st not in ("transferring", "handed_off", "locked"):
                return
            res = await self._peer_call(rec["target"], "peer.migrate.status", {"migration": mig}, retries=0)
            tstate = (res.get("result") or {}).get("state") if res and res.get("ok") else None
            rec = self.store.get(mig)
            st = rec["state"]
            if st == "transferring":
                if tstate in ("aborted", "unknown", "released"):
                    await self._abort_source(mig, "the target host abandoned the migration", tell_target=False)
                    return
            elif st in ("handed_off", "locked"):
                if not rec.get("handoff_complete"):
                    await self._retry_handoff(mig)
                if tstate == "done":
                    await self._mark_acked(mig)
                    return
                if tstate in ("aborted", "unknown", "released"):
                    # The target ANSWERED that it does not hold the VM: taking it back cannot split it.
                    async with self._lock(mig):
                        rec = self.store.get(mig)
                        if rec["state"] in ("handed_off", "locked"):
                            await self._reclaim_on_source(rec)
                            rec["state"] = "reclaimed"
                            rec["error"] = f"the target reported {tstate} after the handoff — the VM was taken back"
                            await self._save(rec)
                    return
                if tstate is None and st == "handed_off" and \
                        time.time() - rec.get("handed_at", time.time()) > self.t.contact_deadline:
                    async with self._lock(mig):
                        rec = self.store.get(mig)
                        if rec["state"] == "handed_off":
                            rec.update(state="locked", locked_at=_now_i(),
                                       error="the target host cannot be reached to confirm the handoff")
                            await self._save(rec)
                            await self._notify(rec, "locked", rec["error"], force=True)
            await asyncio.sleep(self.t.watch_poll)

    async def _abort_source(self, mig: str, reason: str, tell_target: bool) -> None:
        async with self._lock(mig):
            rec = self.store.get(mig)
            if rec is None or rec["state"] not in SOURCE_PRE_HANDOFF:
                return
            rec.update(state="aborted", error=reason, aborted_at=_now_i())
            rec.setdefault("history", []).append([_now_i(), "aborted"])
            await self._save(rec)
        try:
            d = await self.backend.get(rec["vm"])
            if d is not None:
                if d.meta is not None and d.meta.migration.get("id") == mig:
                    d.meta.migration = {}
                    await self.backend.set_metadata(d.uuid, d.meta, live=d.state == "running")
                if rec.get("autostart"):
                    await self.backend.set_autostart(d.uuid, True)
                if rec.get("was_running") and rec.get("quiesced") and d.state == "shutoff":
                    await self.backend.start(d.uuid)
        except Exception as e:
            logger.warning("[vmhost] migration %s: restoring the VM after abort failed: %s", mig, e)
            rec["warning"] = f"the VM could not be fully restored: {e}"
            await self._save(rec)
        await self._notify(rec, "aborted", reason, force=True)
        if tell_target:
            self._spawn(self._peer_call(rec["target"], "peer.migrate.abort", {"migration": mig, "reason": reason[:200]},
                                        retries=1))

    async def _complete_handoff(self, rec: dict) -> None:
        """Idempotent: whatever of it already happened (before a crash) is skipped. Finished only when libvirt
        CONFIRMS the domain is gone — an undefine that failed, or answered ok and left it, raises, and the commit,
        the ack and every watch tick try again. Until then start is refused here (migrated_away) and the source
        does not enter `done`."""
        vm = rec["vm"]
        self.svc.consoles.revoke(vm)
        d = await self.backend.get(vm)
        if d is not None:
            if d.state != "shutoff":
                await self.backend.destroy(vm)
            await self.backend.undefine_for_migration(vm, keep_nvram=True)
            if await self.backend.get(vm) is not None:
                raise BackendError("the VM is still defined on the source after undefine")
        self.svc._assign.pop(vm, None)
        if not rec.get("retained"):
            rec["retained"] = f"{vm}-{_now_i()}"
            await self._save(rec)
        src = self.storage.root / vm
        dst = self.storage.root / ".retained" / rec["retained"]

        def move():
            if src.is_dir() and not src.is_symlink() and not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
                os.rename(src, dst)
        await asyncio.to_thread(move)
        if not rec.get("handoff_complete"):
            rec["handoff_complete"] = True
            await self._save(rec)

    async def _retry_handoff(self, mig: str) -> bool:
        async with self._lock(mig):
            rec = self.store.get(mig)
            if rec is None or rec["state"] not in ("handed_off", "locked") or rec.get("handoff_complete"):
                return bool(rec and rec.get("handoff_complete"))
            try:
                await self._complete_handoff(rec)
                return True
            except Exception as e:
                logger.warning("[vmhost] migration %s: completing the handoff failed (will retry): %s", mig, e)
                return False

    async def _reclaim_on_source(self, rec: dict) -> None:
        """Put a handed-off VM back on this host from the retained copy. Caller holds the lock."""
        vm = rec["vm"]
        vm_dir = self.storage.root / vm
        ret = self.storage.root / ".retained" / (rec.get("retained") or "")

        def move_back():
            if not vm_dir.exists() and rec.get("retained") and ret.is_dir():
                os.rename(ret, vm_dir)
        await asyncio.to_thread(move_back)
        if not vm_dir.is_dir():
            raise MigrationError("conflict", "the retained copy of this VM is gone — it cannot be reclaimed here")
        if await self.backend.get(vm) is None:
            manifest = await asyncio.to_thread(self._read_manifest, rec)
            await self._define_from_manifest(manifest, vm_dir, migration={})
            if manifest.get("autostart"):
                await self.backend.set_autostart(vm, True)
        d = await self.backend.get(vm)
        if d is not None:
            self.svc._set_index(d)

    async def _mark_acked(self, mig: str) -> None:
        await self._retry_handoff(mig)
        async with self._lock(mig):
            rec = self.store.get(mig)
            if rec is None or rec["state"] not in ("handed_off", "locked"):
                return
            if not rec.get("handoff_complete"):
                return                        # never `done` while the VM may still be defined here
            rec.update(state="done", acked_at=_now_i(), error="")
            rec.setdefault("history", []).append([_now_i(), "done"])
            await self._save(rec)
        await self._notify(rec, "done", "the VM now lives on the target host", force=True)

    # ================================================================== SOURCE: peer ops (from the target)
    def _peer_rec(self, pk: str, args: dict, role: str) -> Optional[dict]:
        mig = args.get("migration")
        rec = self.store.get(mig) if isinstance(mig, str) and _MIG_RE.match(mig) else None
        if rec is None or rec["role"] != role:
            return None
        if pk != (rec["target"] if role == "source" else rec["source"]):
            return None
        return rec

    async def _op_peer_migrate_status(self, pk, args):
        rec = self._peer_rec(pk, args, "source") or self._peer_rec(pk, args, "target")
        if rec is None:
            return {"state": "unknown"}
        return {"state": rec["state"], "role": rec["role"]}

    async def _op_peer_migrate_commit(self, pk, args):
        rec = self._peer_rec(pk, args, "source")
        if rec is None:
            raise MigrationError("not_found", "no such migration")
        async with self._lock(rec["id"]):
            rec = self.store.get(rec["id"])
            st = rec["state"]
            if st in ("handed_off", "locked") and not rec.get("handoff_complete"):
                await self._complete_handoff(rec)          # a failure answers an error: the target asks again
            if st in ("handed_off", "locked", "done", "released"):
                return {"state": "handed_off"}
            if st in ("aborted", "reclaimed"):
                raise MigrationError("aborted", "the source abandoned this migration")
            if st != "transferring":
                raise MigrationError("conflict", f"not ready to hand off ({st})")
            # THE COMMIT POINT. Journaled before anything is undone here.
            rec.update(state="handed_off", handed_at=time.time())
            rec.setdefault("history", []).append([_now_i(), "handed_off"])
            await self._save(rec)
            await self._complete_handoff(rec)
        await self._notify(rec, "handoff", "handed off to the target", force=True)
        return {"state": "handed_off"}

    async def _op_peer_migrate_ack(self, pk, args):
        rec = self._peer_rec(pk, args, "source")
        if rec is None:
            raise MigrationError("not_found", "no such migration")
        await self._mark_acked(rec["id"])
        rec = self.store.get(rec["id"])
        if rec["state"] in ("handed_off", "locked") and not rec.get("handoff_complete"):
            raise MigrationError("busy", "the source is still removing its copy of the VM — ask again")
        return {"state": rec["state"]}

    async def _op_peer_migrate_abort(self, pk, args):
        rec = self._peer_rec(pk, args, "source")
        if rec is not None:
            if rec["state"] in ("handed_off", "locked", "done"):
                raise MigrationError("conflict", "already handed off")
            await self._abort_source(rec["id"], "the target host aborted: " + str(args.get("reason", ""))[:200],
                                     tell_target=False)
            return {"state": self.store.get(rec["id"])["state"]}
        rec = self._peer_rec(pk, args, "target")
        if rec is None:
            return {"state": "unknown"}
        await self._abort_target(rec["id"], "the source host aborted: " + str(args.get("reason", ""))[:200],
                                 tell_source=False, allow_locked=True)
        return {"state": self.store.get(rec["id"])["state"]}

    # ================================================================== source: serving the transfer
    async def serve_transfer(self, mig: str, index: str, authorization: Optional[str], range_header: Optional[str],
                             path: str):
        from starlette.responses import PlainTextResponse, StreamingResponse
        rec = self.store.get(mig) if _MIG_RE.match(mig or "") else None
        if rec is None or rec["role"] != "source":
            return PlainTextResponse("no such migration", status_code=404)
        expected = TRANSFER_PATH.format(mig=mig, index=index)
        if path != expected or not check_transfer_auth(authorization, expected, rec["target"],
                                                       self.svc.cfg.public_url):
            return PlainTextResponse("unauthorized", status_code=401)
        if rec["state"] not in ("exporting", "transferring"):
            return PlainTextResponse("this migration is not transferring", status_code=409)
        mpath = self.store.manifest_path(mig)
        if index == "manifest":
            if not rec.get("manifest_sha256"):
                return PlainTextResponse("not exported yet", status_code=409)
            data = await asyncio.to_thread(mpath.read_bytes)
            return StreamingResponse(iter([data]), media_type="application/json",
                                     headers={"Content-Length": str(len(data)), "Cache-Control": "no-store"})
        if not re.fullmatch(r"\d{1,4}", index or ""):
            return PlainTextResponse("no such file", status_code=404)
        files = rec.get("files") or []
        i = int(index)
        if i >= len(files):
            return PlainTextResponse("no such file", status_code=404)
        f = files[i]
        try:
            full = self.storage.vm_dir(rec["vm"]) / f["name"]
            if full.is_symlink() or not _FILE_RE.match(f["name"]):
                raise PathEscape("symlink")
            size = (await asyncio.to_thread(os.stat, full)).st_size
        except (PathEscape, OSError):
            return PlainTextResponse("no such file", status_code=404)
        if size != f["size"]:
            return PlainTextResponse("the file changed since it was exported", status_code=409)
        start, end, status = 0, size - 1, 200
        if range_header:
            m = re.fullmatch(r"bytes=(\d+)-(\d*)", range_header.strip())
            if not m:
                return PlainTextResponse("bad range", status_code=416, headers={"Content-Range": f"bytes */{size}"})
            start = int(m.group(1))
            end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
            if start >= size or end < start:
                return PlainTextResponse("range not satisfiable", status_code=416,
                                         headers={"Content-Range": f"bytes */{size}"})
            status = 206
        chunk = self.t.chunk
        mbps = self.mcfg.transfer_max_mbps

        async def body():
            fh = await asyncio.to_thread(open, full, "rb")
            try:
                await asyncio.to_thread(fh.seek, start)
                left = end - start + 1
                pacer = Pacer(mbps)
                served = start
                while left > 0:
                    cur = self.store.get(mig)
                    if cur is None or cur["state"] not in ("exporting", "transferring"):
                        return               # aborted mid-stream: a short body the target will not accept
                    b = await asyncio.to_thread(fh.read, min(chunk, left))
                    if not b:
                        return
                    left -= len(b)
                    served += len(b)
                    yield b
                    await pacer.tick(len(b))
            finally:
                await asyncio.to_thread(fh.close)

        headers = {"Content-Length": str(end - start + 1), "Accept-Ranges": "bytes", "Cache-Control": "no-store",
                   "X-Accel-Buffering": "no"}
        if status == 206:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        return StreamingResponse(body(), status_code=status, media_type="application/octet-stream", headers=headers)

    # ================================================================== TARGET: peer ops (from the source)
    def verify_authz(self, ev, source: str, vm_uuid: str) -> str:
        """The admin's signed `vm.migrate.authorize` → the admin's pubkey, or MigrationError."""
        bad = MigrationError("forbidden", "the migration request is not authorized by an admin of this host")
        if not isinstance(ev, dict) or ev.get("kind") != kinds.REQ_KIND or self.node_pk not in kinds.tag_values(ev, "p"):
            raise bad
        if not nostr_event.verify_event(ev):
            raise bad
        try:
            if abs(time.time() - int(ev.get("created_at", 0))) > AUTHZ_MAX_AGE:
                raise MigrationError("forbidden", "the migration authorization has expired — try again")
            body = json.loads(nip44.decrypt_from(self.node_sk, bytes.fromhex(ev["pubkey"]), ev["content"]))
        except MigrationError:
            raise
        except Exception:
            raise bad
        a = body.get("args") if isinstance(body, dict) else None
        if not isinstance(a, dict) or body.get("op") != AUTHZ_OP or a.get("source") != source \
                or a.get("target") != self.node_pk or a.get("vm") != vm_uuid:
            raise bad
        return str(ev["pubkey"]).lower()

    async def _op_peer_migrate_precheck(self, pk, args):
        mig = args.get("migration")
        vm = args.get("vm") if isinstance(args.get("vm"), dict) else {}
        dry = bool(args.get("dry_run"))
        vm_uuid = valid_uuid(vm.get("uuid"))
        from .storage import clean_name
        name = clean_name(vm.get("name"))
        if not isinstance(mig, str) or not _MIG_RE.match(mig) or not vm_uuid or not name or name != vm.get("name"):
            raise MigrationError("bad_request", "malformed precheck")
        admin = self.verify_authz(args.get("authz"), pk, vm_uuid)
        if await self.svc.role_of(admin) != "admin":
            raise MigrationError("forbidden", "you are not an admin of the target host")
        if not dry:
            prior = self.store.get(mig)
            if prior is not None:
                if prior["role"] == "target" and prior["source"] == pk and prior["state"] == "prechecked":
                    return prior.get("precheck", {})
                raise MigrationError("conflict", "that migration id is already in use")
            if any(r.get("authz_id") == args["authz"].get("id") for r in self.store.all()):
                raise MigrationError("conflict", "that authorization was already used")
        try:
            total = int(vm.get("total_bytes") or 0)
        except (TypeError, ValueError):
            raise MigrationError("bad_request", "malformed precheck")
        avail = await self.backend.available()
        if not avail.get("ok"):
            raise MigrationError("unsupported", "libvirt is not reachable on the target host")
        if await self.backend.get(vm_uuid) is not None or (self.storage.root / vm_uuid).exists() \
                or self.active_for(vm_uuid):
            raise MigrationError("conflict", "a VM with this id already exists on the target host")
        if any(d.name == name for d in await self.backend.list_domains()):
            raise MigrationError("conflict", f"a VM named {name} already exists on the target host")
        loader = str(vm.get("loader") or "")
        if loader and not vm.get("firmware_auto") and not os.path.exists(loader):
            raise MigrationError("unsupported", f"the target host has no firmware at {loader}")

        def writable():
            inc = self.storage.root / ".incoming"
            inc.mkdir(parents=True, exist_ok=True, mode=0o750)
            probe = inc / f".probe-{mig}"
            probe.write_bytes(b"")
            probe.unlink()
            return True
        try:
            await asyncio.to_thread(writable)
        except OSError:
            raise MigrationError("unsupported", "the target host's VM storage is not writable")
        st = await self.backend.host_stats(str(self.storage.root))
        need_gib = math.ceil(1.1 * total / GIB) + self.svc.cfg.reserve_disk_gib
        free_gib = int(st.get("disk_free_gib") or 0)
        if free_gib < need_gib:
            raise MigrationError("insufficient_capacity",
                                 f"the target host needs {need_gib} GiB free (1.1× the disks + its reserve) and has {free_gib}")
        iso_ok = False
        if vm.get("iso"):
            try:
                await asyncio.to_thread(self.storage.iso_path, vm["iso"])
                iso_ok = True
            except (PathEscape, FileNotFoundError):
                iso_ok = False
        result = {"ok": True, "free_gib": free_gib, "need_gib": need_gib, "iso_available": iso_ok,
                  "name": self.svc.cfg.display_name or "PosterChan VM host", "admin": admin}
        if not dry:
            rec = {"id": mig, "role": "target", "state": "prechecked", "vm": vm_uuid, "name": name,
                   "source": pk, "target": self.node_pk, "requester": admin, "authz_id": args["authz"]["id"],
                   "start_after": bool(args.get("start_after")), "bytes_total": total, "created": _now_i(),
                   "precheck": result, "history": [[_now_i(), "prechecked"]]}
            await self._save(rec)
        return result

    async def _op_peer_migrate_begin(self, pk, args):
        rec = self._peer_rec(pk, args, "target")
        if rec is None:
            raise MigrationError("not_found", "no such migration")
        sha, sig = args.get("manifest_sha256"), args.get("manifest_sig")
        if not (isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{64}", sha)
                and isinstance(sig, str) and re.fullmatch(r"[0-9a-f]{128}", sig)):
            raise MigrationError("bad_request", "malformed manifest reference")
        if not bip340.verify(bytes.fromhex(sha), bytes.fromhex(pk), bytes.fromhex(sig)):
            raise MigrationError("forbidden", "the manifest is not signed by the source host")
        async with self._lock(rec["id"]):
            rec = self.store.get(rec["id"])
            if rec["state"] != "prechecked":
                if rec["state"] in ("receiving", "defining", "defined", "committed", "done") \
                        and rec.get("manifest_sha256") == sha:
                    return {"state": rec["state"]}
                raise MigrationError("conflict", f"this migration is {rec['state']}")
            rec.update(state="receiving", manifest_sha256=sha, manifest_sig=sig)
            rec.setdefault("history", []).append([_now_i(), "receiving"])
            await self._save(rec)
        self._spawn(self._run_target(rec["id"]))
        return {"state": "receiving"}

    # ================================================================== TARGET: the run
    def _incoming(self, mig: str) -> Path:
        return self.storage.root / ".incoming" / mig

    def _url(self, rec: dict, index) -> str:
        peer = self.peer(rec["source"])
        if peer is None:
            raise MigrationAbort("the source host is no longer paired with this one")
        return peer.https + TRANSFER_PATH.format(mig=rec["id"], index=index)

    def _read_manifest(self, rec: dict) -> dict:
        raw = self.store.manifest_path(rec["id"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != rec.get("manifest_sha256"):
            raise MigrationAbort("the stored manifest does not match its checksum")
        return json.loads(raw)

    async def _run_target(self, mig: str) -> None:
        try:
            manifest = await self._fetch_manifest(mig)
            await self._pull_files(mig, manifest)
            await self._define_target(mig, manifest)
        except Superseded:
            return
        except Exception as e:
            # Anything unexpected before the commit point is a failure before the commit point: roll
            # back. A task that dies silently here would leave the migration stuck in `defining` for ever.
            msg = getattr(e, "message", None) or str(e) or type(e).__name__
            logger.warning("[vmhost] incoming migration %s failed: %s", mig, msg,
                           exc_info=not isinstance(e, (MigrationAbort, MigrationError)))
            await self._abort_target(mig, msg, tell_source=True)
            return
        await self._commit_loop(mig)

    async def _fetch_manifest(self, mig: str) -> dict:
        rec = self.store.get(mig)
        mp = self.store.manifest_path(mig)
        if mp.exists():
            try:
                return await asyncio.to_thread(self._read_manifest, rec)
            except (MigrationAbort, ValueError, OSError):
                pass
        url = self._url(rec, "manifest")
        last = "no answer"
        for attempt in range(self.t.transfer_attempts):
            try:
                async with self.http_client() as client:
                    r = await client.get(url, headers={"Authorization": nip98_header(self.node_sk, url)})
                if r.status_code == 200:
                    raw = r.content
                    break
                if r.status_code in (401, 404, 409):
                    raise MigrationAbort(f"the source host refused the manifest ({r.status_code})")
                last = f"HTTP {r.status_code}"
            except MigrationAbort:
                raise
            except Exception as e:
                last = str(e) or type(e).__name__
            await asyncio.sleep(self.t.transfer_backoff * (attempt + 1))
        else:
            raise MigrationAbort(f"could not fetch the manifest: {last}")
        if hashlib.sha256(raw).hexdigest() != rec["manifest_sha256"]:
            raise MigrationAbort("the manifest does not match the checksum the source signed")
        manifest = json.loads(raw)
        if manifest.get("migration") != mig or manifest.get("source") != rec["source"] \
                or manifest.get("target") != self.node_pk or (manifest.get("vm") or {}).get("uuid") != rec["vm"]:
            raise MigrationAbort("the manifest describes a different migration")
        if (manifest.get("vm") or {}).get("name") != rec["name"]:
            raise MigrationAbort("the manifest describes a different VM than the precheck")
        names = set()
        files = manifest.get("files")
        if not isinstance(files, list) or len(files) > 64:
            raise MigrationAbort("the manifest lists an invalid file")
        for i, f in enumerate(files):
            try:
                ok = (isinstance(f, dict) and f.get("i") == i and f.get("name") not in names
                      and ((f.get("role") == "disk" and _DISK_FILE_RE.match(str(f.get("name", ""))))
                           or (f.get("role") == "nvram" and f.get("name") == "nvram.fd"))
                      and re.fullmatch(r"[0-9a-f]{64}", str(f.get("sha256", ""))) is not None
                      and not isinstance(f.get("size"), bool) and int(f.get("size", -1)) >= 0)
            except (TypeError, ValueError):
                ok = False
            if not ok:
                raise MigrationAbort("the manifest lists an invalid file")
            names.add(f["name"])
        if not names:
            raise MigrationAbort("the manifest lists no files")
        # Refuse a definition this host would not build BEFORE pulling gigabytes for it (checked again at define).
        plan = validate_incoming_domain(manifest.get("xml") or "", vm_uuid=rec["vm"], name=rec["name"],
                                        disk_names={f["name"] for f in files if f["role"] == "disk"},
                                        cfg=self.svc.cfg)
        validate_incoming_snapshots(manifest.get("snapshots") or [], plan)
        await asyncio.to_thread(self.store.write_manifest, mig, raw)
        return manifest

    async def _pull_files(self, mig: str, manifest: dict) -> None:
        import httpx
        rec = self.store.get(mig)
        inc = self._incoming(mig)
        await asyncio.to_thread(inc.mkdir, parents=True, exist_ok=True, mode=0o750)
        files = manifest["files"]
        total = sum(int(f["size"]) for f in files)
        async with self._lock(mig):
            rec = self.store.get(mig)
            rec["bytes_total"] = total
            rec.setdefault("verified", [])
            await self._save(rec)
        done_before = sum(int(f["size"]) for f in files if f["name"] in rec["verified"])
        pacer = Pacer(self.mcfg.transfer_max_mbps)
        for f in files:
            name, size = f["name"], int(f["size"])
            final, part = inc / name, inc / (name + ".part")
            if name in (self.store.get(mig).get("verified") or []) and final.exists():
                continue
            attempts = 0
            while True:
                if self.store.get(mig)["state"] != "receiving":
                    raise Superseded()
                have = part.stat().st_size if part.exists() else 0
                if have > size:
                    await asyncio.to_thread(part.unlink)
                    have = 0
                if have == size:
                    break
                url = self._url(rec, f["i"])
                headers = {"Authorization": nip98_header(self.node_sk, url), "Range": f"bytes={have}-"}
                progressed = False
                try:
                    async with self.http_client() as client:
                        async with client.stream("GET", url, headers=headers) as r:
                            if r.status_code in (401, 404, 409, 416):
                                raise MigrationAbort(f"the source host refused the transfer of {name} (HTTP {r.status_code})")
                            if r.status_code not in (200, 206):
                                raise httpx.HTTPError(f"HTTP {r.status_code}")
                            mode = "ab"
                            if r.status_code == 200 or not str(r.headers.get("content-range", "")).startswith(f"bytes {have}-"):
                                mode, have = "wb", 0
                            fh = await asyncio.to_thread(open, part, mode)
                            try:
                                async for b in r.aiter_bytes(self.t.chunk):
                                    await asyncio.to_thread(fh.write, b)
                                    have += len(b)
                                    progressed = True
                                    await pacer.tick(len(b))
                                    await self._notify(rec, "transfer", f"copying {name}",
                                                       bytes_done=done_before + have)
                            finally:
                                await asyncio.to_thread(fh.close)
                except MigrationAbort:
                    raise
                except (httpx.HTTPError, OSError) as e:
                    attempts = 0 if progressed else attempts + 1
                    if attempts >= self.t.transfer_attempts:
                        raise MigrationAbort(f"the transfer of {name} kept failing: {e}")
                    logger.info("[vmhost] migration %s: %s interrupted (%s) — resuming", mig, name, e)
                    await asyncio.sleep(self.t.transfer_backoff * max(1, attempts))
                    continue
                if have < size and not progressed:
                    attempts += 1
                    if attempts >= self.t.transfer_attempts:
                        raise MigrationAbort(f"the source stopped sending {name}")
                    await asyncio.sleep(self.t.transfer_backoff * attempts)
            await self._notify(rec, "verify", f"verifying {name}", bytes_done=done_before + size, force=True)
            digest = await asyncio.to_thread(sha256_file, part)
            if digest != f["sha256"]:
                await asyncio.to_thread(part.unlink)
                raise MigrationAbort(f"checksum mismatch for {name} — the copy is not what the source exported")
            fmt = None
            if f.get("role") == "disk":
                fmt = await self._probe_disk(part, name)
            await asyncio.to_thread(os.replace, part, final)
            done_before += size
            async with self._lock(mig):
                rec = self.store.get(mig)
                if fmt:
                    rec.setdefault("formats", {})[name] = fmt
                rec.setdefault("verified", []).append(name)
                rec["bytes_done"] = done_before
                await self._save(rec)

    async def _probe_disk(self, path: Path, name: str) -> str:
        """Ask qemu-img what the received bytes ARE. A qcow2 may name a backing file or an external data file —
        a path opened on THIS host when the VM runs — and the source's say-so about the format is not evidence.
        The probed format is what `<driver type>` becomes, so libvirt never probes again."""
        try:
            info = await self.backend.img_info(str(path))
        except BackendError as e:
            raise MigrationAbort(f"{name} could not be inspected with qemu-img on this host: {e}")
        if info.get("format") not in ("qcow2", "raw"):
            raise MigrationAbort(f"{name} is a {str(info.get('format'))[:20]} image — only qcow2 and raw can be migrated")
        if info.get("backing"):
            raise MigrationAbort(f"{name} names a backing file, which would be opened on this host — refused")
        if info.get("data_file"):
            raise MigrationAbort(f"{name} names an external data file, which would be opened on this host — refused")
        return info["format"]

    def _iso_resolver(self):
        def resolve(name):
            try:
                return str(self.storage.iso_path(name)) if name else None
            except (PathEscape, FileNotFoundError):
                return None
        return resolve

    async def _define_incoming(self, rec: dict, manifest: dict, vm_dir: Path, migration: dict) -> None:
        """TARGET: define a REBUILT domain (see the rebuild section above), never the source's XML."""
        cfg = self.svc.cfg
        vm = manifest.get("vm") if isinstance(manifest.get("vm"), dict) else {}
        if vm.get("uuid") != rec["vm"] or vm.get("name") != rec["name"]:
            raise MigrationAbort("the manifest describes a different VM than the precheck")
        files = manifest["files"]
        disk_names = {f["name"] for f in files if f.get("role") == "disk"}
        plan = validate_incoming_domain(manifest.get("xml") or "", vm_uuid=rec["vm"], name=rec["name"],
                                        disk_names=disk_names, cfg=cfg)
        if plan["uuid"] != rec["vm"] or plan["name"] != rec["name"]:      # never reached; never trusted either
            raise MigrationAbort("the rebuilt definition does not describe the migrating VM")
        snaps = validate_incoming_snapshots(manifest.get("snapshots") or [], plan)
        meta = clean_incoming_meta(manifest.get("meta"), cfg=cfg, assign_allow=rec.get("assign_allow"))
        meta.migration = dict(migration)
        formats = dict(rec.get("formats") or {})
        for dk in plan["disks"]:
            if dk["name"] not in formats:        # a journal from before the probe existed: ask now, never assume
                formats[dk["name"]] = await self._probe_disk(vm_dir / dk["name"], dk["name"])
        has_nvram = any(f.get("role") == "nvram" for f in files)
        xml = build_incoming_domain(plan, vm_dir, meta, cfg, formats, has_nvram)
        uuid = rec["vm"]
        existing = await self.backend.get(uuid)
        if existing is None:
            if any(d.name == rec["name"] for d in await self.backend.list_domains()):
                raise MigrationAbort(f"a VM named {rec['name']} already exists on this host")
            await self.backend.define(xml, str(vm_dir))
        elif not (existing.meta and existing.meta.migration.get("id") == rec["id"]):
            raise MigrationAbort("a VM with this id already exists on this host")
        snap_domain = build_incoming_domain(plan, vm_dir, clean_incoming_meta(manifest.get("meta"), cfg=cfg,
                                                                              assign_allow=rec.get("assign_allow")),
                                            cfg, formats, has_nvram)
        for s in snaps:
            await self.backend.snapshot_redefine(uuid, build_incoming_snapshot(s, snap_domain), str(vm_dir),
                                                 current=s["current"])

    async def _define_from_manifest(self, manifest: dict, vm_dir: Path, migration: dict) -> None:
        """SOURCE only (reclaim): its OWN export, read back through the journaled checksum."""
        names = {f["name"] for f in manifest["files"]}
        meta = domainxml.parse_meta(manifest.get("meta") or "") or domainxml.VmMeta()
        meta.migration = dict(migration)
        resolve = self._iso_resolver()
        xml = rewrite_domain_xml(manifest["xml"], vm_dir, names, meta, resolve)
        uuid = manifest["vm"]["uuid"]
        if await self.backend.get(uuid) is None:
            await self.backend.define(xml, str(vm_dir))
        for s in manifest.get("snapshots") or []:
            sx = rewrite_snapshot_xml(s["xml"], vm_dir, names, resolve)
            await self.backend.snapshot_redefine(uuid, sx, str(vm_dir), current=bool(s.get("current")))

    async def _define_target(self, mig: str, manifest: dict) -> None:
        rec = await self._transition(mig, ("receiving",), "defining")
        uuid = rec["vm"]
        vm_dir = self.storage.root / uuid
        inc = self._incoming(mig)
        existing = await self.backend.get(uuid)
        if existing is not None and not (existing.meta and existing.meta.migration.get("id") == mig):
            raise MigrationAbort("a VM with this id appeared on the target host during the transfer")
        await self._notify(rec, "define", "defining the VM", force=True)

        def place():
            if vm_dir.exists() and not rec.get("dir_created"):
                raise MigrationAbort("the VM directory already exists on the target host")
            vm_dir.mkdir(mode=0o750, exist_ok=True)
            for f in manifest["files"]:
                s, t = inc / f["name"], vm_dir / f["name"]
                if s.exists():
                    os.replace(s, t)
                elif not t.exists():
                    raise MigrationAbort(f"{f['name']} went missing before it was placed")
        if not vm_dir.exists():
            rec["dir_created"] = True
            await self._save(rec)
        await asyncio.to_thread(place)
        await self._define_incoming(rec, manifest, vm_dir, {"id": mig, "state": "incoming", "peer": rec["source"]})
        d = await self.backend.get(uuid)
        if d is not None:
            self.svc._set_index(d)
        await self._transition(mig, ("defining",), "defined", start_after=bool(manifest.get("start_after")),
                               autostart=bool(manifest.get("autostart")))

    async def _commit_loop(self, mig: str) -> None:
        rec = self.store.get(mig)
        started = rec.get("commit_started") or time.time()
        if not rec.get("commit_started"):
            rec["commit_started"] = started
            await self._save(rec)
        delay = self.t.commit_retry
        await self._notify(rec, "commit", "asking the source to hand the VM over", force=True)
        while not self.closed:
            rec = self.store.get(mig)
            if rec["state"] not in ("defined", "locked"):
                return
            res = await self._peer_call(rec["source"], "peer.migrate.commit", {"migration": mig}, retries=0)
            if res is not None and res.get("ok"):
                await self._finalize_target(mig)
                return
            if res is not None and (res.get("error") or {}).get("code") in ("aborted", "not_found"):
                await self._abort_target(mig, "the source host abandoned the migration", tell_source=False,
                                         allow_locked=True)
                return
            if res is None and rec["state"] == "defined" and time.time() - started > self.t.contact_deadline:
                async with self._lock(mig):
                    rec = self.store.get(mig)
                    if rec["state"] == "defined":
                        rec.update(state="locked", locked_at=_now_i(),
                                   error="the source host cannot be reached to confirm the handoff")
                        await self._save(rec)
                        await self._notify(rec, "locked", rec["error"], force=True)
            await asyncio.sleep(delay)
            delay = min(delay * 2, self.t.commit_retry_max)

    async def _finalize_target(self, mig: str) -> None:
        async with self._lock(mig):
            rec = self.store.get(mig)
            if rec["state"] not in ("defined", "locked", "committed"):
                return
            if rec["state"] != "committed":
                rec["state"] = "committed"
                rec.setdefault("history", []).append([_now_i(), "committed"])
                await self._save(rec)
            await self._finalize_target_locked(rec, forced=False)

    async def _finalize_target_locked(self, rec: dict, forced: bool) -> None:
        """Idempotent; the caller holds the migration lock and has journaled `committed`."""
        uuid = rec["vm"]
        d = await self.backend.get(uuid)
        if d is None:
            raise MigrationError("conflict", "the incoming VM is no longer defined on this host")
        if d.meta is not None and d.meta.migration:
            d.meta.migration = {}
            await self.backend.set_metadata(uuid, d.meta, live=False)
        if rec.get("autostart") and not d.autostart:
            await self.backend.set_autostart(uuid, True)
        rec.update(state="done", completed_at=_now_i(), error="")
        rec.setdefault("history", []).append([_now_i(), "done"])
        await self._save(rec)
        await asyncio.to_thread(shutil.rmtree, self._incoming(rec["id"]), True)
        d = await self.backend.get(uuid)
        if d is not None:
            self.svc._set_index(d)
        if rec.get("start_after") and d is not None and d.state == "shutoff":
            try:
                await self.backend.start(uuid)
            except BackendError as e:
                rec["warning"] = f"the VM arrived but did not start: {e}"
                await self._save(rec)
        await self._notify(rec, "done", "the VM now lives on this host", force=True)
        if not forced:
            self._spawn(self._send_ack(rec["id"]))

    async def _send_ack(self, mig: str) -> None:
        for i in range(5):
            rec = self.store.get(mig)
            res = await self._peer_call(rec["source"], "peer.migrate.ack", {"migration": mig}, retries=0)
            if res is not None and res.get("ok"):
                async with self._lock(mig):
                    rec = self.store.get(mig)
                    rec["ack_sent"] = True
                    await self._save(rec)
                return
            await asyncio.sleep(self.t.commit_retry * (i + 1))

    async def _drop_target_copy(self, rec: dict) -> None:
        uuid = rec["vm"]
        d = await self.backend.get(uuid)
        if d is not None and d.meta is not None and d.meta.migration.get("id") == rec["id"]:
            if d.state != "shutoff":
                await self.backend.destroy(uuid)
            await self.backend.undefine_for_migration(uuid, keep_nvram=False)
            self.svc._assign.pop(uuid, None)
        if rec.get("dir_created"):
            await asyncio.to_thread(shutil.rmtree, self.storage.root / uuid, True)
        await asyncio.to_thread(shutil.rmtree, self._incoming(rec["id"]), True)

    async def _abort_target(self, mig: str, reason: str, tell_source: bool, allow_locked: bool = False) -> None:
        allowed = TARGET_PRE_COMMIT + (("locked",) if allow_locked else ())
        async with self._lock(mig):
            rec = self.store.get(mig)
            if rec is None or rec["role"] != "target" or rec["state"] not in allowed:
                return
            rec.update(state="aborted", error=reason, aborted_at=_now_i())
            rec.setdefault("history", []).append([_now_i(), "aborted"])
            await self._save(rec)
            try:
                await self._drop_target_copy(rec)
            except Exception as e:
                rec["warning"] = f"cleanup after abort was incomplete: {e}"
                await self._save(rec)
        await self._notify(rec, "aborted", reason, force=True)
        if tell_source:
            self._spawn(self._peer_call(rec["source"], "peer.migrate.abort", {"migration": mig, "reason": reason[:200]},
                                        retries=1))

    # ================================================================== recovery + housekeeping
    async def resume(self) -> None:
        """At startup: pick every unfinished migration up from its journal."""
        for rec in list(self.store.all()):
            mig, st = rec["id"], rec["state"]
            try:
                if rec["role"] == "source":
                    if st in ("planned", "quiescing", "exporting"):
                        await self._abort_source(mig, "the source host restarted mid-migration", tell_target=True)
                    elif st == "transferring":
                        self._spawn(self._watch_source(mig))
                    elif st == "handed_off":
                        await self._retry_handoff(mig)
                        self._spawn(self._watch_source(mig))
                    elif st == "locked":
                        self._spawn(self._watch_source(mig))
                else:
                    if st == "prechecked":
                        continue
                    if st in ("receiving",):
                        self._spawn(self._run_target(mig))
                    elif st == "defining":
                        async def redo(mig=mig):
                            try:
                                async with self._lock(mig):
                                    r = self.store.get(mig)
                                    r["state"] = "receiving"
                                    await self._save(r)
                                manifest = await asyncio.to_thread(self._read_manifest, self.store.get(mig))
                                await self._define_target(mig, manifest)
                            except (MigrationAbort, MigrationError, BackendError, OSError, ValueError) as e:
                                await self._abort_target(mig, getattr(e, "message", None) or str(e), tell_source=True)
                                return
                            await self._commit_loop(mig)
                        self._spawn(redo())
                    elif st in ("defined", "locked"):
                        self._spawn(self._commit_loop(mig))
                    elif st == "committed":
                        await self._finalize_target(mig)
                    elif st == "done" and not rec.get("ack_sent"):
                        self._spawn(self._send_ack(mig))
            except Exception as e:
                logger.warning("[vmhost] resuming migration %s failed: %s", mig, e)

    async def housekeeping(self) -> None:
        """Reap retained source copies — only after the target ACKED, and only after the keep window —
        and expire prechecks that were never begun."""
        now = _now_i()
        for rec in list(self.store.all()):
            if rec["role"] == "source" and rec["state"] == "done" and rec.get("acked_at") \
                    and rec.get("retained") and not rec.get("retained_reaped") \
                    and now - int(rec["acked_at"]) >= self.mcfg.keep_source_hours * 3600:
                path = self.storage.root / ".retained" / rec["retained"]
                await asyncio.to_thread(shutil.rmtree, path, True)
                rec["retained_reaped"] = now
                await self._save(rec)
            elif rec["role"] == "target" and rec["state"] == "prechecked" and now - int(rec.get("created", now)) > PRECHECK_TTL:
                await self._abort_target(rec["id"], "the source never began the transfer", tell_source=False)


def attach(svc, node_sk: bytes, publish, settings: dict, **kw) -> Migrator:
    m = Migrator(svc, node_sk, MigrateConfig.from_settings(settings), publish=publish, **kw)
    svc.migrator = m
    return m
