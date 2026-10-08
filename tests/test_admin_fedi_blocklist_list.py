"""Admin → Fediverse → Blocking is a LIST, like the relay lists, and it writes what the blocker enforces.

"Fediverse -> Block instances -> make the text area lists like you did for relays. test cases for this of
course" (2026-10-08). The blocked instances and accounts were one textarea: no search, no way to see whether a
line blocks an instance or one person, and no Remove. Now each line is a row labelled INSTANCE or ACCOUNT with a
Remove, and an Add box -- and the list reads every line through fedi_blocklist.normalize, the same function the
blocker uses, so a row on screen is exactly what is enforced. Its hand-written `# why` notes survive edits.
"""
import asyncio
import json
import subprocess
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.services import fedi_blocklist, relay_lists
from tests.test_admin_relay_lists import store, _edit  # noqa: F401  (pytest fixture)

ROOT = Path(__file__).resolve().parents[1]
KEY = "fedi_bridge_blocked_domains"
REAL = """# spam waves 2026
spam.example, bad.instance.tld
https://Troll.Example/@bob   # one account, not the whole instance
*.farm.example
嘟文.com
someone@elsewhere.example
"""


def _rows(raw):
    return asyncio.run(relay_lists.rows(KEY, raw))["items"]


def test_every_line_is_a_row_labelled_instance_or_account():
    got = [(r["value"], r["type"], r["valid"]) for r in _rows(REAL)]
    assert got == [("spam.example", "instance", True), ("bad.instance.tld", "instance", True),
                   ("https://Troll.Example/@bob", "account", True), ("*.farm.example", "instance", True),
                   ("嘟文.com", "instance", True), ("someone@elsewhere.example", "account", True)], got


def test_the_rows_are_exactly_what_the_blocker_enforces():
    hosts, accounts = fedi_blocklist.parse(REAL)
    for r in _rows(REAL):
        n = fedi_blocklist.normalize(r["value"])
        if r["type"] == "account":
            assert n in accounts and not fedi_blocklist.host_blocked(n.split("@")[1], hosts), r
        else:
            assert fedi_blocklist.host_blocked("x." + n, hosts), r


def test_adding_an_account_blocks_that_person_only_and_says_so(store):  # noqa: F811
    vals, written, _ = store
    vals[KEY] = REAL
    r = _edit(key=KEY, add="https://mastodon.example/@pest")
    hosts, accounts = fedi_blocklist.parse(r["value"])
    assert fedi_blocklist.account_blocked("pest@mastodon.example", accounts, hosts)
    assert not fedi_blocklist.account_blocked("friend@mastodon.example", accounts, hosts), "the instance was blocked"
    assert r["value"].splitlines()[-1] == "pest@mastodon.example" and written


def test_removing_an_instance_unblocks_it_and_keeps_every_note_and_neighbour(store):  # noqa: F811
    vals, _, _ = store
    vals[KEY] = REAL
    r = _edit(key=KEY, remove="SPAM.example")            # another spelling of the row
    hosts, _ = fedi_blocklist.parse(r["value"])
    assert not fedi_blocklist.host_blocked("spam.example", hosts)
    assert fedi_blocklist.host_blocked("bad.instance.tld", hosts), "its neighbour on the same line went too"
    assert "# spam waves 2026" in r["value"] and "# one account, not the whole instance" in r["value"]
    r = _edit(key=KEY, remove="bob@troll.example")       # the account, by its plain spelling
    assert "troll.example" not in r["value"].split("#")[0] and "# one account, not the whole instance" in r["value"]


def test_an_internationalised_name_is_one_entry_in_both_spellings(store):  # noqa: F811
    vals, _, _ = store
    vals[KEY] = REAL
    with pytest.raises(HTTPException) as e:
        _edit(key=KEY, add="xn--j5r817a.com")              # the punycode of 嘟文.com
    assert "already" in e.value.detail


@pytest.mark.parametrize("junk", ["hello", "@", "https://", "someone@", "@host", "two words.example x.example"])
def test_junk_is_refused_and_changes_nothing(store, junk):  # noqa: F811
    vals, written, _ = store
    vals[KEY] = REAL
    with pytest.raises(HTTPException) as e:
        _edit(key=KEY, add=junk)
    assert e.value.status_code == 400 and vals[KEY] == REAL and not written


def test_the_page_draws_the_type_and_escapes_what_was_typed():
    js = r"""
const { LISTS, rowHtml } = require(process.argv[1]);
process.stdout.write(JSON.stringify({ kind: LISTS.fedi_bridge_blocked_domains[0],
  acct: rowHtml('fedi', { value: 'bob@troll.example', type: 'account', valid: true }),
  inst: rowHtml('fedi', { value: '<b>x</b>.example', type: 'instance', valid: true }) }));
"""
    out = json.loads(subprocess.run(["node", "-e", js, str(ROOT / "static/js/admin-relay-lists.js")],
                                    capture_output=True, text=True, check=True).stdout)
    assert out["kind"] == "fedi"
    assert ">account<" in out["acct"] and ">instance<" in out["inst"]
    assert "<b>x</b>" not in out["inst"] and "&lt;b&gt;" in out["inst"]


def test_the_social_tab_loads_the_list_and_the_field_is_in_the_form():
    src = (ROOT / "static/js/admin-relay-lists.js").read_text()
    assert '[data-tab="social"]' in src and "'#tab-social'" in src
    tpl = (ROOT / "templates/admin/tabs/social.html").read_text()
    assert 'id="fedi_bridge_blocked_domains" name="fedi_bridge_blocked_domains"' in tpl
