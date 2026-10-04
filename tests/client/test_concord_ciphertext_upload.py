"""Encrypted attachments go to Blossom servers that store any bytes, never to NIP-96.

Runs tests/client/concord_ciphertext_upload_runtime.mjs: the SHIPPED upload.js under node with a fake
network -- a non-member's ciphertext lands on Vector's first default (not nostr.build), a refusing
server fails over, their own kind-10063 list is tried first, a member still uses this node, no custom
header reaches somebody else's server, an all-refused upload names every server, and an ordinary
upload is unchanged."""
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_encrypted_attachments_reach_a_blossom_server_that_takes_them():
    r = subprocess.run(["node", str(HERE / "concord_ciphertext_upload_runtime.mjs")], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and r.stdout.strip().endswith("ok"), r.stdout + r.stderr
