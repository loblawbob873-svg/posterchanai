"""No blocked-word term may match every event — the relay's own list is the fixture.

THIS IS THE TEST THAT WAS MISSING, and its absence caused an outage on 2026-09-09.

`blocked_word` decides what the relay ACCEPTS and, through `delete_by_words`, what it DELETES. A
term that matches every string therefore stops the timeline and starts removing history. Separator
folding was added to catch `zone_presence` and `zone presence` with one entry; the operator's real
list contains `--------------`, which folded to the EMPTY STRING, and `"" in content` is true for
every event ever published. Reported within the hour as "i am not seeing new posts, just reposts".

The tests written alongside that change all used the ONE example word from the bug report. Not one
of them fed the predicate a realistic operator list — which was sitting in the settings store, had
already been printed during the investigation, and contains punctuation-only entries. So the fixture
here is deliberately a real-shaped block list, and the assertion is the property that matters:
ordinary content must survive it.
"""
import pytest

from app.services.nostr_relay.langfilter import blocked_word, screen_blocked_words

# Shaped after the list this node actually runs: hashtags, domains, a URL, plain words, an
# invisible-ish entry, and two that are pure punctuation.
REAL_SHAPED = {
    "#gaza", "#csam", "#bots", "t.me", "rwatimes.io", "otherstuff.ai", "sylunara",
    "zone_presence", "channel:__roster", "token-diligence", "block height", "previous hashes:",
    "https://theboard.world", "--------------", "✄-", "sp_4c", "wordle",
}

# Content that must never be refused by a content filter.
INNOCENT = [
    "good morning everyone",
    "just shipped a new release, feedback welcome",
    "",
    "gm",
    "a photo of my dog https://example.com/dog.jpg",
    "reply: I agree with this",
    "-",
    "...",
    "The quick brown fox jumps over the lazy dog.",
]


@pytest.mark.parametrize("content", INNOCENT)
def test_ordinary_content_survives_the_real_block_list(content):
    hit = blocked_word(content, REAL_SHAPED)
    assert hit is None, f"{hit!r} matched ordinary content {content!r} — this is a relay outage"


def test_every_single_term_is_checked_alone():
    """One universal term poisons the whole set, so each is asked on its own: a set-level pass can
    hide which entry is the problem, and the log line has to be able to name it."""
    for term in REAL_SHAPED:
        for content in INNOCENT:
            hit = blocked_word(content, {term})
            assert hit is None, f"term {term!r} matches ordinary content {content!r}"


def test_a_degenerate_term_is_refused_where_the_list_is_read():
    """The structural guard, not the matcher: screening happens once, where the setting is read, so
    one bad line costs that line rather than the relay."""
    usable, refused = screen_blocked_words({"zone_presence", "", "   ", "\t"})
    assert "zone_presence" in usable
    assert len(refused) == 3, refused
    for content in INNOCENT:
        assert blocked_word(content, usable) is None


def test_the_screen_would_have_caught_the_folding_regression():
    """Reproduce the exact defect: a transformation that maps a real entry to the empty string."""
    # The matcher's own `if w` already skips an EMPTY entry, which is why an empty line in the
    # settings box was never a problem. The folding defeated exactly that guard: `w` stayed truthy
    # while the value actually COMPARED became empty.
    assert blocked_word("good morning", {""}) is None, "an empty entry was always skipped"

    def folded(term):                       # the transformation that shipped, restated
        import re
        return re.sub(r"[\s_.\-]+", " ", term.lower()).strip()

    assert folded("--------------") == "", "the real entry mapped to nothing"
    # …and a matcher comparing the FOLDED term against content matches every string, because the
    # truthiness guard is on the raw entry and no longer on the thing being compared.
    assert folded("--------------") in "good morning", (
        "this is the outage: a truthy entry whose compared form is empty")

    usable, refused = screen_blocked_words({"--------------", ""})
    assert "" not in usable and "--------------" in usable
    assert blocked_word("good morning", usable) is None


def test_the_terms_still_match_what_they_say():
    """A guard that also stopped the filter working would be the opposite mistake."""
    usable, _ = screen_blocked_words(REAL_SHAPED)
    assert blocked_word('{"type":"zone_presence","zone":"x"}', usable) == "zone_presence"
    assert blocked_word("look at this -------------- divider", usable) == "--------------"
    assert blocked_word("join t.me/spam", usable) == "t.me"
