"""Small, local-only Monero Wallet RPC client for a low-balance tipping wallet.

The wallet and its spend keys belong to ``monero-wallet-rpc``.  PosterChan only sends
authenticated JSON-RPC calls to a loopback socket and deliberately has no API for
opening/creating wallets or handling seeds and keys.
"""
from __future__ import annotations

import asyncio
import importlib
import ipaddress
import os
import secrets
import sqlite3
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any
from urllib.parse import urlsplit

import httpx

ATOMIC_UNITS = Decimal("1000000000000")
_B58 = frozenset("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
_ATOMIC_FIELDS = frozenset({
    "amount", "amounts", "balance", "change", "fee", "spent", "unlocked_balance",
    "total_received", "total_sent",
})


class WalletError(Exception):
    """A safe, client-displayable wallet error."""


class WalletBusy(WalletError):
    """THE WALLET IS THERE AND IT IS WORKING — it just has not answered yet.

    monero-wallet-rpc BLOCKS while it scans blocks, so on a node that is catching up every call
    times out. Both outcomes used to become "Local Monero wallet is unavailable", which the screen
    renders as "This device is in safe external-wallet mode" — a sentence that describes a wallet
    that is not configured, shown for hours to somebody whose wallet is fine and busy.

    A refused CONNECTION is genuinely "not there". A connection that was accepted and then went
    quiet is the opposite: something is on the other end, doing work. They must not read the same."""


@dataclass(frozen=True)
class WalletConfig:
    enabled: bool
    url: str
    username: str
    password: str
    network: str
    transfer_cap_atomic: int
    daily_cap_atomic: int
    timeout_seconds: float
    spend_ledger_path: str = "data/monero_wallet_spend.sqlite3"

    @classmethod
    def from_env(cls) -> "WalletConfig":
        """Load service bootstrap values, overridden by encrypted node settings when configured."""
        def setting(name: str, env_name: str, default: str) -> str:
            env_value = os.getenv(env_name, default)
            try:
                settings_store = importlib.import_module("app.services.settings_store")
                stored = settings_store.get(name, "")
                return str(stored) if stored not in (None, "") else env_value
            except Exception:
                return env_value

        try:
            timeout = float(setting("monero_wallet_rpc_timeout", "MONERO_WALLET_RPC_TIMEOUT", "8"))
            return cls(
                enabled=setting("monero_wallet_enabled", "MONERO_WALLET_ENABLED", "").lower() in {"1", "true", "yes"},
                url=setting("monero_wallet_rpc_url", "MONERO_WALLET_RPC_URL", "http://127.0.0.1:38083/json_rpc"),
                username=setting("monero_wallet_rpc_user", "MONERO_WALLET_RPC_USER", ""),
                password=setting("monero_wallet_rpc_password", "MONERO_WALLET_RPC_PASSWORD", ""),
                network=setting("monero_wallet_network", "MONERO_WALLET_NETWORK", "stagenet").lower(),
                transfer_cap_atomic=xmr_to_atomic(setting("monero_wallet_transfer_cap_xmr", "MONERO_WALLET_TRANSFER_CAP_XMR", "0.1")),
                daily_cap_atomic=xmr_to_atomic(setting("monero_wallet_daily_cap_xmr", "MONERO_WALLET_DAILY_CAP_XMR", "0.5")),
                timeout_seconds=timeout,
                spend_ledger_path=setting("monero_wallet_spend_ledger", "MONERO_WALLET_SPEND_LEDGER", "data/monero_wallet_spend.sqlite3"),
            )
        except ValueError as exc:
            raise WalletError("Invalid Monero wallet configuration") from exc

    def validate(self) -> None:
        if not self.enabled:
            raise WalletError("Monero tipping wallet is disabled")
        if self.network not in {"stagenet", "mainnet"}:
            raise WalletError("Monero network must be stagenet or mainnet")
        parsed = urlsplit(self.url)
        if parsed.scheme != "http" or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise WalletError("Wallet RPC must use a plain loopback/RFC1918 HTTP URL without embedded credentials")
        try:
            rpc_ip = ipaddress.ip_address(parsed.hostname or "")
            rfc1918 = isinstance(rpc_ip, ipaddress.IPv4Address) and any(rpc_ip in block for block in (
                ipaddress.ip_network("10.0.0.0/8"), ipaddress.ip_network("172.16.0.0/12"),
                ipaddress.ip_network("192.168.0.0/16"),
            ))
            if not rpc_ip.is_loopback and not rfc1918:
                raise WalletError("Wallet RPC must use a numeric loopback or RFC1918 address")
        except ValueError as exc:
            raise WalletError("Wallet RPC must use a numeric loopback or RFC1918 address") from exc
        if not parsed.port or not parsed.path.endswith("/json_rpc"):
            raise WalletError("Wallet RPC URL must include a port and /json_rpc")
        if not self.username or not self.password:
            raise WalletError("Wallet RPC authentication is required")
        if not (0 < self.transfer_cap_atomic <= self.daily_cap_atomic):
            raise WalletError("Invalid wallet spending caps")
        if not (0.5 <= self.timeout_seconds <= 30):
            raise WalletError("Wallet RPC timeout must be between 0.5 and 30 seconds")
        if not self.spend_ledger_path or self.spend_ledger_path == ":memory:":
            raise WalletError("Wallet spending ledger must use durable storage")


def _xmr_env(name: str, default: str) -> int:
    try:
        value = Decimal(os.getenv(name, default))
        if not value.is_finite() or value <= 0 or value.as_tuple().exponent < -12:
            raise WalletError(f"Invalid {name}")
        atomic = int((value * ATOMIC_UNITS).to_integral_exact(rounding=ROUND_DOWN))
    except (InvalidOperation, OverflowError, ValueError):
        raise WalletError(f"Invalid {name}")
    return atomic


def xmr_to_atomic(value: str) -> int:
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise WalletError("Amount must be a decimal XMR value") from exc
    if not amount.is_finite() or amount <= 0 or amount.as_tuple().exponent < -12:
        raise WalletError("Amount must be positive with at most 12 decimal places")
    return int(amount * ATOMIC_UNITS)


def atomic_to_xmr(value: int) -> str:
    """Return exact XMR text; atomic integers must never cross into browser Number values."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise WalletError("Local Monero wallet returned an invalid monetary amount")
    text = format(Decimal(value) / ATOMIC_UNITS, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def normalize_amounts(value: Any, field: str | None = None) -> Any:
    if field in _ATOMIC_FIELDS and isinstance(value, int) and not isinstance(value, bool):
        return atomic_to_xmr(value)
    if field == "amounts" and isinstance(value, list):
        return [atomic_to_xmr(item) for item in value]
    if isinstance(value, dict):
        return {key: normalize_amounts(item, key) for key, item in value.items()}
    if isinstance(value, list):
        return [normalize_amounts(item) for item in value]
    return value


def validate_address(address: str, network: str = "stagenet") -> str:
    prefixes = {"stagenet": {"5", "7"}, "mainnet": {"4", "8"}}.get(network)
    if prefixes is None:
        raise WalletError("Invalid Monero network")
    if len(address) not in {95, 106} or address[0] not in prefixes or any(c not in _B58 for c in address):
        raise WalletError(f"Invalid Monero {network} address")
    return address


class MoneroWallet:
    def __init__(self, config: WalletConfig | None = None):
        self.config = config or WalletConfig.from_env()
        self.config.validate()

    async def rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = {"jsonrpc": "2.0", "id": "0", "method": method, "params": params or {}}
        try:
            async with httpx.AsyncClient(
                auth=httpx.DigestAuth(self.config.username, self.config.password),
                timeout=httpx.Timeout(self.config.timeout_seconds, connect=min(2.0, self.config.timeout_seconds)),
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = await client.post(self.config.url, json=payload)
                response.raise_for_status()
                body = response.json()
        except httpx.ConnectTimeout as exc:
            # Nothing accepted the connection — that is "not there", not "busy".
            raise WalletError("Local Monero wallet is unavailable") from exc
        except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout) as exc:
            raise WalletBusy("Local Monero wallet is busy — it is still reading the chain") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise WalletError("Local Monero wallet is unavailable") from exc
        if not isinstance(body, dict):
            raise WalletError("Local Monero wallet returned an invalid response")
        if body.get("error"):
            # SAY WHICH REFUSAL IT WAS, without forwarding internals.
            #
            # Every failure became "Monero wallet rejected the request", which tells somebody whose
            # payment did not go through nothing at all — reported exactly that way. The daemon's own
            # text can carry paths and RPC internals and is not safe to show, but the CLASS of
            # refusal is both safe and the only part that helps: a wallet with no spendable balance
            # (which is every wallet whose daemon is still catching up) reads completely differently
            # from a bad address.
            err = body.get("error") or {}
            detail = str(err.get("message") or "").lower()
            code = err.get("code")
            if code == -37 or "not enough" in detail or "insufficient" in detail:
                raise WalletError("Not enough unlocked balance in the local wallet — "
                                  "tip from an external wallet, or wait for it to finish syncing")
            if "daemon" in detail or "not connected" in detail or "busy" in detail:
                raise WalletError("The wallet is not caught up with the Monero network yet — "
                                  "it cannot spend until it is")
            if "address" in detail:
                raise WalletError("That Monero address was refused by the wallet")
            raise WalletError("Monero wallet rejected the request")
        result = body.get("result")
        if not isinstance(result, dict):
            raise WalletError("Local Monero wallet returned an invalid response")
        return result

    async def balance(self) -> dict[str, Any]:
        return normalize_amounts(await self.rpc("get_balance", {"account_index": 0}))

    async def address(self) -> dict[str, Any]:
        return await self.rpc("get_address", {"account_index": 0})

    async def sync_state(self) -> dict[str, Any]:
        """IS THIS WALLET AT THE TIP OF THE CHAIN, OR STILL READING ITS WAY THERE?

        Reported as "people have been zapping my monero address but wallet still says 0". Measured
        on the real node: monerod was at 3,468,468 of 3,753,339 — 284,871 blocks behind, actively
        syncing — so the wallet had simply never seen the blocks those payments were in. It reported
        `balance: 0` perfectly correctly, and a bare 0 is indistinguishable from "you have nothing".

        `sync_info` and `get_info` are DAEMON methods and monero-wallet-rpc answers `Method not
        found` for both (verified against the live wallet), so the daemon's target height cannot be
        read from here at all. `refresh` can: it returns `blocks_fetched`, which is 0 for a wallet
        at the tip and >0 for one still catching up. That is the only signal available and it is a
        real one.

        A failure answers "I could not tell" — never "synchronised". This is the same rule the drive
        check and the uptime doc follow, and it matters more here: the reassuring answer is the one
        that would be wrong."""
        try:
            got = await self.rpc("refresh")
        except WalletBusy:
            # A wallet that accepted the connection and then did not answer within the budget is
            # scanning — which is exactly the state this call exists to report, and the one in which
            # it is least able to reply. Reporting "unknown" here would silence the banner precisely
            # when it is true.
            return {"checked": True, "scanning": True, "blocks_fetched": 0, "busy": True}
        except WalletError:
            return {"checked": False, "scanning": None, "blocks_fetched": 0}
        fetched = got.get("blocks_fetched")
        fetched = fetched if isinstance(fetched, int) and not isinstance(fetched, bool) else 0
        return {"checked": True, "scanning": fetched > 0, "blocks_fetched": fetched}

    async def node_status(self) -> dict[str, Any]:
        """Minimal operational health only: no addresses, transfers, keys, or RPC credentials."""
        height, balance = await asyncio.gather(
            self.rpc("get_height"), self.rpc("get_balance", {"account_index": 0}),
        )
        return {
            "wallet_rpc_reachable": True,
            "daemon_connected": isinstance(height.get("height"), int),
            "network": self.config.network,
            "height": height.get("height") if isinstance(height.get("height"), int) else None,
            # monero-wallet-rpc does not expose daemon target_height/busy in this call.
            "target_height": None,
            "synchronized": None,
            "busy": False,
            "balance": atomic_to_xmr(balance.get("balance", 0)),
            "unlocked_balance": atomic_to_xmr(balance.get("unlocked_balance", 0)),
        }

    async def history(self, *, limit: int = 50) -> dict[str, Any]:
        if not 1 <= limit <= 100:
            raise WalletError("History limit must be between 1 and 100")
        # `pending` is OUTGOING unconfirmed; `pool` is INCOMING unconfirmed. Asking for one and not
        # the other made a tip that had not been mined yet invisible in Recent activity — the exact
        # window in which somebody looks, having just been told the payment was sent.
        result = await self.rpc("get_transfers",
                                {"in": True, "out": True, "pending": True, "failed": True, "pool": True})
        for key in ("in", "out", "pending", "failed", "pool"):
            if isinstance(result.get(key), list):
                result[key] = result[key][-limit:]
        return normalize_amounts(result)

    async def make_uri(self, address: str, amount_atomic: int, description: str = "") -> dict[str, Any]:
        validate_address(address, self.config.network)
        if len(description) > 140 or any(ord(c) < 32 for c in description):
            raise WalletError("Description must be at most 140 printable characters")
        return await self.rpc("make_uri", {"address": address, "amount": amount_atomic, "tx_description": description})

    async def transfer(self, address: str, amount_atomic: int) -> dict[str, Any]:
        validate_address(address, self.config.network)
        return await self.rpc("transfer", {
            "destinations": [{"amount": amount_atomic, "address": address}],
            "account_index": 0,
            "priority": 1,
            "get_tx_key": False,
            "get_tx_hex": False,
            "get_tx_metadata": False,
        })


@dataclass
class PendingTransfer:
    user_id: int
    address: str
    amount_atomic: int
    expires_at: float


class TransferGate:
    """One-use confirmations backed by a durable, conservative rolling daily cap."""

    def __init__(self) -> None:
        self._pending: dict[str, PendingTransfer] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _connect(path: str) -> sqlite3.Connection:
        if os.path.islink(path):
            raise WalletError("Wallet spending ledger is unavailable")
        existed = os.path.exists(path)
        try:
            db = sqlite3.connect(path, timeout=5)
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS monero_spend_attempts (at REAL NOT NULL, user_id INTEGER NOT NULL, amount_atomic INTEGER NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS monero_spend_attempts_at ON monero_spend_attempts(at)")
            # Repair overly broad modes on every open, not only first creation. Refuse
            # symlinks above so chmod cannot be redirected to an unrelated file.
            os.chmod(path, 0o600)
            return db
        except (OSError, sqlite3.Error) as exc:
            raise WalletError("Wallet spending ledger is unavailable") from exc

    def _durable_spent(self, wallet: MoneroWallet, now: float) -> int:
        db = self._connect(wallet.config.spend_ledger_path)
        try:
            row = db.execute("SELECT COALESCE(SUM(amount_atomic), 0) FROM monero_spend_attempts WHERE at > ?", (now - 86400,)).fetchone()
            return int(row[0])
        except sqlite3.Error as exc:
            raise WalletError("Wallet spending ledger is unavailable") from exc
        finally:
            db.close()

    def _reserve_attempt(self, wallet: MoneroWallet, user_id: int, amount: int, now: float) -> None:
        """Atomically enforce and reserve before RPC; uncertain attempts consume budget safely."""
        db = self._connect(wallet.config.spend_ledger_path)
        try:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM monero_spend_attempts WHERE at <= ?", (now - 86400,))
            spent = int(db.execute("SELECT COALESCE(SUM(amount_atomic), 0) FROM monero_spend_attempts").fetchone()[0])
            if spent + amount > wallet.config.daily_cap_atomic:
                raise WalletError("Amount exceeds the daily spending cap")
            db.execute("INSERT INTO monero_spend_attempts(at, user_id, amount_atomic) VALUES (?, ?, ?)", (now, user_id, amount))
            db.commit()
        except WalletError:
            db.rollback()
            raise
        except sqlite3.Error as exc:
            db.rollback()
            raise WalletError("Wallet spending ledger is unavailable") from exc
        finally:
            db.close()

    async def prepare(self, wallet: MoneroWallet, user_id: int, address: str, amount_atomic: int) -> tuple[str, float]:
        validate_address(address, wallet.config.network)
        if amount_atomic > wallet.config.transfer_cap_atomic:
            raise WalletError("Amount exceeds the per-transfer spending cap")
        now = time.time()
        async with self._lock:
            self._purge(now)
            # The RPC wallet is node-wide, so its cap must be node-wide too. User-scoped
            # accounting would let several accounts multiply the operator's intended limit.
            spent = self._durable_spent(wallet, now)
            pending = sum(p.amount_atomic for p in self._pending.values())
            if spent + pending + amount_atomic > wallet.config.daily_cap_atomic:
                raise WalletError("Amount exceeds the daily spending cap")
            token = secrets.token_urlsafe(32)
            expires = now + 90
            self._pending[token] = PendingTransfer(user_id, address, amount_atomic, expires)
            return token, expires

    async def confirm(self, wallet: MoneroWallet, user_id: int, token: str) -> dict[str, Any]:
        now = time.time()
        async with self._lock:
            self._purge(now)
            pending = self._pending.pop(token, None)  # one attempt only, including RPC failure
            if pending is None or pending.user_id != user_id:
                raise WalletError("Transfer confirmation is invalid or expired")
            # Reserve durably while protected against in-process confirms. The actual
            # network call is deliberately outside this lock; the popped token cannot race.
            self._reserve_attempt(wallet, user_id, pending.amount_atomic, now)
        return await wallet.transfer(pending.address, pending.amount_atomic)

    def _purge(self, now: float) -> None:
        self._pending = {key: item for key, item in self._pending.items() if item.expires_at > now}


transfer_gate = TransferGate()
