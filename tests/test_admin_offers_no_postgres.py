"""Admin never offers PostgreSQL (#161). Reported: "we should not support postgres any more, why do I see it in the
admin settings!" -- Admin → Nostr Relay → PosterChanDB had a Mode dropdown whose options were "Off — PostgreSQL
only", "Shadow — PostgreSQL + mirror" and "Serve — PostgreSQL + mirror", on nodes that have run PosterChanDB alone
since the cutover. Picking one could only ever take the relay back to a database the install does not have.

Three halves, each checked against what ships: the page (no Postgres word, no mode control), the settings API (the
fields are gone, so GET can no longer hand out the relay DSN and PUT cannot store a mode), and the relay (a mode
left stored by the old dropdown is NOT read -- otherwise deleting the control would leave the old choice live)."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TAB = ROOT / "templates/admin/tabs/nostr_relay.html"


def test_the_relay_tab_never_says_postgres():
    html = TAB.read_text(encoding="utf-8")
    assert not re.search(r"postgres", html, re.I), re.findall(r".{40}postgres.{40}", html, re.I | re.S)[:3]
    assert 'name="posterchandb_mode"' not in html and 'id="posterchandb_mode"' not in html


def test_no_admin_tab_offers_postgres():
    hits = [f.name for f in (ROOT / "templates/admin").rglob("*.html") if re.search(r"postgres", f.read_text(), re.I)]
    assert not hits, hits


def test_the_settings_api_has_no_store_mode_or_dsn():
    from app.schemas import SettingsResponse
    fields = set(SettingsResponse.model_fields)
    assert "posterchandb_mode" not in fields
    assert "nostr_relay_pg_dsn" not in fields, "GET /api/admin/settings would hand out the relay DB password"


def test_a_mode_stored_by_the_old_dropdown_is_ignored(monkeypatch, tmp_path):
    from app.services import settings_store
    from app.services.nostr_relay import thread
    for k in ("DATABASE_URL", "NOSTR_RELAY_PG_DSN", "POSTERCHANDB_MODE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("POSTERCHANDB_DIR", str(tmp_path / "pcdb"))
    real_get = settings_store.get
    monkeypatch.setattr(settings_store, "get",
                        lambda k, d=None: "off" if k == "posterchandb_mode"
                        else None if k == "nostr_relay_pg_dsn" else real_get(k, d))
    assert thread._pcdb_mode() == "primary"
    assert thread._read_config()["pcdb_mode"] == "primary"
