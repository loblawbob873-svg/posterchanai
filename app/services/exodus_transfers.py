"""Encrypted, durable transfer identities shared by this node's server workers."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re

from app.services import exodus_wallet_service as W, exodus_send_service as S
from app.services.exodus_monero import _write


def scope(user_id, symbol, address):
    # Importing the same seed twice must not create two independent nonce/spend locks.
    canonical = address.lower() if symbol in S.NETWORKS else address
    return hashlib.sha256(json.dumps([user_id, symbol, canonical]).encode()).hexdigest()


def _folder(identity):
    folder = Path(os.environ.get('EXODUS_TRANSFER_DIR', 'data/exodus-transfers')).resolve() / identity
    folder.mkdir(parents=True, mode=0o700, exist_ok=True)
    folder.parent.chmod(0o700); folder.chmod(0o700)
    return folder


@contextmanager
def _lock(folder):
    fd = os.open(folder / 'lock', os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise S.SendRefused('A transfer is already being processed for this wallet') from error
        yield
    finally:
        os.close(fd)


def _save(path, record, key):
    _write(path, W.seal(json.dumps(record), key).hex())


def _read(path, key):
    return json.loads(W.unseal(bytes.fromhex(path.read_text()), key))


def _token(value):
    if not re.fullmatch(r'[0-9a-f]{32}', value or ''):
        raise S.SendRefused('A transfer identifier is required; update the app and try again')
    return value


async def send(identity, key, token, to, units, operation, *, symbol=None, destination_tag=None):
    token = _token(token)
    folder = _folder(identity)
    path, pending = folder / (token + '.enc'), folder / 'pending'
    # Preserve the existing EVM fingerprint on disk. Base58 addresses are case-sensitive.
    details = [to if symbol in ('SOL', 'XRP') else to.lower(), units]
    if symbol == 'XRP':
        details.append(destination_tag)
    fingerprint = hashlib.sha256(json.dumps(details).encode()).hexdigest()
    with _lock(folder):
        if path.exists():
            previous = _read(path, key)
            if previous['fingerprint'] != fingerprint:
                raise S.SendRefused('This transfer identifier belongs to a different payment')
            if previous.get('result'):
                return previous['result']
            raise S.SendUnsure(previous.get('message') or 'This transfer is unconfirmed; check its status before another payment')
        if pending.exists():
            raise S.SendUnsure('A previous transfer is unconfirmed. Check its status before another payment')
        record = {'fingerprint': fingerprint, 'state': 'preparing', 'token': token}
        _save(path, record, key)
        _write(pending, token)

        async def before_broadcast(tx_hash, nonce, **metadata):
            if any(name not in ('solana', 'xrp') for name in metadata):
                raise ValueError('Unsupported transaction checkpoint metadata')
            record.update(state='broadcast', hash=tx_hash, nonce=nonce,
                          message=f'Transaction {tx_hash} may have been sent. Check its status before another payment.')
            record.update(metadata)
            _save(path, record, key)

        try:
            result = await operation(before_broadcast)
            record.update(state='broadcast' if result.get('pending') else 'accepted', result=result)
            _save(path, record, key)
            if not result.get('pending'):
                pending.unlink(missing_ok=True)
            return result
        except S.SendRefused:
            # send_evm reserves this exception for failures BEFORE its broadcast hook.
            if record['state'] != 'preparing':
                raise S.SendUnsure(record['message']) from None
            path.unlink(missing_ok=True); pending.unlink(missing_ok=True)
            raise
        except BaseException:
            # Cancellation/process loss keeps the durable state. Never issue another transaction
            # automatically because a caller stopped waiting.
            raise


async def status(identity, key, symbol, endpoint):
    folder = _folder(identity)
    pending = folder / 'pending'
    with _lock(folder):
        if not pending.exists():
            return {'state': 'idle'}
        token = _token(pending.read_text())
        path = folder / (token + '.enc')
        record = _read(path, key)
        if record.get('result') and not record['result'].get('pending'):
            pending.unlink()
            return {'state': 'accepted', **record['result']}
        if record['state'] == 'preparing':
            # Acquiring the lock proves the originating worker is no longer executing. No hash
            # was checkpointed, so the enforced hook proves it never reached broadcast.
            path.unlink(); pending.unlink()
            return {'state': 'not_sent'}
        async with S._client(S.RPC_TIMEOUT) as client:
            if symbol in ('SOL', 'XRP'):
                from app.services.exodus_account_send import transfer_status
                answer = await transfer_status(client, endpoint, symbol, record)
                if answer['state'] == 'not_sent':
                    path.unlink(); pending.unlink()
                    return answer
                if answer['state'] in ('accepted', 'failed'):
                    result = {**record.get('result', {}), **answer, 'pending': False}
                    record.update(state=answer['state'], result=result)
                    _save(path, record, key); pending.unlink()
                return answer
            chain = S._quantity(await S._rpc(client, endpoint, 'eth_chainId', []))
            if chain != S.NETWORKS.get(symbol):
                raise S.SendRefused('The RPC network does not match the selected asset')
            transaction = await S._rpc(client, endpoint, 'eth_getTransactionByHash', [record['hash']])
        if isinstance(transaction, dict) and transaction.get('hash', '').lower() == record['hash']:
            record.update(state='accepted', result={'hash': record['hash'], 'nonce': record['nonce']})
            _save(path, record, key); pending.unlink()
            return {'state': 'accepted', **record['result']}
        return {'state': 'unconfirmed', 'hash': record['hash']}
