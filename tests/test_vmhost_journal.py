"""The op journal: an honest retry returns the stored answer, and nothing ELSE does — without the
journal's disk write stalling the event loop the whole app runs on.

  * `run_once` was keyed on (requester, id) and compared only the op NAME, so a client that reused an id
    for `vm.power start` on a DIFFERENT VM (a bug, or a crafted request) was told "done" and nothing
    happened — or, worse, was handed the stored result of another VM's operation.
  * `_persist` rewrote the whole journal file synchronously inside the coroutine, on the single uvicorn
    worker's loop, once per successful mutating op.
"""
import asyncio
import threading

from app.services.vmhost import domainxml
from app.services.vmhost import journal as journal_mod
from app.services.vmhost.config import VmHostConfig
from app.services.vmhost.journal import OpJournal
from app.services.vmhost.service import VmHostService
from app.services.vmhost.storage import Storage
from tests.vmhost_fake import FakeBackend

ADMIN = "1b" * 32
U1 = "11111111-1111-4111-8111-111111111111"
U2 = "22222222-2222-4222-8222-222222222222"


def make(tmp_path):
    root = tmp_path / "vms"
    st = Storage(root)
    st.ensure()
    cfg = VmHostConfig(enabled=True, storage_dir=str(root), admin_pubkeys=[ADMIN])
    be = FakeBackend()
    for u, n in ((U1, "alpha"), (U2, "beta")):
        (root / u).mkdir()
        be.add_domain(u, n, meta=domainxml.VmMeta(owner=ADMIN))

    async def admins():
        return set()
    return VmHostService(cfg, be, node_pubkey="0a" * 32, admin_provider=admins, storage=st), be


def test_a_reused_id_with_different_arguments_is_refused_not_answered(tmp_path):
    svc, be = make(tmp_path)

    async def go():
        first = await svc.handle(ADMIN, "vm.power", {"vm": U1, "action": "start"}, "same-id")
        other = await svc.handle(ADMIN, "vm.power", {"vm": U2, "action": "start"}, "same-id")
        retry = await svc.handle(ADMIN, "vm.power", {"action": "start", "vm": U1}, "same-id")
        return first, other, retry
    first, other, retry = asyncio.run(go())
    assert first["ok"] is True
    assert other["ok"] is False and other["error"]["code"] == "bad_request", \
        "U2 was reported started from U1's stored result"
    assert be.domains[U2]["state"] == "shutoff"
    assert retry == first, "an honest retry (same op, same args, any key order) still gets the stored answer"


def test_a_mismatched_call_is_refused_while_the_first_is_still_running(tmp_path):
    j = OpJournal(tmp_path / "ops.jsonl")

    async def go():
        gate = asyncio.Event()

        async def slow():
            await gate.wait()
            return {"ok": True, "v": 1}
        first = asyncio.create_task(j.run_once("r", "id", "vm.power", slow, args_hash="A"))
        await asyncio.sleep(0.01)
        other = await j.run_once("r", "id", "vm.power", slow, args_hash="B")
        gate.set()
        return await first, other
    first, other = asyncio.run(go())
    assert first["ok"] is True and other is None


def test_the_journal_is_written_off_the_event_loop_and_reloads_with_its_hashes(tmp_path, monkeypatch):
    path = tmp_path / "ops.jsonl"
    j = OpJournal(path)
    writers = []
    real_replace = journal_mod.os.replace
    monkeypatch.setattr(journal_mod.os, "replace",
                        lambda a, b: writers.append(threading.current_thread()) or real_replace(a, b))

    async def go():
        loop_thread = threading.current_thread()

        async def ok():
            return {"ok": True, "result": {"x": 1}}
        await j.run_once("r", "id-1", "vm.assign", ok, args_hash="H1")
        return loop_thread
    loop_thread = asyncio.run(go())
    assert writers, "nothing was persisted"
    assert all(w is not loop_thread for w in writers), "the journal file was rewritten on the event loop"

    again = OpJournal(path)                     # a restarted process

    async def replay():
        async def never():
            raise AssertionError("a journaled op ran twice")
        same = await again.run_once("r", "id-1", "vm.assign", never, args_hash="H1")
        diff = await again.run_once("r", "id-1", "vm.assign", never, args_hash="H2")
        return same, diff
    same, diff = asyncio.run(replay())
    assert same == {"ok": True, "result": {"x": 1}} and diff is None
