"""`sessions.json` must stay small and its writes must never stall the event loop.

Session keys exist for clients that poll: a client that loses its key opens another, and a buggy (or hostile but
allowed) one opens thousands. The registry used to KEEP every closed or expired entry for a grace day, rewrite
the whole growing file synchronously on the event loop at every open, and had no global bound — so ten thousand
opens by ONE allowed user meant a multi-megabyte file rewritten ten thousand times inside the single worker that
also serves every other request.

Now: an ended session leaves the file at once (a small in-memory tombstone keeps answering `session_expired`),
live sessions are capped per owner and globally, and the file is written off the loop (tmp + fsync + rename,
coalesced).
"""
import asyncio
import json
import time

import pytest

from app.services.vmhost import sessions
from app.services.vmhost.service import VmHostError
from tests.test_vmhost_phase2 import NODE, USER, make


def run(coro):
    return asyncio.run(coro)


def fast_open_args(i):
    """A session.open for synthetic key i. Deriving 10k real keys is ~24ms each in pure Python, which would measure
    the test, not the registry; the proof's SIGNATURE check is stubbed here (it is covered in test_vmhost_phase2)."""
    spk = (0x5000 + i).to_bytes(32, "big").hex()
    exp = int(time.time()) + 3600
    return spk, {"pk": spk, "exp": exp, "scope": "use",
                 "proof": {"pubkey": spk, "kind": sessions.PROOF_KIND, "content": sessions.proof_content(USER, exp, NODE),
                           "id": "00" * 32, "sig": "00" * 64, "created_at": int(time.time()), "tags": []}}


def test_ten_thousand_opens_keep_the_file_bounded_and_never_stall_the_loop(tmp_path, monkeypatch):
    from app.services.nostr import event as nostr_event
    monkeypatch.setattr(nostr_event, "verify_event", lambda ev: True)
    svc, be, root = make(tmp_path)

    async def go():
        loop = asyncio.get_running_loop()
        gaps, stop = [], asyncio.Event()

        async def ticker():
            last = loop.time()
            while not stop.is_set():
                await asyncio.sleep(0.002)
                now = loop.time()
                gaps.append(now - last)
                last = now
        tick = asyncio.create_task(ticker())
        start = time.monotonic()
        for i in range(10_000):
            spk, args = fast_open_args(i)
            res = await svc.handle(USER, "session.open", args, f"o{i}")
            assert res["ok"], res
            if i % 500 == 0:
                await asyncio.sleep(0)
                assert time.monotonic() - start < 90, f"only {i} opens in 90s — the registry is quadratic"
        await asyncio.sleep(0.2)                     # let the last coalesced write land
        stop.set()
        await tick
        return gaps
    gaps = run(go())
    path = root / ".state" / "sessions.json"
    data = json.loads(path.read_text())
    assert len(data["sessions"]) <= sessions.MAX_PER_OWNER, len(data["sessions"])
    assert path.stat().st_size < 16_000, path.stat().st_size
    assert max(gaps) < 0.25, f"the event loop stalled for {max(gaps):.3f}s"
    # the newest session still works; one closed by the cap is ANSWERED expired, not forgotten
    assert svc._sessions().lookup(fast_open_args(9_999)[0]) == (USER, "live")
    assert svc._sessions().lookup(fast_open_args(9_990)[0]) == (USER, "expired")


def test_the_registry_has_a_global_cap(tmp_path):
    reg = sessions.SessionRegistry(tmp_path / "sessions.json")
    exp = int(time.time()) + 3600
    for i in range(sessions.MAX_SESSIONS):
        reg.open((i + 1).to_bytes(32, "big").hex(), (10**9 + i).to_bytes(32, "big").hex(), exp)
    with pytest.raises(VmHostError) as e:
        reg.open("ff" * 32, "ee" * 32, exp)
    assert e.value.code in ("busy", "rate_limited")
    run(reg.flush())
    assert len(json.loads((tmp_path / "sessions.json").read_text())["sessions"]) == sessions.MAX_SESSIONS


def test_ended_sessions_leave_the_file_immediately_and_tombstones_are_bounded(tmp_path):
    t = [time.time()]
    reg = sessions.SessionRegistry(tmp_path / "sessions.json", now=lambda: t[0])
    owner = "ab" * 32
    for i in range(200):
        spk = (5000 + i).to_bytes(32, "big").hex()
        reg.open(owner, spk, int(t[0]) + 600)
        reg.close(owner, spk)
    run(reg.flush())
    assert json.loads((tmp_path / "sessions.json").read_text())["sessions"] == {}
    assert len(reg._ended) <= sessions.MAX_ENDED_PER_OWNER
    reg.open(owner, "cd" * 32, int(t[0]) + 600)
    t[0] += 601
    assert reg.lookup("cd" * 32) == (owner, "expired")
    run(reg.flush())
    assert json.loads((tmp_path / "sessions.json").read_text())["sessions"] == {}
