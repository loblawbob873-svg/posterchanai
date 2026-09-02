"""A MONERO WALLET FOR EVERY USER — one pooled wallet, one ACCOUNT each.

WHY ACCOUNTS AND NOT WALLETS. The first attempt gave each user their own wallet FILE behind a
`--wallet-dir` daemon. That daemon opens ONE wallet at a time, so every zap would have been
open + sync + transfer + close, with concurrent zaps queueing behind each other — a bottleneck built
in on day one. Monero supports many accounts inside one wallet, each with its own address and its
own outputs, which is how exchanges and tipping services do it: no open/close, one file to back up,
one thing to protect.

WHAT THIS MEANS, PLAINLY: the node holds these keys. That is what custodial means and no design here
removes it. It is a SEPARATE wallet and a separate daemon from the operator's own (38084 vs 38083),
so a bug in this path cannot reach the operator's funds, and the whole directory is backed up
encrypted off-box hourly before any user money existed.

THE ACCOUNT LABEL IS THE INDEX. A user's account is found by its label, which is their pubkey. That
avoids a second source of truth: no table to migrate, no row that can disagree with the wallet about
whose money is whose. The wallet file itself is the record.
"""
from __future__ import annotations

import asyncio
import importlib
import os
from typing import Any

import httpx

from app.services.monero_wallet_service import (
    WalletBusy, WalletError, atomic_to_xmr, normalize_amounts, validate_address,
)

#: Monero caps the outputs in one transaction and the change takes a slot. Measured against the real
#: daemon: 15 destinations built one transaction, 16 was refused.
MAX_DESTINATIONS = 15


def _setting(name: str, env_name: str, default: str) -> str:
    env_value = os.getenv(env_name, default)
    try:
        settings_store = importlib.import_module("app.services.settings_store")
        stored = settings_store.get(name, "")
        return str(stored) if stored not in (None, "") else env_value
    except Exception:
        return env_value


class UserWallets:
    """The pooled wallet. Every method takes the user's pubkey, never an account index from a
    caller — an index is an integer somebody could get wrong and spend the wrong person's money."""

    def __init__(self) -> None:
        self.url = _setting("monero_pool_rpc_url", "MONERO_POOL_RPC_URL", "")
        self.user = _setting("monero_pool_rpc_user", "MONERO_POOL_RPC_USER", "")
        self.password = _setting("monero_pool_rpc_password", "MONERO_POOL_RPC_PASSWORD", "")
        self.network = _setting("monero_wallet_network", "MONERO_WALLET_NETWORK", "stagenet").lower()
        self.timeout = float(_setting("monero_wallet_rpc_timeout", "MONERO_WALLET_RPC_TIMEOUT", "8"))
        self._lock = asyncio.Lock()

    def enabled(self) -> bool:
        return bool(self.url and self.user and self.password)

    async def rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.enabled():
            raise WalletError("Per-user Monero wallets are not configured on this node")
        payload = {"jsonrpc": "2.0", "id": "0", "method": method, "params": params or {}}
        try:
            async with httpx.AsyncClient(
                auth=httpx.DigestAuth(self.user, self.password),
                timeout=httpx.Timeout(self.timeout, connect=min(2.0, self.timeout)),
                follow_redirects=False, trust_env=False,
            ) as client:
                response = await client.post(self.url, json=payload)
                response.raise_for_status()
                body = response.json()
        except httpx.ConnectTimeout as exc:
            raise WalletError("The wallet service is unavailable") from exc
        except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout) as exc:
            raise WalletBusy("The wallet is busy — it is still reading the chain") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise WalletError("The wallet service is unavailable") from exc
        if not isinstance(body, dict):
            raise WalletError("The wallet returned an invalid response")
        if body.get("error"):
            err = body["error"] or {}
            detail = str(err.get("message") or "").lower()
            if err.get("code") == -37 or "not enough" in detail or "insufficient" in detail:
                raise WalletError("Not enough unlocked balance in your wallet")
            if "daemon" in detail or "not connected" in detail or "busy" in detail:
                raise WalletError("The wallet is not caught up with the Monero network yet")
            raise WalletError("The wallet rejected the request")
        result = body.get("result")
        if not isinstance(result, dict):
            raise WalletError("The wallet returned an invalid response")
        return result

    @staticmethod
    def _label(pubkey: str) -> str:
        """The account label IS the user's key, and it is the only thing that selects a wallet.

        Accepts either form deliberately. `User.nostr_npub` holds BECH32 (`npub1…`, 63 chars)
        despite the model calling it a pubkey and a comment elsewhere calling it hex — checked
        against real rows, because assuming hex would have made every existing user fail the
        validation and, worse, an unvalidated empty string would have become ONE SHARED WALLET for
        everyone who has not signed in with Nostr."""
        key = str(pubkey or "").strip().lower()
        is_hex = len(key) == 64 and all(c in "0123456789abcdef" for c in key)
        is_npub = key.startswith("npub1") and 59 <= len(key) <= 68 and key.isalnum()
        if not (is_hex or is_npub):
            raise WalletError("A wallet needs a valid account key")
        return "pc:" + key

    async def _find(self, pubkey: str) -> dict[str, Any] | None:
        label = self._label(pubkey)
        got = await self.rpc("get_accounts")
        for account in got.get("subaddress_accounts") or []:
            if str(account.get("label") or "") == label:
                return account
        return None

    async def account(self, pubkey: str, create: bool = True) -> dict[str, Any]:
        """This user's account, creating it the first time they need one.

        Serialised: two requests arriving together for a user who has no account yet would otherwise
        both see "none" and create two, and the second would silently become the one their address
        is published from while the first quietly holds any money sent in between."""
        found = await self._find(pubkey)
        if found:
            return found
        if not create:
            raise WalletError("No wallet yet")
        async with self._lock:
            found = await self._find(pubkey)          # re-check inside the lock
            if found:
                return found
            await self.rpc("create_account", {"label": self._label(pubkey)})
            found = await self._find(pubkey)
            if not found:
                raise WalletError("The wallet could not be created")
            return found

    async def address(self, pubkey: str) -> str:
        return str((await self.account(pubkey)).get("base_address") or "")

    async def balance(self, pubkey: str) -> dict[str, Any]:
        account = await self.account(pubkey)
        index = int(account.get("account_index"))
        got = await self.rpc("get_balance", {"account_index": index})
        subs = got.get("per_subaddress") or []
        return {
            "address": str(account.get("base_address") or ""),
            "balance": atomic_to_xmr(int(got.get("balance") or 0)),
            "unlocked_balance": atomic_to_xmr(int(got.get("unlocked_balance") or 0)),
            "blocks_to_unlock": got.get("blocks_to_unlock"),
            "outputs": sum(int(x.get("num_unspent_outputs") or 0) for x in subs),
        }

    async def pay(self, pubkey: str, payments: list[tuple[str, int]]) -> dict[str, Any]:
        """Pay one or many people from THIS user's account, in as few transactions as possible.

        Batched at 15 for the same reason the operator wallet is: the outputs in one transaction are
        capped and the change takes a slot. Every address is validated before anything is sent, so a
        bad one in a batch cannot let fourteen good payments go and then fail."""
        if not payments:
            raise WalletError("No payments given")
        account = await self.account(pubkey)
        index = int(account.get("account_index"))
        dests = []
        for address, atomic in payments:
            validate_address(address, self.network)
            if not isinstance(atomic, int) or isinstance(atomic, bool) or atomic <= 0:
                raise WalletError("Each payment needs a positive amount")
            dests.append({"address": address, "amount": atomic})

        out: dict[str, Any] = {"recipients": len(dests), "batches": [], "tx_hash_list": [],
                               "amount": 0, "fee": 0}
        for i in range(0, len(dests), MAX_DESTINATIONS):
            chunk = dests[i:i + MAX_DESTINATIONS]
            got = await self.rpc("transfer_split", {
                "destinations": chunk, "account_index": index, "priority": 1,
                "get_tx_keys": False, "get_tx_hex": False,
            })
            out["batches"].append(len(chunk))
            out["tx_hash_list"].extend(got.get("tx_hash_list") or [])
            out["amount"] += sum(got.get("amount_list") or [])
            out["fee"] += sum(got.get("fee_list") or [])
        return normalize_amounts(out)

    async def withdraw(self, pubkey: str, address: str) -> dict[str, Any]:
        """Send everything to an address the user names.

        THE WAY OUT, and it exists from the first commit. Custody without a withdrawal is a trap,
        and a user who cannot leave has not been given a wallet — they have been given an IOU."""
        validate_address(address, self.network)
        account = await self.account(pubkey, create=False)
        index = int(account.get("account_index"))
        return normalize_amounts(await self.rpc("sweep_all", {
            "address": address, "account_index": index, "priority": 1, "get_tx_keys": False,
        }))


user_wallets = UserWallets()
