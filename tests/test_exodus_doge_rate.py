from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from app.services import exodus_doge_rate as R, exodus_utxo_provider as P, exodus_bitcoin_discovery as B
from app.services.exodus_send_service import SendRefused


@pytest.fixture
def anyio_backend():return 'asyncio'


@pytest.fixture(autouse=True)
def folder(tmp_path,monkeypatch):
    monkeypatch.setenv('EXODUS_TRANSFER_DIR',str(tmp_path))
    return tmp_path


def test_parallel_reservations_share_a_single_node_wide_schedule():
    with ThreadPoolExecutor(max_workers=8) as pool:
        delays=list(pool.map(lambda _:R.reserve(1000),range(8)))
    assert sorted(delays)==pytest.approx([index*.4 for index in range(8)])


@pytest.mark.parametrize('stored',['100000','nan','invalid','-10'])
def test_reboot_and_invalid_persisted_clock_do_not_block_forever(folder,stored):
    (folder/'dogecoin-rate').write_text(stored)
    assert R.reserve(100)==0


def test_full_queue_refuses_before_extending_its_deadline(folder):
    for _ in range(60):
        previous=(folder/'dogecoin-rate').read_text() if (folder/'dogecoin-rate').exists() else None
        try:R.reserve(1000)
        except SendRefused:
            assert (folder/'dogecoin-rate').read_text()==previous
            break
    else:raise AssertionError('queue was unbounded')
    assert R.reserve(1100)==0


def test_canceled_reservation_only_leaves_a_bounded_gap():
    assert R.reserve(1000)==0
    # The caller cancels rather than using its reserved instant.
    assert R.reserve(1000)==pytest.approx(.4)
    assert R.reserve(1002)==0


@pytest.mark.anyio
async def test_sends_and_discovery_call_the_same_rate_scheduler(monkeypatch):
    called=[]
    async def pace():called.append(1)
    monkeypatch.setattr(R,'pace',pace)
    one,two=P.Provider('DOGE',{}),P.Provider('DOGE',{})
    def handler(request):
        if request.url.path.endswith('/balance'):
            return httpx.Response(200,json={'address':'fixture','final_balance':0,'final_n_tx':0})
        return httpx.Response(200,json={'name':'DOGE.main','medium_fee_per_kb':1000000})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await one.network_and_fee(client)
        await two.network_and_fee(client)
        await B.other_address_state(client,'https://fixture.invalid','fixture','DOGE')
    assert len(called)==3
