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
        for d in (self.root, self.root / "isos", self.root / ".state"):
            d.mkdir(parents=True, exist_ok=True, mode=0o750)

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
