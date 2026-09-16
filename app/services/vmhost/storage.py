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

    def is_managed_dir(self, vm_uuid: str) -> bool:
        try:
            return self.vm_dir(vm_uuid).is_dir()
        except PathEscape:
            return False
