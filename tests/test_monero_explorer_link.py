"""WHERE A TRANSACTION ID IS LOOKED UP IS DECIDED ON THE NODE, AND IT IS A PRIVACY DECISION.

Both wallet screens make a txid clickable. The URL those links are built from is resolved here,
never in the browser, and this file holds the three rules that makes it safe:

  * **THE NETWORK COMES FROM THE WALLET, NOT FROM THE CALLER.** `monero_wallet_network` defaults to
    stagenet and one setting separates the two chains. A mainnet explorer holding a stagenet txid is
    a "not found" page, which on a wallet screen reads as a lost payment — so the base is built from
    the network the node's own wallet is configured for, and an unknown network yields NO base at
    all. The client appends a validated id to whatever it is given and never guesses.
  * **AN OPERATOR CAN POINT IT ANYWHERE, INCLUDING NOWHERE.** A block-explorer request tells that
    explorer's operator a viewer's IP address and which transaction they care about, which is the
    correlation Monero is chosen to avoid. `monero_explorer_base` takes `off` (no links anywhere on
    this node), a base URL of the operator's own explorer, or a `.onion` — and it is DECLARED in
    `SettingsResponse`, without which the admin field would never hydrate.
  * **THE STORED VALUE IS UNTRUSTED INPUT BY THE TIME IT IS A LINK.** It is concatenated with a txid
    and handed to a browser, so anything that is not a plain http(s) URL — a `javascript:` scheme,
    embedded credentials, a quote that would break out of the attribute it is rendered into — must
    resolve to no link rather than to a working one.

Nothing here contacts an explorer, and no wallet RPC is required: the resolver is pure.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import auth
from app.models import User
from app.routers import monero_user_wallet as user_router
from app.routers import monero_wallet as node_router
from app.schemas import SettingsResponse
from app.services import monero_wallet_service as svc

ADMIN = User(id=3, username="root", is_admin=True, nostr_npub="a" * 64)

MAINNET_DEFAULT = "https://xmrchain.net/tx/"
STAGENET_DEFAULT = "https://stagenet.xmrchain.net/tx/"


@pytest.fixture
def setting(monkeypatch):
    """Set (or clear) `monero_explorer_base` the way the operator's Save does."""
    store = {}

    def get(key, default=None):
        return store.get(key, default if default is not None else "")

    monkeypatch.setattr("app.services.settings_store.get", get)
    monkeypatch.delenv("MONERO_EXPLORER_BASE", raising=False)
    return store


# ------------------------------------------------------------------------------- the network


def test_the_default_follows_the_wallets_own_network(setting):
    assert svc.explorer_tx_base("mainnet") == MAINNET_DEFAULT
    assert svc.explorer_tx_base("stagenet") == STAGENET_DEFAULT
    assert svc.explorer_tx_base("mainnet") != svc.explorer_tx_base("stagenet"), (
        "one explorer for both chains means every link on one of them is a dead page")


@pytest.mark.parametrize("network", ["", None, "testnet", "MAINNET_", "nonsense"])
def test_an_unknown_network_yields_no_link_rather_than_a_guess(setting, network):
    """Better no link than a mainnet lookup of a stagenet payment: the second one looks exactly
    like money that never arrived."""
    assert svc.explorer_tx_base(network) == ""


def test_the_network_is_case_insensitive_because_the_setting_is_free_text(setting):
    assert svc.explorer_tx_base("MainNet") == MAINNET_DEFAULT


# ------------------------------------------------------------------- the operator's own choice


@pytest.mark.parametrize("off", ["off", "OFF", "none", "no", "disabled", "0", "false", "  off  "])
def test_an_operator_can_take_the_links_off_this_node_entirely(setting, off):
    setting["monero_explorer_base"] = off
    assert svc.explorer_tx_base("mainnet") == ""
    assert svc.explorer_tx_base("stagenet") == ""


def test_an_operators_own_explorer_replaces_the_default_on_every_network(setting):
    setting["monero_explorer_base"] = "https://explorer.example.org/tx/"
    assert svc.explorer_tx_base("mainnet") == "https://explorer.example.org/tx/"
    assert svc.explorer_tx_base("stagenet") == "https://explorer.example.org/tx/"


def test_an_onion_is_allowed_because_it_is_the_most_private_answer_available(setting):
    setting["monero_explorer_base"] = "http://exploreqwjgdqwj.onion/tx/"
    assert svc.explorer_tx_base("mainnet") == "http://exploreqwjgdqwj.onion/tx/"


@pytest.mark.parametrize("base,expect", [
    # Already ends in a separator — used exactly as typed, so a `?tx=` explorer works.
    ("https://e.example/?tx=", "https://e.example/?tx="),
    ("https://e.example/tx/", "https://e.example/tx/"),
    ("https://e.example/x?a=1&", "https://e.example/x?a=1&"),
    # Otherwise it gets the separator it obviously meant: ".../tx" + a txid is a 404 nobody would
    # think to debug, because the base looks right in the settings field.
    ("https://e.example/tx", "https://e.example/tx/"),
    ("https://e.example", "https://e.example/"),
])
def test_a_base_is_joined_to_the_id_the_way_the_operator_meant(setting, base, expect):
    setting["monero_explorer_base"] = base
    assert svc.explorer_tx_base("mainnet") == expect


@pytest.mark.parametrize("bad", [
    "javascript:alert(1)",                       # a link is handed to a browser
    "data:text/html,<script>",
    "ftp://explorer.example/tx/",
    "//explorer.example/tx/",                    # scheme-relative is not an http URL
    "explorer.example/tx/",
    "https://user:pw@explorer.example/tx/",      # embedded credentials
    'https://e.example/"onmouseover="x',         # would break out of the href attribute
    "https://e.example/tx/ and more",            # whitespace
    "https://e.example/<script>",
])
def test_a_base_that_is_not_a_plain_http_url_produces_no_link_at_all(setting, bad):
    setting["monero_explorer_base"] = bad
    assert svc.explorer_tx_base("mainnet") == "", bad


def test_the_env_var_is_only_a_fallback_for_an_unconfigured_node(setting, monkeypatch):
    monkeypatch.setenv("MONERO_EXPLORER_BASE", "https://from-env.example/tx/")
    assert svc.explorer_tx_base("mainnet") == "https://from-env.example/tx/"
    setting["monero_explorer_base"] = "https://from-settings.example/tx/"
    assert svc.explorer_tx_base("mainnet") == "https://from-settings.example/tx/"


def test_an_unreadable_settings_store_falls_back_and_never_raises(monkeypatch):
    """Reading a setting must not be able to break the wallet's status route."""
    def boom(key, default=None):
        raise RuntimeError("relay unreachable")

    monkeypatch.setattr("app.services.settings_store.get", boom)
    monkeypatch.delenv("MONERO_EXPLORER_BASE", raising=False)
    assert svc.explorer_tx_base("stagenet") == STAGENET_DEFAULT


# --------------------------------------------------------------------------- the admin surface


def test_the_setting_is_declared_or_the_admin_field_never_hydrates():
    """An undeclared key is dropped from the GET, so the input loads blank on every visit and the
    operator's choice is invisible to them. See the Settings section of CLAUDE.md."""
    assert "monero_explorer_base" in SettingsResponse.model_fields
    assert SettingsResponse().monero_explorer_base == "", (
        "the default must be blank — blank is what selects the per-network built-in explorer")


def test_the_admin_input_exists_and_its_id_matches_its_name():
    """Hydration reads the id and Save reads the name; a mismatch loads or saves nothing."""
    from pathlib import Path
    html = (Path(__file__).resolve().parents[1]
            / "templates/admin/tabs/monero_wallet.html").read_text(encoding="utf-8")
    assert 'id="monero_explorer_base" name="monero_explorer_base"' in html
    # The field is only honest if it says what a link costs.
    assert "tells a third party" in html


# ------------------------------------------------------- both status routes carry the same answer


def _node_client(monkeypatch, tmp_path, network="stagenet"):
    for key, value in {
        "MONERO_WALLET_ENABLED": "1",
        "MONERO_WALLET_RPC_URL": "http://127.0.0.1:38083/json_rpc",
        "MONERO_WALLET_RPC_USER": "posterchan",
        "MONERO_WALLET_RPC_PASSWORD": "secret",
        "MONERO_WALLET_NETWORK": network,
        "MONERO_WALLET_TRANSFER_CAP_XMR": "0",
        "MONERO_WALLET_DAILY_CAP_XMR": "0",
        "MONERO_WALLET_RPC_TIMEOUT": "8",
        "MONERO_WALLET_SPEND_LEDGER": str(tmp_path / "spend.sqlite3"),
    }.items():
        monkeypatch.setenv(key, value)
    api = FastAPI()
    api.include_router(node_router.router, prefix="/api/wallet/xmr")
    # The membership gate is a different feature with its own tests, and it needs a hydrated
    # settings store this file deliberately does not build. Overridden at the wallet-owner
    # dependency, so what is measured here is the route body and nothing else.
    api.dependency_overrides[node_router.get_member_wallet_owner] = lambda: ADMIN
    api.dependency_overrides[auth.get_current_user] = lambda: ADMIN
    return TestClient(api)


@pytest.mark.parametrize("network,expect", [("stagenet", STAGENET_DEFAULT),
                                            ("mainnet", MAINNET_DEFAULT)])
def test_the_operator_status_route_carries_the_explorer_for_its_own_network(
        setting, monkeypatch, tmp_path, network, expect):
    body = _node_client(monkeypatch, tmp_path, network).get("/api/wallet/xmr/status").json()
    assert body["network"] == network
    assert body["explorer_tx_base"] == expect, (
        "the client would have to pair a base with a cached network to build this itself")


def test_the_operator_status_route_reports_no_explorer_when_the_operator_said_off(
        setting, monkeypatch, tmp_path):
    setting["monero_explorer_base"] = "off"
    body = _node_client(monkeypatch, tmp_path).get("/api/wallet/xmr/status").json()
    assert body["explorer_tx_base"] == ""


def test_the_user_wallet_status_route_carries_the_same_field(setting, monkeypatch):
    """A user's own wallet is a different screen reading a different route, and it is the one MOST
    people see — the node wallet is admin-only. It must not be the surface that got forgotten."""
    monkeypatch.setattr(user_router.user_wallets, "network", "mainnet", raising=False)
    monkeypatch.setattr(user_router.user_wallets, "enabled", lambda: True, raising=False)
    api = FastAPI()
    api.include_router(user_router.router, prefix="/api/wallet/xmr/me")
    api.dependency_overrides[user_router.get_member_wallet_user] = lambda: ADMIN
    api.dependency_overrides[auth.get_current_user] = lambda: ADMIN
    body = TestClient(api).get("/api/wallet/xmr/me/status").json()
    assert body["network"] == "mainnet"
    assert body["explorer_tx_base"] == MAINNET_DEFAULT
