"""Where a VM's files live, and the confinement rule that keeps a request from naming anything else.

Clients NEVER send a path. They send a VM uuid, an ISO id (a bare file name from `iso.list`) and
sizes; every path is built here from the configured storage root and then RESOLVED and checked to
still be inside it. Resolving is the half that matters: `isos/ubuntu.iso` can be a symlink to
`/etc/shadow`, and a string check on the name passes it.

Layout under `vmhost_storage_dir`:
    <root>/<uuid>/disk-vda.qcow2     the VM's disk (created by us)
    <root>/<uuid>/nvram.fd           its EFI variable store
    <root>/<uuid>/domain.xml         the definition we handed libvirt (kept for recovery)
    <root>/isos/<name>.iso           the ISO library
    <root>/.state/                   lock, op journal
"""
from __future__ import annotations

import os
import re
import uuid as _uuid
from pathlib import Path

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_ISO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\.iso$", re.I)


SNAPSHOT_FILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,47}$")
SNAPSHOT_NVRAM_FILE = re.compile(r"^snap-[A-Za-z0-9][A-Za-z0-9_.-]{0,47}\.nvram\.fd$")
# See Storage.ensure / make_vm_dir: traversable by libvirt's qemu user, never listable or readable by others.
DIR_MODE = 0o751
FILE_MODE = 0o600


class PathEscape(ValueError):
    """A built path resolved outside the storage root."""


def clean_name(n) -> str:
    """The VM-name rule from desktop/vm.js, ported: letters, digits, `_ . -`; no leading/trailing
    dot or dash; at most 48 characters. An empty result means the name was unusable."""
    s = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(n or "").strip())
    s = re.sub(r"^[.-]+|[.-]+$", "", s)
    return s[:48]


def clean_iso_name(n) -> str:
    """A library file name from a URL basename or an upload's name: ISO-id characters only, `.iso`
    appended when missing, and '' when nothing usable is left."""
    s = re.sub(r"[^A-Za-z0-9._+-]+", "-", str(n or "").strip())
    s = re.sub(r"^[._+-]+", "", s)
    if not s:
        return ""
    if not s.lower().endswith(".iso"):
        s = s[:120] + ".iso"
    s = s[:-4][:124] + s[-4:]
    return s if _ISO_RE.match(s) and ".." not in s else ""


def valid_uuid(u) -> str | None:
    s = str(u or "").strip().lower()
    return s if _UUID_RE.match(s) else None


def new_uuid() -> str:
    return str(_uuid.uuid4())


class Storage:
    def __init__(self, root):
        self.root = Path(root)

    # ---- confinement -------------------------------------------------------------------------
    def _inside(self, p: Path, base: Path | None = None) -> Path:
        base = (base or self.root).resolve()
        r = p.resolve()
        if r != base and not r.is_relative_to(base):
            raise PathEscape(f"{p} is outside {base}")
        return r

    def ensure(self) -> None:
        """Create what is missing with the modes a libvirt host needs. QEMU runs as ANOTHER user (`qemu`, or
        `libvirt-qemu` on Debian): libvirt's dynamic ownership hands it the disk FILES, never the directories it
        must walk through, so the root is traversable (0751, not listable), the ISO library readable (0755 —
        installers are not secrets) and `.state` the app's alone. Existing directories keep the admin's modes."""
        for d, m in ((self.root, DIR_MODE), (self.root / "isos", 0o755), (self.root / ".state", 0o700)):
            if not d.exists():
                d.mkdir(parents=True, exist_ok=True)
                os.chmod(d, m)

    def make_vm_dir(self, vm_uuid: str, exist_ok: bool = False) -> Path:
        """A VM's directory, traversable by the qemu user and listable by nobody else. LIVE: created 0750 by the
        app, every VM failed its first start with `Cannot access storage file … (as uid:77, gid:77): Permission
        denied`. Files inside are 0600 (`FILE_MODE`) — libvirt chowns them to qemu while the guest runs and gives
        them back on stop, so traversal is all the directory has to grant."""
        d = self.vm_dir(vm_uuid)
        d.mkdir(mode=0o700, exist_ok=exist_ok)
        os.chmod(d, DIR_MODE)
        return d

    @property
    def state_dir(self) -> Path:
        return self.root / ".state"

    def vm_dir(self, vm_uuid: str) -> Path:
        u = valid_uuid(vm_uuid)
        if not u:
            raise PathEscape("not a VM id")
        return self._inside(self.root / u)

    def disk_path(self, vm_uuid: str, n: int = 0) -> Path:
        dev = "vd" + "abcdefghijklmnopqrstuvwxyz"[n]
        return self._inside(self.vm_dir(vm_uuid) / f"disk-{dev}.qcow2", self.vm_dir(vm_uuid))

    def nvram_path(self, vm_uuid: str) -> Path:
        return self._inside(self.vm_dir(vm_uuid) / "nvram.fd", self.vm_dir(vm_uuid))

    def iso_path(self, iso_id: str) -> Path:
        """An ISO from the library by its id, or PathEscape. The id is a bare name; the RESOLVED path
        must still be a regular file inside `<root>/isos` — a symlink out of it is refused."""
        name = str(iso_id or "")
        if not _ISO_RE.match(name) or "/" in name or "\\" in name or ".." in name:
            raise PathEscape("not an ISO id")
        isos = self.root / "isos"
        p = self._inside(isos / name, isos)
        if not p.is_file():
            raise FileNotFoundError(name)
        return p

    def list_isos(self) -> list:
        isos = self.root / "isos"
        out = []
        try:
            entries = sorted(os.scandir(isos), key=lambda e: e.name.lower())
        except OSError:
            return out
        for e in entries:
            try:
                p = self.iso_path(e.name)
            except (PathEscape, FileNotFoundError):
                continue
            try:
                size = p.stat().st_size
            except OSError:
                continue
            out.append({"id": e.name, "name": e.name, "size": size})
        return out

    # ---- phase 2 -----------------------------------------------------------------------------
    def extra_disk_path(self, vm_uuid: str, target: str) -> Path:
        """An added disk, named by its guest target (`disk-vdb.qcow2`). The target is validated here
        too, not only where it was chosen: this is the function that turns it into a path."""
        if not re.fullmatch(r"(vd|sd)[b-z]", str(target or "")):
            raise PathEscape("not a disk target")
        base = self.vm_dir(vm_uuid)
        return self._inside(base / f"disk-{target}.qcow2", base)

    def snapshot_nvram_path(self, vm_uuid: str, name: str) -> Path:
        """The copy of a VM's EFI variable store taken with offline snapshot `name` — a flat file in the VM's own
        directory (`snap-<name>.nvram.fd`), so a migration's file list stays flat and name-validated."""
        if not SNAPSHOT_FILE_NAME.match(str(name or "")):
            raise PathEscape("not a snapshot name")
        base = self.vm_dir(vm_uuid)
        return self._inside(base / f"snap-{name}.nvram.fd", base)

    @property
    def iso_dir(self) -> Path:
        return self.root / "isos"

    def iso_incoming(self) -> Path:
        """Where fetches and uploads are written until they are whole. Inside the library directory so
        the final rename is atomic, and dot-named so `list_isos` (which demands `*.iso`) never shows one."""
        d = self.iso_dir / ".incoming"
        d.mkdir(parents=True, exist_ok=True, mode=0o750)
        return d

    def new_iso_target(self, name: str) -> Path:
        """The final path for a NEW library entry. Refuses a name that is not an ISO id, and one that is
        already taken (FileExistsError) — a fetch never silently replaces an installer a VM boots from."""
        if not _ISO_RE.match(name or "") or "/" in name or "\\" in name or ".." in name:
            raise PathEscape("not an ISO id")
        p = self.iso_dir / name
        if os.path.lexists(p):
            raise FileExistsError(name)
        return self._inside(p.parent, self.iso_dir) / name

    def is_managed_dir(self, vm_uuid: str) -> bool:
        try:
            return self.vm_dir(vm_uuid).is_dir()
        except PathEscape:
            return False
