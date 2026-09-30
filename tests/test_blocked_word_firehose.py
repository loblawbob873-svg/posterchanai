"""The firehose applies blocked words to every kind -- the third ingestion path.

Reported: "i have https://otherstuff.ai blocked as a word but the posts keep coming in". The relay's
list held `https://otherstuff.ai` AND `otherstuff.ai` (decrypted on the node: 49 terms). What kept
arriving were kind-6 REPOSTS whose embedded note links `https://otherstuff.ai/word5/`, stored with
origin='wot' -- the firehose, which still filtered words only for `kind == 1` after the direct-write
and sync paths were fixed (test_blocked_word_is_blocked_on_every_kind only ever checked those two).

Runs the SHIPPED `_firehose_event` closure (the harness in test_json_spam_every_path) with the
reported word and a signed repost of the reported shape.
"""
import asyncio
import ast
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.services.nostr.event import build_event, verify_event
from app.services.nostr_relay import ingest, server, thread
from app.services.nostr_relay.langfilter import blocked_language, blocked_word
from tests.test_json_spam_every_path import SK, Member, _fn

WORDS = {"https://otherstuff.ai", "otherstuff.ai"}
INNER = json.dumps({"kind": 1, "content": "WORD5 #726 2/6* (Hard Mode)\n\nhttps://otherstuff.ai/word5/",
                    "tags": [["t", "word5"]], "pubkey": "bc" * 32, "created_at": 1790799186})


def firehose(words):
    cfg = {"blocked_words": set(words), "blocked_langs": set(), "blocked_relays": [], "operator": [],
           "block_bridged": False, "fetch_ancestors": False, "block_json": True}
    store = SimpleNamespace(has_event=AsyncMock(return_value=False), add_event=AsyncMock(return_value=True))
    srv = SimpleNamespace(subs=SimpleNamespace(fanout=Mock()), _send=Mock(), _can_serve_event=Mock(return_value=True))
    env = {**vars(thread), "cfg": cfg, "_bl": set(), "_bw": cfg["blocked_words"], "store": store, "gate": Member(),
           "server": srv, "verify_event": verify_event, "_FH_SEEN": set(), "_fh_mark": Mock(),
           # imported inside the relay's run() in production, which the closure closes over
           "blocked_language": blocked_language, "blocked_word": blocked_word}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[_fn("_firehose_event")], type_ignores=[])),
                 thread.__file__, "exec"), env)
    return env["_firehose_event"], store


@pytest.mark.parametrize("kind,content", [
    (6, INNER),                                                            # the reported repost
    (16, INNER),                                                           # a generic repost
    (1111, "look https://otherstuff.ai/word5/"),                           # a comment
    (30023, "long read about https://otherstuff.ai/"),                     # an article
    (1, "WORD5 https://otherstuff.ai/word5/"),                             # the plain note (always worked)
])
def test_a_blocked_word_is_dropped_whatever_kind_the_firehose_delivers(kind, content):
    fh, store = firehose(WORDS)
    tags = [["d", "x"]] if kind == 30023 else []
    asyncio.run(fh(build_event(SK, kind, content, tags)))
    store.add_event.assert_not_called()


def test_an_ordinary_repost_still_arrives():
    fh, store = firehose(WORDS)
    asyncio.run(fh(build_event(SK, 6, json.dumps({"kind": 1, "content": "good morning"}))))
    store.add_event.assert_called_once()


def test_the_apps_own_datastore_is_still_never_word_filtered():
    fh, store = firehose({"otherstuff.ai"})
    asyncio.run(fh(build_event(SK, 30078, "a note that mentions otherstuff.ai", [["d", "pcai:note:1"]])))
    store.add_event.assert_called_once()


def test_all_three_ingestion_paths_use_the_one_rule():
    """The earlier fix checked the direct-write and sync modules and missed this one."""
    import inspect
    fh_src = ast.get_source_segment(inspect.getsource(thread), _fn("_firehose_event")) or ""
    assert "_content_blocked(" in fh_src, "the firehose has its own copy of the content rules again"
    for mod in (ingest, server):
        assert "_NEVER_WORD_FILTERED" in inspect.getsource(mod)
