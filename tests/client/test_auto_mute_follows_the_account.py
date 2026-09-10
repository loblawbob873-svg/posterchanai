"""THE AUTO-MUTE SWITCH HAD NO MEMORY THAT REACHED PAST ONE BROWSER PROFILE.

Reported as "auto-mute people who mute you — why is it disabled again? I thought we fixed this!",
and it had never been fixed, because there was nothing to fix in the code that ran: the only copy of
the preference was `localStorage['pc_auto_mute:<pubkey>']`. localStorage is per ORIGIN, and this
client ships on three of them — the web app, the APK, and the desktop build, which serves the bundle
from `app://posterchan`. So turning the switch on in one left it off in the other two permanently,
and clearing site data turned it off everywhere at once. Nothing was regressing. The switch simply
did not follow the account.

`enabled` and `ignored` are a PERSON'S DECISION, so they go where every other private decision in
this client goes: an encrypted kind-30078 document, `d=pcai:automute`, NIP-44-sealed to the user's
own key, like `pcai:desktop` and `pcai:budget`. `records` — who has muted you — deliberately stays
local: it is a cache rebuilt from public mute lists on every refresh, it can run to thousands of
entries, and NIP-44 is a single 64 KiB envelope.

Each of these is a rule some private document in this codebase has already broken at least once.
"""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
APP = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")
STORE = (ROOT / "static/js/client/store.js").read_text(encoding="utf-8")


def _fn(header):
    """The body of a function in app.js, to its closing brace at the same indent."""
    i = APP.index(header)
    indent = header[: len(header) - len(header.lstrip(" "))]
    return APP[i : APP.index("\n" + indent + "}", i)]


def test_the_preference_is_an_encrypted_document_on_the_relays():
    assert "const AUTO_MUTE_D = 'pcai:automute';" in APP
    load = _fn("  async function _autoMuteDocLoad()")
    assert "kinds:[30078]" in load and "'#d':[AUTO_MUTE_D]" in load
    assert "signer.nip44dec(owner" in load, "the document must be sealed to the user's own key"
    save = _fn("  async function _autoMuteDocSave()")
    assert "signer.nip44enc(owner" in save
    assert "publish(30078, ct, [['d', AUTO_MUTE_D]]" in save


def test_only_the_decisions_travel_and_not_the_cache():
    """`records` is thousands of entries rebuilt from public lists on every refresh, and NIP-44 is
    one 64 KiB envelope. Putting it in the document would make the document fail to encrypt — i.e.
    would break the preference it was added to carry."""
    save = _fn("  async function _autoMuteDocSave()")
    assert "enabled:" in save and "ignored:" in save
    assert "records" not in save, "the cache must not ride in the document"


def test_nothing_is_published_before_a_relay_has_answered():
    """A 30078 is REPLACED whole. Publishing on the strength of an unanswered read replaces a real
    preference with this device's default — the replaceable-doc wipe, which this codebase has paid
    for with mute lists, follow lists and a drive index."""
    load = _fn("  async function _autoMuteDocLoad()")
    catch = load.index("catch(_){ return null; }")
    read = load.index("_autoMuteDocRead = true")
    assert catch < read, "a failed query must not count as a read"
    save = _fn("  async function _autoMuteDocSave()")
    assert "_autoMuteDocReady()" in save, "the save does not check that a read happened"


def test_the_local_copy_is_never_believed_over_the_account():
    """The early return read the LOCAL copy, so a device that had never had the switch on reported
    "off" and never looked any further. That is the bug, exactly."""
    fn = _fn("  async function _syncAutoMutes()")
    load = fn.index("_autoMuteDocLoad()")
    early = fn.index("!_autoMuteStored().enabled")
    assert load < early, "the device's copy still decides whether to ask the account"


def test_the_document_is_registered_in_both_places():
    """Every private document here has missed one of these at least once, and the symptom is the
    DEFAULT drawing — which is indistinguishable from never having set the preference."""
    assert "t[1] === 'pcai:automute'" in STORE, "the client cache will evict it (store.js _isPinned)"
    carry = APP[APP.index("const _CARRY_D = ["):][:1200]
    assert "/^pcai:automute$/" in carry, "a relay change would leave it behind (_CARRY_D)"


def test_turning_the_switch_on_publishes_it():
    handler = APP[APP.index("if(toggle) toggle.onchange="):][:1800]
    assert "_autoMuteDocSave()" in handler
    assert "_autoMuteDocLoad()" in handler, "it may publish before ever having read"


def test_the_engine_publishes_its_own_changes_too():
    """The engine maintains `ignored` — an account that stops muting you is forgotten — so its
    writes are decisions as well."""
    write = re.search(r"write:state=>\{[^\n]*\n?[^\n]*", APP).group(0)
    assert "_autoMuteDocSave()" in write, write
