"""Admin → Bots → Edit: the list fields' SERVER half (app/services/relay_lists.py FIELD_KINDS + the
/api/admin/list-field/* endpoints).

A bot reads each of its lists with its own parser (botframework/*). The editor now draws those lists as
rows, so the one thing that must never happen is the editor showing a list split differently from how
the bot will read it -- a row the bot never sees, or two topics drawn as one. Each kind is therefore
checked against the bot's OWN function on the same text. Also pinned: an invite is compared EXACTLY
(the part after `#` is the room key; folding its case would merge two rooms), a row never carries the
key in what it displays, and the endpoints store nothing and refuse what the bot could not use.

The page half is tests/client/test_bot_list_fields_full_app.py.
"""
import asyncio
import re
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.services import relay_blocklist, relay_lists

ROOT = Path(__file__).resolve().parents[1]
BOTS = ROOT / "botframework"


def _bot(module):
    if str(BOTS) not in sys.path:
        sys.path.insert(0, str(BOTS))
    return __import__(module)


TOPICS = "gaming, comics\n  media double standards \n\nretro tech,anime"
INVITES = ("https://poster.place/c/naddr1aaa#KeyOne, https://poster.place/c/naddr1bbb#keytwo\n"
           "https://poster.place/c/naddr1bbb#KEYTWO")
HOSTS = "nas.lan, 192.168.0.85\nMedia.Example.com  [fd00::1]:8080"


def test_topics_split_exactly_like_the_bot_reads_them(monkeypatch):
    try:
        autopost = _bot("autopost")
    except Exception as e:  # noqa: BLE001
        pytest.skip("botframework autopost not importable: %s" % e)
    monkeypatch.setenv("AUTO_POST_TOPICS", TOPICS)
    assert relay_lists.entries("topic", TOPICS) == autopost._topics()


def test_invites_split_exactly_like_the_bot_and_case_is_meaning():
    concord = _bot("concord")
    want = concord.invites_from_env({"CONCORD_INVITE": INVITES})
    assert relay_lists.entries("invite", INVITES) == want
    assert len(want) == 3, "#keytwo and #KEYTWO are two different room keys"


def test_hosts_split_like_the_bot_reads_them():
    src = (BOTS / "config.py").read_text()
    line = next(l for l in src.splitlines() if l.startswith("TRUSTED_MEDIA_HOSTS"))
    assert 're.split(r"[,\\s]+"' in line, "the bot changed how it reads hosts: update relay_lists 'host'"
    bot = [h.strip().lower() for h in re.split(r"[,\s]+", HOSTS) if h.strip()]
    assert [relay_lists.same("host", e) for e in relay_lists.entries("host", HOSTS)] == bot


@pytest.mark.parametrize("kind,good,bad", [
    ("topic", ["media double standards", "anime"], ["a, b", "two\nlines", ""]),
    ("invite", ["https://poster.place/c/naddr1x#k"], ["https://poster.place/c/naddr1x", "naddr1x#k", "a b#c"]),
    ("host", ["nas.lan", "192.168.0.85", "media.example.com:8443", "[fd00::1]"], ["http://x y", "a b", "x/y"]),
])
def test_validation_refuses_what_the_bot_could_not_use(kind, good, bad):
    for g in good:
        assert relay_lists.validate(kind, g)[1] is None, (kind, g)
    for b in bad:
        assert relay_lists.validate(kind, b)[1], (kind, b)


def test_edit_adds_and_removes_one_entry_and_keeps_the_rest():
    v, err = relay_lists.edit(TOPICS, "topic", add="politics")
    assert not err and relay_lists.entries("topic", v)[-1] == "politics"
    v, err = relay_lists.edit(v, "topic", remove="COMICS")
    assert not err and "comics" not in v and "gaming" in v and "media double standards" in v
    v, err = relay_lists.edit(INVITES, "invite", remove="https://poster.place/c/naddr1bbb#keytwo")
    assert not err and "#KEYTWO" in v and "#keytwo" not in v, "removing one room took its case-twin too"
    assert relay_lists.edit(HOSTS, "host", add="NAS.lan")[1] == "already in the list"


def test_an_invite_row_never_displays_the_room_key():
    out = asyncio.run(relay_lists.rows_of("invite", INVITES))
    for r in out["items"]:
        assert "#" in r["value"]
        assert r["value"].split("#", 1)[1] not in r["shown"], r


def test_endpoints_answer_rows_and_edits_and_refuse_other_kinds(monkeypatch):
    from app.routers import admin

    async def profiles(pks):
        return {pk: {"name": "Somebody"} for pk in pks}, True
    monkeypatch.setattr(relay_blocklist, "profiles", profiles)
    from app.services.nostr import bip340, nostr_service
    pk = bip340.pubkey_from_seckey(b"\x09" * 32).hex()
    npub = nostr_service.npub_of(pk)
    rows = asyncio.run(admin.list_field_rows(admin.ListFieldReq(kind="pubkey", raw=npub + ", junk"), admin=None))
    assert [r["valid"] for r in rows["items"]] == [True, False]
    assert rows["items"][0]["name"] == "Somebody" and rows["items"][0]["pubkey"] == pk
    out = admin.list_field_apply(admin.ListFieldReq(kind="topic", raw=TOPICS, add="news"), admin=None)
    assert out["value"].endswith("news")
    with pytest.raises(HTTPException) as e:
        admin.list_field_apply(admin.ListFieldReq(kind="topic", raw=TOPICS, add="a, b"), admin=None)
    assert e.value.status_code == 400
    for kind in ("peer", "fedi", "nonsense"):          # not a field kind: no route into settings-only rules
        with pytest.raises(HTTPException):
            admin.list_field_apply(admin.ListFieldReq(kind=kind, raw=""), admin=None)
