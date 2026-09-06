"""Decode real SDK signatures against controlled RPC fixtures; never use live funds."""
import base64
import json

import httpx
import pytest

from app.services import exodus_account_send as A, exodus_send_service as S


@pytest.fixture
def anyio_backend():
    return 'asyncio'


KEY = bytes.fromhex('01' * 32)


def sol_fixture(monkeypatch, *, overrides=None, lost_ack=False):
    from solders.hash import Hash
    from solders.keypair import Keypair
    from solders.transaction import VersionedTransaction
    source = str(Keypair.from_seed(KEY).pubkey())
    target = str(Keypair.from_seed(bytes.fromhex('02' * 32)).pubkey())
    calls, checkpoints, raw = [], [], []
    values = {'getGenesisHash': A.SOLANA_MAINNET,
              'getLatestBlockhash': {'context': {'slot': 100}, 'value': {
                  'blockhash': str(Hash.from_bytes(bytes.fromhex('03' * 32))), 'lastValidBlockHeight': 200}},
              'getFeeForMessage': {'value': 5000}, 'getBalance': {'value': 10**9},
              'simulateTransaction': {'value': {'err': None}}}
    values.update(overrides or {})
    def handle(request):
        call = json.loads(request.content); method = call['method']; calls.append(method)
        if method == 'sendTransaction':
            transaction = VersionedTransaction.from_bytes(base64.b64decode(call['params'][0]))
            raw.append(transaction)
            assert checkpoints[0][0] == str(transaction.signatures[0])
            assert all(transaction.verify_with_results())
            if lost_ack:
                raise httpx.ReadTimeout('lost acknowledgement', request=request)
            answer = str(transaction.signatures[0])
        else:
            answer = values[method]
        return httpx.Response(200, json={'result': answer})
    monkeypatch.setattr(S, '_client', lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    async def checkpoint(tx_hash, nonce, **metadata):
        checkpoints.append((tx_hash, nonce, metadata))
    async def send(**changes):
        args = dict(private_key=KEY, to=target, units=123456, endpoint='https://fixture.invalid',
                    from_address=source, request_id='ab'*16, before_broadcast=checkpoint)
        args.update(changes)
        return await A.send_solana(**args)
    return send, calls, raw, checkpoints, source, target


@pytest.mark.anyio
async def test_solana_signed_message_has_exact_recipient_amount_and_unique_payment_memo(monkeypatch):
    send, calls, raw, checkpoints, source, target = sol_fixture(monkeypatch)
    result = await send()
    tx = raw[0]; message = tx.message
    transfer, memo = message.instructions
    assert str(message.account_keys[0]) == source
    assert str(message.account_keys[transfer.accounts[1]]) == target
    assert str(message.account_keys[transfer.program_id_index]) == '11111111111111111111111111111111'
    assert bytes(transfer.data) == (2).to_bytes(4, 'little') + (123456).to_bytes(8, 'little')
    assert str(message.account_keys[memo.program_id_index]) == A.SOLANA_MEMO
    assert bytes(memo.data) == ('cloudos:' + 'ab'*16).encode()
    assert result['hash'] == str(tx.signatures[0]) and result['pending'] is True
    assert checkpoints[0][2]['solana']['lastValidBlockHeight'] == 200
    # A second intentional payment with the same blockhash must get a different signature.
    checkpoints.clear()
    second = await send(request_id='cd'*16)
    assert second['hash'] != result['hash']


@pytest.mark.anyio
@pytest.mark.parametrize('override', [
    {'getGenesisHash': 'testnet'}, {'getFeeForMessage': {'value': None}},
    {'getBalance': {'value': 123456}}, {'simulateTransaction': {'value': {'err': {'InstructionError': [0, 'fail']}}}},
    {'simulateTransaction': {'value': {}}}, {'getBalance': {'value': True}},
])
async def test_solana_preflight_failures_never_checkpoint_or_submit(monkeypatch, override):
    send, calls, raw, checkpoints, _, _ = sol_fixture(monkeypatch, overrides=override)
    with pytest.raises(S.SendRefused):
        await send()
    assert not checkpoints and not raw and 'sendTransaction' not in calls


@pytest.mark.anyio
async def test_solana_lost_ack_preserves_locally_signed_identity(monkeypatch):
    send, calls, raw, checkpoints, _, _ = sol_fixture(monkeypatch, lost_ack=True)
    with pytest.raises(S.SendUnsure, match='may have been submitted') as error:
        await send()
    assert checkpoints[0][0] in str(error.value)
    assert len(raw) == 1 and calls.count('sendTransaction') == 1


def xrp_fixture(monkeypatch, *, overrides=None, target_flags=0, lost_ack=False):
    from bip_utils import Secp256k1PrivateKey
    from xrpl.constants import CryptoAlgorithm
    from xrpl.core.binarycodec import decode, encode_for_signing
    from xrpl.core.keypairs import is_valid_message
    from xrpl.models.transactions import Payment
    from xrpl.wallet import Wallet
    public = Secp256k1PrivateKey.FromBytes(KEY).PublicKey().RawCompressed().ToHex().upper()
    source = Wallet(public_key=public, private_key='00' + KEY.hex(), algorithm=CryptoAlgorithm.SECP256K1).address
    target = 'rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh'
    calls, checkpoints, raw = [], [], []
    values = {'server_info': {'info': {'network_id': 0, 'validated_ledger': {
        'seq': 1000, 'age': 1, 'reserve_base_xrp': 1, 'reserve_inc_xrp': 0.2}}},
        'fee': {'drops': {'open_ledger_fee': '12'}}}
    values.update(overrides or {})
    def handle(request):
        call = json.loads(request.content); method = call['method']; calls.append(method)
        if method == 'submit':
            payment = decode(call['params'][0]['tx_blob']); raw.append(payment)
            assert is_valid_message(bytes.fromhex(encode_for_signing(payment)),
                                    bytes.fromhex(payment['TxnSignature']), payment['SigningPubKey'])
            tx_hash = Payment.from_xrpl(payment).get_hash()
            assert checkpoints[0][0] == tx_hash
            if lost_ack:
                raise httpx.ReadTimeout('lost acknowledgement', request=request)
            answer = {'tx_json': {'hash': tx_hash}, 'engine_result': 'tesSUCCESS'}
        elif method == 'account_info':
            address = call['params'][0]['account']
            answer = {'validated': True, 'account_data': {'Account': address, 'Sequence': 7,
                      'OwnerCount': 2, 'Balance': '100000000', 'Flags': target_flags if address == target else 0}}
            answer = values.get('source' if address == source else 'target', answer)
        else:
            answer = values[method]
        return httpx.Response(200, json={'result': answer})
    monkeypatch.setattr(S, '_client', lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    async def checkpoint(tx_hash, nonce, **metadata):
        checkpoints.append((tx_hash, nonce, metadata))
    async def send(**changes):
        args = dict(private_key=KEY, to=target, units=2000000, endpoint='https://fixture.invalid',
                    from_address=source, before_broadcast=checkpoint)
        args.update(changes)
        return await A.send_xrp(**args)
    return send, calls, raw, checkpoints, source, target


@pytest.mark.anyio
async def test_xrp_signed_payment_exact_fields_and_destination_tag(monkeypatch):
    send, calls, raw, checkpoints, source, target = xrp_fixture(monkeypatch, target_flags=A.XRP_REQUIRE_DEST_TAG)
    result = await send(destination_tag=123)
    payment = raw[0]
    assert {k: payment[k] for k in ('Account', 'Destination', 'Amount', 'Fee', 'Sequence', 'LastLedgerSequence', 'DestinationTag')} == {
        'Account': source, 'Destination': target, 'Amount': '2000000', 'Fee': '12',
        'Sequence': 7, 'LastLedgerSequence': 1004, 'DestinationTag': 123}
    assert checkpoints[0][2]['xrp'] == {'firstLedger': 1000, 'lastLedger': 1004}
    assert result['pending'] is True and result['hash'] == checkpoints[0][0]


@pytest.mark.anyio
@pytest.mark.parametrize('override', [
    {'server_info': {'info': {'network_id': 1}}},
    {'server_info': {'info': {'network_id': 0, 'validated_ledger': {'age': 40}}}},
    {'fee': {'drops': {'open_ledger_fee': '10001'}}},
    {'fee': {'drops': {'open_ledger_fee': '0'}}},
])
async def test_xrp_wrong_network_stale_node_and_excess_fee_never_submit(monkeypatch, override):
    send, calls, raw, checkpoints, _, _ = xrp_fixture(monkeypatch, overrides=override)
    with pytest.raises(S.SendRefused):
        await send()
    assert not checkpoints and not raw and 'submit' not in calls


@pytest.mark.anyio
async def test_xrp_reserve_and_required_tag_checked_before_submit(monkeypatch):
    send, calls, raw, checkpoints, _, _ = xrp_fixture(monkeypatch, target_flags=A.XRP_REQUIRE_DEST_TAG)
    with pytest.raises(S.SendRefused, match='destination tag'):
        await send()
    with pytest.raises(S.SendRefused, match='reserve'):
        await send(units=99000000, destination_tag=1)
    assert not checkpoints and not raw


@pytest.mark.anyio
async def test_xrp_lost_ack_retains_signed_transaction_hash(monkeypatch):
    send, calls, raw, checkpoints, _, _ = xrp_fixture(monkeypatch, lost_ack=True)
    with pytest.raises(S.SendUnsure) as error:
        await send()
    assert checkpoints[0][0] in str(error.value) and calls.count('submit') == 1


def test_xrp_embedded_tags_and_testnet_addresses():
    from xrpl.core.addresscodec import classic_address_to_xaddress
    classic = 'rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh'
    xaddress = classic_address_to_xaddress(classic, 123, False)
    assert A.xrp_recipient(xaddress) == (classic, 123)
    assert A.xrp_recipient(xaddress, 123) == (classic, 123)
    with pytest.raises(S.SendRefused, match='does not match'):
        A.xrp_recipient(xaddress, 124)
    with pytest.raises(S.SendRefused, match='mainnet'):
        A.xrp_recipient(classic_address_to_xaddress(classic, 123, True))


@pytest.mark.anyio
@pytest.mark.parametrize('symbol', ['SOL', 'XRP'])
@pytest.mark.parametrize('outcome', ['accepted', 'failed', 'missing', 'provisional'])
async def test_status_requires_confirmed_network_result(monkeypatch, symbol, outcome):
    async def rpc(client, endpoint, method, params):
        if method == 'getGenesisHash': return A.SOLANA_MAINNET
        if method == 'server_info':
            return {'info': {'network_id': 0, 'validated_ledger': {'seq': 9000, 'age': 1}}}
        if method == 'getSignatureStatuses':
            return {'value': [None if outcome == 'missing' else {
                'confirmationStatus': 'processed' if outcome == 'provisional' else 'finalized',
                'err': 'failed' if outcome == 'failed' else None}]}
        assert method == 'tx'
        if outcome == 'missing': return {'error': 'txnNotFound'}
        return {'hash': 'signed-hash', 'validated': outcome != 'provisional',
                'meta': {'TransactionResult': 'tecFAILED' if outcome == 'failed' else 'tesSUCCESS'}}
    monkeypatch.setattr(S, '_rpc', rpc)
    answer = await A.transfer_status(None, 'fixture', symbol, {'hash': 'signed-hash',
             'solana': {'lastValidBlockHeight': 1}, 'xrp': {'lastLedger': 1}})
    assert answer['state'] == (outcome if outcome in ('accepted', 'failed') else 'unconfirmed')


@pytest.mark.anyio
@pytest.mark.parametrize('searched,head,expected', [(True, 1005, 'not_sent'), (True, 1004, 'unconfirmed'),
                                                  (False, 1005, 'unconfirmed'), (None, 1005, 'unconfirmed')])
async def test_xrp_expiry_needs_complete_history_and_a_validated_ledger_past_expiry(monkeypatch, searched, head, expected):
    async def rpc(client, endpoint, method, params):
        if method == 'server_info':
            return {'info': {'network_id': 0, 'validated_ledger': {'seq': head, 'age': 1}}}
        assert params == [{'transaction': 'hash', 'binary': False, 'min_ledger': 1000, 'max_ledger': 1004}]
        return {'error': 'txnNotFound', 'searched_all': searched}
    monkeypatch.setattr(S, '_rpc', rpc)
    result = await A.transfer_status(None, 'fixture', 'XRP', {'hash': 'hash', 'xrp': {'firstLedger':1000,'lastLedger':1004}})
    assert result['state'] == expected
