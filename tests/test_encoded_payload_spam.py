"""ONE BASE64 TOKEN OF CIPHERTEXT OR JSON IS REFUSED ON EVERY PATH, PURGED, AND HIDDEN BY THE CLIENT.

Reported 2026-10-08 ("new spam ... can we protect from this?" / "maybe reject base64 like we did for json for
kind1?"), hours after the zero-width "webmesh" notes were refused: the same traffic came back as bare base64 --
ciphertext tagged with a random hex hashtag (`#b2b4782f4cc77abc`, 120 notes from one key, no profile) and
base64 JSON (`#swarmmesh`, 72). Measured over 30 days of kind 1 on this relay: 195 notes from 5 keys matched,
every one a bot; none of the 79 one-token notes people post (bech32 links and invoices, bitcoin addresses, hex
ids, base58) did. Those real shapes are pinned below so the rule cannot grow into them.
"""
import asyncio
import base64
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from app.services.nostr.event import build_event
from app.services.nostr_relay.ingest import _content_blocked
from app.services.nostr_relay.langfilter import is_encoded_payload, is_machine_payload
from tests.test_json_spam_every_path import SK, firehose, _note
from tests.test_hidden_payload_spam import _client_fns, HARNESS
from tests.test_relay_prune import store_factory, _run  # noqa: F401  (pytest fixture)

CIPHER = "dVWmA+VVFJcJT3Evk2UF8FqlPCnJBhHijKGrw27kY2sn9vhICDStUGAzSgR+kGJuDBIKeN3KUGGROZbmMTd8OpOBw2g="  # the reported note
B64_JSON = base64.b64encode(json.dumps({"type": "feed", "id": "2c172705-9114-4e41-aa0c-fdbf2a1d5f3e",
                                        "peers": ["a", "b"]}).encode()).decode()                       # #swarmmesh
BOTS = [CIPHER, B64_JSON, "  " + CIPHER + "\n"]
PEOPLE = [
    "npub1xfyd2p842jnat6mx88v940xzshrmma5wd55us80a80e86a22z20qexwrn9",
    "nevent1qqsvaaysjc9k2uqh6vr5d09k3q7ut3wp09xt64mtrh72lhnvkc7hydgm5t27y",
    "note1he6yy2pmuk556lglwjhg0cdvmtmpdc6vlm73505c6msehh7w4arq9xs0wl",
    "lnbc10u1p4tccvqpp5cdq8u5lmcesf8ufpqahxrp7adakm4xptjr46lz25hz6fgc3cj3ns",
    "bc1qm34lsc65zpw79lxes69zkqmk6ee3ewf0j77s3h",
    "978e1bcb0573fe426c8fcedc06620afc305b35a667266899db2c02f93bc7df7c",   # a hex id
    "89CogUB3jFzbetHUbsGHkHNbXyf8BJye7fxAKGWBtvcgVmsHMB2Gt2Di3vFHaEetmz6Hq",  # base58
    base64.b64encode(b"just a sentence somebody base64-encoded for fun, nothing more").decode(),  # decodes to TEXT
    "Kkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkk",
    "gm",
    "",
]


@pytest.mark.parametrize("content", BOTS)
def test_base64_ciphertext_and_base64_json_are_payloads(content):
    assert is_encoded_payload(content) and is_machine_payload(content)


@pytest.mark.parametrize("content", PEOPLE)
def test_the_one_token_notes_people_post_are_not(content):
    assert not is_encoded_payload(content) and not is_machine_payload(content)


def test_sync_refuses_it_for_kind_1_only_and_follows_the_switch():
    assert _content_blocked({"kind": 1, "content": CIPHER}, set(), set(), True)
    assert not _content_blocked({"kind": 1, "content": CIPHER}, set(), set(), False)
    assert not _content_blocked({"kind": 30078, "content": CIPHER}, set(), set(), True)


def test_the_live_firehose_drops_it_and_keeps_a_link():
    fh, store, srv = firehose()
    asyncio.run(fh(build_event(SK, 1, CIPHER, [["t", "b2b4782f4cc77abc"]])))
    store.add_event.assert_not_called()
    fh, store, _ = firehose()
    asyncio.run(fh(build_event(SK, 1, PEOPLE[1])))
    store.add_event.assert_called_once()


def test_a_client_publishing_here_is_refused_with_a_reason():
    from app.services.nostr_relay import server
    src = Path(server.__file__).read_text()
    i = src.index("is_encoded_payload(content)")
    assert 'kind == 1 and self.cfg.get("block_json", True)' in src[i - 80:i] and "_refuse(" in src[i:i + 200]


def test_the_purge_removes_stored_encoded_notes_and_nothing_else(store_factory):  # noqa: F811
    async def go(loop):
        st = store_factory(loop)
        now = int(time.time())
        evs = [{"id": f"{i + 1:064x}", "pubkey": "c" * 64, "kind": 1, "created_at": now - i, "content": c,
                "tags": [], "sig": "0" * 128} for i, c in enumerate(BOTS[:2] + [p for p in PEOPLE if p])]
        await st.add_events_bulk(evs, origin="wot")
        removed = await st.delete_hidden_payload()
        left = {r["id"] for r in st._conn().execute("SELECT id FROM events").fetchall()}
        return removed, left, evs
    removed, left, evs = _run(go)
    assert removed == 2 and left == {e["id"] for e in evs[2:]}


def test_the_client_hides_it_and_keeps_every_real_one_token_note():
    if not shutil.which("node"):
        pytest.skip("node not installed")
    cases = ([[{}, _note(c)] for c in BOTS] + [[{"block_json_posts": False}, _note(CIPHER)]]
             + [[{}, _note(p)] for p in PEOPLE])
    r = subprocess.run(["node", "-e", HARNESS % _client_fns(), json.dumps(cases)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == [True] * len(BOTS) + [False] + [False] * len(PEOPLE)
