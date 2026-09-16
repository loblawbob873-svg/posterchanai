"""How much a disk transfer costs the SINGLE uvicorn worker everything else runs on.

The plan flagged it as a risk: the source serves multi-GB files from the same event loop that answers
every chat, relay proxy and console. So this runs the REAL transfer route under a REAL uvicorn server
on a loopback socket, pulls a file from a separate thread (as the target host would, over the
network), and meanwhile measures the server loop's lag with a 5 ms sleep probe.

Default size is small enough for the suite. For the 1 GiB measurement quoted in docs/VM_HOSTING.md:

    VMHOST_LAG_BYTES=1073741824 VMHOST_LAG_DIR=/some/disk/dir \\
        venv-unified/bin/python -m pytest tests/test_vmhost_transfer_lag.py -q -s

(`/tmp` is tmpfs on some nodes — a 1 GiB file there is 1 GiB of RAM.)
"""
import asyncio
import os
import statistics
import threading
import time
from pathlib import Path

import httpx
import pytest

from app.services.vmhost import migrate
from tests.vmhost_migration_fake import S_HTTPS, T_SK, VM, World


async def _measure(tmp: Path, nbytes: int) -> dict:
    import uvicorn
    w = World(tmp, timing={"chunk": 1 << 20})
    try:
        vm_dir = w.S.root / VM
        vm_dir.mkdir(parents=True)
        disk = vm_dir / "disk-vda.qcow2"
        block = os.urandom(4 << 20)
        with open(disk, "wb") as f:
            left = nbytes
            while left > 0:
                f.write(block[:min(left, len(block))])
                left -= min(left, len(block))
        mig = "c" * 32
        await w.S.migrator._save({"id": mig, "role": "source", "state": "transferring", "vm": VM, "target": w.T.pk,
                                  "source": w.S.pk, "files": [{"i": 0, "name": disk.name, "size": nbytes,
                                                               "role": "disk", "sha256": "0" * 64}],
                                  "bytes_total": nbytes, "created": int(time.time())})
        server = uvicorn.Server(uvicorn.Config(w.app, host="127.0.0.1", port=0, log_level="warning",
                                               lifespan="off", access_log=False))
        serve = asyncio.create_task(server.serve())
        while not server.started:
            await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]

        loop = asyncio.get_running_loop()
        lags, stop = [], threading.Event()

        async def probe():
            while not stop.is_set():
                t0 = loop.time()
                await asyncio.sleep(0.005)
                lags.append(max(0.0, loop.time() - t0 - 0.005))

        path = f"/api/vmhost/transfer/{mig}/0"
        result = {}

        def pull():
            got, t0 = 0, time.monotonic()
            with httpx.Client(timeout=120) as c:
                with c.stream("GET", f"http://127.0.0.1:{port}{path}",
                              headers={"Authorization": migrate.nip98_header(T_SK, S_HTTPS + path)}) as r:
                    result["status"] = r.status_code
                    for b in r.iter_bytes(1 << 20):
                        got += len(b)
            result.update(bytes=got, seconds=time.monotonic() - t0)
            stop.set()

        p = asyncio.create_task(probe())
        await asyncio.sleep(0.2)
        idle = list(lags)
        lags.clear()
        await asyncio.to_thread(pull)
        await p
        server.should_exit = True
        await serve
        lags_ms = sorted(x * 1000 for x in lags)
        return {"status": result["status"], "bytes": result["bytes"], "seconds": round(result["seconds"], 2),
                "MiB_per_s": round(result["bytes"] / (1 << 20) / max(result["seconds"], 1e-6), 1),
                "lag_max_ms": round(lags_ms[-1], 2), "lag_p99_ms": round(lags_ms[int(len(lags_ms) * 0.99) - 1], 2),
                "lag_median_ms": round(statistics.median(lags_ms), 2),
                "idle_lag_max_ms": round(max(idle) * 1000, 2) if idle else 0.0, "samples": len(lags_ms)}
    finally:
        await w.close()


def test_a_large_transfer_does_not_stall_the_event_loop(tmp_path):
    nbytes = int(os.environ.get("VMHOST_LAG_BYTES") or 64 << 20)
    base = Path(os.environ["VMHOST_LAG_DIR"]) if os.environ.get("VMHOST_LAG_DIR") else tmp_path
    base.mkdir(parents=True, exist_ok=True)
    try:
        stats = asyncio.run(_measure(base / f"lag-{os.getpid()}", nbytes))
    finally:
        import shutil
        shutil.rmtree(base / f"lag-{os.getpid()}", ignore_errors=True)
    print("\n[vmhost transfer lag]", stats)
    assert stats["status"] == 200 and stats["bytes"] == nbytes
    # Reads are 1 MiB each in a worker thread; the loop only ever hands bytes to the socket. A stall of
    # a quarter second would mean something is reading or hashing ON the loop.
    if stats["lag_max_ms"] >= 250:
        pytest.fail(f"the transfer stalled the event loop: {stats}")
