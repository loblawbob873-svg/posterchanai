"""The native LLM's settings are `llm_*`, and a node's stored values MOVE to the new names (2026-10-26: "ollama should
have nothing to do with this project anymore"). The 16 sampling/runtime keys carried the prefix of an inference server
this app no longer has; they are stored per node as operator relay documents, so renaming the fields alone would have
read DEFAULTS on every node that had tuned them (context size, timeout, system prompt...).

Driven through the real settings_store, with the relay read/write/delete stubbed at their edges:
  * a node whose relay holds only the old name READS its value under the new name at once, before anything moved;
  * the startup move writes it under the new name and only then deletes the old document;
  * a node that already holds the new name keeps that value (the newer one) and just drops the old document;
  * a write that does not land, or a relay that cannot be read to the end, changes nothing (retried next start);
  * nothing reads an old name any more, and the app calls the move BEFORE the default seeding.
"""
import asyncio
import json
import re
from pathlib import Path

import pytest

from app.services import settings_store as S
from app.services.nostr import nip44

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def relay(monkeypatch):
    docs, writes, deletes, state = {}, [], [], {"authoritative": True, "short": False}

    def events(_sk, _hex):
        return {S.store.NS_SETTING + k: {"content": json.dumps({"value": v})} for k, v in docs.items()}

    async def write_through(_db, changes):
        if state["short"]:
            return 0
        writes.append(dict(changes))
        docs.update(changes)
        return len(changes)

    async def delete_doc(_port, _sk, d):
        deletes.append(d)
        docs.pop(d[len(S.store.NS_SETTING):], None)

    def keys(_db):
        return (set(docs), True) if state["authoritative"] else (set(), False)

    monkeypatch.setattr(S, "_operator_seckey", lambda db: b"\x01" * 32)
    monkeypatch.setattr(S, "_operator_hex", lambda sk: "aa" * 32)
    monkeypatch.setattr(S, "_operator_setting_events", events)
    monkeypatch.setattr(nip44, "decrypt_self", lambda sk, c: c)
    monkeypatch.setattr(S, "write_through", write_through)
    monkeypatch.setattr(S.store, "delete_doc", delete_doc)
    monkeypatch.setattr(S, "_relay_setting_keys_from_db", keys)
    monkeypatch.setattr(S, "_port", lambda db=None: 3052)
    saved = dict(S._CACHE)
    S._CACHE.clear(); S._RENAMED_FROM_RELAY.clear()
    yield {"docs": docs, "writes": writes, "deletes": deletes, "state": state}
    S._CACHE.clear(); S._CACHE.update(saved); S._RENAMED_FROM_RELAY.clear()


def test_an_old_name_answers_for_the_new_one_and_then_moves(relay):
    relay["docs"].update({"ollama_num_ctx": "8192", "ollama_system_prompt": "Be brief.", "unrelated": "x"})
    S.apply_defaults({"llm_num_ctx": "auto", "llm_system_prompt": ""})
    S.hydrate_from_db(None)
    assert S.get("llm_num_ctx") == "8192" and S.get("llm_system_prompt") == "Be brief.", "the node read defaults"
    assert not S.exists("ollama_num_ctx"), "the old name is still in the cache"
    moved = asyncio.run(S.rename_settings(None))
    assert moved == 2
    assert relay["writes"] == [{"llm_num_ctx": "8192", "llm_system_prompt": "Be brief."}]
    assert sorted(relay["deletes"]) == sorted([S.store.NS_SETTING + "ollama_num_ctx", S.store.NS_SETTING + "ollama_system_prompt"])
    assert relay["docs"] == {"llm_num_ctx": "8192", "llm_system_prompt": "Be brief.", "unrelated": "x"}
    # a restart: nothing left to move
    S._CACHE.clear(); S._RENAMED_FROM_RELAY.clear(); relay["writes"].clear(); relay["deletes"].clear()
    S.hydrate_from_db(None)
    assert S.get("llm_num_ctx") == "8192" and asyncio.run(S.rename_settings(None)) == 0 and not relay["deletes"]


def test_a_new_name_already_held_wins_and_the_old_document_goes(relay):
    relay["docs"].update({"ollama_temperature": "0.9", "llm_temperature": "0.3"})
    S.hydrate_from_db(None)
    assert S.get("llm_temperature") == "0.3"
    assert asyncio.run(S.rename_settings(None)) == 0
    assert relay["writes"] == [] and relay["deletes"] == [S.store.NS_SETTING + "ollama_temperature"]
    assert relay["docs"] == {"llm_temperature": "0.3"}


@pytest.mark.parametrize("failure", ["short_write", "not_authoritative"])
def test_a_move_that_cannot_complete_changes_nothing(relay, failure):
    relay["docs"].update({"ollama_timeout": "600000"})
    S.hydrate_from_db(None)
    if failure == "short_write":
        relay["state"]["short"] = True
    else:
        relay["state"]["authoritative"] = False
    assert asyncio.run(S.rename_settings(None)) == 0
    assert relay["deletes"] == [], "the old document was deleted without its value landing under the new name"
    assert relay["docs"] == {"ollama_timeout": "600000"}
    assert S.get("llm_timeout") == "600000", "the value is still answered meanwhile"


def test_nothing_reads_an_old_name_and_the_move_runs_before_the_seeding():
    src = []
    for p in list((ROOT / "app").rglob("*.py")) + list((ROOT / "templates").rglob("*.html")) + list((ROOT / "static/js").rglob("*.js")):
        if p.name == "settings_store.py":
            continue
        t = p.read_text(errors="ignore")
        if "ollama" in t.lower():
            src.append(str(p.relative_to(ROOT)))
    assert src == [], ("the app still names Ollama", src)
    main = (ROOT / "app/main.py").read_text()
    assert main.index("rename_settings(_db)") < main.index("seed_relay_defaults(_db, DEFAULT_SETTINGS)"), \
        "the defaults would be written over a node's values before they moved"
    assert set(S._RENAMED.values()) <= set(re.findall(r"^    (llm_[a-z_]+):", (ROOT / "app/schemas.py").read_text(), re.M)), \
        "a renamed setting is not declared in SettingsResponse, so it would never hydrate in Admin"
