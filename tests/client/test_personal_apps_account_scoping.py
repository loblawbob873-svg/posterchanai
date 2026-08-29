"""Personal-app caches and errors must remain scoped to the signed-in owner."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CAL = (ROOT / "static/js/client/calendar.js").read_text()
CONTACTS = (ROOT / "static/js/client/contacts.js").read_text()
NOTES = (ROOT / "static/js/client/notes.js").read_text()


def test_calendar_snapshot_and_offline_queue_are_owner_scoped():
    cache = CAL[CAL.index("const CalCache = {"):CAL.index("const CalQueue = {")]
    assert "which + ':' + me" in cache
    assert "st.get('snapshot')" not in cache and "st.put(q, 'queue')" not in cache
    assert "if(!key) return null" in cache and "if(!key)return []" in cache


def test_calendar_discards_a_previous_accounts_late_load():
    load = CAL[CAL.index("async function load(){"):CAL.index("// ---- rendering")]
    assert "gen = ++S.loadGen" in load and "mine !== owner()" in load
    assert load.count("if(stale()) return") >= 5


def test_notes_discards_an_old_accounts_late_decryption_and_refresh():
    load = NOTES[NOTES.index("async function load(force)"):NOTES.index("function _stamp()")]
    assert "who !== _owner" in load
    assert "mine !== _owner" in load
    assert "lib !== _lib" in load


def test_contacts_preserves_http_status_for_auth_and_retry_decisions():
    api = CONTACTS[CONTACTS.index("async function api(path, opts)"):CONTACTS.index("const jput")]
    assert "e.status = r.status" in api
