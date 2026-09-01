import json
from unittest import mock

from app.routers import admin as admin_router
from app.schemas import SettingsUpdate
from app.services import settings_store
from app.services.monero_wallet_service import MoneroWallet, WalletConfig


class DB:
    def __init__(self, rows=()): self.rows = rows
    def execute(self, *args, **kwargs): return self
    def fetchall(self): return self.rows
    def rollback(self): pass


def test_encrypted_cold_start_hydrate_drives_wallet_config(monkeypatch, tmp_path):
    values = {
        "monero_wallet_enabled": "true",
        "monero_wallet_rpc_url": "http://127.0.0.1:38083/json_rpc",
        "monero_wallet_rpc_user": "posterchan",
        "monero_wallet_rpc_password": "hydrated-secret",
        "monero_wallet_network": "stagenet",
        "monero_wallet_spend_ledger": str(tmp_path / "ledger.sqlite3"),
    }
    rows = [("pcai:setting:" + key, "cipher:" + key) for key in values]
    old_cache = dict(settings_store._CACHE)
    try:
        settings_store._CACHE.clear()  # process restart: no in-memory values survive
        monkeypatch.setattr(settings_store, "_operator_seckey", lambda db: "operator-secret")
        monkeypatch.setattr("app.services.nostr.nostr_service.derive_pubkey", lambda key: "ab" * 32)
        monkeypatch.setattr(
            "app.services.nostr.nip44.decrypt_self",
            lambda key, ciphertext: json.dumps({"value": values[ciphertext.removeprefix("cipher:")]}),
        )
        assert settings_store.hydrate_from_db(DB(rows)) == len(values)
        cfg = WalletConfig.from_env()
        wallet = MoneroWallet(cfg)
        assert wallet.config.password == "hydrated-secret"
        assert wallet.config.url == "http://127.0.0.1:38083/json_rpc"
        assert wallet.config.network == "stagenet"
    finally:
        settings_store._CACHE.clear()
        settings_store._CACHE.update(old_cache)


def test_admin_mask_round_trip_preserves_secret_and_flushes_before_success(monkeypatch):
    old_cache = dict(settings_store._CACHE)
    writes = []
    try:
        settings_store._CACHE.update({"monero_wallet_rpc_password": "real-secret",
                                      "monero_wallet_enabled": "false"})

        async def durable(db, changes):
            writes.append(dict(changes))
            return len(changes)

        monkeypatch.setattr(settings_store, "write_through", durable)
        result = admin_router.update_settings(
            SettingsUpdate(settings={"monero_wallet_rpc_password": "********",
                                     "monero_wallet_enabled": "true"}),
            DB(), object(),
        )
        assert result == {"message": "Settings updated"}
        assert settings_store.get("monero_wallet_rpc_password") == "real-secret"
        assert writes == [{"monero_wallet_enabled": "true"}]
    finally:
        settings_store._CACHE.clear()
        settings_store._CACHE.update(old_cache)


def test_admin_read_masks_password_and_public_config_has_no_wallet_secret(monkeypatch):
    monkeypatch.setattr("app.database.safe_query_settings", lambda db: {
        "monero_wallet_rpc_password": "real-secret", "monero_wallet_enabled": "true",
    })
    response = admin_router.get_settings(DB(), object())
    assert response.monero_wallet_rpc_password == "********"
    client_source = open("app/routers/client.py", encoding="utf-8").read()
    assert "monero_wallet_rpc_password" not in client_source


def test_node_status_admin_ui_and_route_do_not_render_secrets():
    from app.routers.monero_wallet import router
    route = next(item for item in router.routes if item.path.endswith("/node-status"))
    dependencies = repr(route.dependant.dependencies)
    assert "get_admin_user" in dependencies
    ui = open("templates/admin/tabs/monero_wallet.html", encoding="utf-8").read()
    assert "Check Node Status" in ui and "/api/wallet/xmr/node-status" in ui
    status_block = ui[ui.index("/api/wallet/xmr/node-status"):]
    assert "rpc_password" not in status_block and "tx_hash" not in status_block
