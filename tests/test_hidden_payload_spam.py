"""NOTES MADE OF HIDDEN CHARACTERS ARE REFUSED ON EVERY PATH, PURGED FROM THE STORE, AND HIDDEN BY THE CLIENT.

Reported 2026-10-08: "i suspect this user is spam, seeing many kinds like this now, 0 followers, 0
following, how did it make it to my relay!" — npub1549pu62…, one of seven "webmesh" keys that put 1,538
notes on server1 in a day. Each note is one bait line ("Anyone else excited about the new season
starting next week?") followed by 130+ zero-width characters encoding a payload, tagged
`#webmesh-v1-nodes`. They came in through the ordinary web of trust (six accounts the network follows
follow the bot, and depth 3 admits anyone with three), so no trust rule could have stopped them; the
content can, because no person types a hundred invisible characters.

The bound was MEASURED over 30 days / 629k notes on the live relay: every webmesh note had >=132
invisible and <=70 visible characters; real people's notes that carry a run of them (a client
watermark, a pasted article) had <=50 invisible beside 100-2,400 visible. So the rule needs BOTH.
"""
import ast
import asyncio
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from app.services.nostr.event import build_event
from app.services.nostr_relay.ingest import _content_blocked
from app.services.nostr_relay.langfilter import is_hidden_payload
from tests.test_json_spam_every_path import SK, firehose, _note
from tests.test_relay_prune import store_factory, _run  # noqa: F401  (pytest fixture)

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "static" / "js" / "client" / "app.js"

ZW = "‌‌‌⁠‌​⁠‌​​"
# The real shape: a bait sentence, a space, then the payload (here 140 invisible characters).
WEBMESH = "Anyone else excited about the new season starting next week? " + ZW * 14
WEBMESH_SHORT_BAIT = "Anyone " + ZW * 14
PEOPLE = [
    # a client watermark of 50 inside an ordinary post (seen on 11 real notes)
    "The Senate did not vote on the CLARITY Act yesterday.\nIt voted on cloture. " + ZW * 5 + " more soon",
    # 17 scattered through a long post
    ("x " * 400) + ZW + "​" * 7,
    # emoji with zero-width joiners
    "👨‍👩‍👧‍👦 family day 👩‍💻👨‍🚀 " * 10,
    "gm",
    "",
    ZW * 9,          # 90 invisible — under the floor, even with nothing visible
]


@pytest.mark.parametrize("content", [WEBMESH, WEBMESH_SHORT_BAIT, ZW * 30])
def test_a_note_that_is_mostly_hidden_characters_is_a_payload(content):
    assert is_hidden_payload(content)


@pytest.mark.parametrize("content", PEOPLE)
def test_peoples_notes_are_not(content):
    assert not is_hidden_payload(content)


def test_the_sync_and_backfill_predicate_refuses_it_for_kind_1_only_and_follows_the_switch():
    note = {"kind": 1, "content": WEBMESH}
    assert _content_blocked(note, set(), set(), True)
    assert not _content_blocked(note, set(), set(), False), "the admin switch must turn it off"
    assert not _content_blocked({"kind": 30078, "content": WEBMESH}, set(), set(), True)


def test_the_live_firehose_drops_it():
    fh, store, srv = firehose()
    asyncio.run(fh(build_event(SK, 1, WEBMESH, [["t", "webmesh-v1-nodes"]])))
    store.add_event.assert_not_called()
    srv.subs.fanout.assert_not_called()
    fh, store, _ = firehose()
    asyncio.run(fh(build_event(SK, 1, PEOPLE[0])))
    store.add_event.assert_called_once()


def test_a_client_publishing_here_is_refused_with_a_reason():
    from app.services.nostr_relay import server
    src = Path(server.__file__).read_text()
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_on_event")
    body = ast.get_source_segment(src, fn)
    i = body.index("is_hidden_payload(content)")
    assert 'kind == 1 and self.cfg.get("block_json", True)' in body[i - 80:i]
    assert "_refuse(" in body[i:i + 200]


def test_the_purge_removes_stored_payload_notes_and_nothing_else(store_factory):  # noqa: F811
    async def go(loop):
        st = store_factory(loop)
        now = int(time.time())
        evs = []
        for i, c in enumerate([WEBMESH, WEBMESH_SHORT_BAIT] + [p for p in PEOPLE if p]):
            evs.append({"id": f"{i + 1:064x}", "pubkey": "b" * 64, "kind": 1, "created_at": now - i,
                        "content": c, "tags": [], "sig": "0" * 128})
        await st.add_events_bulk(evs, origin="wot")
        removed = await st.delete_hidden_payload()
        left = {e["id"] for e in await st.query([{"limit": 5000}])}     # both backends (tests/relay_backends.py)
        return removed, left, evs
    removed, left, evs = _run(go)
    assert removed == 2
    assert left == {e["id"] for e in evs[2:]}


def test_the_nightly_and_admin_purge_runs_it():
    from app.services.nostr_relay import thread
    src = Path(thread.__file__).read_text()
    i = src.index("async def _purge_blocks_now")
    body = src[i:src.index("async def _maybe_purge_blocks", i)]
    assert "store.delete_hidden_payload()" in body and "by_hidden" in body.split("total =")[1].split("\n")[0]


# ------------------------------------------------------------------------------ the client backstop
def _client_fns():
    src = APP.read_text(encoding="utf-8")
    a = src.index("  function _isJsonOnlyContent(")
    b = src.index("  function isMutedView(")
    return src[a:src.index("\n  }\n", b) + 4]


HARNESS = r"""
let CFG = {};
const isMutedAuthor = () => false, mutedByWord = () => false, _mutedThread = () => false,
      _repostDeleted = () => false, Store = { get: () => null };
%s
const cases = JSON.parse(process.argv[1]);
process.stdout.write(JSON.stringify(cases.map(([cfg, ev]) => { CFG = cfg; return isMutedView(ev); })));
"""


def test_the_client_hides_it_when_it_arrives_from_a_public_relay():
    if not shutil.which("node"):
        pytest.skip("node not installed")
    cases = ([[{}, _note(WEBMESH)], [{}, _note(WEBMESH_SHORT_BAIT)], [{"block_json_posts": False}, _note(WEBMESH)]]
             + [[{}, _note(p)] for p in PEOPLE])
    r = subprocess.run(["node", "-e", HARNESS % _client_fns(), json.dumps(cases)],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == [True, True, False] + [False] * len(PEOPLE)
