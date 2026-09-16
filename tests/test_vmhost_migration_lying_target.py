"""A HOSTILE (or broken) TARGET must not be able to make the source give up its only copy.

`peer.migrate.commit` is the moment the source undefines its VM and moves the directory aside. The source used to
take the target's word that it held the VM: a target that committed without downloading a byte — or with a corrupt
copy — got the VM handed off, and after the keep window the source reaped the retained directory. Three things
stand in the way now, each pinned here:
  (a) the source tracks the bytes it SERVED per file since the transfer began and refuses a commit (and a
      challenge) until every file was served whole;
  (b) at commit the target must answer a CHALLENGE — SHA-256 over a fresh nonce and random ranges of every file,
      chosen by the source after the transfer — which the source checks against its own copy; a wrong answer
      aborts the migration and the VM stays here;
  (c) `vmhost_migration_keep_source_hours = 0` means KEEP until an admin deletes it, never "reap at once".
"""
import asyncio

from app.services.vmhost import migrate
from tests.vmhost_migration_fake import S, T, VM, World, seed_vm, sha, until


def run(coro):
    return asyncio.run(coro)


def state(host, mig):
    r = host.rec(mig)
    return r and r["state"]


async def begin(w, **extra):
    args = {"vm": VM, "target": T, "authz": w.authz(), "start_after": False}
    args.update(extra)
    res = await w.call(w.S, "vm.migrate", args)
    assert res["ok"], res
    return res["result"]["migration"]["id"]


async def as_target(w, op, args):
    """The target host's key, speaking directly — what a hostile target implementation would send."""
    return await w.T.migrator.rpc.call(w.T.migrator.peer(S), op, args, timeout=1.5, retries=0)


def test_a_target_that_commits_without_downloading_is_refused(tmp_path):
    async def go():
        w = World(tmp_path, timing={"contact_deadline": 60.0})
        try:
            disk = seed_vm(w.S, state="shutoff", snapshots=False)
            await w.S.svc.refresh_index()
            w.T.http.gate = asyncio.Event()                # the honest target code never downloads anything
            mig = await begin(w)
            await until(lambda: state(w.S, mig) == "transferring", what="transferring")
            r = await as_target(w, "peer.migrate.commit", {"migration": mig})
            assert r is not None and not r["ok"], r
            r = await as_target(w, "peer.migrate.challenge", {"migration": mig})
            assert r is not None and not r["ok"], "no challenge before every byte was served"
            r = await as_target(w, "peer.migrate.commit", {"migration": mig, "proofs": ["00" * 32] * 8})
            assert r is not None and not r["ok"], r
            assert state(w.S, mig) == "transferring" and VM in w.S.backend.domains and disk.exists()
            assert not (w.S.root / ".retained").exists()
            w.T.http.gate.set()
        finally:
            await w.close()
    run(go())


def test_a_target_whose_copy_fails_the_challenge_is_refused_and_the_vm_stays(tmp_path):
    async def go():
        w = World(tmp_path, timing={"contact_deadline": 60.0})
        try:
            disk = seed_vm(w.S, state="shutoff", snapshots=False)
            want = sha(disk)
            await w.S.svc.refresh_index()

            async def corrupt(u):                          # downloaded and verified — then the copy rots
                p = w.T.root / VM / "disk-vda.qcow2"
                p.write_bytes(b"\x00" * p.stat().st_size)
            w.T.backend.hooks["define"] = corrupt
            mig = await begin(w)
            await until(lambda: state(w.S, mig) in ("aborted", "handed_off", "done"), what="decided")
            assert state(w.S, mig) == "aborted", w.S.rec(mig)
            await until(lambda: state(w.T, mig) == "aborted", what="target told")
            assert VM in w.S.backend.domains and sha(disk) == want
            assert not (w.S.root / ".retained").exists() or not any((w.S.root / ".retained").iterdir())
            assert VM not in w.T.backend.domains
        finally:
            await w.close()
    run(go())


def test_a_challenge_is_bound_to_the_source_copy_and_a_guessed_answer_fails(tmp_path):
    async def go():
        w = World(tmp_path, timing={"contact_deadline": 60.0})
        try:
            seed_vm(w.S, state="shutoff", snapshots=False)
            await w.S.svc.refresh_index()
            real = w.T.migrator._answer_challenge

            def liar(rec, ch):
                return ["ab" * 32 for _ in real(rec, ch)]
            w.T.migrator._answer_challenge = liar
            mig = await begin(w)
            await until(lambda: state(w.S, mig) in ("aborted", "handed_off", "done"), what="decided")
            assert state(w.S, mig) == "aborted" and VM in w.S.backend.domains
        finally:
            await w.close()
    run(go())


def test_keep_zero_never_reaps_and_a_positive_window_still_does(tmp_path):
    async def go():
        w = World(tmp_path, keep_hours=0)
        try:
            seed_vm(w.S, state="shutoff", snapshots=False)
            await w.S.svc.refresh_index()
            mig = await begin(w)
            await until(lambda: state(w.S, mig) == "done" and state(w.T, mig) == "done", what="done")
            retained = w.S.root / ".retained" / w.S.rec(mig)["retained"]
            rec = w.S.migrator.store.get(mig)
            rec["acked_at"] = rec["acked_at"] - 10 * 365 * 86400
            await w.S.migrator.housekeeping()
            assert retained.exists(), "keep=0 means keep until an admin deletes it"
            assert migrate.MigrateConfig.from_settings({"vmhost_migration_keep_source_hours": "0"}).keep_source_hours == 0
            w.S.migrator.mcfg = migrate.MigrateConfig.from_settings({"vmhost_migration_keep_source_hours": "1"})
            await w.S.migrator.housekeeping()
            assert not retained.exists(), "an acked copy past a positive window is reaped"
        finally:
            await w.close()
    run(go())
