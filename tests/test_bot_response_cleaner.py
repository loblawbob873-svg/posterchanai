"""THE LAST THING BETWEEN RAW MODEL OUTPUT AND A PUBLIC POST.

`botframework/ai/response_cleaner.py` had ZERO test references across 428 lines. Every reply the
fediverse and Nostr bots make goes through `clean_ai_response` on its way to a timeline, under
somebody's name, where it cannot be taken back.

The stakes are not "the post reads badly". They are:

  * **A `<think>` block reaching a timeline publishes the model's private reasoning** — which is
    where it discusses the persona, the prompt, and what it has decided about the person it is
    replying to. Reasoning models emit these constantly and there is no second filter.
  * **An apology reaching a timeline is the bot announcing its own failure**, repeatedly, to
    everyone. `clean_ai_response` returns None for those so nothing is posted at all, and that
    None is load-bearing: a caller that got "" would post an empty status instead.

`remove_think_tags` handles four shapes and only one of them is the tidy case. The other three are
what a streamed or truncated generation actually looks like: an opener with no closer, a closer with
no opener, and several blocks in one response. Each is tested by name, because a regression that
handled only the tidy case would look like it worked in every manual check.

The bot framework is imported the way tests/test_game_mentions.py does it, and skips rather than
fails where it will not import — it is a separate program with its own dependencies.
"""
import sys
from pathlib import Path

import pytest

BF = Path(__file__).resolve().parents[1] / "botframework"
if str(BF) not in sys.path:
    sys.path.insert(0, str(BF))

try:
    from ai.response_cleaner import (
        clean_ai_response,
        remove_preambles,
        remove_think_tags,
        strip_emojis,
    )
except Exception as e:                                     # pragma: no cover - environment guard
    pytest.skip(f"botframework.ai will not import here: {e}", allow_module_level=True)


# --------------------------------------------------------------------------- private reasoning


def test_a_closed_think_block_is_removed():
    assert remove_think_tags("<think>secret reasoning</think>Hello world") == "Hello world"


def test_an_unclosed_think_block_takes_everything_with_it():
    """A truncated or still-streaming generation. There is no answer yet — only reasoning — so the
    right result is nothing. Keeping the tail would publish the reasoning verbatim."""
    assert remove_think_tags("<think>secret reasoning that never closed") == ""


def test_a_closer_with_no_opener_keeps_only_what_follows():
    """The commonest real shape: the model starts reasoning immediately, without an opening tag,
    then closes it and answers. Everything BEFORE `</think>` is the private half."""
    got = remove_think_tags("secret reasoning</think>Hello world")
    assert got == "Hello world"
    assert "secret" not in got


def test_several_blocks_are_all_removed():
    got = remove_think_tags("<think>a</think>Hi<think>b</think>there")
    assert "a" not in got.replace("Hi", "").replace("there", "")
    assert "Hi" in got and "there" in got


def test_the_tags_are_case_insensitive():
    """Models emit `<THINK>` and `<Think>`. A case-sensitive strip would publish those."""
    assert remove_think_tags("<THINK>secret</THINK>Hello") == "Hello"
    assert remove_think_tags("<Thinking>secret</Thinking>Hello") == "Hello"


def test_the_thinking_spelling_is_handled_too():
    assert remove_think_tags("<thinking>secret</thinking>Hello") == "Hello"


def test_a_multiline_block_is_removed_whole():
    """DOTALL matters — reasoning is always multiple lines, so a strip that stopped at the first
    newline would leave most of it."""
    got = remove_think_tags("<think>\nline one\nline two\n</think>\nHello")
    assert got == "Hello"


@pytest.mark.parametrize("shape", [
    "<think>reasoning</think>answer",
    "<think>reasoning",
    "reasoning</think>answer",
    "<THINK>reasoning</THINK>answer",
    "<thinking>reasoning</thinking>answer",
    "<think>\nreasoning\n</think>\nanswer",
    "prefix<think>reasoning</think>answer",
])
def test_no_shape_of_think_block_ever_survives(shape):
    """THE SWEEP, and the one that matters. Whatever else changes, the word "reasoning" must not
    reach a timeline in any of the shapes a model actually produces."""
    assert "reasoning" not in remove_think_tags(shape)


def test_ordinary_text_is_untouched():
    """The other direction. Over-eager stripping silently truncates real posts."""
    assert remove_think_tags("Just a normal post") == "Just a normal post"
    assert remove_think_tags("I was thinking about it") == "I was thinking about it"


def test_the_leaked_control_token_is_stripped():
    """The model is fed `/no_think` to suppress reasoning and sometimes echoes it as literal text
    instead of obeying it. Posted, it reads as gibberish in the middle of a sentence."""
    assert "no_think" not in remove_think_tags("Hey no_think what's up")
    assert "no_think" not in remove_think_tags("Hey /no_think what's up")
    assert "no_think" not in remove_think_tags("Hey @no_think what's up")


def test_the_control_token_strip_tidies_the_space_it_leaves():
    assert remove_think_tags("Hello no_think, friend") == "Hello, friend"


def test_the_control_token_strip_also_eats_the_phrase_no_think():
    """MEASURED, AND NOT ENDORSED. `\\bno[ _]?think\\b` matches the ordinary English "no think", so
    "There is no think tank here" becomes "There is tank here" — a mangled public post.

    Recorded rather than fixed because the fix is a judgement call about bot output, not an
    unambiguous defect: requiring the `/` or `@` prefix (or the underscore form) would keep the
    documented case and drop this one, at the cost of missing a bare-spaced echo if models produce
    those. This test exists so the trade-off is visible and a change to it is deliberate."""
    assert remove_think_tags("There is no think tank here") == "There is tank here"


# --------------------------------------------------------------------------- refusing to post


@pytest.mark.parametrize("text", [
    "I apologize, I wasn't able to generate a proper response. Please try again.",
    "I apologize, I wasn't able to generate a proper response.",
])
def test_a_generation_failure_is_never_posted(text):
    """It returns None so the caller posts NOTHING. The alternative is the bot publicly announcing
    its own failure, to everyone, every time the model has a bad minute."""
    assert clean_ai_response(text) is None


@pytest.mark.parametrize("empty", ["", "   ", "\n\n", "\t"])
def test_empty_input_is_none_not_an_empty_string(empty):
    """Callers branch on truthiness. An empty string would post a blank status."""
    assert clean_ai_response(empty) is None


def test_a_real_post_survives_the_pipeline():
    """The whole thing is a filter, so the test that it does not eat ordinary content is as
    important as any removal test."""
    assert clean_ai_response("A real post about coffee.") == "A real post about coffee."


def test_a_preamble_is_removed_but_the_post_is_kept():
    got = clean_ai_response("Here is your social media post: Actual content")
    assert got == "Actual content"


def test_the_pipeline_removes_think_tags():
    got = clean_ai_response("<think>my private reasoning</think>The actual reply")
    assert got == "The actual reply"
    assert "private" not in got


def test_debug_mode_returns_the_raw_text():
    """It is the only way to see what the model really said. If it ever started cleaning, the one
    tool for diagnosing a cleaning bug would be the thing hiding it."""
    raw = "<think>x</think>real"
    assert clean_ai_response(raw, debug_mode=True) == raw


def test_debug_mode_still_refuses_nothing_at_all():
    assert clean_ai_response("", debug_mode=True) is None


# --------------------------------------------------------------------------- preambles / emoji


@pytest.mark.parametrize("preamble", [
    "Here is your post: ",
    "Here's the post: ",
    "Here is the social media post: ",
])
def test_common_preambles_are_removed(preamble):
    """A bot that opens every reply with "Here is your post:" is instantly recognisable as one."""
    got = remove_preambles(preamble + "Actual content")
    assert got.strip() == "Actual content"


def test_a_post_that_merely_starts_with_here_is_kept():
    """"Here is the thing I meant" is a sentence, not a preamble — the patterns are anchored on the
    preamble shape for this reason."""
    text = "Here is the coffee shop I mentioned yesterday."
    assert remove_preambles(text) == text


def test_emojis_are_stripped_when_asked():
    assert strip_emojis("Hello 😀 world 🎉") .strip() == "Hello  world"


def test_stripping_emojis_keeps_ordinary_punctuation_and_accents():
    """The ranges must not reach into ordinary text — an over-broad class would quietly delete
    accented characters from every reply in half the languages the bots speak."""
    text = "Café: c'est déjà 100% — naïve, non?"
    assert strip_emojis(text) == text
