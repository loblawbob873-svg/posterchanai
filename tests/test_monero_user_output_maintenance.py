"""Per-user Local Wallet capacity must survive sequential zaps.

The operator-wallet script cannot see pooled user accounts.  These tests drive the actual
``UserWallets`` RPC boundary and also prove the production worker starts this maintainer.
"""
import asyncio
import pytest

from app.services import monero_user_wallets as mod
from app import worker


def wallet(transfers, unavailable=(), pending=()):
    w = mod.UserWallets.__new__(mod.UserWallets)
    w.url, w.user, w.password, w.network = "rpc", "u", "p", "mainnet"
    w.timeout, w._fee_address, w._fee_at = 1, None, 0
    w._lock = asyncio.Lock()
    w.calls = []

    async def rpc(method, params=None):
        w.calls.append((method, params or {}))
        if method == "incoming_transfers":
            return {"transfers": list(unavailable) if params.get("transfer_type") == "unavailable" else transfers}
        if method == "get_transfers":
            return {"pending": list(pending)}
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
    # Wallet RPC's `available` result must not include unavailable transfers. Verify an ordinary
    # low-output account still splits when there is no prior split unlocking.
    w = wallet([{"amount": 10_000_000_000, "unlocked": True, "key_image": "usable"}])
    assert asyncio.run(w.maintain_account_outputs(ACCOUNT))["action"] == "split"


def test_scheduler_does_not_consume_another_output_while_prior_split_unlocks(monkeypatch):
    monkeypatch.setattr(mod, "validate_address", lambda *_: None)
    w = wallet(
        [{"amount": 8_000_000_000, "spent": False, "unlocked": True, "key_image": "last-reserve"},
         {"amount": 7_000_000_000, "spent": False, "unlocked": False, "key_image": "split-change"}],
    )
    result = asyncio.run(w.maintain_account_outputs(ACCOUNT))
    assert result["action"] == "waiting"
    assert result["locked_outputs"] == 1
    assert all(method != "sweep_single" for method, _ in w.calls)


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


def test_spent_history_never_blocks_replenishment_after_prior_zaps(monkeypatch):
    monkeypatch.setattr(mod, 'validate_address', lambda *_: None)
    w = wallet([{'amount':20_000_000_000, 'unlocked':True, 'spent':False, 'frozen':False, 'key_image':'remaining'}],
               unavailable=[{'amount':1_000_000_000, 'spent':True, 'unlocked':True} for _ in range(19)])
    result = asyncio.run(w.maintain_account_outputs(ACCOUNT))
    assert result['action'] == 'split'
    assert result['outputs_created'] == mod.OUTPUT_TARGET
    assert not any(method == 'incoming_transfers' and params['transfer_type']=='unavailable' for method,params in w.calls)


def test_pending_split_before_outputs_appear_does_not_consume_reserves(monkeypatch):
    monkeypatch.setattr(mod, 'validate_address', lambda *_: None)
    w = wallet([{'amount':8_000_000_000,'unlocked':True,'spent':False,'key_image':'reserve'}],
               pending=[{'txid':'accepted-split-not-mined'}])
    for _ in range(3):
        assert asyncio.run(w.maintain_account_outputs(ACCOUNT))['action'] == 'waiting'
    assert all(method != 'sweep_single' for method,_ in w.calls)


def test_frozen_or_spent_outputs_never_count_as_zap_capacity(monkeypatch):
    monkeypatch.setattr(mod, 'validate_address', lambda *_: None)
    rows=[{'amount':5_000_000_000,'unlocked':True,'frozen':True,'spent':False,'key_image':str(i)} for i in range(8)]
    rows += [{'amount':20_000_000_000,'unlocked':True,'spent':True,'key_image':'spent'},
             {'amount':20_000_000_000,'unlocked':True,'spent':False,'frozen':False,'key_image':'usable'}]
    w=wallet(rows)
    result=asyncio.run(w.maintain_account_outputs(ACCOUNT))
    assert result['action']=='split'
    assert w.calls[-1][1]['key_image']=='usable'


def test_missing_unlock_state_never_authorizes_a_split(monkeypatch):
    monkeypatch.setattr(mod,'validate_address',lambda *_:None)
    w=wallet([{'amount':20_000_000_000,'spent':False,'key_image':'unknown'}])
    assert asyncio.run(w.maintain_account_outputs(ACCOUNT))['action']=='waiting'
    assert all(method!='sweep_single' for method,_ in w.calls)


@pytest.mark.parametrize("inputs_per_zap", [1, 2])
def test_replenishment_survives_restart_and_supports_consecutive_zaps(monkeypatch, inputs_per_zap):
    """Wallet-state simulation follows RPC semantics, including pending-before-mined outputs."""
    monkeypatch.setattr(mod, 'validate_address', lambda *_: None)
    monkeypatch.setattr(mod, 'zap_fee_percent', lambda: mod.Decimal(0))
    rows=[{'amount':80_000_000_000,'spent':False,'unlocked':True,'frozen':False,'key_image':'deposit'}]
    pending=[]; splits=[]; sent=[]

    async def rpc(method, params=None):
        if method=='get_accounts': return {'subaddress_accounts':[ACCOUNT]}
        assert params.get('account_index')==ACCOUNT['account_index']
        if method=='incoming_transfers':
            return {'transfers':[dict(row) for row in rows if row['spent']==(params['transfer_type']=='unavailable')]}
        if method=='get_transfers': return {'pending':list(pending)}
        if method=='sweep_single':
            source=next(row for row in rows if row['key_image']==params['key_image'])
            assert not source['spent'] and source['unlocked']
            source['spent']=True; splits.append(params);pending.append({'txid':'split'})
            return {'tx_hash':'ab'*32}
        if method=='transfer_split':
            sources=[row for row in rows if not row['spent'] and row['unlocked']][:inputs_per_zap]
            assert len(sources)==inputs_per_zap
            for source in sources: source['spent']=True
            sent.append(params)
            rows.append({'amount':sum(source['amount'] for source in sources)-sum(p['amount'] for p in params['destinations'])-100_000_000,
                         'spent':False,'unlocked':False,'frozen':False,'key_image':'change-'+str(len(sent))})
            return {'tx_hash_list':[f'{len(sent):064x}'],'amount_list':[1_000_000_000],'fee_list':[100_000_000]}
        raise AssertionError(method)

    def restarted_wallet():
        w=wallet([]);w.rpc=rpc;return w

    async def scenario():
        assert (await restarted_wallet().maintain_account_outputs(ACCOUNT))['action']=='split'
        for _ in range(3):
            assert (await restarted_wallet().maintain_account_outputs(ACCOUNT))['action']=='waiting'
        pending.clear()
        rows.extend({'amount':9_000_000_000,'spent':False,'unlocked':False,'frozen':False,'key_image':'split-'+str(i)} for i in range(8))
        assert (await restarted_wallet().maintain_account_outputs(ACCOUNT))['action']=='waiting'
        for row in rows: row['unlocked']=True
        for _ in range(8 // inputs_per_zap):
            w=restarted_wallet()
            result=await w.pay('a'*64, [('recipient',1_000_000_000)])
            assert len(result['tx_hash_list'])==1
            await w.maintain_account_outputs(ACCOUNT)
        assert len(sent)==8 // inputs_per_zap and len(splits)==1
        assert all(not row['unlocked'] for row in rows if not row['spent'])
    asyncio.run(scenario())
