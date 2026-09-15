"""Pack a tiny filesystem using the builder's real exclusion array."""
from pathlib import Path
import shutil
import subprocess

import pytest


SOURCE = Path(__file__).resolve().parents[1] / "os/gentoo.sh"


def test_previous_iso_vm_and_binhost_cache_are_not_os_payload(tmp_path):
    for tool in ("mksquashfs", "unsquashfs"):
        if not shutil.which(tool):
            pytest.skip(f"{tool} is required for the real packed-image check")
    root = tmp_path / "root"
    artifacts = (
        "var/iso/posterchan-live-20260913.iso",
        "var/iso/pc-install.qcow2",
        "var/cache/binhost/binhost/Packages",
        "var/cache/binhost/binhost/desktop.gpkg.tar",
    )
    payloads = ("usr/bin/gentoo.sh", "opt/posterchan/resources/app.asar",
                "var/db/pkg/app-misc/posterchan-desktop-1.0.1586/CONTENTS")
    for name in artifacts + payloads:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name)
    source = SOURCE.read_text()
    start = source.index("\tlocal EXCLUDES=(", source.index("liveCD() {"))
    end = source.index("\n\t)", start) + len("\n\t)")
    declaration = source[start:end].replace("local EXCLUDES=", "EXCLUDES=", 1)
    script = declaration + '''
EXARGS=()
for f in "${EXCLUDES[@]}"; do EXARGS+=(-e "$f"); done
mksquashfs "$1" "$2" -noappend -no-progress -processors 1 "${EXARGS[@]}"
'''
    image = tmp_path / "filesystem.squashfs"
    subprocess.run(["bash", "-c", script, "pack-fixture", str(root), str(image)],
                   check=True, capture_output=True, text=True, timeout=30)
    result = subprocess.run(["unsquashfs", "-l", str(image)], check=True,
                            capture_output=True, text=True, timeout=10)
    paths = set(result.stdout.splitlines())
    for name in artifacts:
        assert "squashfs-root/" + name not in paths, name
        assert (root / name).read_text() == name, "host artifact was modified"
    for name in payloads:
        assert "squashfs-root/" + name in paths, name
