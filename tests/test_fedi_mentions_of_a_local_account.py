"""A LOCAL ACCOUNT MENTIONED IN QUALIFIED FORM MUST STILL BECOME A PROFILE LINK.

Reported as "fedi bridge broken, usernames are not displaying like nostr". Measured on the live
relay: a mirrored note carrying `@ChristiJunior@detroitriotcity.com` verbatim, while the same
account written bare rewrote correctly.

Pleroma reports a LOCAL account's `acct` WITHOUT a host ("ChristiJunior"), but a note federated in
from another instance renders the mention fully qualified. So:

  * the `acct` replacement looks for "@ChristiJunior" and the text says
    "@ChristiJunior@detroitriotcity.com" -- the negative lookahead `(?![A-Za-z0-9_.\\-@])` refuses it,
    correctly, because that same guard is what stops "@ann" eating "@anna_x";
  * the BARE fallback is refused by the same lookahead, for the same reason.

The p-tag was added either way, so the person WAS notified and only the link was missing -- which is
precisely why this survived: nothing looked broken except the rendering.

The rewriter is RUN here against a stubbed puppet provisioner, because the bug is in a regular
expression and the interesting cases are all about what it does and does not match.
"""
import asyncio

import pytest

from app.services import fedi_nostr_bridge_service as br

HOST = "detroitriotcity.com"
NPUB = "npub1" + "q" * 58
LOCAL = [{"acct": "ChristiJunior", "username": "ChristiJunior",
          "url": f"https://{HOST}/users/ChristiJunior"}]
REMOTE = [{"acct": "D00B@clew.lol", "username": "D00B", "url": "https://clew.lol/users/D00B"}]


@pytest.fixture
def rewrite(monkeypatch):
    async def puppet(db, port, account, instance_host="", profile_refresh=True):
        return {"pubkey_hex": "ab" * 32, "npub": NPUB}
    monkeypatch.setattr(br.ident, "ensure_puppet", puppet)
    monkeypatch.setattr(br, "_linked_actor_pubkeys", lambda db: _empty())
    monkeypatch.setattr(br, "_author_muted", lambda *a, **k: False)

    async def _go(text, mentions):
        return await br._rewrite_mentions(None, 3052, HOST, text, mentions, frozenset())
    return lambda text, mentions=LOCAL: asyncio.run(_go(text, mentions))


async def _empty():
    return {}


def test_the_qualified_form_of_a_local_account_is_linked(rewrite):
    out, ptags = rewrite("@ChristiJunior@detroitriotcity.com hello there")
    assert out.startswith("nostr:" + NPUB), out
    assert HOST not in out, f"the host was left dangling after the reference: {out}"
    assert ptags


def test_the_bare_form_still_works(rewrite):
    out, _ = rewrite("@ChristiJunior hello there")
    assert out == f"nostr:{NPUB} hello there", out


def test_both_forms_in_one_note(rewrite):
    out, _ = rewrite("@ChristiJunior@detroitriotcity.com and @ChristiJunior again")
    assert out.count(f"nostr:{NPUB}") == 2, out
    assert "@ChristiJunior" not in out, out


def test_a_remote_account_is_unaffected(rewrite):
    out, _ = rewrite("@D00B@clew.lol hi", REMOTE)
    assert out == f"nostr:{NPUB} hi", out


def test_the_guard_that_made_this_necessary_is_intact(rewrite):
    """`@ann` must never eat `@anna_x` -- the reason the lookahead exists at all."""
    ms = [{"acct": "ann", "username": "ann", "url": f"https://{HOST}/users/ann"}]
    out, _ = rewrite("@anna_x is not @ann", ms)
    assert "@anna_x" in out, out
    assert out.endswith("nostr:" + NPUB), out


def test_a_lookalike_host_is_not_swallowed(rewrite):
    """The qualified replacement is boundary-guarded too: `@bob@host` must not match inside
    `@bob@host.evil`, which would mint a reference no client can resolve."""
    ms = [{"acct": "bob", "username": "bob", "url": f"https://{HOST}/users/bob"}]
    out, _ = rewrite(f"@bob@{HOST}.evil is somebody else", ms)
    assert f"@bob@{HOST}.evil" in out, out
