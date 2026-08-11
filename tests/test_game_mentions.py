"""A game bot's result post must MENTION the players, not print their handles.

Run: venv-unified/bin/python -m pytest tests/test_game_mentions.py

Reported as "games not tagging users right", with a real post:

    🏁 #holdem hand over — @npub1mq3s439… wins 80.

A bare `@handle` in a kind-1 notifies the person — the p-tag does that — and renders as plain text
everywhere: no name, no link to the profile. NIP-27 wants the npub in the CONTENT, beside the p-tag.
And that handle is TRUNCATED, so it could not be resolved even by hand: `_name()` falls back to a
shortened npub when the player has no username.

Two separate faults produced it:

  * hold'em and blackjack DID rewrite the handles — and then the image branch rebuilt `content` from
    the ORIGINAL body, silently throwing the rewrite away. Every result post carries a board image, so
    the rewrite never survived to a single published event.
  * chess, connect 4, hangman and tic-tac-toe never rewrote at all.

Six copies of one idea, four of them missing. It is one helper now, and this pins both halves.
"""
import re
from pathlib import Path

import pytest

BF = Path(__file__).resolve().parents[1] / "botframework"
GAMES = ["holdem", "blackjack", "chess", "connect4", "hangman", "ttt"]


def src(game):
    return (BF / f"{game}Listener.py").read_text(encoding="utf-8")


@pytest.mark.parametrize("game", GAMES)
def test_every_game_makes_real_mentions(game):
    assert "_nk.mentionify(" in src(game), (
        f"{game} publishes bare @handles — they notify via the p-tag and render as plain text")


@pytest.mark.parametrize("game", ["holdem", "blackjack"])
def test_the_image_branch_does_not_discard_the_rewrite(game):
    """`content = f"{body}\\n{url}"` rebuilds from the ORIGINAL string. Every result post has a board
    image, so this one line is why the rewrite never reached a published event."""
    body = src(game)
    assert 'content = f"{body}\\n{url}"' not in body, (
        f"{game} rebuilds content from `body` after the mention rewrite, discarding it")
    assert 'content = f"{content}\\n{url}"' in body


@pytest.mark.parametrize("game", GAMES)
def test_the_mention_list_is_a_real_variable(game):
    """The four that were missing it had no `players` in scope — passing one would have been a
    NameError at publish time, which is the failure mode this whole file exists for."""
    body = src(game)
    m = re.search(r"_nk\.mentionify\([^,]+,\s*([^,]+),", body)
    assert m, f"could not find {game}'s mentionify call"
    arg = m.group(1).strip()
    fn = body[: m.start()].rsplit("\ndef ", 1)[-1]
    names = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", arg)
    sig = fn.split(")")[0]
    for n in names:
        assert n in sig or f"{n} =" in fn, (
            f"{game} passes `{arg}` to mentionify but `{n}` is not in scope in that function")


def test_the_helper_is_shared_not_copied():
    """Two copies drifted and four were missing; a third would be the same afternoon again."""
    helper = (BF / "nostr.py").read_text(encoding="utf-8")
    assert "def mentionify(" in helper
    for game in GAMES:
        assert 'content.replace(_name(pk)' not in src(game), (
            f"{game} has grown its own copy of the rewrite again")


def test_the_helper_does_not_double_wrap_an_existing_mention():
    """Running twice (or over a body that already names someone properly) must be a no-op."""
    import sys

    sys.path.insert(0, str(BF))
    try:
        import nostr as N
    except Exception as e:                                       # pragma: no cover
        pytest.skip(f"botframework.nostr will not import here: {e}")
    if not hasattr(N, "_svc") or N._svc is None:
        pytest.skip("no nostr service bound in this environment")

    npub = N._svc.npub_of("aa" * 32)
    out = N.mentionify(f"nostr:{npub} wins", ["aa" * 32], lambda pk: "@someone")
    assert out.count("nostr:") == 1
