"""Independent seed-derived Monero wallets, with bounded background synchronization.

Only the blockchain daemon is shared. Every wallet/portfolio has its own encrypted wallet
files and receives a private, temporary wallet-RPC process. No built-in wallet module,
account index, operator seed or pooled RPC endpoint participates in this service.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import subprocess
import time

import httpx

from app.services import exodus_wallet_service as W

_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix='exodus-xmr')
_JOBS = {}


class Unavailable(W.WalletError):
    pass


def _root():
    path = Path(os.environ.get('EXODUS_MONERO_DIR', 'data/exodus-monero')).resolve()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def _binary():
    value = os.environ.get('EXODUS_MONERO_RPC_BINARY', '') or shutil.which('monero-wallet-rpc')
    if not value:
        raise Unavailable('Monero wallet service is not installed on this node')
    return str(Path(value).resolve())


def _daemon():
    value = os.environ.get('EXODUS_MONERO_DAEMON', '').strip()
    if not value or '\n' in value or '\r' in value:
        raise Unavailable('Monero blockchain connection is not configured')
    return value


def identity(user_id, wallet_id, portfolio, address):
    return hashlib.sha256(json.dumps([user_id, address], separators=(',', ':')).encode()).hexdigest()


def _write(path, value):
    """Atomic private state; never leave a half-written wallet status or credential file."""
    path = Path(path)
    tmp = path.with_name(path.name + '.' + secrets.token_hex(8))
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, 'w') as out:
            out.write(value); out.flush(); os.fsync(out.fileno())
        os.replace(tmp, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        tmp.unlink(missing_ok=True)


@contextmanager
def _lock(path):
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise Unavailable('This Monero wallet is synchronizing; try again when it finishes') from error
        yield
    finally:
        os.close(fd)


@contextmanager
def _slot(root):
    from contextlib import ExitStack
    for index in range(4):
        with ExitStack() as stack:
            try:
                stack.enter_context(_lock(root / f'slot-{index}.lock'))
            except Unavailable:
                continue
            yield
            return
    raise Unavailable('Monero synchronization is queued')


class Rpc:
    def __init__(self, port, password):
        self.url = f'http://127.0.0.1:{port}/json_rpc'
        self.auth = httpx.DigestAuth('exodus', password)

    def call(self, method, params=None, *, timeout=15):
        with httpx.Client(timeout=timeout, trust_env=False, auth=self.auth) as client:
            response = client.post(self.url, json={'jsonrpc': '2.0', 'id': 'exodus',
                                                   'method': method, 'params': params or {}})
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict) or value.get('error') or not isinstance(value.get('result'), dict):
            # RPC error text can contain private arguments. Keep it out of API responses/logs.
            raise Unavailable(f'Monero could not complete {method}')
        return value['result']


@contextmanager
def wallet_rpc(scope, keys, storage_key, *, offline=False, restore_height=0):
    if type(restore_height) is not int or restore_height < 0:
        raise Unavailable("Invalid Monero restore height")
    root = _root()
    folder = root / scope
    folder.mkdir(mode=0o700, exist_ok=True)
    with _lock(folder / 'wallet.lock'), _slot(root):
        with socket.socket() as socket_:
            socket_.bind(('127.0.0.1', 0)); port = socket_.getsockname()[1]
        password = secrets.token_hex(32)
        config = folder / 'rpc.conf'
        daemon = 'offline=1' if offline else 'daemon-address=' + _daemon()
        _write(config, '\n'.join([
            f'wallet-dir={folder}', f'rpc-bind-port={port}', 'rpc-bind-ip=127.0.0.1',
            f'rpc-login=exodus:{password}', 'rpc-ssl=disabled', daemon,
            'non-interactive=1', 'log-level=0', 'log-file=/dev/null', 'max-log-files=1', '']))
        # A worker crash cannot leave an unbounded, orphaned wallet service. Credentials and
        # spend/view keys never appear in argv. Tests use public seeds with offline=1.
        process = subprocess.Popen(['timeout', '--signal=INT', '--kill-after=20s', '900',
                                    _binary(), '--config-file', str(config)],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, start_new_session=True)
        rpc = Rpc(port, password)
        try:
            deadline = time.monotonic() + 15
            while True:
                try:
                    rpc.call('get_version', timeout=1)
                    break
                except Exception:
                    if process.poll() is not None or time.monotonic() >= deadline:
                        raise Unavailable('The independent Monero wallet did not start') from None
                    time.sleep(.1)
            wallet_password = hmac.new(storage_key, ('exodus-monero:' + scope).encode(), hashlib.sha256).hexdigest()
            address = keys.PrimaryAddress()
            if (folder / 'wallet.keys').exists():
                rpc.call('open_wallet', {'filename': 'wallet', 'password': wallet_password}, timeout=30)
            else:
                rpc.call('generate_from_keys', {'filename': 'wallet', 'password': wallet_password,
                         'address': address, 'spendkey': keys.PrivateSpendKey().Raw().ToHex(),
                         'viewkey': keys.PrivateViewKey().Raw().ToHex(), 'restore_height': restore_height,
                         'language': 'English'}, timeout=30)
            rpc.call('auto_refresh', {'enable': False})
            actual = rpc.call('get_address', {'account_index': 0}).get('address')
            if actual != address:
                raise Unavailable('The selected Monero wallet address does not match its recovery keys')
            yield rpc
        finally:
            try:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGINT)
                    try:
                        process.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL); process.wait(timeout=5)
            finally:
                config.unlink(missing_ok=True)



def _daemon_height():
    base = _daemon()
    if '://' not in base:
        base = 'http://' + base
    with httpx.Client(timeout=10, trust_env=False) as client:
        response = client.get(base.rstrip('/') + '/get_info')
    response.raise_for_status()
    info = response.json()
    height, target = info.get('height'), info.get('target_height')
    if info.get('status') != 'OK' or info.get('mainnet') is not True or info.get('synchronized') is not True or type(height) is not int or height <= 0 or type(target) is not int or target > height:
        raise Unavailable('The Monero blockchain node is still synchronizing')
    return height


def _require_synced(rpc):
    height = rpc.call('get_height').get('height')
    target = _daemon_height()
    if type(height) is not int or height < target - 2:
        raise Unavailable('This Monero wallet is still synchronizing')


def _synchronize(scope, keys, storage_key, restore_height=0):
    try:
        with wallet_rpc(scope, keys, storage_key, restore_height=restore_height) as rpc:
            rpc.call('refresh', timeout=840)
            _require_synced(rpc)
            balance = rpc.call('get_balance', {'account_index': 0})
            units, unlocked = balance.get('balance'), balance.get('unlocked_balance')
            if type(units) is not int or type(unlocked) is not int or not 0 <= unlocked <= units:
                raise Unavailable('Monero returned an unreadable balance')
            rpc.call('store')
            _write(_root() / scope / 'balance.json', json.dumps({'units': units, 'unlocked': unlocked,
                                                               'checkedAt': time.time()}))
    except Exception:
        # No exception text: a library or RPC transport may include private data in its error.
        return False
    return True


async def balance(user_id, wallet_id, portfolio, phrase, storage_key, restore_height=0, recovery=None):
    keys = await asyncio.to_thread(W.monero_keys, phrase, portfolio, recovery)
    address = keys.PrimaryAddress()
    scope = identity(user_id, wallet_id, portfolio, address)
    result = {'address': address, 'known': False, 'units': None, 'amount': None,
              'note': 'Synchronizing this wallet with the Monero blockchain'}
    try:
        _binary(); _daemon()
        cached = None
        try:
            cached = json.loads((_root() / scope / 'balance.json').read_text())
        except (OSError, ValueError):
            pass
        age = time.time() - cached['checkedAt'] if isinstance(cached, dict) and type(cached.get('checkedAt')) in (int, float) else float('inf')
        if cached and 0 <= age <= 300 and type(cached.get('units')) is int and type(cached.get('unlocked')) is int and 0 <= cached['unlocked'] <= cached['units']:
            result.update(known=True, units=cached['units'], amount=W.from_base_units(cached['units'], 'XMR'),
                          spendable=W.from_base_units(cached['unlocked'], 'XMR'), checkedAt=cached['checkedAt'],
                          note='' if cached['units'] == cached['unlocked'] else 'Some funds are still locked')
        job = _JOBS.get(scope)
        if age >= 60 and (job is None or job.done()):
            # Do not build an unbounded queue when many accounts load at once.
            for old, task in list(_JOBS.items()):
                if task.done(): _JOBS.pop(old, None)
            if len(_JOBS) < 4:
                _JOBS[scope] = _EXECUTOR.submit(_synchronize, scope, keys, storage_key, restore_height)
    except Unavailable as error:
        result['note'] = str(error)
    return result


def _send(scope, keys, storage_key, request_id, to, units, restore_height=0):
    """Prepare without relay, durably retain its identity, then relay that transaction once."""
    from app.services.exodus_send_service import SendRefused, SendUnsure
    import re
    if not re.fullmatch(r'[0-9a-f]{32}', request_id or ''):
        raise SendRefused('A transfer identifier is required; update the app and try again')
    if type(units) is not int or not 0 < units < 2**64:
        raise SendRefused('Invalid Monero amount')
    folder = _root() / scope
    folder.mkdir(mode=0o700, exist_ok=True)
    fingerprint = hashlib.sha256(json.dumps([to, units]).encode()).hexdigest()
    journal = folder / ('send-' + request_id + '.enc')
    pending = folder / 'pending-send.enc'

    def save(path, value):
        # _write is text-only; hex encodes the encrypted document, never the plaintext keys.
        _write(path, W.seal(json.dumps(value), storage_key).hex())

    def read(path):
        return json.loads(W.unseal(bytes.fromhex(path.read_text()), storage_key))

    with _lock(folder / 'send.lock'):
        if journal.exists():
            previous = read(journal)
            if previous['fingerprint'] != fingerprint:
                raise SendRefused('This transfer identifier belongs to a different payment')
            if previous.get('result'):
                return previous['result']
            raise SendUnsure(previous.get('message') or 'This transfer has an unconfirmed outcome; do not send it again')
        if pending.exists():
            previous = read(pending)
            raise SendUnsure('A previous Monero transfer is unconfirmed. Check transaction ' + str(previous.get('hash') or 'status') + ' before another payment')
        relay_started = False
        try:
            with wallet_rpc(scope, keys, storage_key, restore_height=restore_height) as rpc:
                valid = rpc.call('validate_address', {'address': to, 'any_net_type': False, 'allow_openalias': False})
                if valid.get('valid') is not True or valid.get('nettype') != 'mainnet':
                    raise SendRefused('Enter a valid Monero mainnet address')
                rpc.call('refresh', timeout=120)
                _require_synced(rpc)
                available = rpc.call('get_balance', {'account_index': 0}).get('unlocked_balance')
                if type(available) is not int or available < units:
                    raise SendRefused('Not enough unlocked Monero for this transfer')
                prepared = rpc.call('transfer', {'account_index': 0, 'destinations': [{'address': to, 'amount': units}],
                                    'do_not_relay': True, 'get_tx_metadata': True, 'unlock_time': 0}, timeout=120)
                tx_hash, metadata, fee = prepared.get('tx_hash'), prepared.get('tx_metadata'), prepared.get('fee')
                if not isinstance(tx_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', tx_hash) or not isinstance(metadata, str) or not re.fullmatch(r'[0-9a-f]+', metadata) or type(fee) is not int or fee < 0 or units + fee > available or prepared.get('amount') != units:
                    raise SendRefused('Monero could not prepare a verified transfer')
                entry = {'fingerprint': fingerprint, 'hash': tx_hash, 'metadata': metadata, 'requestId': request_id,
                         'message': f'Monero transaction {tx_hash} may have been sent. Check its status before sending again.'}
                # Both records are synced to disk before any relay call. A worker restart cannot
                # silently turn the same payment into a new transfer with different outputs.
                save(journal, entry); save(pending, entry)
                try:
                    relay_started = True
                    answer = rpc.call('relay_tx', {'hex': metadata}, timeout=45)
                    if answer.get('tx_hash') != tx_hash:
                        raise ValueError('unconfirmed relay response')
                    result = {'hash': tx_hash, 'fee': fee, 'units': units, 'to': to}
                    entry['result'] = result
                    save(journal, entry)
                    pending.unlink()
                    return result
                except Exception as error:
                    raise SendUnsure(entry['message']) from error
        except (SendRefused, SendUnsure):
            raise
        except Exception as error:
            # The wallet process may fail while its context manager is shutting down, after
            # relay_tx returned or raised. Cleanup must never turn that into "not relayed".
            if relay_started:
                raise SendUnsure(entry['message']) from error
            raise SendRefused('Monero could not prepare this payment; no transaction was relayed') from error


async def send(user_id, wallet_id, portfolio, phrase, storage_key, request_id, to, units, restore_height=0, recovery=None):
    keys = await asyncio.to_thread(W.monero_keys, phrase, portfolio, recovery)
    scope = identity(user_id, wallet_id, portfolio, keys.PrimaryAddress())
    return await asyncio.to_thread(_send, scope, keys, storage_key, request_id, to, units, restore_height)


def recovery_phrase(phrase, portfolio=0, recovery=None):
    from bip_utils import MoneroMnemonicEncoder
    keys = W.monero_keys(phrase, portfolio, recovery)
    return MoneroMnemonicEncoder().EncodeWithChecksum(keys.PrivateSpendKey().Raw().ToBytes()).ToStr()


async def birth_height(imported):
    if imported:
        return 0
    try:
        # A newly generated seed cannot own transactions before creation. Keep twenty minutes
        # of overlap; imported wallets always scan from genesis unless explicitly restored later.
        return max(0, await asyncio.to_thread(_daemon_height) - 10)
    except Exception:
        return 0



def _send_status(scope, key):
    folder = _root() / scope
    folder.mkdir(mode=0o700, exist_ok=True)
    with _lock(folder / 'send.lock'):
        pending = folder / 'pending-send.enc'
        if not pending.exists():
            return {'state': 'idle'}
        record = json.loads(W.unseal(bytes.fromhex(pending.read_text()), key))
        import re
        token = record.get('requestId')
        if not re.fullmatch(r'[0-9a-f]{32}', token or ''):
            raise Unavailable('The transfer record needs review')
        journal = folder / ('send-' + token + '.enc')
        saved = json.loads(W.unseal(bytes.fromhex(journal.read_text()), key))
        if saved.get('result'):
            pending.unlink()
            return {'state':'accepted', **saved['result']}
        base = _daemon()
        if '://' not in base: base = 'http://' + base
        with httpx.Client(timeout=10, trust_env=False) as client:
            response = client.post(base.rstrip('/') + '/get_transactions',
                                   json={'txs_hashes':[record['hash']], 'decode_as_json':False})
        response.raise_for_status()
        data = response.json()
        if data.get('status') != 'OK':
            raise Unavailable('Monero transfer status is unavailable')
        if any(tx.get('tx_hash') == record['hash'] for tx in data.get('txs', []) if isinstance(tx, dict)):
            saved['result'] = {'hash':record['hash']}
            _write(journal, W.seal(json.dumps(saved), key).hex())
            pending.unlink()
            return {'state':'accepted', 'hash':record['hash']}
        return {'state':'unconfirmed', 'hash':record['hash']}


async def send_status(scope, key):
    return await asyncio.to_thread(_send_status, scope, key)
