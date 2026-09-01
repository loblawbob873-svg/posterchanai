"""THE HTTP SURFACE OF THE BUILT-IN MONERO WALLET — the half a browser can actually reach.

`tests/test_monero_wallet.py` covers the service in isolation, but every rule it proves is only
worth what the six routes in `app/routers/monero_wallet.py` enforce, and those had no test at all.
The routes are the whole attack surface: they are same-origin, cookie-authenticated, and the one at
the end of them moves money out of a hot wallet with no confirmation step of its own.

What can be wrong here without any of the service tests noticing:

  * A route added without `WalletOwner` is an unauthenticated spend endpoint. The annotation is easy
    to forget precisely because the five routes above it already have it, so this file sweeps EVERY
    route on the router — statically and by calling it — rather than naming the six that exist today.
  * `_bad()` decides 400-vs-503 by looking for the word "unavailable" in the message. A wallet that
    is down must not read as "you sent a bad request", and a rejected amount must not read as an
    outage the client should retry.
  * A wallet error carries a daemon path, an RPC URL or the rpc-login credential straight into a
    JSON body the browser can read. The service is careful about this; the router is what actually
    serialises it.
  * Amounts have to leave the process as decimal STRINGS. A balance above 2^53 atomic units that
    crosses the wire as a JSON number is silently a different amount by the time it is on screen,
    and the response body is the last place that can be checked.
  * Spending is two calls and one token. Nothing but this file proves the first call moves no money
    and the second cannot be replayed.

The RPC is stubbed everywhere: no test in this file may depend on a monero-wallet-rpc being up, and
none of them may send a real transfer.
"""
from __future__ import annotations

import sqlite3
import time

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import auth
from app.models import User
from app.routers import monero_wallet as router_module
from app.services import monero_wallet_service as svc

ROOT = __import__('pathlib').Path(__file__).resolve().parents[1]

ADDRESS = "5" + "A" * 94
MAINNET = "4" + "A" * 94
ADMIN = User(id=3, username="root", is_admin=True)
OTHER_ADMIN = User(id=9, username="second", is_admin=True)
MEMBER = User(id=4, username="joe", is_admin=False)


class RpcLog(list):
    """The calls that reached the wallet, plus the canned answers a test can rewrite."""
    replies: dict


@pytest.fixture
def rpc_calls(monkeypatch):
    """Every JSON-RPC call the routes make, with the real client swapped out."""
    calls = RpcLog()
    replies: dict[str, object] = {
        "get_balance": {"balance": 5, "unlocked_balance": 5},
        "get_address": {"address": ADDRESS, "addresses": [{"address": ADDRESS, "used": False}]},
        "get_transfers": {"in": [], "out": [], "pending": [], "failed": []},
        "make_uri": {"uri": "monero:" + ADDRESS},
        "transfer": {"tx_hash": "deadbeef", "amount": 50_000_000_000, "fee": 30_000_000},
    }

    async def rpc(self, method, params=None):
        calls.append((method, params or {}))
        reply = replies[method]
        if isinstance(reply, Exception):
            raise reply
        return dict(reply)

    monkeypatch.setattr(svc.MoneroWallet, "rpc", rpc)
    calls.replies = replies
    return calls


@pytest.fixture
def env(monkeypatch, tmp_path):
    """A valid, enabled, stagenet configuration pointed at a throwaway spend ledger."""
    values = {
        "MONERO_WALLET_ENABLED": "1",
        "MONERO_WALLET_RPC_URL": "http://127.0.0.1:38083/json_rpc",
        "MONERO_WALLET_RPC_USER": "posterchan",
        "MONERO_WALLET_RPC_PASSWORD": "secret",
        "MONERO_WALLET_NETWORK": "stagenet",
        "MONERO_WALLET_TRANSFER_CAP_XMR": "0.1",
        "MONERO_WALLET_DAILY_CAP_XMR": "0.5",
        "MONERO_WALLET_RPC_TIMEOUT": "8",
        "MONERO_WALLET_SPEND_LEDGER": str(tmp_path / "spend.sqlite3"),
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return values


@pytest.fixture
def gate(monkeypatch):
    """A fresh confirmation gate per test — the shipped one is a module singleton."""
    fresh = svc.TransferGate()
    monkeypatch.setattr(router_module, "transfer_gate", fresh)
    return fresh


def make_client(user: User | None = ADMIN) -> TestClient:
    api = FastAPI()
    # Mounted the way main.py mounts it: the router itself carries no prefix so the app can serve
    # both the canonical path and the legacy one (see the WAF note in the router).
    api.include_router(router_module.router, prefix="/api/wallet/xmr")
    if user is not None:
        api.dependency_overrides[auth.get_current_user] = lambda: user
    return TestClient(api)


@pytest.fixture
def client(env, gate, rpc_calls):
    return make_client()


#: (method, path, body) for every route, so the auth sweeps below cannot go stale.
ROUTES = [
    ("GET", "/api/wallet/xmr/status", None),
    ("GET", "/api/wallet/xmr/node-status", None),
    ("GET", "/api/wallet/xmr/balance", None),
    ("GET", "/api/wallet/xmr/address", None),
    ("GET", "/api/wallet/xmr/history", None),
    ("POST", "/api/wallet/xmr/make-uri", {"address": ADDRESS, "amount": "0.01"}),
    ("POST", "/api/wallet/xmr/transfer/prepare", {"address": ADDRESS, "amount": "0.01"}),
    ("POST", "/api/wallet/xmr/transfer/confirm", {"confirmation": "z" * 43}),
]


def test_the_route_table_this_file_sweeps_is_the_whole_router():
    """If a route is added, it joins ROUTES or this fails — the auth sweeps below are only as
    complete as that list, and an unswept spend route is the failure this whole file exists for."""
    live = {(method, "/api/wallet/xmr" + route.path) for route in router_module.router.routes
            for method in route.methods}
    assert live == {(method, path) for method, path, _ in ROUTES}


def test_the_canonical_path_does_not_say_monero():
    """CLOUDFLARE'S MANAGED WAF BLOCKS ANY PATH CONTAINING "monero" AS A CRYPTOMINING PATTERN, and
    serves its own 403 with NO CORS headers — so the browser rejects it and the client sees a bare
    "Failed to fetch". Measured through Cloudflare: /api/wallet/monero/status → 403 from cloudflare,
    /api/wallet/foo → 404 from us, /api/walletx/monero/status → 403. The trigger is the word.

    It only bit users coming through Cloudflare, which is why it read as Android-only: the
    operator's browser is on the LAN, where DNS skips Cloudflare, and the phone on cellular is not.
    "It worked on wifi" was the tell."""
    for _, path, _ in ROUTES:
        assert "monero" not in path.lower(), (
            f"{path} carries the word a managed WAF blocks — every request to it 403s before it "
            f"reaches the node, with no CORS on the refusal")


def test_the_old_path_is_still_served_for_installed_clients():
    """An APK already on somebody's phone asks for the old path, and on a node that is not behind
    such a WAF it has always worked. Both prefixes are mounted; only the canonical one is asked for."""
    main_py = (ROOT / "app/main.py").read_text(encoding="utf-8")
    assert 'prefix="/api/wallet/xmr"' in main_py
    assert 'prefix="/api/wallet/monero"' in main_py


def test_the_client_only_ever_asks_for_the_canonical_path():
    """The compatibility mount must not become the one we use, or the WAF bug is back."""
    for rel in ("static/js/client/monero-wallet.js", "static/js/client/os.js",
                "templates/admin/tabs/monero_wallet.html"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "/api/wallet/monero/" not in text, f"{rel} still asks for the WAF-blocked path"


def test_every_route_is_declared_admin_only():
    """Static half: the `WalletOwner` annotation, on every route, with no exceptions."""
    for route in router_module.router.routes:
        names = [dep.call.__name__ for dep in route.dependant.dependencies]
        assert "get_admin_user" in names, f"{route.path} is not admin-gated"


@pytest.mark.parametrize("method,path,body", ROUTES)
def test_no_route_answers_without_a_session(env, gate, rpc_calls, method, path, body):
    """Functional half. Auth is resolved before the body is even validated, so a malformed request
    to an ungated route would still show up here as something other than 401."""
    anonymous = make_client(user=None)
    response = anonymous.request(method, path, json=body)
    assert response.status_code == 401, f"{path} answered {response.status_code} with no session"
    assert rpc_calls == []


@pytest.mark.parametrize("method,path,body", ROUTES)
def test_a_signed_in_non_admin_can_neither_read_nor_spend(env, gate, rpc_calls, method, path, body):
    """The wallet is the node operator's. A normal account — including a Nostr signup — must not
    see the balance or the receive address, let alone reach the transfer routes."""
    member = make_client(user=MEMBER)
    response = member.request(method, path, json=body)
    assert response.status_code == 403
    assert rpc_calls == [], "a forbidden request still reached the wallet RPC"


# --------------------------------------------------------------------------- failure shapes


def test_a_disabled_wallet_is_a_503_that_names_no_configuration(monkeypatch, gate, rpc_calls):
    """The default on every node is "no wallet". That is a service state, not a client error, and
    the answer must not describe the operator's setup."""
    monkeypatch.delenv("MONERO_WALLET_ENABLED", raising=False)
    monkeypatch.setenv("MONERO_WALLET_RPC_PASSWORD", "hunter2")
    response = make_client().get("/api/wallet/xmr/balance")
    assert response.status_code == 503
    assert "hunter2" not in response.text
    assert rpc_calls == []


def test_a_misconfigured_wallet_never_returns_a_500(monkeypatch, env, gate, rpc_calls):
    """A bad cap or a non-loopback URL is an operator mistake; it still has to come back as a
    handled 503 rather than an unhandled exception with a traceback in the log."""
    for key, value in [("MONERO_WALLET_RPC_URL", "http://100.64.0.5:38083/json_rpc"),
                       ("MONERO_WALLET_RPC_PASSWORD", ""),
                       ("MONERO_WALLET_NETWORK", "testnet"),
                       ("MONERO_WALLET_DAILY_CAP_XMR", "0.01")]:
        monkeypatch.setenv(key, value)
        response = make_client().get("/api/wallet/xmr/balance")
        assert response.status_code == 503, f"{key}={value!r} gave {response.status_code}"
        monkeypatch.setenv(key, env[key])


def test_a_wallet_that_rejects_the_call_is_400_and_a_wallet_that_is_down_is_503(client, rpc_calls):
    """`_bad()` splits on the word "unavailable". A client retries a 503 and reports a 400, so
    getting this backwards either hammers a dead daemon or tells the user their input was wrong."""
    rpc_calls.replies["get_balance"] = svc.WalletError("Monero wallet rejected the request")
    assert client.get("/api/wallet/xmr/balance").status_code == 400
    rpc_calls.replies["get_balance"] = svc.WalletError("Local Monero wallet is unavailable")
    assert client.get("/api/wallet/xmr/balance").status_code == 503


def test_no_error_body_carries_a_daemon_path_url_or_credential(env, gate, monkeypatch):
    """monero-wallet-rpc puts wallet file paths and daemon addresses in its OWN error messages, and
    `_bad()` serialises the exception text straight into `detail`. So the redaction has to happen
    where the RPC answer is read, and this drives the real transport to prove it does: the router
    must not become a window onto the operator's filesystem for anyone who can make the daemon fail.

    Stubbing `MoneroWallet.rpc` (as the rest of this file does) would skip the only code that sees
    the leaky text, so this test deliberately does not use the `rpc_calls` fixture."""
    leaky = ("no connection to daemon at http://127.0.0.1:38081, "
             "failed to open wallet /home/op/.monero/tips.keys")

    class LeakyClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, json):
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": "0",
                                             "error": {"code": -1, "message": leaky}},
                                  request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "AsyncClient", LeakyClient)
    response = make_client().get("/api/wallet/xmr/balance")
    assert response.status_code == 400
    for secret in ("/home/op", ".keys", ".monero", "38081", "posterchan", "secret"):
        assert secret not in response.text, f"the error body leaked {secret!r}"


# --------------------------------------------------------------------------- status surfaces


def test_status_tells_the_client_which_network_and_caps_it_is_working_against(client):
    """The client renders its whole risk warning from this, and picks the address alphabet it will
    accept from `network` — so a wrong answer here is either a scary label on a test wallet or, far
    worse, a stagenet label on a wallet holding real funds."""
    body = client.get("/api/wallet/xmr/status").json()
    assert body["network"] == "stagenet" and body["mainnet"] is False
    assert body["transfer_cap"] == "0.1" and body["daily_cap"] == "0.5"
    assert "no value" in body["warning"]


def test_status_says_MAINNET_HOT_WALLET_when_the_node_is_configured_for_real_funds(
        env, gate, rpc_calls, monkeypatch):
    """The only difference between a test wallet and somebody's money is this one setting. The
    warning has to change with it, in the answer the client actually draws from."""
    monkeypatch.setenv("MONERO_WALLET_NETWORK", "mainnet")
    body = make_client().get("/api/wallet/xmr/status").json()
    assert body["network"] == "mainnet" and body["mainnet"] is True
    assert "MAINNET" in body["warning"]


def test_status_carries_no_credential_url_or_ledger_path(client):
    """It is the one wallet route with no error path, so it is the easiest one to grow a field on."""
    text = client.get("/api/wallet/xmr/status").text
    for secret in ("posterchan", "secret", "127.0.0.1", "38083", "json_rpc", "sqlite"):
        assert secret not in text, f"/status leaked {secret!r}"


def test_status_caps_are_decimal_strings_not_atomic_integers(client):
    """They are rendered straight into the admin panel and the send sheet. An atomic integer there
    reads as a cap 10^12 times larger than the operator set."""
    text = client.get("/api/wallet/xmr/status").text
    assert "100000000000" not in text and "500000000000" not in text


def test_node_status_reports_reachability_without_naming_an_address_or_a_key(client, rpc_calls):
    """The admin panel's "Check Node Status" button. It exists to answer "is the daemon there", and
    it is the one wallet call whose whole output is pasted into a <pre> an operator may screenshot."""
    rpc_calls.replies["get_height"] = {"height": 1_234_567}
    body = client.get("/api/wallet/xmr/node-status").json()
    assert body["wallet_rpc_reachable"] is True and body["daemon_connected"] is True
    assert body["height"] == 1_234_567 and body["network"] == "stagenet"
    assert body["balance"] == "0.000000000005"
    for leaked in ("address", "addresses", "txid", "tx_key", "seed", "password", "url"):
        assert leaked not in body, f"node-status exposed {leaked!r}"
    assert [method for method, _ in rpc_calls] == ["get_height", "get_balance"]


def test_node_status_reports_a_daemon_that_is_not_connected_rather_than_guessing(client, rpc_calls):
    """A wallet RPC that is up with no daemon behind it answers a height that is not a number. That
    is "disconnected", not a height of 0 and not an exception."""
    rpc_calls.replies["get_height"] = {"error": "no daemon"}
    body = client.get("/api/wallet/xmr/node-status").json()
    assert body["wallet_rpc_reachable"] is True
    assert body["daemon_connected"] is False
    assert body["height"] is None


def test_node_status_is_a_503_when_the_wallet_rpc_itself_is_down(env, gate, monkeypatch):
    """`wallet_rpc_reachable` is hardcoded True — it can only be reported at all if the two RPC
    calls above it succeeded. The unreachable case therefore has to be the error path, or the panel
    would print "Wallet RPC: reachable" about a wallet that is not running."""
    class Dead:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, json):
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "AsyncClient", Dead)
    response = make_client().get("/api/wallet/xmr/node-status")
    assert response.status_code == 503
    assert "reachable" not in response.text


# --------------------------------------------------------------------------- the RPC URL boundary


@pytest.mark.parametrize("url,allowed", [
    ("http://127.0.0.1:38083/json_rpc", True),
    ("http://192.168.0.85:38083/json_rpc", True),      # a wallet box on the operator's own LAN
    ("http://10.4.4.4:38083/json_rpc", True),
    ("http://172.16.9.9:38083/json_rpc", True),
    ("http://172.32.0.1:38083/json_rpc", False),       # just outside 172.16/12
    ("http://8.8.8.8:38083/json_rpc", False),
    ("http://wallet.lan:38083/json_rpc", False),       # DNS is not trusted for this boundary
    ("https://127.0.0.1:38083/json_rpc", False),
    ("http://u:p@127.0.0.1:38083/json_rpc", False),
    ("http://127.0.0.1:38083/json_rpc?x=1", False),
])
def test_the_wallet_rpc_may_only_be_local_or_on_the_operators_own_network(env, gate, url, allowed):
    """The RPC speaks for the whole wallet with one digest credential and no TLS, so where it may
    live is a security boundary. It was loopback-only and now admits RFC1918 for a separate wallet
    box — this pins exactly how far that went, since the failure mode of getting it wrong is a hot
    wallet's credentials crossing the public internet in the clear."""
    import os
    os.environ["MONERO_WALLET_RPC_URL"] = url
    status = make_client().get("/api/wallet/xmr/status").status_code
    assert (status == 200) is allowed, f"{url} -> {status}"


# --------------------------------------------------------------------------- amounts on the wire


def test_balances_leave_the_process_as_decimal_strings_never_json_numbers(client, rpc_calls):
    """9007199254740993 atomic units is one unit above Number.MAX_SAFE_INTEGER: parsed as a JSON
    number it comes back as ...992 and the balance on screen is wrong by a full atomic unit with
    nothing to say so. The assertion is against the raw TEXT, because `response.json()` would
    quietly repair it in Python where the browser cannot."""
    rpc_calls.replies["get_balance"] = {"balance": 9_007_199_254_740_993, "unlocked_balance": 1,
                                        "blocks_to_unlock": 12}
    response = client.get("/api/wallet/xmr/balance")
    assert response.status_code == 200
    assert "9007199254740993" not in response.text, "an atomic integer crossed the wire unconverted"
    assert response.json() == {"balance": "9007.199254740993",
                               "unlocked_balance": "0.000000000001", "blocks_to_unlock": 12}


def test_history_amounts_are_converted_inside_every_bucket(client, rpc_calls):
    rpc_calls.replies["get_transfers"] = {
        "in": [{"amount": 2_000_000_000_000, "fee": 0, "txid": "a" * 64, "height": 900}],
        "out": [{"amount": 1, "fee": 30_000_000, "txid": "b" * 64, "height": 901}],
        "pending": [], "failed": [],
    }
    body = client.get("/api/wallet/xmr/history").json()
    assert body["in"][0] == {"amount": "2", "fee": "0", "txid": "a" * 64, "height": 900}
    assert body["out"][0]["amount"] == "0.000000000001"
    assert body["out"][0]["fee"] == "0.00003"
    assert body["out"][0]["height"] == 901, "a block height is not an amount"


@pytest.mark.parametrize("limit,status", [(0, 400), (-1, 400), (101, 400), (1, 200), (100, 200)])
def test_the_history_limit_is_bounded_at_the_route(client, limit, status):
    """An unbounded limit is a request for the whole wallet history over a websocket-less GET. The
    refusal has to be a 400, not a 500 from slicing with a negative number."""
    assert client.get(f"/api/wallet/xmr/history?limit={limit}").status_code == status


def test_history_keeps_the_newest_rows_when_it_truncates(client, rpc_calls):
    """`[-limit:]` is a tail slice. Getting it the wrong way round shows the oldest transfers on a
    screen whose whole purpose is "did my tip go out", and both slices are the same length."""
    rpc_calls.replies["get_transfers"] = {
        "in": [{"amount": n, "height": n} for n in range(1, 6)], "out": [], "pending": [], "failed": []}
    rows = client.get("/api/wallet/xmr/history?limit=2").json()["in"]
    assert [row["height"] for row in rows] == [4, 5]


# --------------------------------------------------------------------------- request validation


@pytest.mark.parametrize("body,status", [
    ({"address": ADDRESS, "amount": "0.01"}, 200),
    ({"address": MAINNET, "amount": "0.01"}, 400),                 # wrong network for this node
    ({"address": "5A", "amount": "0.01"}, 422),                    # below the length floor
    ({"address": ADDRESS + "AAAAAAAAAAAA", "amount": "0.01"}, 422),
    ({"address": ADDRESS, "amount": ""}, 422),
    ({"address": ADDRESS, "amount": "abc"}, 400),
    ({"address": ADDRESS, "amount": "0"}, 400),
    ({"address": ADDRESS, "amount": "-0.01"}, 400),
    ({"address": ADDRESS, "amount": "0.0000000000001"}, 400),      # below one atomic unit
    ({"address": ADDRESS, "amount": "0.01", "description": "x" * 141}, 422),
    ({"address": ADDRESS, "amount": "0.01", "description": "line\nbreak"}, 400),
], ids=lambda v: str(v)[:60])
def test_make_uri_validates_before_it_asks_the_wallet(client, rpc_calls, body, status):
    response = client.post("/api/wallet/xmr/make-uri", json=body)
    assert response.status_code == status, response.text
    if status != 200:
        assert rpc_calls == [], "an invalid payment request still reached the wallet RPC"


def test_make_uri_hands_the_wallet_atomic_units_not_the_typed_string(client, rpc_calls):
    """The RPC takes atomic units. Passing the decimal string through would make `make_uri` build a
    URI for 0.01 piconero, or for nothing at all."""
    client.post("/api/wallet/xmr/make-uri", json={"address": ADDRESS, "amount": "0.01"})
    assert rpc_calls[-1] == ("make_uri", {"address": ADDRESS, "amount": 10_000_000_000,
                                          "tx_description": ""})


@pytest.mark.parametrize("confirmation", ["", "z" * 31, "z" * 129])
def test_a_confirmation_outside_the_token_shape_is_refused_by_the_schema(client, confirmation):
    response = client.post("/api/wallet/xmr/transfer/confirm", json={"confirmation": confirmation})
    assert response.status_code == 422


# --------------------------------------------------------------------------- spending


def test_preparing_a_transfer_moves_no_money(client, rpc_calls):
    """The first call exists solely so the UI can show what is about to happen. If it spends, the
    "I understand this cannot be reversed" checkbox is decoration."""
    response = client.post("/api/wallet/xmr/transfer/prepare",
                           json={"address": ADDRESS, "amount": "0.05"})
    assert response.status_code == 200
    body = response.json()
    assert body["address"] == ADDRESS and body["amount_atomic"] == 50_000_000_000
    assert body["expires_at"] > time.time()
    assert len(body["confirmation"]) >= 32
    assert [method for method, _ in rpc_calls] == [], "prepare called the wallet RPC"


def test_the_confirmation_is_one_use_and_the_replay_sends_nothing(client, rpc_calls):
    token = client.post("/api/wallet/xmr/transfer/prepare",
                        json={"address": ADDRESS, "amount": "0.05"}).json()["confirmation"]
    sent = client.post("/api/wallet/xmr/transfer/confirm", json={"confirmation": token})
    assert sent.status_code == 200 and sent.json()["tx_hash"] == "deadbeef"
    assert [method for method, _ in rpc_calls] == ["transfer"]

    replay = client.post("/api/wallet/xmr/transfer/confirm", json={"confirmation": token})
    assert replay.status_code == 400
    assert [method for method, _ in rpc_calls] == ["transfer"], "a replayed token sent a second time"


def test_the_amount_that_is_sent_is_the_one_the_server_parsed(client, rpc_calls):
    """The confirm call carries a token and nothing else, and an `amount_atomic` smuggled into the
    prepare body is not a field — so the destination amount can only come from the server's own
    Decimal parse of the typed XMR string."""
    prepared = client.post("/api/wallet/xmr/transfer/prepare", json={
        "address": ADDRESS, "amount": "0.05", "amount_atomic": 99_000_000_000_000}).json()
    client.post("/api/wallet/xmr/transfer/confirm",
                json={"confirmation": prepared["confirmation"], "address": MAINNET,
                      "amount": "500"})
    method, params = rpc_calls[-1]
    assert method == "transfer"
    assert params["destinations"] == [{"amount": 50_000_000_000, "address": ADDRESS}]


def test_a_fabricated_token_is_refused_without_touching_the_wallet(client, rpc_calls):
    response = client.post("/api/wallet/xmr/transfer/confirm", json={"confirmation": "z" * 43})
    assert response.status_code == 400
    assert rpc_calls == []


def test_a_confirmation_only_works_for_the_account_that_prepared_it(env, gate, rpc_calls):
    """Two operators share one node-wide RPC wallet. A token is a promise made to one session."""
    first = make_client(user=ADMIN)
    token = first.post("/api/wallet/xmr/transfer/prepare",
                       json={"address": ADDRESS, "amount": "0.05"}).json()["confirmation"]
    second = make_client(user=OTHER_ADMIN)
    assert second.post("/api/wallet/xmr/transfer/confirm",
                       json={"confirmation": token}).status_code == 400
    assert rpc_calls == []


def test_the_per_transfer_cap_is_enforced_at_prepare_time(client, rpc_calls):
    """0.1 XMR from the environment. The cap has to bite before a token exists, or the UI shows a
    confirmation dialog for a payment that can never go out."""
    response = client.post("/api/wallet/xmr/transfer/prepare",
                           json={"address": ADDRESS, "amount": "0.10000000001"})
    assert response.status_code == 400
    assert "per-transfer" in response.json()["detail"]
    assert rpc_calls == []


def test_the_daily_cap_is_node_wide_and_survives_a_restart(env, gate, rpc_calls, tmp_path):
    """0.5 XMR daily against a 0.1 per-transfer cap. The wallet is one hot wallet for the whole
    node, so a second operator must not get their own allowance — and the budget lives in a file,
    so restarting the service must not hand the allowance back."""
    first = make_client(user=ADMIN)
    for _ in range(5):
        token = first.post("/api/wallet/xmr/transfer/prepare",
                           json={"address": ADDRESS, "amount": "0.1"}).json()["confirmation"]
        assert first.post("/api/wallet/xmr/transfer/confirm",
                          json={"confirmation": token}).status_code == 200

    refused = first.post("/api/wallet/xmr/transfer/prepare",
                         json={"address": ADDRESS, "amount": "0.000000000001"})
    assert refused.status_code == 400 and "daily" in refused.json()["detail"]

    other = make_client(user=OTHER_ADMIN)
    assert other.post("/api/wallet/xmr/transfer/prepare",
                      json={"address": ADDRESS, "amount": "0.1"}).status_code == 400

    # A restart is a brand-new gate with an empty in-memory pending map.
    restarted = svc.TransferGate()
    fresh_app = FastAPI()
    fresh_app.include_router(router_module.router, prefix="/api/wallet/xmr")
    fresh_app.dependency_overrides[auth.get_current_user] = lambda: ADMIN
    with TestClient(fresh_app) as client_after_restart:
        router_module.transfer_gate = restarted
        try:
            assert client_after_restart.post("/api/wallet/xmr/transfer/prepare",
                                             json={"address": ADDRESS, "amount": "0.1"}).status_code == 400
        finally:
            router_module.transfer_gate = gate


def test_unconfirmed_preparations_hold_budget_so_they_cannot_be_stacked(client):
    """Five 0.1 tokens against a 0.5 daily cap, none of them spent yet. Counting only settled
    spending would let a UI (or a script) mint tokens for ten times the operator's limit and then
    cash them all in."""
    for _ in range(5):
        assert client.post("/api/wallet/xmr/transfer/prepare",
                           json={"address": ADDRESS, "amount": "0.1"}).status_code == 200
    stacked = client.post("/api/wallet/xmr/transfer/prepare",
                          json={"address": ADDRESS, "amount": "0.1"})
    assert stacked.status_code == 400 and "daily" in stacked.json()["detail"]


def test_a_transfer_the_wallet_refused_still_spends_the_budget(client, rpc_calls, env):
    """Deliberate, and worth pinning: the attempt is written to the ledger BEFORE the RPC, so a
    call that fails — or times out with the transaction already broadcast — consumes its allowance.
    The alternative (credit it back on error) hands an attacker an unbounded retry loop against a
    wallet whose answer they can influence."""
    rpc_calls.replies["transfer"] = svc.WalletError("Local Monero wallet is unavailable")
    token = client.post("/api/wallet/xmr/transfer/prepare",
                        json={"address": ADDRESS, "amount": "0.1"}).json()["confirmation"]
    assert client.post("/api/wallet/xmr/transfer/confirm",
                       json={"confirmation": token}).status_code == 503

    ledger = sqlite3.connect(env["MONERO_WALLET_SPEND_LEDGER"])
    try:
        spent = ledger.execute("SELECT COALESCE(SUM(amount_atomic),0) FROM monero_spend_attempts").fetchone()[0]
    finally:
        ledger.close()
    assert spent == 100_000_000_000


def test_the_ledger_file_is_never_world_readable(client, env):
    """It records what the operator spent and when. It is created by the app on first use, so its
    mode is the app's responsibility, not the packaging's."""
    import os
    client.post("/api/wallet/xmr/transfer/prepare", json={"address": ADDRESS, "amount": "0.01"})
    token = client.post("/api/wallet/xmr/transfer/prepare",
                        json={"address": ADDRESS, "amount": "0.01"}).json()["confirmation"]
    client.post("/api/wallet/xmr/transfer/confirm", json={"confirmation": token})
    assert os.stat(env["MONERO_WALLET_SPEND_LEDGER"]).st_mode & 0o777 == 0o600


def test_the_transfer_route_never_asks_for_the_key_hex_or_metadata(client, rpc_calls):
    """`get_tx_key`/`get_tx_hex`/`get_tx_metadata` would put spendable or de-anonymising material in
    an HTTP response body. The route asks for a hash and nothing else."""
    token = client.post("/api/wallet/xmr/transfer/prepare",
                        json={"address": ADDRESS, "amount": "0.01"}).json()["confirmation"]
    response = client.post("/api/wallet/xmr/transfer/confirm", json={"confirmation": token})
    _, params = rpc_calls[-1]
    assert params["get_tx_key"] is False
    assert params["get_tx_hex"] is False
    assert params["get_tx_metadata"] is False
    for forbidden in ("tx_key", "tx_blob", "tx_metadata", "key_image", "seed"):
        assert forbidden not in response.text


# --------------------------------------------------------------------------- mainnet


@pytest.fixture
def mainnet_client(env, gate, rpc_calls, monkeypatch):
    monkeypatch.setenv("MONERO_WALLET_NETWORK", "mainnet")
    return make_client()


def test_a_mainnet_node_pays_mainnet_addresses_and_refuses_stagenet_ones(mainnet_client, rpc_calls):
    """Cross-network sends are the expensive mistake this wallet can still make: the two alphabets
    are interchangeable to the eye, and a payment to a valid address of the WRONG network is money
    gone. The node's own configuration is what decides which one is acceptable — not the caller."""
    prepared = mainnet_client.post("/api/wallet/xmr/transfer/prepare",
                                   json={"address": MAINNET, "amount": "0.01"})
    assert prepared.status_code == 200

    refused = mainnet_client.post("/api/wallet/xmr/transfer/prepare",
                                  json={"address": ADDRESS, "amount": "0.01"})
    assert refused.status_code == 400
    assert "mainnet" in refused.json()["detail"]
    assert rpc_calls == [], "a wrong-network address reached the wallet RPC"


def test_a_mainnet_node_also_refuses_a_stagenet_address_in_make_uri(mainnet_client, rpc_calls):
    """The URI path is the one that ends up in a QR somebody else scans, so it gets the same gate."""
    assert mainnet_client.post("/api/wallet/xmr/make-uri",
                               json={"address": ADDRESS, "amount": "0.01"}).status_code == 400
    assert mainnet_client.post("/api/wallet/xmr/make-uri",
                               json={"address": MAINNET, "amount": "0.01"}).status_code == 200


def test_the_caps_still_bind_on_mainnet(mainnet_client):
    """The caps exist for real funds more than for test funds. Nothing about switching network may
    relax them — and the daily cap is still node-wide, not per account."""
    over = mainnet_client.post("/api/wallet/xmr/transfer/prepare",
                               json={"address": MAINNET, "amount": "0.2"})
    assert over.status_code == 400 and "per-transfer" in over.json()["detail"]

    for _ in range(5):
        token = mainnet_client.post("/api/wallet/xmr/transfer/prepare",
                                    json={"address": MAINNET, "amount": "0.1"}).json()["confirmation"]
        assert mainnet_client.post("/api/wallet/xmr/transfer/confirm",
                                   json={"confirmation": token}).status_code == 200
    spent_out = mainnet_client.post("/api/wallet/xmr/transfer/prepare",
                                    json={"address": MAINNET, "amount": "0.001"})
    assert spent_out.status_code == 400 and "daily" in spent_out.json()["detail"]
