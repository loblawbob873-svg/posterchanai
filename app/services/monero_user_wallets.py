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
import logging
import os
import time
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Any

import httpx

from app.services.monero_wallet_service import (
    WalletBusy, WalletError, WalletUnsure, atomic_to_xmr, normalize_amounts, validate_address,
)

logger = logging.getLogger(__name__)

#: Monero caps the outputs in one transaction and the change takes a slot. Measured against the real
#: daemon: 15 destinations built one transaction, 16 was refused.
MAX_DESTINATIONS = 15

# Keep enough independent outputs for repeated zaps.  This policy belongs to the per-user pooled
# wallet, not the operator wallet: every account owns a disjoint set of outputs.
OUTPUT_TARGET = 8
OUTPUT_LOW_WATER = 4
OUTPUT_MIN_ATOMIC = 10**9       # 0.001 XMR per resulting output
OUTPUT_MAINTENANCE_SECONDS = 60


# ── The operator's zap fee ───────────────────────────────────────────────────────────────────────
#
# A percentage of every custodial zap, paid to the node operator's own address. It applies ONLY on
# this path — the one where the node actually executes the transfer. The URI/QR flow is
# non-custodial (the payment never touches this server, so there is nothing to take a cut of) and
# the operator's own node wallet is not charged, because that would be the operator paying
# themselves a fee out of their own wallet and losing a transaction fee to do it.
#
# THE FEE MUST NEVER BLOCK A PAYMENT. Every failure to work out a fee — no address configured, an
# address that does not validate, a percentage that is nonsense, a cut too small to be worth a
# destination — results in the zap going out in full, unchanged. Money moving is the feature; the
# fee is the operator's business arrangement, and an arrangement that can strand somebody's tip is
# worse than no arrangement.
# RingCT has no protocol dust floor for ordinary outputs.  The previous 0.00001 XMR floor silently
# waived a 2% service fee on every zap below 0.0005 XMR -- precisely the small zaps this feature is
# meant to cover.  One atomic unit is the only honest lower bound: if the configured percentage can
# be represented, collect it; if rounding produces zero, leave the recipient's amount untouched.
FEE_MIN_ATOMIC = 1


def zap_fee_percent() -> Decimal:
    """The configured cut, as a percentage. Anything unparseable is no fee at all."""
    raw = _setting("monero_zap_fee_percent", "MONERO_ZAP_FEE_PERCENT", "2")
    try:
        pct = Decimal(str(raw).strip() or "0")
    except (InvalidOperation, ValueError):
        return Decimal(0)
    if not pct.is_finite() or pct <= 0:
        return Decimal(0)
    # A cut of more than half is far likelier to be a typo (200 for 2.00) than an intention, and the
    # cost of being wrong lands on somebody else's tip.
    return min(pct, Decimal(50))


def split_fee(atomic: int, pct: Decimal) -> tuple[int, int]:
    """(what the recipient gets, what the operator gets) — exact integers, never floats.

    The cut comes OUT of the amount rather than being added on top, so the sender is debited exactly
    what they typed. Rounded DOWN, so rounding always favours the recipient, and skipped entirely
    when it would be dust or would leave the recipient with nothing."""
    if pct <= 0 or atomic <= 0:
        return atomic, 0
    fee = int((Decimal(atomic) * pct / Decimal(100)).to_integral_value(rounding=ROUND_DOWN))
    if fee < FEE_MIN_ATOMIC or fee >= atomic:
        return atomic, 0
    return atomic - fee, fee


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
        self._fee_address: str | None = None
        self._fee_at = 0.0
        self._lock = asyncio.Lock()

    def enabled(self) -> bool:
        return bool(self.url and self.user and self.password)

    #: Methods that MOVE MONEY — see MoneroWallet.SPENDING. A spend is given far longer than a read;
    #: using a read's budget for a transfer is what reported "payment not sent" over a payment that
    #: had already been broadcast, and made somebody send twice.
    SPENDING = frozenset({"transfer", "transfer_split", "sweep_all", "sweep_single", "sweep_dust"})
    SPEND_TIMEOUT = 120.0

    async def rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.enabled():
            raise WalletError("Per-user Monero wallets are not configured on this node")
        payload = {"jsonrpc": "2.0", "id": "0", "method": method, "params": params or {}}
        spending = method in self.SPENDING
        budget = max(self.timeout, self.SPEND_TIMEOUT) if spending else self.timeout
        try:
            async with httpx.AsyncClient(
                auth=httpx.DigestAuth(self.user, self.password),
                timeout=httpx.Timeout(budget, connect=min(2.0, budget)),
                follow_redirects=False, trust_env=False,
            ) as client:
                response = await client.post(self.url, json=payload)
                response.raise_for_status()
                body = response.json()
        except httpx.ConnectTimeout as exc:
            raise WalletError("The wallet service is unavailable") from exc
        except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout) as exc:
            if spending:
                # The money may have left. Never word this as a failure, and never invite a retry.
                raise WalletUnsure(
                    "The wallet did not answer in time. This payment may have been sent — "
                    "check your transaction history before trying again") from exc
            raise WalletBusy("The wallet did not answer in time — it is busy") from exc
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

    async def maintain_account_outputs(self, account: dict[str, Any]) -> dict[str, Any]:
        """Top up one user's *spendable* output capacity without blocking their remaining zaps.

        ``num_unspent_outputs`` includes locked change and cannot drive this decision.  We inspect
        the individual available transfers and split only the largest unlocked output with
        ``sweep_single``.  Splitting the whole account with ``sweep_all`` would lock every usable
        output for ten blocks and manufacture the very outage this job exists to prevent.
        """
        index = int(account.get("account_index"))
        address = str(account.get("base_address") or "").strip()
        label = str(account.get("label") or "")
        if not label.startswith("pc:") or not address:
            return {"account_index": index, "action": "ignored"}
        validate_address(address, self.network)
        got = await self.rpc("incoming_transfers", {
            "transfer_type": "available", "account_index": index,
        })
        # Wallet RPC's "available" means UNSPENT, not unlocked. "unavailable" means
        # already SPENT; historical zaps must never masquerade as a split still unlocking.
        unspent = [t for t in (got.get("transfers") or [])
                   if isinstance(t, dict) and not t.get("spent") and not t.get("frozen")
                   and type(t.get("amount")) is int and t["amount"] > 0]
        transfers = [t for t in unspent if t.get("unlocked") is True]
        count = len(transfers)
        if count >= OUTPUT_LOW_WATER:
            return {"account_index": index, "action": "healthy", "spendable_outputs": count}

        # A successful split consumes one available source and creates outputs that stay locked for
        # the chain's spend lock. The scheduler runs every minute (and process-local cooldowns vanish
        # on restart), so looking only at `available` would split another reserve on every tick:
        # 3 -> 2 -> 1 -> 0 spendable outputs. Locked outputs are an on-wallet, restart-safe witness
        # that replenishment is already in flight. Wait for them before paying another transaction
        # fee or consuming another source.
        locked = [t for t in unspent if t.get("unlocked") is not True]
        if locked:
            return {"account_index": index, "action": "waiting", "spendable_outputs": count,
                    "locked_outputs": len(locked), "reason": "output split is still unlocking"}

        # Before a split is mined, its new outputs may not appear in incoming_transfers yet.
        # Pending outgoing transactions are wallet-backed evidence too, surviving worker restarts.
        pending = await self.rpc("get_transfers", {"pending": True, "account_index": index})
        if pending.get("pending"):
            return {"account_index": index, "action": "waiting", "spendable_outputs": count,
                    "reason": "an outgoing transaction is still confirming"}

        # sweep_single is deliberately fail-closed: without a key image we cannot preserve the
        # account's other outputs, so waiting is safer than turning maintenance into a lockout.
        candidates = [t for t in transfers if str(t.get("key_image") or "")]
        if not candidates:
            return {"account_index": index, "action": "waiting", "spendable_outputs": count,
                    "reason": "no individually spendable output reported"}
        source = max(candidates, key=lambda t: int(t.get("amount") or 0))
        amount = int(source.get("amount") or 0)
        preserved = max(0, count - 1)
        wanted = OUTPUT_TARGET - preserved
        outputs = min(wanted, amount // OUTPUT_MIN_ATOMIC)
        if outputs < 2:
            return {"account_index": index, "action": "waiting", "spendable_outputs": count,
                    "reason": "not enough unlocked value for useful outputs"}
        result = await self.rpc("sweep_single", {
            "address": address, "account_index": index,
            "key_image": str(source["key_image"]), "outputs": int(outputs),
            "priority": 1, "get_tx_keys": False,
        })
        hashes = result.get("tx_hash_list") or ([result["tx_hash"]] if result.get("tx_hash") else [])
        return {"account_index": index, "action": "split", "spendable_outputs": count,
                "outputs_created": int(outputs), "tx_hash_list": hashes}

    async def maintain_outputs(self) -> dict[str, Any]:
        """Maintain all existing per-user accounts; one bad account cannot stop the rest."""
        if not self.enabled():
            return {"enabled": False, "accounts": []}
        accounts = (await self.rpc("get_accounts")).get("subaddress_accounts") or []
        results = []
        for account in accounts:
            if not str(account.get("label") or "").startswith("pc:"):
                continue
            try:
                results.append(await self.maintain_account_outputs(account))
            except Exception as exc:
                logger.warning("[monero-users] output maintenance failed for account %s: %s",
                               account.get("account_index"), exc)
                results.append({"account_index": account.get("account_index"), "action": "error"})
        return {"enabled": True, "accounts": results}

    #: How long the NODE-WALLET fallback address is reused. The configured setting is
    #: never cached — see fee_address().
    FEE_ADDRESS_TTL = 60.0

    async def fee_address(self) -> str:
        """Where the operator's cut goes — their configured address, else the node wallet's own.

        THE CONFIGURED VALUE IS RE-READ ON EVERY CALL, and that is deliberate. The first version
        cached the resolved address for the life of the process, on the reasoning that "an address
        does not change" — which is exactly wrong for a field an operator edits in Admin. Two ways
        it fails, both silent: an address saved AFTER the process resolved it once takes no effect
        until a restart (the fee quietly stays off), and worse, an address CHANGED after the fact
        keeps sending the operator's money to the old one. Reading a hydrated setting is an
        in-memory dict lookup, so there was nothing to save.

        Only the node-wallet FALLBACK is cached, because that one costs an RPC round trip, and it is
        cached briefly rather than for ever.

        Returns "" for anything it cannot verify, which switches the fee off rather than sending
        money somewhere unproven. The ORDER matters: an explicitly configured address wins, so the
        fee can be banked somewhere other than the hot wallet this node spends from."""
        configured = _setting("monero_zap_fee_address", "MONERO_ZAP_FEE_ADDRESS", "").strip()
        if configured:
            try:
                validate_address(configured, self.network)
                return configured
            except WalletError:
                logger.warning("[monero] the configured zap fee address is not a valid %s address "
                               "— no fee will be taken", self.network)
                return ""

        # Fall back to the node's own wallet: "goes to my wallet" with nothing else to configure.
        now = time.monotonic()
        if self._fee_address is not None and now - self._fee_at < self.FEE_ADDRESS_TTL:
            return self._fee_address
        resolved = ""
        try:
            node = importlib.import_module("app.services.monero_wallet_service")
            # Built per call, exactly as the node-wallet router does — there is no singleton, and
            # the CONSTRUCTOR is what refuses when the wallet is disabled or misconfigured.
            wallet = node.MoneroWallet()
            got = await wallet.address()
            addr = str((got or {}).get("address") or "").strip()
            validate_address(addr, self.network)
            resolved = addr
        except Exception:
            # Including a node wallet that is off, unreachable or on another network. No fee.
            resolved = ""
        self._fee_address, self._fee_at = resolved, now
        return resolved

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

        # THE OPERATOR'S CUT, worked out in full before anything is sent.
        #
        # `service_fee` is what the operator receives; `fee` remains the MINER's fee. They are
        # different numbers and a caller that conflates them will report the wrong thing to a payer.
        #
        # Every step here fails OPEN: no configured address, an address that will not validate, a
        # percentage that is nonsense, or a cut too small to be worth an output, and the zap simply
        # goes out in full. A fee that can strand somebody's tip is worse than no fee.
        pct = zap_fee_percent()
        fee_to = await self.fee_address() if pct > 0 else ""

        # (address, what the recipient gets, what the operator gets) per payment.
        plan: list[tuple[str, int, int]] = []
        for dest in dests:
            gross = int(dest["amount"])
            if not fee_to or dest["address"] == fee_to:
                # A payment TO the fee address is never charged: that is the operator paying
                # themselves and burning a miner fee for the privilege.
                plan.append((dest["address"], gross, 0))
                continue
            net, cut = split_fee(gross, pct)
            plan.append((dest["address"], net, cut))
        taken = sum(cut for _, _, cut in plan)
        if taken <= 0:
            fee_to = ""

        # ONE aggregated fee output per transaction, never one per recipient: a transaction's
        # outputs are capped, so a cut per person would halve how many people a single zap reaches.
        per_tx = MAX_DESTINATIONS - (1 if fee_to else 0)

        out: dict[str, Any] = {"recipients": len(plan), "batches": [], "tx_hash_list": [],
                               "amount": 0, "fee": 0, "service_fee": taken,
                               "service_fee_percent": str(pct if fee_to else Decimal(0))}
        for i in range(0, len(plan), per_tx):
            slice_ = plan[i:i + per_tx]
            chunk = [{"address": a, "amount": net} for a, net, _ in slice_]
            cut = sum(c for _, _, c in slice_)
            if fee_to and cut > 0:
                chunk.append({"address": fee_to, "amount": cut})
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

user_wallet_output_scheduler = None


def start_user_wallet_output_scheduler():
    """Start the production worker's per-user output maintainer (idempotently)."""
    global user_wallet_output_scheduler
    if user_wallet_output_scheduler is not None:
        return user_wallet_output_scheduler
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.interval import IntervalTrigger

    async def tick():
        await user_wallets.maintain_outputs()

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(tick, IntervalTrigger(seconds=OUTPUT_MAINTENANCE_SECONDS),
                      id="monero-user-output-maintenance", replace_existing=True,
                      max_instances=1, coalesce=True, next_run_time=datetime.now(timezone.utc))
    scheduler.start()
    user_wallet_output_scheduler = scheduler
    return scheduler
