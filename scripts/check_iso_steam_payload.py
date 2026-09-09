#!/usr/bin/env python3
"""Does this ISO actually carry a Steam that can run? Asked of the image, in about a second.

    venv-unified/bin/python scripts/check_iso_steam_payload.py iso-out/posterchan-live-*.iso

WHY THIS EXISTS. A PosterChanOS LiveUSB is a COPY of the machine it was packed from --
`gentoo.sh install-live` writes the squashfs onto the disk and emerges nothing -- so every
question about what the finished machine has is a question about what is in this file. Steam is
the one application on the image whose payload is easy to lose and impossible to notice losing:
`/usr/bin/steam` is a forty-line bash wrapper and `steam.desktop` is a text file, and both survive
an image that cannot start Steam at all.

WHAT MAKES STEAM RUN IS THE OTHER ABI. Valve's client is `ELF 32-bit LSB pie executable, Intel
i386` asking for `/lib/ld-linux.so.2`. With no 32-bit loader and libc it cannot be exec'd; with no
32-bit libGL and no 32-bit Mesa drivers it starts and every game draws nothing. Measured inside a
booted image, `ldd` on the unpacked bootstrap resolves all six of its libraries out of /usr/lib --
which on this merged-usr layout IS the 32-bit libdir, /usr/lib64 being the 64-bit one. So the
32-bit runtime is what is checked here, not the launcher.

NO ROOT, NO COPY, NO VM. The ISO's directory table is read in Python to find where
LiveOS/squashfs.img starts, and `unsquashfs -o <that offset> -l` lists the image in place. Nothing
is mounted and nothing is extracted -- a full listing of a 3.9 GB image costs under a second.

Exit 0 the image carries a runnable Steam, 1 it does not, 2 the question could not be asked (no
ISO, no unsquashfs, an image this cannot read). Exit 2 is a SKIP and is never a pass.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys

SECTOR = 2048

# The launcher half. Present without the runtime below, Steam is a menu entry that does nothing.
LAUNCHER = (
    "usr/bin/steam",
    "usr/lib/steam/bin_steam.sh",
    "usr/lib/steam/bootstraplinux_ubuntu12_32.tar.xz",
    "usr/share/applications/steam.desktop",
)
# The runtime half, and the reason this script is not a `grep steam`. /usr/lib is the 32-bit
# libdir here; /usr/lib64 is the 64-bit one.
RUNTIME = (
    "usr/lib/ld-linux.so.2",
    "usr/lib/libc.so.6",
    "usr/lib/libGL.so.1",
    "usr/lib/libvulkan.so.1",
)


def _records(fh, lba: int, size: int):
    """Yield (name, extent_lba, size, flags) for one ISO9660 directory."""
    fh.seek(lba * SECTOR)
    data = fh.read(((size + SECTOR - 1) // SECTOR) * SECTOR)
    off = 0
    while off < size:
        length = data[off]
        if length == 0:                      # padding to the end of this sector
            off = (off // SECTOR + 1) * SECTOR
            continue
        rec = data[off:off + length]
        yield (rec[33:33 + rec[32]], struct.unpack("<I", rec[2:6])[0],
               struct.unpack("<I", rec[10:14])[0], rec[25])
        off += length


def squashfs_offset(iso: Path) -> int | None:
    """Byte offset of LiveOS/squashfs.img inside `iso`, or None if it is not there.

    ISO9660 files are contiguous extents, which is the whole reason this works: the image can be
    listed where it lies instead of being carved out of a 4 GB file first.
    """
    with iso.open("rb") as fh:
        fh.seek(16 * SECTOR)
        pvd = fh.read(SECTOR)
        if pvd[1:6] != b"CD001":
            return None
        root = pvd[156:190]
        lba, size = struct.unpack("<I", root[2:6])[0], struct.unpack("<I", root[10:14])[0]
        for name, ext, sz, flags in _records(fh, lba, size):
            if flags & 0x02 and name.upper() == b"LIVEOS":
                for fname, fext, fsz, fflags in _records(fh, ext, sz):
                    # ISO9660 appends a version suffix: SQUASHFS.IMG;1
                    if fname.upper().split(b";")[0] == b"SQUASHFS.IMG":
                        return fext * SECTOR
    return None


def listing(iso: Path, offset: int) -> set[str] | None:
    try:
        out = subprocess.run(["unsquashfs", "-o", str(offset), "-l", str(iso)],
                             capture_output=True, text=True, timeout=900)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"SKIP  could not list the image ({exc})")
        return None
    if out.returncode != 0:
        print("SKIP  unsquashfs refused the image: " + (out.stderr or "").strip()[-300:])
        return None
    return {line[len("squashfs-root/"):] for line in out.stdout.splitlines()
            if line.startswith("squashfs-root/")}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("iso", nargs="?", default=os.environ.get("PC_LIVECD_ISO", ""))
    args = ap.parse_args()

    iso = Path(args.iso) if args.iso else None
    if iso is None:
        # The build's own output directory, newest first. A check nobody can run without typing a
        # path is a check nobody runs.
        found = sorted(Path(__file__).resolve().parents[1].glob("iso-out/*.iso"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        iso = found[0] if found else None
    if iso is None or not iso.is_file():
        print("SKIP  no ISO to read — pass one, or set PC_LIVECD_ISO. Nothing was verified "
              "about Steam; this is not a pass.")
        return 2
    if not shutil.which("unsquashfs"):
        print("SKIP  unsquashfs (sys-fs/squashfs-tools) is not installed on this host")
        return 2

    offset = squashfs_offset(iso)
    if offset is None:
        print(f"SKIP  {iso} has no LiveOS/squashfs.img — not a PosterChanOS live image")
        return 2
    names = listing(iso, offset)
    if names is None:
        return 2

    # THE PACKAGE DATABASE, not just the files. A path can be left behind by an uninstall; the vdb
    # entry is what says this image believes Steam is installed.
    installed = sorted(n for n in names
                       if n.startswith("var/db/pkg/games-util/steam-launcher-")
                       and n.count("/") == 4)
    missing_launcher = [p for p in LAUNCHER if p not in names]
    missing_runtime = [p for p in RUNTIME if p not in names]
    dri32 = sorted(n for n in names if n.startswith("usr/lib/dri/") and n.endswith("_dri.so"))

    print(f"image:    {iso}")
    print(f"steam:    {installed[0].rsplit('/', 1)[-1] if installed else 'NOT INSTALLED'}")
    print(f"launcher: {len(LAUNCHER) - len(missing_launcher)}/{len(LAUNCHER)}"
          + (f"  missing {' '.join(missing_launcher)}" if missing_launcher else ""))
    print(f"32-bit:   {len(RUNTIME) - len(missing_runtime)}/{len(RUNTIME)}"
          + (f"  missing {' '.join(missing_runtime)}" if missing_runtime else ""))
    print(f"mesa32:   {len(dri32)} DRI drivers")

    if not installed:
        print("FAIL  this image has no games-util/steam-launcher — a LiveUSB made from it "
              "installs a machine with no Steam.")
        return 1
    if missing_launcher or missing_runtime:
        print("FAIL  Steam is recorded as installed and its payload is not in the image: "
              + " ".join("/" + p for p in missing_launcher + missing_runtime))
        return 1
    if not dri32:
        # Not the loader, so Steam still starts -- and every game renders nothing, which is a
        # worse thing to discover on a stranger's machine than a launcher that will not open.
        print("FAIL  no 32-bit Mesa drivers in /usr/lib/dri — Steam would start and its games "
              "would not draw.")
        return 1
    print("OK    Steam and its 32-bit runtime are in this image.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
