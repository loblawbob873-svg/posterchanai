"""The repo Edit form must not drop tags it doesn't show.

A kind-30617 repo announcement is REPLACEABLE: re-publishing it overwrites the whole event. The
Edit form (Discover → Git → a repo you own → Edit) shows five fields, and the announcement carries
more than five tags — `maintainers` holds the hosting node's operator key, which signs the 30618
state witness and is half the push ACL, and `relays` is the advertised push endpoint. Emitting only
the fields on screen would leave the repo looking fine and quietly break pushing to it.

This runs the SHIPPED publishRepo() out of static/js/client/app.js under node, rather than a copy of
its logic — a copy is the one thing that can't catch the regression it exists to catch. The function
is sliced out by brace-matching and given stubs for the browser it expects.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[1] / "static" / "js" / "client" / "app.js"

EXISTING = {
    "id": "a" * 64,
    "pubkey": "b" * 64,
    "kind": 30617,
    "created_at": 1700000000,
    "content": "",
    "tags": [
        ["d", "posterchanai"],
        ["r", "3fe691fd66d102ea1c810f1dc0ffa6e6f3dfceb6", "euc"],
        ["name", "PosterChanAI"],
        ["description", "the old description"],
        ["clone", "https://poster.place/git/npub1owner/posterchanai.git", "https://mirror/x.git"],
        ["web", "https://poster.place/client"],
        ["relays", "wss://relay.poster.place", "wss://poster.place/git"],
        ["maintainers", "b" * 64, "c" * 64],
        ["alt", "git repository: PosterChanAI"],
    ],
}


def _slice_function(src: str, header: str) -> str:
    """The text of a `function name(...){ … }` declaration, by brace matching."""
    start = src.index(header)
    i = src.index("{", start)
    depth, j = 0, i
    while j < len(src):
        c = src[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
        j += 1
    raise AssertionError("unterminated %s" % header)


def _harness(edits: dict) -> str:
    src = APP_JS.read_text()
    own = re.search(r"const _REPO_OWN_TAGS=new Set\(\[[^\]]*\]\);", src)
    assert own, "_REPO_OWN_TAGS is gone — the carry-over it drives probably went with it"
    fn = _slice_function(src, "function publishRepo(existing)")
    return """
'use strict';
const EDITS = %s, EXISTING = %s;
let PUBLISHED = null, MOUNT = null, OPENED = null, TOASTS = [];
const ME = {pubkey: EXISTING.pubkey};
const enc = s => String(s == null ? '' : s);
const toast = m => TOASTS.push(m);
const closeModal = () => {};
const switchView = () => {};
const openRepo = e => { OPENED = e; };
const Store = {query: () => []};
const publish = async (kind, content, tags) => { PUBLISHED = {kind, content, tags}; return {ok: true}; };
const modal = (html, onMount) => { MOUNT = {html, onMount}; };
/* The form's own controls, addressed by id exactly as the function does. */
const FIELDS = {}, BTN = {}, STATUS = {textContent: ''};
for (const id of ['rp-d', 'rp-name', 'rp-desc', 'rp-clone', 'rp-web'])
  FIELDS[id] = {value: '', focus(){}, textContent: ''};
const $ = (sel) => {
  const id = String(sel).replace(/^#/, '');
  if (id === 'rp-pub')    return {set onclick(f){ BTN.pub = f; }};
  if (id === 'rp-cancel') return {set onclick(f){ BTN.cancel = f; }};
  if (id === 'rp-status') return STATUS;
  return FIELDS[id] || {value: '', focus(){}, textContent: ''};
};

%s
%s

publishRepo(EXISTING);
if (!MOUNT) { console.log(JSON.stringify({error: 'no modal'})); process.exit(0); }
/* Seed every field from the rendered form, then apply the caller's edits — the same order a person
   does it in: the form arrives pre-filled and they change one thing. */
for (const [id, f] of Object.entries(FIELDS)) {
  const m = new RegExp('id="' + id + '"[^>]*value="([^"]*)"').exec(MOUNT.html);
  const t = new RegExp('id="' + id + '"[^>]*>([^<]*)</textarea>').exec(MOUNT.html);
  f.value = m ? m[1] : (t ? t[1] : '');
}
for (const [id, val] of Object.entries(EDITS)) FIELDS[id].value = val;

MOUNT.onMount({});
(async () => {
  await BTN.pub();
  console.log(JSON.stringify({html: MOUNT.html, published: PUBLISHED, toasts: TOASTS,
                              status: STATUS.textContent, opened: OPENED && OPENED.tags}));
})();
""" % (json.dumps(edits), json.dumps(EXISTING), own.group(0), fn)


def _run(edits: dict) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    out = subprocess.run([node, "-e", _harness(edits)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def _tag(tags, key):
    return [t for t in tags if t[0] == key]


def test_editing_the_description_keeps_maintainers_relays_and_euc():
    res = _run({"rp-desc": "a brand new description"})
    tags = res["published"]["tags"]
    assert _tag(tags, "description") == [["description", "a brand new description"]]
    # The three the form never shows. Losing `maintainers` is the one that breaks pushing.
    assert _tag(tags, "maintainers") == [["maintainers", "b" * 64, "c" * 64]]
    assert _tag(tags, "relays") == [["relays", "wss://relay.poster.place", "wss://poster.place/git"]]
    assert _tag(tags, "r") == [["r", "3fe691fd66d102ea1c810f1dc0ffa6e6f3dfceb6", "euc"]]
    # …and it stays the SAME repo: one d tag, unchanged.
    assert _tag(tags, "d") == [["d", "posterchanai"]]


def test_an_untouched_multi_value_tag_keeps_every_value():
    """The form shows clone[0] only; editing the description must not truncate the tag to it."""
    res = _run({"rp-desc": "new"})
    assert _tag(res["published"]["tags"], "clone") == [
        ["clone", "https://poster.place/git/npub1owner/posterchanai.git", "https://mirror/x.git"]]


def test_changing_a_field_rewrites_only_that_tag():
    res = _run({"rp-clone": "https://elsewhere/x.git"})
    tags = res["published"]["tags"]
    assert _tag(tags, "clone") == [["clone", "https://elsewhere/x.git"]]
    assert _tag(tags, "description") == [["description", "the old description"]]
    assert _tag(tags, "maintainers") == [["maintainers", "b" * 64, "c" * 64]]


def test_the_edit_form_arrives_prefilled_and_pins_the_repo_id():
    res = _run({})
    html = res["html"]
    assert "Edit repo details" in html
    assert 'id="rp-d" value="posterchanai"' in html and "readonly" in html
    assert "the old description" in html
    # Publishing with nothing changed must be a no-op in content, not a partial rewrite.
    assert _tag(res["published"]["tags"], "maintainers") == [["maintainers", "b" * 64, "c" * 64]]
