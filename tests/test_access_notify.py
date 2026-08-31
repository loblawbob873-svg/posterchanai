"""THE DM THAT TELLS SOMEBODY THEIR ACCESS REQUEST WAS APPROVED.

`access_notify_service.py` had ZERO test references. Access here is request-then-approve: somebody
asks, an admin ticks a box later, and this is the only thing that tells them. If it silently does
nothing, the user finds out by trying again — on the one interaction whose answer is "yes".

It is best-effort BY CONSTRUCTION: every path swallows its errors, because a grant must never fail
because a DM could not be wrapped. That is correct and it is exactly why the layer needs tests — the
caller cannot tell the difference between "sent" and "quietly gave up", and neither can the user.

The rule with the most user-visible consequence is the combining one: granting AI and Blossom at
once sends ONE message, not two notifications a second apart.

A NOTE ON WHO SENDS IT. The comment above the `system_dm.send` call in the source says it uses
"system_dm (a distinct sender), NOT the operator key", and CLAUDE.md says the same ("all via
`system_dm` — never the operator key"). MEASURED, `system_dm.send` signs with
`keystore.get_operator_nsec()` — it IS the operator key, and `system_dm`'s own module docstring says
so and argues the resulting self-DM is the intent. The two statements cannot both be true. These
tests pin the mechanism that exists (delegation to `system_dm`) and deliberately do NOT assert which
key is right, because that is a product decision rather than something a test should settle.
"""
import asyncio

import pytest

from app.services import access_notify_service as an


def run(coro):
    return asyncio.run(coro)


class User:
    def __init__(self, npub="npub1alice"):
        self.nostr_npub = npub


@pytest.fixture
def sent(monkeypatch):
    """Captures the DM instead of publishing it."""
    out = []

    async def _send(recipient, text):
        out.append({"to": recipient, "text": text})
        return True

    from app.services import system_dm, settings_store
    monkeypatch.setattr(system_dm, "send", _send)
    monkeypatch.setattr(settings_store, "get", lambda key, default="": "poster.place")
    return out


# --------------------------------------------------------------------------- it sends


def test_a_grant_is_announced(sent):
    assert run(an.notify_access_granted(None, User(), "ai")) is True
    assert len(sent) == 1
    assert sent[0]["to"] == "npub1alice"


def test_the_message_says_what_was_granted(sent):
    run(an.notify_access_granted(None, User(), "ai"))
    assert "AI access" in sent[0]["text"]


def test_the_site_name_is_used(sent):
    """A DM from an unnamed server is a DM from nobody — the recipient may hold accounts on several."""
    run(an.notify_access_granted(None, User(), "ai"))
    assert "poster.place" in sent[0]["text"]


def test_an_unnamed_site_still_reads_as_a_sentence(sent, monkeypatch):
    from app.services import settings_store
    monkeypatch.setattr(settings_store, "get", lambda key, default="": "")
    run(an.notify_access_granted(None, User(), "ai"))
    assert "this server" in sent[0]["text"]
    assert "{site}" not in sent[0]["text"], "the template placeholder reached the user"


@pytest.mark.parametrize("kind", sorted(an._MSG))
def test_every_advertised_kind_produces_its_own_message(sent, kind):
    """A kind in `_MSG` that no longer formats is a grant that notifies nobody. Swept over the real
    table, so a fourth capability added later is covered by being added there."""
    del sent[:]
    assert run(an.notify_access_granted(None, User(), kind)) is True
    assert sent[0]["text"].strip()
    assert "{" not in sent[0]["text"], "an unformatted placeholder reached the user"


def test_the_messages_are_distinct(sent):
    """Three grants that all say the same thing would make the notification useless."""
    texts = set()
    for kind in an._MSG:
        del sent[:]
        run(an.notify_access_granted(None, User(), kind))
        texts.add(sent[0]["text"])
    assert len(texts) == len(an._MSG)


# --------------------------------------------------------------------------- combining


def test_granting_two_things_sends_one_message(sent):
    """Stated in the docstring: "granting both at once sends ONE message, not two notifications a
    second apart". Two DMs a second apart is what makes a helpful notification feel like spam."""
    assert run(an.notify_access_granted(None, User(), ["ai", "blossom"])) is True
    assert len(sent) == 1


def test_the_combined_message_mentions_both(sent):
    run(an.notify_access_granted(None, User(), ["ai", "blossom"]))
    assert "AI access" in sent[0]["text"] and "upload access" in sent[0]["text"]


def test_a_bare_string_is_accepted_as_well_as_a_list(sent):
    """Both call shapes are live. Iterating a string would send one DM per CHARACTER."""
    run(an.notify_access_granted(None, User(), "ai"))
    assert len(sent) == 1 and "AI access" in sent[0]["text"]


# --------------------------------------------------------------------------- refusals


def test_an_unknown_kind_sends_nothing(sent):
    """A typo'd capability must not produce an empty DM — the filter is what stops `_MSG[k]` raising
    a KeyError into a swallow, which would be indistinguishable from a relay failure."""
    assert run(an.notify_access_granted(None, User(), "teleportation")) is False
    assert sent == []


def test_unknown_kinds_are_filtered_without_losing_the_known_ones(sent):
    assert run(an.notify_access_granted(None, User(), ["ai", "teleportation"])) is True
    assert len(sent) == 1 and "AI access" in sent[0]["text"]


@pytest.mark.parametrize("kinds", [None, [], "", ["nope"]])
def test_nothing_to_announce_sends_nothing(sent, kinds):
    assert run(an.notify_access_granted(None, User(), kinds)) is False
    assert sent == []


@pytest.mark.parametrize("recipient", [User(npub=""), User(npub=None), "", "   "])
def test_no_pubkey_sends_nothing(sent, recipient):
    """There is nobody to DM. This is the ordinary case for a whitelist entry that is a bare pubkey
    with no account, so it must be quiet rather than an error."""
    assert run(an.notify_access_granted(None, recipient, "ai")) is False
    assert sent == []


def test_a_bare_pubkey_is_a_valid_recipient(sent):
    """"the Blossom whitelist works on pubkeys, and creating a User row purely so we had something
    to read an npub off would be a real side effect (an account appearing)"."""
    assert run(an.notify_access_granted(None, "npub1bob", "blossom")) is True
    assert sent[0]["to"] == "npub1bob"


def test_a_pubkey_is_trimmed(sent):
    run(an.notify_access_granted(None, "  npub1bob  ", "ai"))
    assert sent[0]["to"] == "npub1bob"


# --------------------------------------------------------------------------- never breaks the grant


def test_a_refused_dm_is_reported_as_false(sent, monkeypatch):
    from app.services import system_dm

    async def _refuse(recipient, text):
        return False

    monkeypatch.setattr(system_dm, "send", _refuse)
    assert run(an.notify_access_granted(None, User(), "ai")) is False


def test_an_exception_never_reaches_the_grant(monkeypatch):
    """"A failed notification must never break the grant that triggered it." The caller has already
    written the permission to the database; raising here would surface as a failed grant that in
    fact succeeded, and the admin would tick the box again."""
    from app.services import system_dm

    async def _boom(recipient, text):
        raise OSError("relay unreachable")

    monkeypatch.setattr(system_dm, "send", _boom)
    assert run(an.notify_access_granted(None, User(), "ai")) is False


def test_the_blocking_wrapper_never_raises(monkeypatch):
    from app.services import system_dm

    async def _boom(recipient, text):
        raise OSError("relay unreachable")

    monkeypatch.setattr(system_dm, "send", _boom)
    an.notify_access_granted_blocking(None, User(), "ai")      # must not raise


def test_the_blocking_wrapper_actually_sends(sent):
    an.notify_access_granted_blocking(None, User(), "ai")
    assert len(sent) == 1


def test_every_blocking_caller_is_a_synchronous_route():
    """`notify_access_granted_blocking` drives its coroutine with a bare `asyncio.run`, which raises
    inside a running event loop — its own docstring says it is safe "precisely because that thread
    has none of its own". That is a property of the CALLERS.

    Make one of those admin routes `async def` and the call raises, the wrapper swallows it, and
    nobody is ever told their access was granted. The grant still works, so nothing looks broken."""
    import ast
    import pathlib
    root = pathlib.Path(an.__file__).resolve().parents[2]
    offenders = []
    for path in sorted((root / "app").rglob("*.py")):
        if path.name == "access_notify_service.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        for fn in ast.walk(tree):
            if isinstance(fn, ast.AsyncFunctionDef) and \
                    "notify_access_granted_blocking" in ast.unparse(fn):
                offenders.append(f"{path.relative_to(root)}: async def {fn.name}")
    assert offenders == [], (
        "asyncio.run raises inside a running loop and the wrapper swallows it — these would stop "
        "telling users their access was granted, silently:\n" + "\n".join(offenders))


# --------------------------------------------------------------------------- delivery route


def test_it_delegates_to_system_dm_rather_than_wrapping_its_own(sent, monkeypatch):
    """One place builds a server→user DM. A second copy here would drift on the parts that are easy
    to get wrong — the local relay port, the NIP-17 wrap, the never-raises contract — and would do
    it on the notification nobody watches."""
    import pathlib
    src = pathlib.Path(an.__file__).read_text(encoding="utf-8")
    assert "system_dm.send" in src
    assert "nip17" not in src, "this module builds its own DM instead of using system_dm"
    assert "get_operator_nsec" not in src
