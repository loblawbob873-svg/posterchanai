"""VM-hosting admin settings persist to Nostr and come back — through the SHIPPED save route and the
SHIPPED hydrate, with real NIP-44 encryption against a fake relay event table.

Three tables must agree or a setting silently does nothing: the service's `config.DEFAULTS`, the
typed `SettingsResponse` (an undeclared key never hydrates, and a checkbox then saves `false` over the
stored value), and the admin form (`templates/admin/tabs/vmhost.html`). Then the round trip itself:
Save → process restart (cache CLEARED) → hydrate from the relay rows the save wrote → the service
reads the same configuration. And the access lists — who may reach the host — must go through the
DURABLE write path: a fire-and-forget write that is lost reads back as the OLD list after a restart,
which for a revocation means somebody quietly regaining access.
"""
import asyncio
import json
import re
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.routers import admin as admin_router
from app.schemas import SettingsResponse, SettingsUpdate
from app.services import settings_store
from app.services.nostr import bip340, nip44, nostr_service
from app.services.vmhost import config as vmconfig

ROOT = Path(__file__).resolve().parents[1]
TAB = ROOT / "templates" / "admin" / "tabs" / "vmhost.html"
OP_SK = bytes.fromhex("42" * 32)


def _inputs():
    html = TAB.read_text(encoding="utf-8")
    out = {}
    for tag in re.findall(r"<(?:input|select|textarea)\b[^>]*>", html):
        name = re.search(r'\bname="([^"]+)"', tag)
        if name:
            out[name.group(1)] = tag
    return out, html


def test_every_vmhost_setting_has_a_form_field_and_vice_versa():
    schema = {k for k in SettingsResponse.model_fields if k.startswith("vmhost_")}
    form = {k for k in _inputs()[0] if k.startswith("vmhost_")}
    assert schema == set(vmconfig.DEFAULTS), "SettingsResponse and config.DEFAULTS disagree"
    assert form == schema, f"form-only {sorted(form - schema)}, schema-only {sorted(schema - form)}"
    assert len(schema) >= 25, "the vmhost key set came through short — the parse is wrong"
    for k, tag in _inputs()[0].items():
        assert re.search(r'\bid="%s"' % re.escape(k), tag), f"{k}: id must equal name"


def test_form_defaults_match_the_service_defaults():
    inputs, html = _inputs()
    for key, default in vmconfig.DEFAULTS.items():
        assert SettingsResponse.model_fields[key].default == default, key
        tag = inputs[key]
        if 'type="checkbox"' in tag:
            assert default in ("true", "false"), key
            continue
        if tag.startswith("<select"):
            sel = re.search(r'<select[^>]*name="%s"[^>]*>(.*?)</select>' % key, html, re.S).group(1)
            assert re.findall(r'value="([^"]*)"', sel)[0] == default, key
            continue
        v = re.search(r'\bvalue="([^"]*)"', tag)
        if v:
            assert v.group(1) == default, f"{key}: form shows {v.group(1)!r}, service default {default!r}"
    assert vmconfig.VmHostConfig.from_settings({}) == vmconfig.VmHostConfig.from_settings(dict(vmconfig.DEFAULTS))
    blank = vmconfig.VmHostConfig.from_settings({"vmhost_announce": "", "vmhost_iso_fetch_enabled": ""})
    assert blank.announce is True, "a BLANK stored value must not switch announcing off"


def test_the_tab_is_in_the_system_nav():
    admin_html = (ROOT / "templates" / "admin.html").read_text()
    assert 'data-tab="vms"' in admin_html and "admin/tabs/vmhost.html" in admin_html
    assert 'id="tab-vms"' in TAB.read_text()


class FakeRelay:
    """The relay's event table: d-tag → NIP-44 ciphertext, written by the REAL write_through."""

    def __init__(self):
        self.docs = {}
        self.durable = []

    async def put_doc(self, port, seckey, d_tag, data, *, encrypt=True, **kw):
        self.docs[d_tag] = nip44.encrypt_self(seckey, json.dumps(data))
        return True

    def rows(self):
        class DB:
            def __init__(s, rows): s._rows = rows
            def execute(s, *a, **k): return s
            def fetchall(s): return s._rows
            def rollback(s): pass
        return DB([(d, c) for d, c in self.docs.items() if d.startswith("pcai:setting:")])


VALUES = {
    "vmhost_enabled": "true",
    "vmhost_display_name": "Basement box",
    "vmhost_libvirt_uri": "qemu+ssh://nobody@host/system",
    "vmhost_storage_dir": "/srv/vms",
    "vmhost_public_url": "https://vm.example",
    "vmhost_admin_npubs": nostr_service.npub_of("aa" * 32),
    "vmhost_allowed_npubs": nostr_service.npub_of("bb" * 32) + "\n" + "cc" * 32,
    "vmhost_max_vcpus_per_vm": "8",
    "vmhost_reserve_ram_mib": "4096",
    "vmhost_allow_overcommit": "true",
    "vmhost_announce": "false",
    "vmhost_console_ticket_ttl_sec": "45",
}


def _save(monkeypatch, relay, values, short=False):
    monkeypatch.setattr(settings_store, "_OP_SK", OP_SK)
    monkeypatch.setattr(settings_store, "_operator_seckey", lambda db: OP_SK)
    monkeypatch.setattr(settings_store.store, "put_doc", relay.put_doc)
    # tests/test_users_reconcile_seed.py replaces settings_store._port and never puts it back; pin our
    # own so this test does not depend on which tests ran before it.
    monkeypatch.setattr(settings_store, "_port", lambda db=None: 3052)
    real_wt = settings_store.write_through

    async def durable(db, changes):
        relay.durable.append(dict(changes))
        n = await real_wt(db, changes)
        return n - 1 if short else n
    monkeypatch.setattr(settings_store, "write_through", durable)
    background = []

    def fire_and_forget(changes):
        background.append(dict(changes))
        asyncio.run(real_wt(None, changes))
    monkeypatch.setattr(settings_store, "_schedule_relay_write", fire_and_forget)
    reloads = []
    from app.services.vmhost import transport
    monkeypatch.setattr(transport, "request_reload", lambda: reloads.append(1) or True)
    upstream = []
    import app.services.nostr_relay.thread as relay_thread
    monkeypatch.setattr(relay_thread, "trigger_upstream_reload", lambda: upstream.append(1))

    class DB:
        def rollback(self): pass
    result = admin_router.update_settings(SettingsUpdate(settings=values), DB(), object())
    return result, background, reloads, upstream


def test_save_then_restart_then_hydrate_gives_the_same_host_configuration(monkeypatch):
    relay = FakeRelay()
    result, background, reloads, upstream = _save(monkeypatch, relay, VALUES)
    assert result == {"message": "Settings updated"}
    before = vmconfig.current()
    assert before.enabled and before.max_vcpus == 8 and before.allowed_pubkeys == ["bb" * 32, "cc" * 32]

    # The process restarts: nothing survives in memory.
    settings_store._CACHE.clear()
    assert vmconfig.current().enabled is False
    n = settings_store.hydrate_from_db(relay.rows())
    assert n >= len(VALUES)
    after = vmconfig.current()
    assert after == before
    for k, v in VALUES.items():
        assert settings_store.get(k) == v, k

    assert reloads, "a vmhost_* save must restart the host service with the new settings"
    assert upstream, "turning the host on must reach the relay's firehose (reload-upstream)"


def test_access_lists_and_the_switch_go_through_the_durable_write(monkeypatch):
    relay = FakeRelay()
    _, background, _, _ = _save(monkeypatch, relay, VALUES)
    durable_keys = set().union(*relay.durable) if relay.durable else set()
    background_keys = set().union(*background) if background else set()
    for k in vmconfig.DURABLE_KEYS:
        assert k in durable_keys, f"{k} was not written durably"
        assert k not in background_keys, f"{k} went through the fire-and-forget writer"
    assert "vmhost_display_name" in background_keys, "ordinary keys keep the ordinary path"


def test_a_short_durable_write_fails_the_save(monkeypatch):
    relay = FakeRelay()
    with pytest.raises(HTTPException) as e:
        _save(monkeypatch, relay, {"vmhost_allowed_npubs": "dd" * 32, "vmhost_enabled": "true"}, short=True)
    assert e.value.status_code == 503


def test_the_ciphertext_is_really_encrypted_to_the_operator():
    relay = FakeRelay()
    asyncio.run(relay.put_doc(0, OP_SK, "pcai:setting:vmhost_allowed_npubs", {"value": "secret-list"}))
    ct = relay.docs["pcai:setting:vmhost_allowed_npubs"]
    assert "secret-list" not in ct
    assert json.loads(nip44.decrypt_self(OP_SK, ct))["value"] == "secret-list"
    assert bip340.pubkey_from_seckey(OP_SK)
