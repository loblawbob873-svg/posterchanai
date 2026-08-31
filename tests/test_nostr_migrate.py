"""THE SETTINGS READ PATH, AND A DESTRUCTIVE FUNCTION NOTHING CALLS.

`nostr_migrate.py` had ZERO test references. Two of its three functions are the read path
`settings_store` hydrates every node from (`settings_all`) and `settings_backup` exports from. The
third deletes things.

`purge_app_docs(port, seckey, prefix="pcai:")` removes every operator-signed doc under a prefix, and
that default matches EVERYTHING the operator key signs: `pcai:setting:*` — every setting on the node
— plus the uptime history, the paid-retention ledger and every bot config. Its docstring calls it
"the 'delete AI notes' action for testing / re-running the migration".

IT HAS NO CALLERS. Not a router, not a script, not the admin panel — `admin.py` even carries a
comment noting that `settings_all()` "lives on", i.e. the rest of this module's migration surface
was retired around it.

Dead destructive code is not harmless: it is a ready-made mistake with a plausible name and a
default argument that hits the widest possible blast radius. So it is covered the way this repo
already covers the dead files full of native dialogs — a TRIPWIRE. `test_the_purge_is_still
_unreachable` fails the moment anything calls it, at which point somebody has to decide whether that
call really means "delete every setting on this node".

The behaviour tests still run it, against a fake store, because if it is ever wired up the thing
that matters is that it deletes what it was asked to and reports honestly how much went.
"""
import asyncio

import pytest

from app.services import nostr_migrate as mig
from app.services import nostr_store as store


def run(coro):
    return asyncio.run(coro)


class FakeStore:
    """Stands in for the relay. Records deletes so "did not delete" is a positive observation."""

    def __init__(self, docs=None, fail=(), read_error=None):
        self.docs = dict(docs or {})
        self.fail = set(fail)
        self.read_error = read_error
        self.deleted = []
        self.listed = []

    async def get_doc(self, port, d_tag, *, seckey=None, **kw):
        return self.docs.get(d_tag)

    async def list_docs(self, port, prefix, *, seckey=None, encrypt=True, **kw):
        self.listed.append({"prefix": prefix, "encrypt": encrypt})
        if self.read_error:
            raise self.read_error
        return {k: v for k, v in self.docs.items() if k.startswith(prefix)}

    async def delete_doc(self, port, seckey, d_tag, **kw):
        if d_tag in self.fail:
            raise OSError("relay refused the delete")
        self.deleted.append(d_tag)
        return True


@pytest.fixture
def fake(monkeypatch):
    def _install(**kw):
        f = FakeStore(**kw)
        monkeypatch.setattr(mig.store, "get_doc", f.get_doc)
        monkeypatch.setattr(mig.store, "list_docs", f.list_docs)
        monkeypatch.setattr(mig.store, "delete_doc", f.delete_doc)
        return f
    return _install


SK = b"\x01" * 32


# --------------------------------------------------------------------------- setting_get


def test_a_setting_is_read_out_of_its_doc(fake):
    fake(docs={store.NS_SETTING + "llm_model": {"value": "qwen"}})
    assert run(mig.setting_get(3052, SK, "llm_model")) == "qwen"


def test_a_missing_setting_returns_the_default(fake):
    fake(docs={})
    assert run(mig.setting_get(3052, SK, "nope", default="fallback")) == "fallback"


def test_a_missing_setting_defaults_to_none_when_no_default_is_given(fake):
    fake(docs={})
    assert run(mig.setting_get(3052, SK, "nope")) is None


@pytest.mark.parametrize("doc", [None, {}, {"val": "x"}, "a string", 7, []])
def test_a_malformed_doc_falls_back_rather_than_returning_junk(fake, doc):
    """A doc written by an older build, or a partial read, must not become the setting's value —
    that value then drives behaviour node-wide, and a dict where a string was expected fails
    somewhere far away from here."""
    fake(docs={store.NS_SETTING + "k": doc})
    assert run(mig.setting_get(3052, SK, "k", default="safe")) == "safe"


def test_a_falsy_stored_value_is_returned_and_not_mistaken_for_missing(fake):
    """`""`, `0` and `False` are real settings values — a switch that is OFF. Treating them as
    absent would silently substitute the default, which for a boolean is usually the opposite."""
    for stored in ("", 0, False, []):
        fake(docs={store.NS_SETTING + "k": {"value": stored}})
        assert run(mig.setting_get(3052, SK, "k", default="DEFAULT")) == stored


def test_the_setting_namespace_is_the_shared_constant(fake):
    """It must be the same prefix the writer uses. Two spellings and every setting reads as unset,
    with the stored ones intact and invisible."""
    f = fake(docs={store.NS_SETTING + "k": {"value": "v"}})
    assert run(mig.setting_get(3052, SK, "k")) == "v"
    assert store.NS_SETTING == "pcai:setting:"


# --------------------------------------------------------------------------- settings_all


def test_every_setting_comes_back_with_the_namespace_stripped(fake):
    """The keys are what `settings_store` hydrates by name. Leaving the prefix on would make every
    setting unfindable while the relay still held all of them."""
    fake(docs={
        store.NS_SETTING + "llm_model": {"value": "qwen"},
        store.NS_SETTING + "relay_port": {"value": "3052"},
    })
    assert run(mig.settings_all(3052, SK)) == {"llm_model": "qwen", "relay_port": "3052"}


def test_a_bare_value_is_accepted_as_well_as_a_wrapped_one(fake):
    """Older docs stored the value directly instead of under `value`. Dropping them would blank
    those settings on the next hydrate."""
    fake(docs={store.NS_SETTING + "old": "plain", store.NS_SETTING + "new": {"value": "wrapped"}})
    assert run(mig.settings_all(3052, SK)) == {"old": "plain", "new": "wrapped"}


def test_it_only_returns_settings(fake):
    """The relay holds every operator doc. A prefix that leaked would hand `settings_store` the
    uptime history as if it were configuration."""
    fake(docs={store.NS_SETTING + "k": {"value": "v"},
               "pcai:kv:uptime": {"monitors": {}},
               "pcai:bot:alice": {"enabled": True}})
    assert set(run(mig.settings_all(3052, SK))) == {"k"}


def test_no_settings_is_an_empty_dict(fake):
    fake(docs={})
    assert run(mig.settings_all(3052, SK)) == {}


def test_a_settings_key_containing_a_colon_survives_the_strip(fake):
    """Only the leading namespace comes off. Stripping on every colon would truncate a key."""
    fake(docs={store.NS_SETTING + "a:b:c": {"value": "v"}})
    assert run(mig.settings_all(3052, SK)) == {"a:b:c": "v"}


# --------------------------------------------------------------------------- the purge


def test_the_purge_is_still_unreachable():
    """THE TRIPWIRE, and the reason this file exists.

    `purge_app_docs` deletes every operator-signed doc under its prefix, and its DEFAULT prefix
    `pcai:` is everything the operator key signs: every setting on the node, the uptime history, the
    paid-retention ledger, every bot config. Nothing calls it — not a router, not a script, not the
    admin panel.

    That is what makes it safe today, and it is a property of the rest of the codebase rather than
    of the function, so it needs saying out loud. If this fails, somebody has wired up a
    plausible-sounding helper whose default argument has the widest blast radius available; the
    question to answer before making it pass is whether that call really means "delete every setting
    on this node"."""
    import pathlib
    root = pathlib.Path(mig.__file__).resolve().parents[2]
    callers = []
    for path in list((root / "app").rglob("*.py")) + list((root / "scripts").rglob("*.py")):
        if path.name == "nostr_migrate.py":
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if "purge_app_docs" in stripped and not stripped.startswith("#"):
                callers.append(f"{path.relative_to(root)}:{lineno}: {stripped}")
    assert callers == [], (
        "purge_app_docs now has a caller. Its default prefix deletes EVERY operator-signed doc on "
        "the node — settings, uptime history, the paid-retention ledger, bot configs:\n"
        + "\n".join(callers))


def test_the_purge_deletes_what_it_was_asked_to_and_nothing_else(fake):
    f = fake(docs={"pcai:setting:a": {}, "pcai:setting:b": {}, "pcai:bot:x": {}})
    assert run(mig.purge_app_docs(3052, SK, prefix="pcai:setting:")) == 2
    assert sorted(f.deleted) == ["pcai:setting:a", "pcai:setting:b"]


def test_the_default_prefix_really_is_everything(fake):
    """Stated so the tripwire above is not the only place this is written down. If this ever stops
    being true — a narrower default, an explicit-prefix requirement — that is a safety improvement
    and this test should be updated to say so."""
    f = fake(docs={"pcai:setting:a": {}, "pcai:kv:uptime": {}, "pcai:bot:x": {}})
    assert run(mig.purge_app_docs(3052, SK)) == 3
    assert len(f.deleted) == 3


def test_it_counts_what_actually_went_not_what_it_tried(fake):
    """The return value is the operator's only evidence. Counting attempts would report a clean
    purge over docs that are still there."""
    f = fake(docs={"pcai:setting:a": {}, "pcai:setting:b": {}}, fail={"pcai:setting:b"})
    assert run(mig.purge_app_docs(3052, SK, prefix="pcai:setting:")) == 1
    assert f.deleted == ["pcai:setting:a"]


def test_one_failed_delete_does_not_abandon_the_rest(fake):
    f = fake(docs={f"pcai:setting:{c}": {} for c in "abcd"}, fail={"pcai:setting:b"})
    assert run(mig.purge_app_docs(3052, SK, prefix="pcai:setting:")) == 3
    assert sorted(f.deleted) == ["pcai:setting:a", "pcai:setting:c", "pcai:setting:d"]


def test_it_lists_keys_without_asking_for_decryption(fake):
    """`encrypt=False` — it only needs the d-tags. Decrypting every doc to throw the body away
    would make a purge of a large relay slow enough to be interrupted half-done."""
    f = fake(docs={"pcai:setting:a": {}})
    run(mig.purge_app_docs(3052, SK, prefix="pcai:setting:"))
    assert f.listed[0]["encrypt"] is False


def test_purging_nothing_removes_nothing(fake):
    f = fake(docs={"pcai:setting:a": {}})
    assert run(mig.purge_app_docs(3052, SK, prefix="pcai:nothing:")) == 0
    assert f.deleted == []


def test_an_unreadable_relay_deletes_nothing(fake):
    """The read decides the delete list. If a failed listing came back empty this would report a
    successful purge of zero docs — harmless here, but the same read-then-write shape that has
    wiped a replaceable doc elsewhere in this codebase. It must not become "delete everything"."""
    f = fake(docs={"pcai:setting:a": {}}, read_error=OSError("relay unreachable"))
    with pytest.raises(OSError):
        run(mig.purge_app_docs(3052, SK, prefix="pcai:setting:"))
    assert f.deleted == []
