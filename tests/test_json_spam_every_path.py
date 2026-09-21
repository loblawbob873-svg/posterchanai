""""REJECT JSON-ONLY TIMELINE POSTS" MUST HOLD ON EVERY PATH A POST ENTERS — NOT JUST TWO OF THEM.

Reported as "not working": npub16xe5cxarq5a7uv8aqksg299pgekdxh69gz8ehxhygzxcu0ep7z3qtpfa3z was in
the feed posting `{"id":"p1789966458130587","n":"mahan","t":"","ts":1789966458130,"ty":"p"}` — a chat
app's presence beacon (tags `t=payamresan-general`, `d=presence`) published as kind 1, every ~30s.

The predicate was never wrong: `is_json_content` catches that payload. What was wrong is WHERE it ran.
Measured on 2026-09-21:

  * The write gate (server._on_event) and the sync sweep (ingest._content_blocked) applied it.
  * The LIVE FIREHOSE (thread._firehose_event) — the path almost the whole public feed arrives by —
    did not. server1's store held 26 notes of exactly this shape from another presence bot, all
    origin='wot', with the toggle ON.
  * That npub itself had 0 events on server1, 0 on nas and 0 on poster.place — and 500 each on
    nos.lol and relay.primal.net. The client reads public relays directly, so no relay-side filter
    could ever have kept it off the screen; there was no client filter at all.
  * Admin Save sent `reload-blocks` for this toggle, and the handler never read it, so turning it
    off (or on) did nothing until the relay restarted.

Each test below fails on the pre-fix tree.
"""
import ast
import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.services.nostr.event import build_event, verify_event
from app.services.nostr_relay import thread
from app.services.nostr_relay.langfilter import is_json_content

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "static" / "js" / "client" / "app.js"

SPAM = '{"id":"p1789966458130587","n":"mahan","t":"","ts":1789966458130,"ty":"p"}'
JSON_SHAPES = [
    SPAM,
    "  " + SPAM + "\n",                                  # surrounding whitespace
    json.dumps(json.loads(SPAM), indent=2),             # pretty-printed
    "[1,2,3]",                                          # array
    '[{"a":1},{"b":[2,3]}]',                            # array of objects
    '{"a":{"b":{"c":[1,{"d":2}]}}}',                    # nested
    '\n\t{"id":"p65c001784e97f2b91f52a","n":"محمد","t":"","ts":1790005324605,"ty":"p"}\r\n',
]
HUMAN = [
    "gm nostr",
    'here is the payload I saw: {"id":"p1","ty":"p"} — anyone know what app this is?',
    '```json\n{"id":"p1","ty":"p"}\n```',
    "`{\"a\":1}` is valid json",
    "{this} is not json",
    "[citation needed]",
    "42",
    '"just a quoted string"',
    "",
]
SK = bytes.fromhex("11" * 32)


@pytest.mark.parametrize("content", JSON_SHAPES)
def test_the_reported_payload_and_its_variants_are_json_only(content):
    assert is_json_content(content)


@pytest.mark.parametrize("content", HUMAN)
def test_human_posts_with_json_inside_are_not(content):
    assert not is_json_content(content)


# ------------------------------------------------------------------------------ the live firehose
SOURCE = Path(thread.__file__).read_text()


def _fn(name):
    tree = ast.parse(SOURCE)
    return next(n for n in ast.walk(tree)
                if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name == name)


class Member:
    def is_member(self, pk):
        return True

    def is_operator(self, pk):
        return False

    def mark_bridged(self, pk):
        pass

    def mark_bridged_identity(self, pk):
        pass


def firehose(block_json=True):
    cfg = {"blocked_words": set(), "blocked_langs": set(), "blocked_relays": [], "operator": [],
           "block_bridged": False, "fetch_ancestors": False}
    if block_json is not None:
        cfg["block_json"] = block_json
    store = SimpleNamespace(has_event=AsyncMock(return_value=False), add_event=AsyncMock(return_value=True))
    srv = SimpleNamespace(subs=SimpleNamespace(fanout=Mock()), _send=Mock())
    env = {**vars(thread), "cfg": cfg, "_bl": set(), "_bw": set(), "store": store, "gate": Member(),
           "server": srv, "verify_event": verify_event, "_FH_SEEN": set(), "_fh_mark": Mock()}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[_fn("_firehose_event")], type_ignores=[])),
                 thread.__file__, "exec"), env)
    return env["_firehose_event"], store, srv


@pytest.mark.parametrize("content", JSON_SHAPES)
def test_the_firehose_drops_a_json_only_note(content):
    fh, store, srv = firehose()
    ev = build_event(SK, 1, content, [["t", "payamresan-general"], ["d", "presence"]])
    asyncio.run(fh(ev))
    store.add_event.assert_not_called()
    srv.subs.fanout.assert_not_called()


def test_the_firehose_default_is_on_when_the_key_is_absent():
    fh, store, _ = firehose(block_json=None)
    asyncio.run(fh(build_event(SK, 1, SPAM)))
    store.add_event.assert_not_called()


@pytest.mark.parametrize("content", [c for c in HUMAN if c])
def test_the_firehose_keeps_a_human_note(content):
    fh, store, _ = firehose()
    asyncio.run(fh(build_event(SK, 1, content)))
    store.add_event.assert_called_once()


def test_the_firehose_keeps_json_of_other_kinds_and_honours_the_toggle():
    fh, store, _ = firehose()
    asyncio.run(fh(build_event(SK, 0, '{"name":"alice"}')))       # a profile IS json
    store.add_event.assert_called_once()
    fh, store, _ = firehose(block_json=False)
    asyncio.run(fh(build_event(SK, 1, SPAM)))
    store.add_event.assert_called_once()


def test_the_firehose_ancestor_backfill_is_told_the_toggle():
    call = next(n for n in ast.walk(_fn("_firehose_event"))
                if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "backfill_ancestors")
    assert "block_json" in {k.arg for k in call.keywords}, \
        "ancestor backfill from the firehose ignores the toggle (defaults it ON even when OFF)"


def test_a_live_reload_applies_the_toggle():
    # The admin Save path triggers reload-blocks for nostr_relay_block_json_posts; the handler has
    # to copy it into the live cfg, or the switch waits for a relay restart.
    i = SOURCE.index('cmd.get("cmd") == "reload-blocks"')
    body = SOURCE[i:SOURCE.index('cmd.get("cmd") == "purge-blocks"', i)]
    assert 'cfg["block_json"] = fresh.get("block_json"' in body


# ------------------------------------------------------------------------------ the client backstop
def _client_fns():
    src = APP.read_text(encoding="utf-8")
    a = src.index("  function _isJsonOnlyContent(")
    b = src.index("  function isMutedView(")
    c = src.index("\n  }\n", b) + 4
    return src[a:c]


NODE_HARNESS = r"""
let CFG = {};
const isMutedAuthor = () => false, mutedByWord = () => false, _mutedThread = () => false,
      _repostDeleted = () => false, Store = { get: () => null };
%s
const cases = JSON.parse(process.argv[1]);
const out = cases.map(([cfg, ev]) => { CFG = cfg; return isMutedView(ev); });
process.stdout.write(JSON.stringify(out));
"""


def _run_client(cases):
    if not shutil.which("node"):
        pytest.skip("node not installed")
    js = NODE_HARNESS % _client_fns()
    r = subprocess.run(["node", "-e", js, json.dumps(cases)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def _note(content, kind=1):
    return {"id": "e" * 64, "pubkey": "a" * 64, "kind": kind, "content": content, "tags": []}


def test_the_client_hides_json_only_notes_read_from_public_relays():
    got = _run_client([[{}, _note(c)] for c in JSON_SHAPES])
    assert got == [True] * len(JSON_SHAPES)


def test_the_client_keeps_human_notes_and_non_note_json():
    cases = [[{}, _note(c)] for c in HUMAN] + [[{}, _note('{"name":"alice"}', kind=0)]]
    assert _run_client(cases) == [False] * len(cases)


def test_the_client_hides_a_repost_of_json_spam_and_follows_the_node_toggle():
    repost = {"id": "f" * 64, "pubkey": "b" * 64, "kind": 6, "tags": [["e", "e" * 64]],
              "content": json.dumps(_note(SPAM))}
    assert _run_client([[{}, repost],
                        [{"block_json_posts": False}, _note(SPAM)],
                        [{"block_json_posts": True}, _note(SPAM)]]) == [True, False, True]


def test_client_config_publishes_the_toggle():
    src = (ROOT / "app/routers/client.py").read_text(encoding="utf-8")
    assert '"block_json_posts": (_setting(db, "nostr_relay_block_json_posts", "true")' in src
