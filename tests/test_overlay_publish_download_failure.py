"""Exercise the real publisher's artifact boundary without contacting a server."""
import os
import hashlib
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.parametrize("download", ["partial_failure", "empty_success", "valid_success"])
@pytest.mark.parametrize("existing_manifest", [False, True])
def test_artifact_download_boundary(tmp_path, download, existing_manifest):
    root = Path(__file__).resolve().parents[1]
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy2(root / "scripts/publish_overlay.sh", scripts / "publish_overlay.sh")
    overlay = tmp_path / "os/overlay"
    (overlay / "profiles").mkdir(parents=True)
    (overlay / "profiles/repo_name").write_text("posterchan\n")
    package = overlay / "app-misc/posterchan-desktop"
    package.mkdir(parents=True)
    (package / "posterchan-desktop-1.0.1.ebuild").write_text("EAPI=8\n")
    manifest = package / "Manifest"
    original = b"existing pinned artifact hashes\n"
    if existing_manifest:
        manifest.write_bytes(original)
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    curl = fakebin / "curl"
    outputs = {
        "partial_failure": 'printf partial > "$1"\nexit 22\n',
        "empty_success": ': > "$1"\nexit 0\n',
        "valid_success": 'printf fixture-artifact > "$1"\nexit 0\n',
    }
    curl.write_text('#!/bin/sh\nwhile [ "$1" != "-o" ]; do shift; done\nshift\n'
                    + outputs[download])
    curl.chmod(0o755)
    for command in ("git", "ssh"):
        stub = fakebin / command
        stub.write_text('#!/bin/sh\nprintf touched >> "$PUBLISH_SPY"\nexit 97\n')
        stub.chmod(0o755)
    spy = tmp_path / "publish-calls"
    env = {**os.environ, "PATH": str(fakebin) + os.pathsep + os.environ["PATH"],
           "PUBLISH_SPY": str(spy), "TMPDIR": str(tmp_path)}
    run = subprocess.run(["bash", str(scripts / "publish_overlay.sh")], env=env,
                         capture_output=True, text=True, timeout=10)
    assert run.returncode != 0
    if download == "valid_success":
        # Git deliberately fails locally: this checks the download boundary, not publication.
        assert spy.exists(), "A valid artifact should allow repository staging to begin"
        data = b"fixture-artifact"
        expected = (f"DIST posterchan-desktop-1.0.1.tar.zst {len(data)} "
                    f"BLAKE2B {hashlib.blake2b(data).hexdigest()} "
                    f"SHA512 {hashlib.sha512(data).hexdigest()}\n")
        assert manifest.read_text() == expected
        return
    assert not spy.exists(), "An unverified artifact reached repository publication"
    assert manifest.exists() == existing_manifest
    if existing_manifest:
        assert manifest.read_bytes() == original
    assert "overlay publication aborted" in run.stderr
