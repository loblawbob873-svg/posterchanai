"""ADMIN → MONERO: the form that configures a hot wallet, and the save that has to be durable.

`tests/test_monero_wallet_settings.py` covers the cold-start hydrate and the masked-password round
trip. This file covers the rest of that surface — the parts that fail silently rather than loudly:

  * **A save that did not reach the relay must not say "Settings updated".** Wallet settings are the
    one group deliberately taken off the fire-and-forget relay writer (`write_relay=False`) and put
    behind an awaited `write_through`, precisely so a failed write is an error. If that check ever
    stops raising, the admin types an RPC password, sees success, and the node comes back after a
    restart with the old credential (or none) — which is CLAUDE.md's "the relay is authoritative, so
    a silent write failure reverts", already paid for once with the bot config.
  * **The credential must not be persisted in clear.** The settings store writes local-only keys to
    a plain JSON file and everything else to a NIP-44-encrypted operator document. The wallet
    password lands in the encrypted half only because it is not local-only, which is a property of
    a key-classification helper nothing else in this feature touches.
  * **The form's defaults have to be the service's defaults.** The tab ships literal `value="…"`
    attributes, so the first Save on a fresh node writes them over whatever the code would have
    used. If the two drift, the wallet's caps and timeout change the moment an admin opens the tab
    and presses Save without editing anything.
  * **Mainnet is explicit.** The form offers both networks and labels mainnet as real hot-wallet funds.

The generic form contract (every named input is declared in `SettingsResponse`, `id` == `name`, the
pane is reachable from the nav) is already enforced for every tab by
`tests/test_admin_settings_coverage.py` and is deliberately not repeated here.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.routers import admin as admin_router
from app.schemas import SettingsResponse, SettingsUpdate
from app.services import settings_store
from app.services.monero_wallet_service import MoneroWallet, WalletConfig, WalletError

ROOT = Path(__file__).resolve().parents[1]
TAB = ROOT / "templates" / "admin" / "tabs" / "monero_wallet.html"

WALLET_KEYS = sorted(k for k in SettingsResponse.model_fields if k.startswith("monero_wallet_"))


class DB:
    """The admin route only ever hands its session to the settings store, which is stubbed here."""

    def execute(self, *args, **kwargs):
        return self

    def fetchall(self):
        return []

    def rollback(self):
        pass


@pytest.fixture
def store(monkeypatch):
    """An isolated settings cache — the real one is a process-wide dict shared with a live node."""
    original = dict(settings_store._CACHE)
    settings_store._CACHE.clear()
    yield settings_store._CACHE
    settings_store._CACHE.clear()
    settings_store._CACHE.update(original)


@pytest.fixture
def saved(monkeypatch):
    """Records what the awaited, durable relay write was asked to persist."""
    calls = []

    async def write_through(db, changes):
        calls.append(dict(changes))
        return len(changes)

    monkeypatch.setattr(settings_store, "write_through", write_through)
    return calls


def form_defaults() -> dict[str, str]:
    """The literal input values and first/selected option each wallet control ships."""
    out = {}
    for tag in re.findall(r"<input\b[^>]*>", TAB.read_text(encoding="utf-8")):
        name = re.search(r'\bname="([^"]+)"', tag)
        value = re.search(r'\bvalue="([^"]*)"', tag)
        if name and value:
            out[name.group(1)] = value.group(1)
    for body in re.findall(r'<select\b[^>]*name="([^"]+)"[^>]*>(.*?)</select>',
                           TAB.read_text(encoding="utf-8"), re.S):
        name, options = body
        selected = re.search(r'<option\b[^>]*value="([^"]*)"[^>]*selected[^>]*>', options)
        first = re.search(r'<option\b[^>]*value="([^"]*)"', options)
        if selected or first:
            out[name] = (selected or first).group(1)
    return out


# --------------------------------------------------------------------------- the form itself


def test_the_tab_carries_every_setting_the_wallet_reads():
    """A wallet key the form cannot set is a key only an environment variable can reach — which on
    a Docker or PosterChanOS node means it cannot be changed at all."""
    named = set(re.findall(r'\bname="(monero_wallet_[a-z_]+)"', TAB.read_text(encoding="utf-8")))
    assert named == set(WALLET_KEYS), f"missing from the tab: {sorted(set(WALLET_KEYS) - named)}"


def test_the_forms_defaults_are_the_services_defaults(store, monkeypatch):
    """The first Save on a fresh node writes these literals. If they drift from what
    `WalletConfig.from_env()` would have used, opening the tab and pressing Save silently changes
    the caps and the timeout the wallet has been running with."""
    for key in WALLET_KEYS:
        monkeypatch.delenv(key.upper().replace("MONERO_WALLET_", "MONERO_WALLET_"), raising=False)
    for var in ("MONERO_WALLET_ENABLED", "MONERO_WALLET_RPC_URL", "MONERO_WALLET_RPC_USER",
                "MONERO_WALLET_RPC_PASSWORD", "MONERO_WALLET_NETWORK",
                "MONERO_WALLET_TRANSFER_CAP_XMR", "MONERO_WALLET_DAILY_CAP_XMR",
                "MONERO_WALLET_RPC_TIMEOUT", "MONERO_WALLET_SPEND_LEDGER"):
        monkeypatch.delenv(var, raising=False)

    code = WalletConfig.from_env()
    form = form_defaults()
    assert form["monero_wallet_rpc_url"] == code.url
    assert form["monero_wallet_network"] == code.network == "stagenet"
    assert form["monero_wallet_spend_ledger"] == code.spend_ledger_path
    assert float(form["monero_wallet_rpc_timeout"]) == code.timeout_seconds
    assert WalletConfig(**{**code.__dict__, **{
        "transfer_cap_atomic": int(float(form["monero_wallet_transfer_cap_xmr"]) * 1e12),
        "daily_cap_atomic": int(float(form["monero_wallet_daily_cap_xmr"]) * 1e12),
    }}).transfer_cap_atomic == code.transfer_cap_atomic


def test_the_network_field_explicitly_offers_stagenet_and_mainnet():
    """Mainnet is never an implicit typo: it is an explicit select choice with a real-funds warning."""
    tab = TAB.read_text(encoding="utf-8")
    assert re.search(r'<select id="monero_wallet_network"[^>]*>', tab)
    assert '<option value="stagenet">' in tab
    assert '<option value="mainnet">' in tab
    assert "MAINNET is a hot wallet" in tab


def test_the_tab_states_the_risk_before_the_first_field():
    """The help text is the only place an operator is told this is a hot wallet on a server. It is
    not decoration and it has been deleted from screens before."""
    tab = TAB.read_text(encoding="utf-8")
    assert "stagenet" in tab.lower() and "mainnet" in tab.lower()
    assert "loopback" in tab.lower() and "RFC1918" in tab
    assert re.search(r"seeds? and spend keys never enter", tab, re.I)


# --------------------------------------------------------------------------- saving


def test_a_wallet_save_is_durable_before_it_reports_success(store, saved):
    """Wallet keys skip the fire-and-forget relay writer; the awaited write is what Save reports on."""
    result = admin_router.update_settings(
        SettingsUpdate(settings={"monero_wallet_enabled": "true",
                                 "monero_wallet_rpc_user": "posterchan"}), DB(), object())
    assert result == {"message": "Settings updated"}
    assert saved == [{"monero_wallet_enabled": "true", "monero_wallet_rpc_user": "posterchan"}]


def test_a_relay_write_that_did_not_land_fails_the_save_loudly(store, monkeypatch):
    """THE ONE THAT MATTERS. A partial or refused write must be a 503, not a green banner over a
    credential that will be gone after the next restart — the settings store's authoritative copy
    is the relay document, and the in-memory cache that Save just updated does not survive one."""
    async def half_written(db, changes):
        return len(changes) - 1

    monkeypatch.setattr(settings_store, "write_through", half_written)
    with pytest.raises(HTTPException) as caught:
        admin_router.update_settings(
            SettingsUpdate(settings={"monero_wallet_rpc_password": "new-secret",
                                     "monero_wallet_enabled": "true"}), DB(), object())
    assert caught.value.status_code == 503
    assert "durably" in caught.value.detail.lower()
    assert "new-secret" not in caught.value.detail


def test_a_relay_that_refused_everything_also_fails_the_save(store, monkeypatch):
    async def refused(db, changes):
        return 0

    monkeypatch.setattr(settings_store, "write_through", refused)
    with pytest.raises(HTTPException) as caught:
        admin_router.update_settings(
            SettingsUpdate(settings={"monero_wallet_rpc_user": "posterchan"}), DB(), object())
    assert caught.value.status_code == 503


def test_wallet_keys_never_go_out_through_the_best_effort_relay_writer(store, saved, monkeypatch):
    """`put(..., write_relay=False)` for wallet keys is what makes the awaited write the ONLY path.
    Leaving them on the background writer would mean the durable check ran beside a second,
    unchecked write of the same credential — and success would again mean nothing."""
    scheduled = []
    monkeypatch.setattr(settings_store, "_schedule_relay_write", lambda changes: scheduled.append(dict(changes)))

    admin_router.update_settings(
        SettingsUpdate(settings={"monero_wallet_rpc_password": "s3cret", "site_name": "Poster"}),
        DB(), object())

    flattened = {key for call in scheduled for key in call}
    assert not any(key.startswith("monero_wallet_") for key in flattened), (
        f"a wallet key was queued on the background relay writer: {sorted(flattened)}")
    assert "site_name" in flattened, "non-wallet settings must still use the background writer"


def test_an_unchanged_save_writes_nothing_at_all(store, saved):
    """The admin form posts every field on every Save. Re-writing an unchanged credential would
    republish the encrypted settings document — and, on a node whose relay is briefly unreachable,
    turn a no-op Save into a 503."""
    store.update({"monero_wallet_enabled": "true", "monero_wallet_rpc_user": "posterchan"})
    result = admin_router.update_settings(
        SettingsUpdate(settings={"monero_wallet_enabled": "true",
                                 "monero_wallet_rpc_user": "posterchan"}), DB(), object())
    assert result == {"message": "Settings updated"}
    assert saved == [], "an unchanged Save republished the wallet settings"


@pytest.mark.parametrize("posted", ["", "********"])
def test_the_password_field_left_alone_keeps_the_stored_credential(store, saved, posted):
    """GET masks the password, so the form posts back either the mask or a blank. Both mean "I did
    not change it". Treating either as a value clears the credential on the next ordinary Save of
    any other field on the tab."""
    store["monero_wallet_rpc_password"] = "real-secret"
    admin_router.update_settings(
        SettingsUpdate(settings={"monero_wallet_rpc_password": posted,
                                 "monero_wallet_enabled": "true"}), DB(), object())
    assert settings_store.get("monero_wallet_rpc_password") == "real-secret"
    assert all("monero_wallet_rpc_password" not in call for call in saved)


def test_the_password_is_not_a_clearable_text_key_even_if_the_mask_check_is_bypassed():
    """Second, independent guard: the key is excluded from the settings form's clearable text keys,
    so an empty value can never be persisted for it by any path through the save loop."""
    assert "monero_wallet_rpc_password" not in admin_router._settings_text_keys()
    for key in WALLET_KEYS:
        if key != "monero_wallet_rpc_password":
            assert key in admin_router._settings_text_keys(), (
                f"{key} is not clearable, so an admin cannot empty it from the form")


def test_the_rpc_password_is_never_written_to_the_plaintext_settings_file(store, saved, tmp_path,
                                                                         monkeypatch):
    """Local-only keys are persisted to a plain JSON file on disk; everything else lives in the
    NIP-44-encrypted operator document. The wallet credential must be in the encrypted half — and
    that is decided by a key-classification helper that knows nothing about this feature."""
    local_file = tmp_path / "settings.local.json"
    monkeypatch.setattr(settings_store, "_LOCAL_PATH", str(local_file))
    admin_router.update_settings(
        SettingsUpdate(settings={"monero_wallet_rpc_password": "plaintext-would-be-a-bug"}),
        DB(), object())
    assert not settings_store._is_local_only("monero_wallet_rpc_password")
    if local_file.exists():
        assert "plaintext-would-be-a-bug" not in local_file.read_text()


def test_a_saved_password_is_masked_on_the_next_read_and_the_real_one_stays_put(store, saved,
                                                                               monkeypatch):
    """The full round trip an admin performs: type a credential, save, reload the page. What comes
    back must be the mask, and what the wallet uses must be the real value."""
    admin_router.update_settings(
        SettingsUpdate(settings={"monero_wallet_rpc_password": "typed-in-the-form"}), DB(), object())
    monkeypatch.setattr("app.database.safe_query_settings",
                        lambda db: {"monero_wallet_rpc_password": settings_store.get(
                            "monero_wallet_rpc_password", "")})
    assert admin_router.get_settings(DB(), object()).monero_wallet_rpc_password == "********"
    assert settings_store.get("monero_wallet_rpc_password") == "typed-in-the-form"


# --------------------------------------------------------------------------- form → live wallet


def test_what_the_admin_saved_is_what_the_wallet_runs_on(store, saved, monkeypatch, tmp_path):
    """The end of the chain. Settings beat the environment, so a node whose unit file still carries
    an old `MONERO_WALLET_*` value must follow the admin panel, not the unit file — otherwise the
    tab appears to do nothing and there is nothing on screen to say why."""
    monkeypatch.setenv("MONERO_WALLET_RPC_USER", "stale-from-the-unit-file")
    monkeypatch.setenv("MONERO_WALLET_DAILY_CAP_XMR", "99")
    ledger = str(tmp_path / "spend.sqlite3")
    admin_router.update_settings(SettingsUpdate(settings={
        "monero_wallet_enabled": "true",
        "monero_wallet_rpc_url": "http://127.0.0.1:38083/json_rpc",
        "monero_wallet_rpc_user": "posterchan",
        "monero_wallet_rpc_password": "from-the-panel",
        "monero_wallet_network": "stagenet",
        "monero_wallet_transfer_cap_xmr": "0.02",
        "monero_wallet_daily_cap_xmr": "0.05",
        "monero_wallet_rpc_timeout": "9",
        "monero_wallet_spend_ledger": ledger,
    }), DB(), object())

    wallet = MoneroWallet()
    assert wallet.config.username == "posterchan"
    assert wallet.config.password == "from-the-panel"
    assert wallet.config.transfer_cap_atomic == 20_000_000_000
    assert wallet.config.daily_cap_atomic == 50_000_000_000
    assert wallet.config.timeout_seconds == 9
    assert wallet.config.spend_ledger_path == ledger


def test_turning_the_switch_off_in_the_panel_takes_the_wallet_down(store, saved, monkeypatch):
    """The kill switch. It is the operator's way to stop a wallet that is misbehaving, so it has to
    beat a `MONERO_WALLET_ENABLED=1` still sitting in the environment."""
    monkeypatch.setenv("MONERO_WALLET_ENABLED", "1")
    monkeypatch.setenv("MONERO_WALLET_RPC_USER", "posterchan")
    monkeypatch.setenv("MONERO_WALLET_RPC_PASSWORD", "secret")
    admin_router.update_settings(SettingsUpdate(settings={"monero_wallet_enabled": "false"}),
                                 DB(), object())
    with pytest.raises(WalletError, match="disabled"):
        MoneroWallet()


@pytest.mark.parametrize("bad,key", [
    ("0", "monero_wallet_transfer_cap_xmr"),
    ("-1", "monero_wallet_daily_cap_xmr"),
    ("abc", "monero_wallet_transfer_cap_xmr"),
    ("0.0000000000001", "monero_wallet_daily_cap_xmr"),
])
def test_a_cap_the_service_cannot_use_takes_the_wallet_offline_rather_than_crashing_it(
        store, saved, monkeypatch, bad, key):
    """The form does not validate the caps, so an unusable value IS storable. What must not happen
    is an unhandled exception on every wallet request: it has to surface as the wallet being
    unavailable, which is the state the client already knows how to draw."""
    monkeypatch.setenv("MONERO_WALLET_ENABLED", "1")
    monkeypatch.setenv("MONERO_WALLET_RPC_USER", "posterchan")
    monkeypatch.setenv("MONERO_WALLET_RPC_PASSWORD", "secret")
    admin_router.update_settings(SettingsUpdate(settings={key: bad}), DB(), object())
    with pytest.raises(WalletError):
        MoneroWallet()


def test_no_wallet_setting_is_readable_without_an_admin_session():
    """`/client/config` is the unauthenticated bootstrap the browser reads before login. A wallet
    key appearing there would publish the operator's RPC credential to every visitor."""
    client_source = (ROOT / "app" / "routers" / "client.py").read_text(encoding="utf-8")
    for key in WALLET_KEYS:
        assert key not in client_source, f"{key} is reachable from an unauthenticated route"
    assert "get_admin_user" in (ROOT / "app" / "routers" / "admin.py").read_text(
        encoding="utf-8")[:40000]
