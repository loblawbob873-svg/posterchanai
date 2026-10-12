"""Admin lists have ONE "Search or add…" box ("to add an item, I have to search and then enter it again below").

The browser half runs scripts/check_admin_search_or_add.py: the shipped admin page in a real Chrome at 390px and
1280px, served by the real admin router over the real settings store and a relay on a socket. For Relay lists and
for a bot-editor list it types ONCE and adds with Enter and with "+ Add", checks an exact match is not duplicated
(and sends nothing), that filtering still works, and that the entries survive Save + restart + a fresh load.
Exit 2 from the check means it could not run here (no Chrome) and is a skip, never a pass.

The node half pins the one rule the client decides on its own: an "already in the list" verdict must never be
WRONG in the direction that hides the Add -- the client may only call two spellings the same entry when the
server's relay_lists.same() does too. (The other direction is safe: the server refuses a duplicate itself.)
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.services import relay_lists

ROOT = Path(__file__).resolve().parents[1]

# (kind, typed, stored) -- spellings a person types next to the entry already in the list.
PAIRS = [
    ("word", "Free Crypto", "free crypto"), ("word", " buy now ", "buy now"), ("word", "buy", "buy now"),
    ("topic", "GAMING", "gaming"), ("topic", "retro", "retro tech"),
    ("domain", "https://Spam.Example/x", "spam.example"), ("domain", "spam.example.", "spam.example"),
    ("domain", "sub.spam.example", "spam.example"),
    ("relay", "wss://Relay.Example/", "wss://relay.example"), ("relay", "wss://relay.example/x", "wss://relay.example"),
    ("server", "https://backup.example.com/blossom/", "https://backup.example.com/blossom"),
    ("origin", "HTTPS://poster.place", "https://poster.place"),
    ("host", "NAS.lan.", "nas.lan"), ("host", "nas.lan:8080", "nas.lan"),
    ("invite", "https://p/c/naddr1#KEY", "https://p/c/naddr1#key"),
    ("fedi", "https://Bad.Example/", "bad.example"), ("fedi", "*.bad.example", "bad.example"),
    ("fedi", "@bad.example", "bad.example"), ("fedi", "https://bad.example/@troll", "troll@bad.example"),
    ("fedi", "Troll@Bad.Example", "troll@bad.example"), ("fedi", "other@bad.example", "troll@bad.example"),
]


def _js_same(script, pairs):
    js = ("const m=require(process.argv[1]);const pairs=JSON.parse(process.argv[2]);"
          "console.log(JSON.stringify(pairs.map(([k,a,b])=>m.sameKey(k,a)===m.sameKey(k,b))))")
    out = subprocess.run(["node", "-e", js, str(ROOT / script), json.dumps(pairs)], capture_output=True, text=True,
                         check=True, timeout=20)
    return json.loads(out.stdout)


@pytest.mark.parametrize("script,kinds", [
    ("static/js/admin-relay-lists.js", None),
    ("static/js/admin-list-fields.js", set(relay_lists.FIELD_KINDS)),
])
def test_the_client_never_calls_two_entries_the_same_when_the_server_does_not(script, kinds):
    pairs = [p for p in PAIRS if kinds is None or p[0] in kinds]
    for (kind, a, b), js_same in zip(pairs, _js_same(script, pairs)):
        if js_same:
            assert relay_lists.same(kind, a) == relay_lists.same(kind, b), (script, kind, a, b)
    # and the obvious ones ARE recognised, or the box would offer to add what is already there
    assert _js_same(script, [("word", "Free Crypto", "free crypto"), ("relay", "wss://r.example/", "wss://r.example")]) \
        == [True, True]


def test_one_search_or_add_box_in_a_real_browser(tmp_path):
    env = dict(os.environ)
    r = subprocess.run([sys.executable, str(ROOT / "scripts/check_admin_search_or_add.py")], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=420)
    if r.returncode == 2:
        pytest.skip(r.stdout.strip() or r.stderr.strip()[-300:])
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
