"""SDK-built native SOL/XRP payments with local signing and durable broadcast identities."""
import base64
import re

from app.services import exodus_send_service as S

SOLANA_MAINNET = '5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d'
SOLANA_MEMO = 'MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr'
XRP_REQUIRE_DEST_TAG = 0x00020000
XRP_MAX_FEE = 10_000  # A plain native payment must not sign an unexpectedly expensive fee.


def _uint(value, maximum=2**64 - 1):
    if type(value) is not int or not 0 <= value <= maximum:
        raise S.SendRefused('The network returned an invalid integer')
    return value


def _integer_text(value):
    if not isinstance(value, str) or not re.fullmatch(r'0|[1-9][0-9]*', value):
        raise S.SendRefused('The network returned an invalid amount')
    return _uint(int(value))


def _xrp_drops(value):
    text = str(value)
    if not re.fullmatch(r'(?:0|[1-9][0-9]*)(?:\.[0-9]{1,6})?', text):
        raise S.SendRefused('The XRP network returned an unreadable reserve')
    whole, _, fraction = text.partition('.')
    return _uint(int(whole) * 1_000_000 + int(fraction.ljust(6, '0') or '0'))


def _value(response):
    if not isinstance(response, dict) or 'value' not in response:
        raise S.SendRefused('The network returned an unreadable response')
    return response['value']


async def send_solana(*, private_key, to, units, endpoint, from_address, request_id, before_broadcast):
    from solders.hash import Hash
    from solders.instruction import Instruction
    from solders.keypair import Keypair
    from solders.message import MessageV0, to_bytes_versioned
    from solders.pubkey import Pubkey
    from solders.system_program import TransferParams, transfer
    from solders.transaction import VersionedTransaction

    if not _uint(units) or not re.fullmatch(r'[0-9a-f]{32}', request_id or ''):
        raise S.SendRefused('A positive amount and transfer identifier are required')
    try:
        sender = Keypair.from_seed(private_key)
        target = Pubkey.from_string(to)
    except (ValueError, TypeError) as error:
        raise S.SendRefused('Enter a valid Solana address') from error
    if str(sender.pubkey()) != from_address:
        raise S.SendRefused('The sender address does not match the selected wallet')
    async with S._client(S.RPC_TIMEOUT) as client:
        if await S._rpc(client, endpoint, 'getGenesisHash', []) != SOLANA_MAINNET:
            raise S.SendRefused('The RPC network does not match Solana mainnet')
        latest = await S._rpc(client, endpoint, 'getLatestBlockhash', [{'commitment': 'confirmed'}])
        block = _value(latest)
        if not isinstance(block, dict):
            raise S.SendRefused('Solana could not supply a recent blockhash')
        expiry = _uint(block.get('lastValidBlockHeight'))
        slot = _uint((latest.get('context') or {}).get('slot'))
        try:
            blockhash = Hash.from_string(block.get('blockhash'))
        except (ValueError, TypeError) as error:
            raise S.SendRefused('Solana returned an invalid blockhash') from error
        instructions = [transfer(TransferParams(from_pubkey=sender.pubkey(), to_pubkey=target, lamports=units)),
                        # A random request token makes intentionally repeated payments distinct,
                        # even when the node returns the same recent blockhash for both.
                        Instruction(Pubkey.from_string(SOLANA_MEMO), ('cloudos:' + request_id).encode(), [])]
        message = MessageV0.try_compile(sender.pubkey(), instructions, [], blockhash)
        encoded_message = base64.b64encode(to_bytes_versioned(message)).decode()
        fee = _uint(_value(await S._rpc(client, endpoint, 'getFeeForMessage',
                         [encoded_message, {'commitment': 'confirmed', 'minContextSlot': slot}])))
        available = _uint(_value(await S._rpc(client, endpoint, 'getBalance',
                               [from_address, {'commitment': 'confirmed', 'minContextSlot': slot}])))
        if available < units + fee:
            raise S.SendRefused('Not enough SOL to cover this amount and its network fee')
        transaction = VersionedTransaction(message, [sender])
        encoded = base64.b64encode(bytes(transaction)).decode()
        simulation = _value(await S._rpc(client, endpoint, 'simulateTransaction',
                       [encoded, {'encoding': 'base64', 'sigVerify': True, 'commitment': 'confirmed',
                                  'minContextSlot': slot}]))
        if not isinstance(simulation, dict) or 'err' not in simulation or simulation['err'] is not None:
            raise S.SendRefused('Solana could not simulate this payment; nothing was submitted')
    signature = str(transaction.signatures[0])
    await before_broadcast(signature, None, solana={'blockhash': str(blockhash), 'lastValidBlockHeight': expiry, 'slot': slot})
    try:
        async with S._client(S.BROADCAST_TIMEOUT) as client:
            answer = await S._rpc(client, endpoint, 'sendTransaction', [encoded,
                        {'encoding': 'base64', 'skipPreflight': False, 'preflightCommitment': 'confirmed',
                         'minContextSlot': slot, 'maxRetries': 0}])
        if answer != signature:
            raise ValueError('Unconfirmed Solana transaction signature')
    except Exception as error:
        raise S.SendUnsure(f'Solana transaction {signature} may have been submitted. Check its status before another payment.') from error
    return {'hash': signature, 'fee': fee, 'units': units, 'to': str(target), 'pending': True}


async def _xrp_rpc(client, endpoint, method, params=None, *, missing=None):
    result = await S._rpc(client, endpoint, method, [params or {}])
    if not isinstance(result, dict):
        raise S.SendRefused('The XRP network returned an unreadable response')
    if result.get('error'):
        if missing and result['error'] == missing:
            return None
        raise S.SendRefused(f'The XRP network could not complete {method}')
    return result


async def xrp_server(client, endpoint):
    result = await _xrp_rpc(client, endpoint, 'server_info')
    info = result.get('info')
    if not isinstance(info, dict) or type(info.get('network_id')) is not int or info['network_id'] != 0:
        raise S.SendRefused('The RPC network does not match XRP mainnet')
    ledger = info.get('validated_ledger')
    if not isinstance(ledger, dict) or type(ledger.get('age')) not in (int, float) or not 0 <= ledger['age'] < 30:
        raise S.SendRefused('The XRP node is not synchronized with a recent validated ledger')
    _uint(ledger.get('seq'), 2**32 - 1)
    return info


def xrp_recipient(address, destination_tag=None):
    from xrpl.core.addresscodec import is_valid_classic_address, is_valid_xaddress, xaddress_to_classic_address
    if destination_tag is not None:
        _uint(destination_tag, 2**32 - 1)
    if is_valid_xaddress(address):
        classic, embedded_tag, testnet = xaddress_to_classic_address(address)
        if testnet:
            raise S.SendRefused('Use an XRP mainnet address')
        if destination_tag is not None and embedded_tag is not None and destination_tag != embedded_tag:
            raise S.SendRefused('The destination tag does not match this XRP address')
        return classic, embedded_tag if embedded_tag is not None else destination_tag
    if not is_valid_classic_address(address):
        raise S.SendRefused('Enter a valid XRP address')
    return address, destination_tag


async def send_xrp(*, private_key, to, units, endpoint, from_address, before_broadcast, destination_tag=None):
    from bip_utils import Secp256k1PrivateKey
    from xrpl.constants import CryptoAlgorithm
    from xrpl.core.binarycodec import encode
    from xrpl.models.transactions import Payment
    from xrpl.transaction import sign
    from xrpl.wallet import Wallet

    if not _uint(units, 100_000_000_000_000_000):
        raise S.SendRefused('The XRP amount must be greater than zero')
    target, tag = xrp_recipient(to, destination_tag)
    public = Secp256k1PrivateKey.FromBytes(private_key).PublicKey().RawCompressed().ToHex().upper()
    wallet = Wallet(public_key=public, private_key='00' + private_key.hex().upper(), algorithm=CryptoAlgorithm.SECP256K1)
    if wallet.address != from_address:
        raise S.SendRefused('The sender address does not match the selected wallet')
    async with S._client(S.RPC_TIMEOUT) as client:
        info = await xrp_server(client, endpoint)
        ledger = info['validated_ledger']
        current_ledger = _uint(ledger['seq'], 2**32 - 5)
        sender = await _xrp_rpc(client, endpoint, 'account_info', {'account': from_address, 'ledger_index': 'validated'})
        account = sender.get('account_data')
        if sender.get('validated') is not True or not isinstance(account, dict) or account.get('Account') != from_address:
            raise S.SendRefused('The selected XRP account could not be verified')
        sequence, owners = _uint(account.get('Sequence'), 2**32 - 1), _uint(account.get('OwnerCount'), 2**32 - 1)
        available = _integer_text(account.get('Balance'))
        fee_response = await _xrp_rpc(client, endpoint, 'fee')
        fee = _integer_text((fee_response.get('drops') or {}).get('open_ledger_fee'))
        if not 0 < fee <= XRP_MAX_FEE:
            raise S.SendRefused('The XRP network fee is unexpectedly high; try again when it falls')
        base_reserve = _xrp_drops(ledger.get('reserve_base_xrp'))
        reserve = base_reserve + owners * _xrp_drops(ledger.get('reserve_inc_xrp'))
        if available < units + fee + reserve:
            raise S.SendRefused('Not enough spendable XRP after the account reserve and network fee')
        destination = await _xrp_rpc(client, endpoint, 'account_info',
                                    {'account': target, 'ledger_index': 'validated'}, missing='actNotFound')
        if destination is None:
            if units < base_reserve:
                raise S.SendRefused('This new XRP account needs at least the network account reserve')
        else:
            data = destination.get('account_data')
            if destination.get('validated') is not True or not isinstance(data, dict) or data.get('Account') != target:
                raise S.SendRefused('The XRP recipient could not be verified')
            if _uint(data.get('Flags'), 2**32 - 1) & XRP_REQUIRE_DEST_TAG and tag is None:
                raise S.SendRefused('This XRP recipient requires a destination tag')
        payment = Payment(account=from_address, destination=target, amount=str(units), fee=str(fee), sequence=sequence,
                          last_ledger_sequence=current_ledger + 4, destination_tag=tag)
        signed = sign(payment, wallet)
        encoded, tx_hash = encode(signed.to_xrpl()), signed.get_hash()
    await before_broadcast(tx_hash, sequence, xrp={'firstLedger': current_ledger, 'lastLedger': current_ledger + 4})
    try:
        async with S._client(S.BROADCAST_TIMEOUT) as client:
            answer = await _xrp_rpc(client, endpoint, 'submit', {'tx_blob': encoded, 'fail_hard': True})
        result_hash = (answer.get('tx_json') or {}).get('hash') or answer.get('tx_hash')
        if result_hash != tx_hash or not isinstance(answer.get('engine_result'), str):
            raise ValueError('Unconfirmed XRP transaction hash')
    except Exception as error:
        raise S.SendUnsure(f'XRP transaction {tx_hash} may have been submitted. Check its status before another payment.') from error
    return {'hash': tx_hash, 'nonce': sequence, 'fee': fee, 'units': units, 'to': target,
            'destinationTag': tag, 'pending': True}


async def transfer_status(client, endpoint, symbol, record):
    """Only validated results release the spend lock; missing history is never proof of failure."""
    tx_hash = record['hash']
    if symbol == 'SOL':
        if await S._rpc(client, endpoint, 'getGenesisHash', []) != SOLANA_MAINNET:
            raise S.SendRefused('The RPC network does not match Solana mainnet')
        entries = _value(await S._rpc(client, endpoint, 'getSignatureStatuses',
                                    [[tx_hash], {'searchTransactionHistory': True}]))
        if not isinstance(entries, list) or len(entries) != 1:
            raise S.SendRefused('Solana returned an unreadable transaction status')
        transaction = entries[0]
        if isinstance(transaction, dict) and transaction.get('confirmationStatus') in ('confirmed', 'finalized'):
            if 'err' not in transaction:
                raise S.SendRefused('Solana returned an unreadable transaction result')
            return {'state': 'accepted' if transaction['err'] is None else 'failed', 'hash': tx_hash}
    elif symbol == 'XRP':
        info = await xrp_server(client, endpoint)
        window = record.get('xrp') or {}
        first, last = window.get('firstLedger'), window.get('lastLedger')
        params = {'transaction': tx_hash, 'binary': False}
        bounded = (type(first) is int and type(last) is int and 0 < first <= last < 2**32
                   and last - first <= 1000)
        if bounded:
            params.update(min_ledger=first, max_ledger=last)
        transaction = await S._rpc(client, endpoint, 'tx', [params])
        if not isinstance(transaction, dict):
            raise S.SendRefused('The XRP network returned an unreadable transaction status')
        if transaction.get('error'):
            if transaction['error'] != 'txnNotFound':
                raise S.SendRefused('The XRP network could not confirm this transaction')
            # XRPL explicitly proves absence only with searched_all for the entire signed
            # validity window, after a validated ledger has passed LastLedgerSequence.
            if bounded and transaction.get('searched_all') is True and info['validated_ledger']['seq'] > last:
                return {'state': 'not_sent', 'hash': tx_hash}
            return {'state': 'unconfirmed', 'hash': tx_hash}
        if transaction and transaction.get('validated') is True:
            # API v1 hash is at the top level; v2 exposes it alongside tx_json.
            if transaction.get('hash') != tx_hash:
                raise S.SendRefused('The XRP transaction response does not match this payment')
            metadata = transaction.get('meta')
            outcome = metadata.get('TransactionResult') if isinstance(metadata, dict) else None
            if not isinstance(outcome, str):
                raise S.SendRefused('XRP returned an unreadable validated transaction result')
            return {'state': 'accepted' if outcome == 'tesSUCCESS' else 'failed', 'hash': tx_hash}
    else:
        raise S.SendRefused('Unsupported transaction network')
    # An expired transaction can still have been included in history this RPC has pruned.
    # Keep the durable lock rather than permit an accidental duplicate payment.
    return {'state': 'unconfirmed', 'hash': tx_hash}
