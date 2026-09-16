"""The client's VM-host RPC (static/js/client/vmrpc.js), run under node — and CROSS-CHECKED against the
Python host: a request the shipped JS signs and NIP-44-encrypts is handled by the shipped Python
transport, and the reply the Python host builds is decoded by the shipped JS.

Two implementations of one wire format are exactly where a test that only checks each side against
itself passes while the feature does not work (NIP-44 payload framing, tag shapes, the id field).
"""
import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app.services.nostr import bip340
from app.services.vmhost import domainxml, transport
from app.services.vmhost.config import VmHostConfig
from app.services.vmhost.service import VmHostService
from app.services.vmhost.storage import Storage
from tests.vmhost_fake import FakeBackend

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tests/client/vms_rpc_runtime.mjs"
pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node is required")


def test_the_rpc_runtime_scenarios():
    r = subprocess.run(["node", str(SCRIPT)], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ALL OK" in r.stdout


def test_a_js_request_is_served_by_the_python_host_and_the_reply_decodes_in_js(tmp_path):
    host_sk = bytes.fromhex("99" * 32)
    host_pk = bip340.pubkey_from_seckey(host_sk).hex()
    user_pk = bip340.pubkey_from_seckey(bytes([7] * 32)).hex()
    u1 = "11111111-1111-4111-8111-111111111111"
    root = tmp_path / "vms"
    storage = Storage(root)
    storage.ensure()
    (root / u1).mkdir()
    be = FakeBackend()
    be.add_domain(u1, "alpha", meta=domainxml.VmMeta(owner="aa" * 32, assigned=[user_pk]))

    async def admins():
        return set()
    svc = VmHostService(VmHostConfig(enabled=True, storage_dir=str(root)), be, node_pubkey=host_pk,
                        admin_provider=admins, storage=storage)
    replies = []

    async def publish(ev):
        replies.append(ev)
        return True
    tr = transport.Transport(svc, host_sk, publish)

    env = dict(os.environ, PC_VMRPC_INTEROP="1", PC_VMRPC_HOST=host_pk)
    p = subprocess.Popen(["node", str(SCRIPT)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, env=env)
    try:
        line = p.stdout.readline()
        assert line.startswith("REQUEST "), line + p.stderr.read()
        req = json.loads(line[len("REQUEST "):])

        async def serve():
            await svc.refresh_index()
            return await tr.on_event(req)
        res = asyncio.run(serve())
        assert res is not None, "the Python host dropped a request the JS client built"
        p.stdin.write(json.dumps(res) + "\n")
        p.stdin.flush()
        out = p.stdout.readline()
        assert out.startswith("RESULT "), out + p.stderr.read()
        result = json.loads(out[len("RESULT "):])
    finally:
        p.kill()
    assert result["ok"] is True, result
    assert result["id"] == "interop-1"
    assert result["result"]["vm"]["state"] == "running"
    assert be.domains[u1]["state"] == "running"
