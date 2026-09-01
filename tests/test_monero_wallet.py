import asyncio
import unittest
import pathlib
import os
import sqlite3
import time
from decimal import Decimal

import httpx
import pytest

from app.services import monero_wallet_service, settings_store
from app.services.monero_wallet_service import (
    MoneroWallet,
    TransferGate,
    WalletConfig,
    WalletError,
    atomic_to_xmr,
    normalize_amounts,
    validate_address,
    xmr_to_atomic,
)

ADDRESS = "5" + "A" * 94


def config(**changes):
    values = dict(
        enabled=True,
        url="http://127.0.0.1:38083/json_rpc",
        username="poster",
        password="secret",
        network="stagenet",
        transfer_cap_atomic=100_000_000_000,
        daily_cap_atomic=100_000_000_000,
        timeout_seconds=2,
        spend_ledger_path=changes.pop("spend_ledger_path", "data/test_monero_wallet_spend.sqlite3"),
    )
    values.update(changes)
    return WalletConfig(**values)


@pytest.mark.parametrize("url", [
    "http://localhost:38083/json_rpc",  # DNS is intentionally not trusted for this boundary
    "http://8.8.8.8:38083/json_rpc",
    "https://127.0.0.1:38083/json_rpc",
    "http://user:pass@127.0.0.1:38083/json_rpc",
    "http://127.0.0.1/json_rpc",
])
def test_rpc_rejects_every_non_literal_loopback_configuration(url):
    with pytest.raises(WalletError):
        MoneroWallet(config(url=url))


@pytest.mark.parametrize("address", ["10.1.2.3", "172.16.9.2", "172.31.255.254", "192.168.0.85"])
def test_rpc_allows_explicit_authenticated_rfc1918_addresses(address):
    wallet = MoneroWallet(config(url=f"http://{address}:38083/json_rpc"))
    assert wallet.config.url.startswith(f"http://{address}:")


@pytest.mark.parametrize("address", ["172.15.1.1", "172.32.1.1", "169.254.1.1", "100.64.0.1", "224.0.0.1"])
def test_rpc_rejects_non_rfc1918_numeric_addresses(address):
    with pytest.raises(WalletError, match="RFC1918"):
        MoneroWallet(config(url=f"http://{address}:38083/json_rpc"))


def test_rpc_requires_authentication_and_known_network():
    with pytest.raises(WalletError, match="authentication"):
        MoneroWallet(config(password=""))
    assert MoneroWallet(config(network="mainnet")).config.network == "mainnet"
    with pytest.raises(WalletError, match="network"):
        MoneroWallet(config(network="testnet"))


def test_environment_caps_reject_non_finite_or_sub_atomic_values(monkeypatch):
    monkeypatch.setenv("MONERO_WALLET_TRANSFER_CAP_XMR", "Infinity")
    with pytest.raises(WalletError):
        WalletConfig.from_env()
    monkeypatch.setenv("MONERO_WALLET_TRANSFER_CAP_XMR", "0.0000000000001")
    with pytest.raises(WalletError):
        WalletConfig.from_env()


def test_amount_and_stagenet_address_validation():
    assert xmr_to_atomic("0.000000000001") == 1
    assert validate_address(ADDRESS) == ADDRESS
    for amount in ("0", "-1", "nan", "0.0000000000001"):
        with pytest.raises(WalletError):
            xmr_to_atomic(amount)
    with pytest.raises(WalletError):
        validate_address("4" + "A" * 94)  # mainnet
    assert validate_address("4" + "A" * 94, "mainnet").startswith("4")
    with pytest.raises(WalletError):
        validate_address(ADDRESS, "mainnet")


def test_rpc_uses_digest_auth_no_proxy_redirects_and_safe_error(monkeypatch):
    observed = {}

    class Client:
        def __init__(self, **kwargs):
            observed.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, json):
            observed["url"] = url
            observed["json"] = json
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": "0", "result": {"balance": 9}}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    result = asyncio.run(MoneroWallet(config()).balance())
    assert result == {"balance": "0.000000000009"}
    assert isinstance(observed["auth"], httpx.DigestAuth)
    assert observed["trust_env"] is False
    assert observed["follow_redirects"] is False
    assert observed["json"]["method"] == "get_balance"

    async def bad_post(self, url, json):
        return httpx.Response(200, json={"error": {"message": "/secret/wallet/path"}}, request=httpx.Request("POST", url))

    Client.post = bad_post
    with pytest.raises(WalletError, match="rejected") as caught:
        asyncio.run(MoneroWallet(config()).balance())
    assert "/secret" not in str(caught.value)


def test_transfer_needs_one_use_confirmation_and_obeys_caps(monkeypatch, tmp_path):
    ledger = tmp_path / "spend.sqlite3"
    wallet = MoneroWallet(config(spend_ledger_path=str(ledger)))
    sent = []

    async def transfer(address, amount):
        sent.append((address, amount))
        return {"tx_hash": "abc"}

    monkeypatch.setattr(wallet, "transfer", transfer)
    gate = TransferGate()
    token, expires = asyncio.run(gate.prepare(wallet, 7, ADDRESS, 90_000_000_000))
    assert expires > time.time()
    assert sent == []  # prepare is the explicit UI confirmation boundary
    assert asyncio.run(gate.confirm(wallet, 7, token)) == {"tx_hash": "abc"}
    assert sent == [(ADDRESS, 90_000_000_000)]
    with pytest.raises(WalletError, match="invalid or expired"):
        asyncio.run(gate.confirm(wallet, 7, token))
    with pytest.raises(WalletError, match="per-transfer"):
        asyncio.run(gate.prepare(wallet, 7, ADDRESS, 100_000_000_001))
    with pytest.raises(WalletError, match="daily"):
        # The cap follows the node-wide RPC wallet, not the requesting identity.
        asyncio.run(gate.prepare(wallet, 8, ADDRESS, 20_000_000_001))

    # A brand-new gate (as after a service restart) sees the durable attempt.
    restarted = TransferGate()
    with pytest.raises(WalletError, match="daily"):
        asyncio.run(restarted.prepare(wallet, 8, ADDRESS, 20_000_000_001))


def test_balance_and_history_amounts_are_exact_xmr_strings(monkeypatch):
    wallet = MoneroWallet(config())

    async def rpc(method, params=None):
        if method == "get_balance":
            return {"balance": 9_007_199_254_740_993, "unlocked_balance": 1, "blocks_to_unlock": 12,
                    "per_subaddress": [{"balance": 2_000_000_000_000}]}
        return {"in": [{"amount": 9_007_199_254_740_993, "fee": 2, "height": 99}], "out": []}

    monkeypatch.setattr(wallet, "rpc", rpc)
    balance = asyncio.run(wallet.balance())
    history = asyncio.run(wallet.history())
    assert balance["balance"] == "9007.199254740993"
    assert balance["unlocked_balance"] == "0.000000000001"
    assert balance["per_subaddress"][0]["balance"] == "2"
    assert balance["blocks_to_unlock"] == 12
    assert history["in"][0]["amount"] == "9007.199254740993"
    assert history["in"][0]["fee"] == "0.000000000002"
    assert history["in"][0]["height"] == 99


def test_node_status_is_redacted_and_exact(monkeypatch):
    wallet = MoneroWallet(config(password="do-not-return-this"))

    async def rpc(method, params=None):
        if method == "get_height": return {"height": 3_456_789}
        return {"balance": 9_007_199_254_740_993, "unlocked_balance": 1}

    monkeypatch.setattr(wallet, "rpc", rpc)
    status = asyncio.run(wallet.node_status())
    assert status["daemon_connected"] is True and status["height"] == 3_456_789
    assert status["target_height"] is None and status["synchronized"] is None
    assert status["balance"] == "9007.199254740993"
    assert "password" not in status and "address" not in status and "do-not-return-this" not in repr(status)


def test_confirm_releases_lock_before_rpc_and_token_cannot_double_spend(monkeypatch, tmp_path):
    wallet = MoneroWallet(config(spend_ledger_path=str(tmp_path / "spend.sqlite3")))
    gate = TransferGate()

    async def scenario():
        entered_rpc = asyncio.Event()
        release_rpc = asyncio.Event()

        async def transfer(address, amount):
            entered_rpc.set()
            await release_rpc.wait()
            return {"tx_hash": "abc"}

        monkeypatch.setattr(wallet, "transfer", transfer)
        token, _ = await gate.prepare(wallet, 7, ADDRESS, 10_000_000_000)
        first = asyncio.create_task(gate.confirm(wallet, 7, token))
        await entered_rpc.wait()

        # If confirm held the gate lock across the network call, this would time out.
        await asyncio.wait_for(gate.prepare(wallet, 7, ADDRESS, 10_000_000_000), timeout=0.2)
        with pytest.raises(WalletError, match="invalid or expired"):
            await gate.confirm(wallet, 7, token)
        release_rpc.set()
        assert await first == {"tx_hash": "abc"}

    asyncio.run(scenario())


def test_ledger_repairs_permissions_and_refuses_symlinks(tmp_path):
    ledger = tmp_path / "spend.sqlite3"
    wallet = MoneroWallet(config(spend_ledger_path=str(ledger)))
    gate = TransferGate()
    asyncio.run(gate.prepare(wallet, 7, ADDRESS, 1))
    assert os.stat(ledger).st_mode & 0o777 == 0o600
    os.chmod(ledger, 0o644)
    asyncio.run(TransferGate().prepare(wallet, 7, ADDRESS, 1))
    assert os.stat(ledger).st_mode & 0o777 == 0o600

    target = tmp_path / "target.sqlite3"
    target.touch()
    link = tmp_path / "linked.sqlite3"
    link.symlink_to(target)
    linked_wallet = MoneroWallet(config(spend_ledger_path=str(link)))
    with pytest.raises(WalletError, match="ledger is unavailable"):
        asyncio.run(TransferGate().prepare(linked_wallet, 7, ADDRESS, 1))


def test_transfer_rpc_never_requests_keys_hex_or_metadata(monkeypatch):
    wallet = MoneroWallet(config())
    called = {}

    async def rpc(method, params=None):
        called.update(method=method, params=params)
        return {"tx_hash": "abc"}

    monkeypatch.setattr(wallet, "rpc", rpc)
    asyncio.run(wallet.transfer(ADDRESS, 1))
    assert called["method"] == "transfer"
    assert called["params"]["get_tx_key"] is False
    assert called["params"]["get_tx_hex"] is False
    assert called["params"]["get_tx_metadata"] is False
    assert "seed" not in repr(called).lower()


# =============================================================================================
# Everything below is the second pass: branches the file above never reached, each one a rule the
# wallet relies on and none of them visible from the API tests in test_monero_wallet_api.py.
# =============================================================================================


def test_a_node_setting_overrides_the_environment_and_an_empty_one_does_not(monkeypatch):
    """`from_env` is no longer only env: an operator configures the wallet in the admin panel and
    the stored value has to win. The empty case is the one that fails quietly — a setting that was
    never filled in reads back as "" and must fall through to the environment rather than blanking
    a working configuration."""
    stored = {"monero_wallet_rpc_user": "from-settings", "monero_wallet_rpc_password": ""}
    monkeypatch.setattr(settings_store, "get", lambda name, default=None: stored.get(name, default))
    monkeypatch.setenv("MONERO_WALLET_ENABLED", "1")
    monkeypatch.setenv("MONERO_WALLET_RPC_USER", "from-env")
    monkeypatch.setenv("MONERO_WALLET_RPC_PASSWORD", "from-env-secret")

    loaded = WalletConfig.from_env()
    assert loaded.username == "from-settings"
    assert loaded.password == "from-env-secret"


def test_an_unreachable_settings_store_falls_back_to_the_environment_not_the_default(monkeypatch):
    """The settings store is a relay read and it can fail. Falling back to the built-in DEFAULT
    would silently point a configured node at 127.0.0.1:38083 with no credentials; falling back to
    the environment keeps whatever the unit file set."""
    def explode(name, default=None):
        raise RuntimeError("relay unreachable")

    monkeypatch.setattr(settings_store, "get", explode)
    monkeypatch.setenv("MONERO_WALLET_ENABLED", "1")
    monkeypatch.setenv("MONERO_WALLET_RPC_URL", "http://127.0.0.1:39999/json_rpc")
    monkeypatch.setenv("MONERO_WALLET_RPC_USER", "unit-user")

    loaded = WalletConfig.from_env()
    assert loaded.url == "http://127.0.0.1:39999/json_rpc"
    assert loaded.username == "unit-user"


@pytest.mark.parametrize("value,expected", [
    ("1", True), ("true", True), ("TRUE", True), ("yes", True),
    ("", False), ("0", False), ("no", False), ("on", False), ("enabled", False),
])
def test_the_enable_switch_is_an_allowlist_so_nothing_ambiguous_turns_a_hot_wallet_on(
        monkeypatch, value, expected):
    monkeypatch.setenv("MONERO_WALLET_ENABLED", value)
    assert WalletConfig.from_env().enabled is expected


@pytest.mark.parametrize("changes,match", [
    ({"timeout_seconds": 0.4}, "timeout"),
    ({"timeout_seconds": 31}, "timeout"),
    ({"spend_ledger_path": ":memory:"}, "durable"),
    ({"spend_ledger_path": ""}, "durable"),
    ({"transfer_cap_atomic": 2, "daily_cap_atomic": 1}, "caps"),
    ({"transfer_cap_atomic": 0}, "caps"),
    ({"url": "http://127.0.0.1:38083/json_rpc?x=1"}, "loopback"),
    ({"url": "http://127.0.0.1:38083/"}, "port and /json_rpc"),
])
def test_every_configuration_guard_refuses_before_the_wallet_object_exists(changes, match):
    """`validate()` runs in `__init__`, so a misconfigured node has no wallet at all rather than
    one that discovers its own limits mid-transfer. A per-transfer cap above the daily cap is the
    subtle one: on its own each number looks deliberate."""
    with pytest.raises(WalletError, match=match):
        MoneroWallet(config(**changes))


def test_a_ledger_in_memory_is_refused_because_a_restart_would_refund_the_daily_cap():
    """`:memory:` is a working sqlite path that loses every recorded spend on restart — which is
    exactly the cap this wallet is built around, handed back for free by `systemctl restart`."""
    with pytest.raises(WalletError, match="durable"):
        MoneroWallet(config(spend_ledger_path=":memory:"))


@pytest.mark.parametrize("value", [True, False, 1.0, "5", None, Decimal(5)])
def test_a_non_integer_from_the_wallet_is_refused_rather_than_rendered(value):
    """`atomic_to_xmr` is the one funnel every displayed amount goes through. A float or a string
    from a wallet build we did not expect would be formatted into something plausible and wrong;
    booleans matter because `isinstance(True, int)` is true in Python."""
    with pytest.raises(WalletError, match="invalid monetary amount"):
        atomic_to_xmr(value)


def test_normalisation_converts_amount_lists_and_leaves_everything_else_alone():
    """`amounts` is a LIST of atomic integers (a multi-destination transfer). Nothing else may be
    touched: a txid is a string, a height and a confirmation count are integers that are not money,
    and converting either would put "0.000000000900" where block 900 belongs."""
    got = normalize_amounts({
        "amounts": [1, 2_000_000_000_000],
        "amount": 500_000,
        "txid": "a" * 64,
        "height": 900, "confirmations": 12, "double_spend_seen": False,
        "subaddr_indices": [0, 1],
        "destinations": [{"amount": 3, "address": ADDRESS}],
    })
    assert got["amounts"] == ["0.000000000001", "2"]
    assert got["amount"] == "0.0000005"
    assert got["txid"] == "a" * 64
    assert got["height"] == 900 and got["confirmations"] == 12
    assert got["double_spend_seen"] is False
    assert got["subaddr_indices"] == [0, 1], "an index list is not an amount list"
    assert got["destinations"][0] == {"amount": "0.000000000003", "address": ADDRESS}


@pytest.mark.parametrize("description,ok", [
    ("", True), ("thanks for the post", True), ("x" * 140, True),
    ("x" * 141, False), ("two\nlines", False), ("bell\x07", False), ("\x00", False),
])
def test_a_payment_uri_description_is_bounded_and_printable(monkeypatch, description, ok):
    """The description is written into a `monero:` URI that goes into a QR and into somebody else's
    wallet. A control character there is a value nothing downstream agreed to parse."""
    wallet = MoneroWallet(config())
    seen = []

    async def rpc(method, params=None):
        seen.append((method, params))
        return {"uri": "monero:" + ADDRESS}

    monkeypatch.setattr(wallet, "rpc", rpc)
    if ok:
        asyncio.run(wallet.make_uri(ADDRESS, 1, description))
        assert seen[-1][1]["tx_description"] == description
    else:
        with pytest.raises(WalletError, match="140 printable"):
            asyncio.run(wallet.make_uri(ADDRESS, 1, description))
        assert seen == [], "an invalid description still reached the wallet RPC"


def test_a_mainnet_address_is_refused_before_the_wallet_or_the_ledger_is_touched(monkeypatch, tmp_path):
    """This preview is stagenet-only and the address is the last thing that can be checked before
    money leaves. Refusing it late would still create the ledger file and a pending reservation for
    a payment that can never be made."""
    ledger = tmp_path / "spend.sqlite3"
    wallet = MoneroWallet(config(spend_ledger_path=str(ledger)))
    seen = []
    monkeypatch.setattr(wallet, "rpc", lambda *a, **k: seen.append(a))

    for bad in ("4" + "A" * 94, "8" + "A" * 94, "5" + "A" * 93, "5" + "0" * 94, ""):
        with pytest.raises(WalletError, match="Invalid Monero stagenet address"):
            asyncio.run(TransferGate().prepare(wallet, 7, bad, 1))
    assert seen == []
    assert not ledger.exists(), "a refused address still created the spending ledger"


def test_an_expired_confirmation_is_refused_and_gives_its_budget_back(monkeypatch, tmp_path):
    """A token lives 90 seconds. Two things have to be true when it lapses: it cannot be spent, and
    the daily allowance it was holding is released — otherwise a user who opened the confirm dialog
    and walked away has burned the node's budget for the rest of the day."""
    wallet = MoneroWallet(config(spend_ledger_path=str(tmp_path / "spend.sqlite3"),
                                 transfer_cap_atomic=100_000_000_000,
                                 daily_cap_atomic=100_000_000_000))
    gate = TransferGate()
    clock = [1_800_000_000.0]
    monkeypatch.setattr(monero_wallet_service.time, "time", lambda: clock[0])

    token, expires = asyncio.run(gate.prepare(wallet, 7, ADDRESS, 100_000_000_000))
    assert expires == clock[0] + 90
    with pytest.raises(WalletError, match="daily"):
        asyncio.run(gate.prepare(wallet, 7, ADDRESS, 1))     # the pending token holds the budget

    clock[0] += 91
    with pytest.raises(WalletError, match="invalid or expired"):
        asyncio.run(gate.confirm(wallet, 7, token))
    asyncio.run(gate.prepare(wallet, 7, ADDRESS, 100_000_000_000))   # released


def test_the_daily_cap_is_a_rolling_window_not_a_calendar_day(monkeypatch, tmp_path):
    """Spending recorded more than 24 hours ago stops counting, and the row is deleted rather than
    accumulating for the life of the node. A calendar-day reset would let a full cap be spent at
    23:59 and again at 00:01."""
    ledger = tmp_path / "spend.sqlite3"
    wallet = MoneroWallet(config(spend_ledger_path=str(ledger),
                                 transfer_cap_atomic=100_000_000_000,
                                 daily_cap_atomic=100_000_000_000))
    clock = [1_800_000_000.0]
    monkeypatch.setattr(monero_wallet_service.time, "time", lambda: clock[0])

    async def transfer(address, amount):
        return {"tx_hash": "abc"}

    monkeypatch.setattr(wallet, "transfer", transfer)
    gate = TransferGate()
    token, _ = asyncio.run(gate.prepare(wallet, 7, ADDRESS, 100_000_000_000))
    asyncio.run(gate.confirm(wallet, 7, token))
    with pytest.raises(WalletError, match="daily"):
        asyncio.run(gate.prepare(wallet, 7, ADDRESS, 1))

    clock[0] += 86_401
    asyncio.run(gate.prepare(wallet, 7, ADDRESS, 100_000_000_000))
    db = sqlite3.connect(str(ledger))
    try:
        assert db.execute("SELECT COUNT(*) FROM monero_spend_attempts").fetchone()[0] == 1
    finally:
        db.close()


def test_the_rpc_timeout_that_is_configured_is_the_one_that_is_used(monkeypatch):
    """A wallet that hangs holds a uvicorn worker on the single-worker deployment. The connect
    timeout is separately clamped to 2s, so a 30s read budget cannot become a 30s connect."""
    wallet = MoneroWallet(config(timeout_seconds=20))
    observed = {}

    class Client:
        def __init__(self, **kwargs):
            observed.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, json):
            return httpx.Response(200, json={"result": {}}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    asyncio.run(wallet.rpc("get_version"))
    assert observed["timeout"].read == 20
    assert observed["timeout"].connect == 2.0


@pytest.mark.parametrize("body", [
    [], "a string", None, 12,
    {"jsonrpc": "2.0"},                       # no result at all
    {"jsonrpc": "2.0", "result": "not-a-dict"},
    {"jsonrpc": "2.0", "result": None},
])
def test_an_answer_that_is_not_a_json_rpc_result_is_an_outage_not_a_balance(monkeypatch, body):
    """Anything can be listening on a loopback port. A captive portal, an unrelated service or a
    half-written response must read as "the wallet is unavailable", never as an empty wallet."""
    wallet = MoneroWallet(config())

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, json):
            return httpx.Response(200, json=body, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    with pytest.raises(WalletError, match="invalid response|unavailable"):
        asyncio.run(wallet.balance())


class TheWalletComesBackByItself(unittest.TestCase):
    """A RESTARTED DAEMON IS NOT A PERMANENT VERDICT.

    Reported as: "does our monero implementation now support monero restarts: Local wallet
    unavailable — This device is in safe external-wallet mode ... I restarted monerod and then this
    happens."

    Nothing was wrong by the time it was read. `monerod` had come back and the wallet needed a
    moment to reconnect; the probe had asked once, during that moment, and the screen then held the
    answer until somebody pressed Retry. The wording made it worse than a spinner: "safe
    external-wallet mode" reads as a DECISION this device has taken, not as one failed request.

    The screen now re-probes on a backoff while it is open. The cost is bounded for the ordinary
    case — a node with no wallet at all, which is the default — and it stops the instant the view is
    left, so it cannot become a background poller nobody asked for.
    """

    SRC = (pathlib.Path(__file__).resolve().parents[1]
           / "static/js/client/monero-wallet.js").read_text(encoding="utf-8")

    def _watch(self):
        body = self.SRC[self.SRC.index("function _watch(s){"):]
        return body[:body.index("\n  }") + 4]

    def test_an_unavailable_wallet_is_asked_again(self):
        # The CALL, not the definition — `function _watch(s){` matched the old assertion, so
        # deleting the call left this green. (Caught by mutating it.)
        self.assertIn("paint(s);\n    _watch(s);", self.SRC, "render never arms a retry")
        self.assertIn("probe(true)", self._watch(), "the retry does not force a fresh probe")

    def test_a_working_wallet_arms_nothing(self):
        """The common case must cost nothing: no timer while the wallet is answering."""
        self.assertIn("if(s && s.available) return;", self._watch())

    def test_it_backs_off_and_has_a_ceiling(self):
        """A node with no wallet is the DEFAULT. Asking every three seconds for ever would be a
        request storm on the machines least able to answer it."""
        watch = self._watch()
        self.assertIn("_watchDelay*2", watch, "the retry does not back off")
        self.assertIn("30000", watch, "the backoff has no ceiling")

    def test_leaving_the_screen_stops_it(self):
        """Twice — before the probe and after it — because the probe is awaited and somebody can
        leave during it. Otherwise this is a background poller for a screen nobody is looking at."""
        watch = self._watch()
        self.assertEqual(watch.count("PC.VIEW!=='wallet'"), 2, watch)
        self.assertIn("_stopWatch()", watch)

    def test_a_repeated_failure_does_not_repaint(self):
        """Redrawing the same failure every few seconds throws away the scroll position and makes a
        quiet retry look like something actively going wrong."""
        watch = self._watch()
        self.assertIn("if(next && next.available) paint(next);", watch)

    def test_the_manual_retry_still_exists(self):
        """A person asking is still allowed to ask immediately, rather than waiting out a backoff."""
        self.assertIn("by('mw-retry').onclick=()=>render(true)", self.SRC)
