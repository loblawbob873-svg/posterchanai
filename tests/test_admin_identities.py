"""Admin → Relay → Identities is a readable list, like Blocked accounts.

Run: venv-unified/bin/python -m pytest tests/test_admin_identities.py

The NIP-05 registry was only a textarea of "name hex" lines. The list now shows, for every granted
name, the owner's picture and profile name from this relay, the full address, and whether their own
profile publishes that address exactly -- the test that grants access to this node's features, and
the one a mixed-case "JonnyFever" silently failed in lowercase clients. Remove takes one name off,
live, and keeps the text box (what Save sends) in step so the next Save cannot put it back.
"""
import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from app.services import nip05_registry, relay_blocklist, settings_store
from app.services.nostr import bip340, nostr_service

ROOT = Path(__file__).resolve().parents[1]
PK = {n: bip340.pubkey_from_seckey(bytes([i + 1]) * 32).hex() for i, n in enumerate(("alice", "fever", "ghost", "root"))}


@pytest.fixture
def registry(monkeypatch):
    vals = {nip05_registry.KEY: "\n".join([
        "# the node's own names",
        f"alice {nostr_service.npub_of(PK['alice'])}",
        f"jonnyfever {PK['fever']}",
        f"ghost {PK['ghost']}",
        f"_ {PK['root']}",
    ])}
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: vals.get(k, d))
    monkeypatch.setattr(settings_store, "put", lambda k, v, **kw: vals.__setitem__(k, v))
    monkeypatch.setattr(settings_store, "is_hydrated", lambda: True)
    reloads = []
    import app.services.nostr_relay.thread as thread
    monkeypatch.setattr(thread, "trigger_nip05_reload", lambda: reloads.append(1) or {})

    async def profiles(pks):
        return {PK["alice"]: {"name": "Alice", "picture": "https://x/a.png", "nip05": "ALICE@poster.place"},
                PK["fever"]: {"name": "JonnyFever", "picture": "", "nip05": "someoneelse@poster.place"},
                PK["root"]: {"name": "Poster", "picture": "", "nip05": "poster.place"}}, True
    monkeypatch.setattr(relay_blocklist, "profiles", profiles)
    return vals, reloads


def _by_name(out):
    return {r["name"]: r for r in out["identities"]}


def test_every_identity_is_listed_with_its_owner_and_whether_it_verifies(registry):
    out = asyncio.run(nip05_registry.rows("poster.place"))
    rows = _by_name(out)
    assert set(rows) == {"alice", "jonnyfever", "ghost", "_"}, "an identity went missing from the list"
    a = rows["alice"]
    assert a["address"] == "alice@poster.place" and a["display"] == "Alice" and a["npub"].startswith("npub1")
    assert a["verified"], "a profile publishing the address in another case must verify"
    assert not rows["jonnyfever"]["verified"] and rows["jonnyfever"]["profile_nip05"] == "someoneelse@poster.place"
    assert rows["ghost"]["display"] == "" and not rows["ghost"]["verified"], "no profile here is still listed"
    assert rows["_"]["verified"], "the root identity published as just the domain must verify"


def test_the_admin_endpoint_uses_the_nodes_domain(registry, monkeypatch):
    from app.routers import admin, client
    monkeypatch.setattr(client, "_nip05_domain", lambda request, db: "poster.place")
    out = asyncio.run(admin.relay_identities(request=None, db=None, admin=None))
    assert out["domain"] == "poster.place" and len(out["identities"]) == 4


def test_remove_takes_exactly_that_name_off_and_applies_it_live(registry):
    vals, reloads = registry
    r = nip05_registry.remove("JonnyFever")                   # case-insensitive, like NIP-05 names
    assert r["ok"] and r["removed"] == 1 and reloads == [1]
    text = vals[nip05_registry.KEY]
    assert "jonnyfever" not in text and "alice" in text and "ghost" in text and "_ " in text
    assert "# the node's own names" in text, "a comment line was dropped"
    assert r["value"] == text, "the UI needs the new text to keep Save in step"


def test_remove_refuses_rather_than_wiping_everyone(registry, monkeypatch):
    vals, _ = registry
    before = vals[nip05_registry.KEY]
    monkeypatch.setattr(settings_store, "is_hydrated", lambda: False)
    assert not nip05_registry.remove("alice")["ok"]
    monkeypatch.setattr(settings_store, "is_hydrated", lambda: True)
    assert not nip05_registry.remove("nobody")["ok"]
    assert vals[nip05_registry.KEY] == before


def test_the_remove_endpoint_reports_a_refusal(registry):
    from fastapi import HTTPException
    from app.routers import admin
    with pytest.raises(HTTPException) as e:
        asyncio.run(admin.relay_identity_remove(admin.RelayIdentityRemoveReq(name="nobody"), db=None, admin=None))
    assert e.value.status_code == 400


# ---- the page -------------------------------------------------------------------------------------

def _node(js):
    r = subprocess.run(["node", "-e", js, str(ROOT / "static/js/admin-identities.js")],
                       capture_output=True, text=True, timeout=20)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_the_list_finds_what_a_person_would_type_and_is_in_name_order():
    out = _node(r"""
const { matches, sortRows } = require(process.argv[1]);
const row = { name: 'jonnyfever', address: 'jonnyfever@poster.place', display: 'JonnyFever',
              profile_nip05: 'jonnyfever@poster.place', npub: 'npub1ggw9h0d9', pubkey: '421c5bbd' };
const q = ['fever', 'JONNY', 'poster.place', 'npub1ggw9', '421c5b', '', 'alice'].map(x => matches(row, x));
const order = sortRows([{name:'b', verified:true}, {name:'a', verified:true}, {name:'z', verified:false}]).map(r => r.name);
process.stdout.write(JSON.stringify({q, order}));
""")
    assert out["q"] == [True, True, True, True, True, True, False]
    # Every row is a member, so nothing is pushed to the top for what the person's profile says.
    assert out["order"] == ["a", "b", "z"], out


def test_a_row_escapes_what_strangers_wrote():
    """A profile name is written by whoever owns the key -- it must not become HTML in the admin page."""
    out = _node(r"""
const { rowHtml } = require(process.argv[1]);
const html = rowHtml({ name: 'x', address: 'x@poster.place', display: '<img src=x onerror=alert(1)>',
                       picture: '" onerror="alert(1)', npub: 'npub1abc', verified: false, profile_nip05: '' });
const ok = rowHtml({ name: 'y', address: 'y@poster.place', display: 'Y', npub: 'npub1y', verified: true });
process.stdout.write(JSON.stringify({ html, ok }));
""")
    assert "<img src=x" not in out["html"] and "&lt;img src=x" in out["html"]
    assert 'src="&quot; onerror=' in out["html"]
    assert "✓ member" in out["html"] and "✓ member" in out["ok"]


def test_a_member_whose_profile_shows_another_address_is_shown_as_a_member():
    """dreadpirate (2026-10-08): granted dreadpirate@poster.place, profile shows DreadPirateRoberts@getalby.com.
    That is allowed -- the name is the membership -- and the list read it as a red "not in profile" sorted to the
    top, i.e. as somebody to remove."""
    out = _node(r"""
const { rowHtml } = require(process.argv[1]);
process.stdout.write(JSON.stringify({
  dread: rowHtml({ name: 'dreadpirate', address: 'dreadpirate@poster.place', display: 'DPR', npub: 'npub1c5b6',
                   verified: false, profile_nip05: 'DreadPirateRoberts@getalby.com' }),
  none:  rowHtml({ name: 'ghost', address: 'ghost@poster.place', display: '', npub: 'npub1g', verified: false, profile_nip05: '' }),
  same:  rowHtml({ name: 'alice', address: 'alice@poster.place', display: 'A', npub: 'npub1a', verified: true }),
  via:   rowHtml({ name: 'al', address: 'al@poster.place', display: 'A', npub: 'npub1a', verified: true, via: 'alice@poster.place' }) }));
""")
    for k in out:
        assert "✓ member" in out[k] and "not in profile" not in out[k] and "ids-badge ids-no" not in out[k], (k, out[k])
    assert "profile shows DreadPirateRoberts@getalby.com" in out["dread"]
    assert "profile shows no NIP-05" in out["none"]
    assert "profile shows this address" in out["same"]
    assert "profile shows alice@poster.place (another of their names here)" in out["via"]


def test_removing_keeps_the_saved_text_in_step():
    """Remove → the text box Save sends is rewritten AND becomes the baseline."""
    out = _node(r"""
const vm = require('vm'), fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
const els = {}, handlers = {}, calls = [];
const el = id => els[id] || (els[id] = { id, value: '', textContent: '', innerHTML: '' });
el('nostr_relay_nip05_names').value = 'alice abc\njonnyfever def';
const loaded = new Map();
const ctx = {
  console, JSON, Promise, setTimeout,
  loadedValues: loaded,
  location: { hash: '' },
  pcConfirm: async () => true, window: {},
  fetch: async (url, opt) => {
    calls.push([url, opt && opt.body]);
    if (url.endsWith('/identities')) return { ok: true, json: async () => ({ names_complete: true,
      identities: [{ name: 'alice', address: 'alice@p', npub: 'npub1a', verified: true },
                   { name: 'jonnyfever', address: 'jonnyfever@p', npub: 'npub1b', verified: false }] }) };
    return { ok: true, json: async () => ({ ok: true, value: 'alice abc' }) };
  },
  document: { getElementById: el, addEventListener: (t, fn) => (handlers[t] = handlers[t] || []).push(fn) },
};
ctx.window.csrfFetch = ctx.fetch;
vm.runInNewContext(src, ctx);
(async () => {
  const tab = { closest: s => s === '[data-tab="relay"]' ? {} : null };
  handlers.click.forEach(h => h({ target: tab }));
  await new Promise(r => setTimeout(r, 10));
  const before = el('ids_list').innerHTML.includes('jonnyfever@p');
  const btn = { disabled: false, textContent: 'Remove', closest: s => s === '.ids-row' ? { dataset: { name: 'jonnyfever' } } : null };
  handlers.click.forEach(h => h({ target: { closest: s => s === '.ids-remove' ? btn : null } }));
  await new Promise(r => setTimeout(r, 10));
  process.stdout.write(JSON.stringify({ before, after: el('ids_list').innerHTML.includes('jonnyfever@p'),
    text: el('nostr_relay_nip05_names').value, baseline: loaded.get('nostr_relay_nip05_names'),
    posted: calls.filter(c => c[0].endsWith('/identity/remove')).map(c => JSON.parse(c[1]).name) }));
})();
""")
    assert out["before"] and not out["after"], "the removed identity is still drawn"
    assert out["posted"] == ["jonnyfever"]
    assert out["text"] == "alice abc" and out["baseline"] == "alice abc", "the next Save would put the name back"


def test_the_page_is_wired():
    tab = (ROOT / "templates/admin/tabs/nostr_relay.html").read_text()
    assert 'id="ids_search"' in tab and 'id="ids_list"' in tab
    assert 'id="nostr_relay_nip05_names" name="nostr_relay_nip05_names"' in tab, "the text box Save sends must stay"
    assert "admin-identities.js" in (ROOT / "templates/admin.html").read_text()


def test_a_blank_profile_is_not_reported_as_no_profile(registry, monkeypatch):
    """2026-10-10 "identities for vyram says no profile on this relay": his second key HAS a profile here,
    with no name and no picture. The row says whether a profile exists, so the page can tell the two apart."""
    async def profiles(pks):
        return {PK["alice"]: {"name": "", "picture": "", "nip05": "alice@poster.place"}}, True
    monkeypatch.setattr(relay_blocklist, "profiles", profiles)
    rows = _by_name(asyncio.run(nip05_registry.rows("poster.place")))
    assert rows["alice"]["display"] == "" and rows["alice"]["has_profile"] is True
    assert rows["ghost"]["has_profile"] is False
