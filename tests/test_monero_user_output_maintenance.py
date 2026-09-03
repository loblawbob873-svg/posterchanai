"""Per-user Local Wallet capacity must survive sequential zaps.

The operator-wallet script cannot see pooled user accounts.  These tests drive the actual
``UserWallets`` RPC boundary and also prove the production worker starts this maintainer.
"""
import asyncio

from app.services import monero_user_wallets as mod
from app import worker


def wallet(transfers):
    w = mod.UserWallets.__new__(mod.UserWallets)
    w.url, w.user, w.password, w.network = "rpc", "u", "p", "mainnet"
    w.timeout, w._fee_address, w._fee_at = 1, None, 0
    w._lock = asyncio.Lock()
    w.calls = []

    async def rpc(method, params=None):
        w.calls.append((method, params or {}))
        if method == "incoming_transfers":
            return {"transfers": transfers}
        if method == "sweep_single":
            return {"tx_hash": "ab" * 32}
        if method == "get_accounts":
            return {"subaddress_accounts": []}
        raise AssertionError(method)
    w.rpc = rpc
    return w


ACCOUNT = {"account_index": 7, "label": "pc:" + "a" * 64,
           "base_address": "4" + "A" * 94}


def test_low_capacity_splits_one_output_and_preserves_the_rest(monkeypatch):
    monkeypatch.setattr(mod, "validate_address", lambda *_: None)
    transfers = [
        {"amount": 9_000_000_000, "unlocked": True, "key_image": "largest"},
        {"amount": 2_000_000_000, "unlocked": True, "key_image": "reserve-a"},
        {"amount": 2_000_000_000, "unlocked": True, "key_image": "reserve-b"},
        # Locked change is not sequential-zap capacity.
        {"amount": 99_000_000_000, "unlocked": False, "key_image": "locked"},
    ]
    w = wallet(transfers)
    result = asyncio.run(w.maintain_account_outputs(ACCOUNT))
    assert result["action"] == "split"
    method, params = w.calls[-1]
    assert method == "sweep_single", "sweep_all would lock every remaining zap output"
    assert params["key_image"] == "largest"
    assert params["account_index"] == 7
    assert params["outputs"] == 6  # two reserves stay usable; six replenish target eight
    assert result["tx_hash_list"] == ["ab" * 32]
    assert len(transfers) - 1 >= 2, "maintenance did not leave capacity for sequential zaps"


def test_healthy_capacity_never_pays_a_maintenance_fee(monkeypatch):
    monkeypatch.setattr(mod, "validate_address", lambda *_: None)
    w = wallet([{"amount": 2_000_000_000, "unlocked": True, "key_image": str(i)}
                for i in range(mod.OUTPUT_LOW_WATER)])
    assert asyncio.run(w.maintain_account_outputs(ACCOUNT))["action"] == "healthy"
    assert all(method != "sweep_single" for method, _ in w.calls)


def test_missing_key_images_fail_closed_instead_of_sweeping_everything(monkeypatch):
    monkeypatch.setattr(mod, "validate_address", lambda *_: None)
    w = wallet([{"amount": 20_000_000_000, "unlocked": True}])
    assert asyncio.run(w.maintain_account_outputs(ACCOUNT))["action"] == "waiting"
    assert all(method not in {"sweep_all", "sweep_single"} for method, _ in w.calls)


def test_locked_outputs_do_not_fake_sequential_zap_capacity(monkeypatch):
    monkeypatch.setattr(mod, "validate_address", lambda *_: None)
    w = wallet([
        {"amount": 10_000_000_000, "unlocked": True, "key_image": "usable"},
        *({"amount": 10_000_000_000, "unlocked": False, "key_image": f"locked-{i}"}
          for i in range(20)),
    ])
    assert asyncio.run(w.maintain_account_outputs(ACCOUNT))["action"] == "split"


def test_production_worker_wires_the_per_user_maintainer():
    assert ("monero-user-outputs", "app.services.monero_user_wallets",
            "start_user_wallet_output_scheduler") in worker._SCHEDULERS


def test_scheduler_is_immediate_single_instance_and_idempotent(monkeypatch):
    class FakeScheduler:
        def __init__(self, **kwargs): self.jobs, self.started = [], False
        def add_job(self, fn, trigger, **kwargs): self.jobs.append((fn, kwargs))
        def start(self): self.started = True
    monkeypatch.setattr("apscheduler.schedulers.asyncio.AsyncIOScheduler", FakeScheduler)
    mod.user_wallet_output_scheduler = None
    first = mod.start_user_wallet_output_scheduler()
    second = mod.start_user_wallet_output_scheduler()
    assert first is second and first.started
    _, options = first.jobs[0]
    assert options["max_instances"] == 1 and options["coalesce"] is True
    assert options["next_run_time"] is not None
    mod.user_wallet_output_scheduler = None
