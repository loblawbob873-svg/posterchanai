"""THE INGEST FILTER THAT DELETES NOTES, WITH NO TEST BEHIND ITS THRESHOLDS.

`nostr_relay/langfilter.py` had ZERO test references. It is imported by `ingest.py`, `store.py`,
`thread.py` and `server.py` — it is the gate that decides what this relay accepts — and its own
docstring states the stakes twice:

    a false positive DELETES the note via the retroactive purge, so both are conservative

That is the same family the project memory records as RECURRING ("Relay purge data-loss — filter at
INGEST; never touch"). Both directions are silent. A filter that has become too eager deletes real
notes and reports nothing; one that has stopped firing accepts the spam it exists to block and
reports nothing. The only visible symptom of either is somebody eventually noticing.

The module is entirely tuned constants — a 20% ratio, an absolute floor of 6, a Vietnamese minimum
of 2 distinctive characters, and three separate Tagalog paths with their own density floors — every
one of which is a number somebody arrived at by looking at real spam. A number with no test is a
number the next person will round.

EVERY EXPECTATION BELOW WAS MEASURED against the shipped filter before it was written down,
including one where reading the code gives the wrong answer: `bánh mì` is NOT Vietnamese to this
filter. `á` and `ì` are ordinary Latin-1 and appear in Spanish and Italian, so only the tone-marked
vowels (U+1EA0-1EF9) and `ăđơư` count as distinctive. Asserting the obvious sample would have
pinned a behaviour the filter does not have.
"""
import pytest

from app.services.nostr_relay import langfilter as lf


#: One real sentence per advertised language. These double as the coverage check below.
SAMPLES = {
    "ru": "Привет как дела друзья",
    "zh": "这是一个中文句子测试",
    "ja": "これは日本語の文です",
    "ko": "이것은 한국어 문장입니다",
    "ar": "هذه جملة عربية",
    "he": "זהו משפט בעברית",
    "el": "Αυτή είναι μια ελληνική",
    "th": "นี่คือประโยคภาษาไทย",
    "hi": "यह एक हिंदी वाक्य है",
    "hy": "Սա հայերեն նախադասություն",
    "ka": "ეს არის ქართული წინადადება",
    "vi": "Chào bạn, hôm nay đẹp quá",
    "tl": "Kumusta ka na po ba diyan",
}

#: Ordinary notes that must never be classified as anything. A hit here is a deleted note.
INNOCENT = [
    "Hello everyone, this is a normal English note about coffee.",
    "Il dito po del piede e rotto oggi",              # Italian; collides with two Tagalog markers
    "The dog sat po on the mat dito",                 # English; the same two collisions
    "gm",
    "Just shipped a fix for the relay, thanks for the reports!",
    "check this out https://example.com/some/very/long/path/image.png",
    "nostr:npub1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq",
    ":meow_bongo_keyboard: :party_parrot:",
    "",
]


# --------------------------------------------------------------------------- the UI contract


def test_every_language_offered_in_the_admin_ui_is_actually_detectable():
    """`LANGUAGES` drives the clickable toggles in Admin → Relay. A code the detector can never
    return is a switch an operator turns on and which then does nothing — indistinguishable from a
    filter that is working and simply seeing no spam."""
    undetectable = [code for code in lf.LANGUAGES
                    if code not in lf.detect_languages(SAMPLES.get(code, ""))]
    assert undetectable == [], \
        f"these are offered as toggles but nothing can trigger them: {undetectable}"


def test_this_file_has_a_sample_for_every_offered_language():
    """Keeps the sweep above honest: a language added to the UI with no sample here would be
    'covered' by `SAMPLES.get(code, "")`, which detects nothing and would fail — but for the right
    reason only if somebody reads it. Say it directly instead."""
    assert set(SAMPLES) == set(lf.LANGUAGES), \
        f"missing samples for {set(lf.LANGUAGES) - set(SAMPLES)}"


@pytest.mark.parametrize("code", sorted(SAMPLES))
def test_a_sample_detects_its_own_language_and_nothing_else(code):
    """Cross-contamination matters as much as detection: an operator blocking Chinese must not have
    Japanese notes deleted as a side effect."""
    got = lf.detect_languages(SAMPLES[code])
    assert code in got
    assert got == {code}, f"{code} sample also matched {got - {code}}"


# --------------------------------------------------------------------------- never delete by default


def test_nothing_is_blocked_when_no_language_is_configured():
    """THE DEFAULT, and the most expensive thing in the file to get wrong. Every node ships with an
    empty blocked set; if `blocked_language` ever treated that as "block everything unrecognised",
    the retroactive purge would delete the relay."""
    for text in list(SAMPLES.values()) + INNOCENT:
        assert lf.blocked_language(text, set()) is None
        assert lf.blocked_language(text, None) is None


@pytest.mark.parametrize("text", INNOCENT)
def test_an_ordinary_note_is_never_classified(text):
    """A false positive here does not hide the note — it DELETES it."""
    assert lf.detect_languages(text) == set(), f"{text!r} would be deleted"


def test_only_the_configured_language_blocks():
    russian = SAMPLES["ru"]
    assert lf.blocked_language(russian, {"ru"}) == "ru"
    assert lf.blocked_language(russian, {"zh", "ja", "ko"}) is None


def test_the_blocked_code_is_returned_so_the_log_can_name_it():
    assert lf.blocked_language(SAMPLES["th"], {"th", "ru"}) == "th"


# --------------------------------------------------------------------------- noise must not dilute


def test_a_url_does_not_dilute_a_short_foreign_note():
    """The documented failure, verbatim from the source: "a Japanese line + an image URL reads as
    11% Japanese". A URL is a long run of Latin characters that is not language; counted, it pushes
    real spam under the 20% ratio and the filter silently stops working on exactly the notes that
    carry links.

    FIVE Japanese characters, deliberately. The obvious sample uses six or more and then proves
    nothing: six trips `_BLOCK_ABS_MIN` on its own, so the note is caught whether or not the URL was
    stripped — measured, deleting the noise-stripping line left that version of this test green.
    Below the absolute floor the ratio is the only thing that can decide, which is the rule this
    test is about."""
    text = "これは日本 hello https://example.com/a/very/long/image/path/that/is/latin.png"
    assert "ja" in lf.detect_languages(text)


def test_nostr_and_bech32_entities_do_not_dilute_either():
    npub = "npub1" + "q" * 58
    assert "ru" in lf.detect_languages(f"Привет {npub} nostr:{npub}")


def test_custom_emoji_shortcodes_do_not_dilute():
    assert "ru" in lf.detect_languages("Привет :meow_bongo_keyboard: :party_parrot:")


def test_stripping_noise_cannot_by_itself_classify_a_note():
    """The other direction: a note that is ONLY a URL has no letters left after stripping, and must
    come back as nothing rather than as some default."""
    assert lf.detect_languages("https://example.com/x") == set()


# --------------------------------------------------------------------------- the two thresholds


def test_a_stray_foreign_word_in_english_is_not_spam():
    """Under both rules: below 20% of the letters AND fewer than 6 characters. "6 chars = a real
    phrase, not a stray foreign name/word" — a person quoting a word must not lose their note."""
    text = "I love the word 日本語 in this long English sentence about language learning today"
    assert lf.detect_languages(text) == set()


@pytest.mark.parametrize("n,expected", [(5, set()), (6, {"zh"}), (7, {"zh"})])
def test_the_absolute_floor_is_exactly_six_characters(n, expected):
    """Catches bilingual spam — a full CJK sentence plus an English translation, where the CJK is
    diluted below the ratio. Measured at the boundary in both directions so the constant cannot
    drift without saying so."""
    english = " ".join(["english"] * 40)
    assert lf.detect_languages("字" * n + " " + english) == expected


def test_the_ratio_rule_fires_at_twenty_percent():
    """1 Cyrillic among 5 letters is exactly 20% and blocks; 1 among 6 is below and does not.
    Neither reaches the absolute floor, so this isolates the ratio."""
    assert lf.detect_languages("abcd" + "д") == {"ru"}          # 1/5 = 20%
    assert lf.detect_languages("abcde" + "д") == set()          # 1/6 = 16.7%


# --------------------------------------------------------------------------- CJK is three languages


def test_kana_makes_it_japanese_and_the_kanji_count_with_it():
    """Japanese is written with kana AND han. Counting the han separately would split one sentence
    across two language buckets and could push both under the threshold."""
    assert lf.detect_languages("これは漢字です") == {"ja"}


def test_hangul_makes_it_korean():
    assert lf.detect_languages("이것은 漢字 입니다") == {"ko"}


def test_bare_han_is_chinese():
    assert lf.detect_languages("这是中文句子测试") == {"zh"}


def test_a_note_mixing_hangul_and_kana_counts_as_both():
    """Blocking Japanese must not silently take Korean with it, or vice versa — an operator who
    blocked one language would be deleting notes in another."""
    assert lf.detect_languages("これは 한국어 입니다") == {"ja", "ko"}


# --------------------------------------------------------------------------- Vietnamese


def test_two_distinctive_characters_make_it_vietnamese():
    assert "vi" in lf.detect_languages("The word đep bạn is here in english")


def test_one_distinctive_character_does_not():
    """`_VIET_MIN = 2`. One is a borrowed word or a name; deleting on it would take out any note
    mentioning a Vietnamese person."""
    assert lf.detect_languages("The word đep is here in english") == set()


def test_plain_latin_accents_are_not_vietnamese():
    """MEASURED, and the opposite of what the sample suggests: `bánh mì` is not Vietnamese here.
    `á` and `ì` are ordinary Latin-1 and appear across Spanish, Italian and French, so they are
    deliberately NOT in the distinctive set. Widening it to 'accented vowels' would delete Spanish."""
    assert lf.detect_languages("bánh mì") == set()
    assert lf.detect_languages("El niño comió la manzana café") == set()


# --------------------------------------------------------------------------- Tagalog is the risky one


@pytest.mark.parametrize("text", [
    "Ako ay masaya po ngayon dito sa bahay",
    "Walang ano man po",
    "Sana all po talaga",
])
def test_real_taglish_is_detected(text):
    assert "tl" in lf.detect_languages(text)


@pytest.mark.parametrize("text", [
    "Il dito po del piede e rotto oggi",     # the collision named in the source: Italian dito + po
    "The dog sat po on the mat dito",
    "po",
    "dito",
    "I went to the store and bought some milk and bread for the week ahead",
])
def test_english_and_italian_collisions_never_classify_as_tagalog(text):
    """Tagalog is Latin-script with no distinctive characters, so it is detected from common short
    function words — which collide with English slang and Italian. Every path demands a CORE marker
    AND a density floor for exactly this reason. This is the single most likely false positive in
    the module, and a false positive deletes the note."""
    assert "tl" not in lf.detect_languages(text)


def test_one_marker_is_never_enough():
    """`len(seen) < 2` returns early. A single `po` or `sana` in an English sentence is a word, not
    a language."""
    assert "tl" not in lf.detect_languages("sana this works out fine for everyone involved today")


# --------------------------------------------------------------------------- blocked_word


def test_a_banned_word_is_matched_case_insensitively():
    assert lf.blocked_word("Hello SPAM here", {"spam"}) == "spam"
    assert lf.blocked_word("hello spam here", {"spam"}) == "spam"


def test_the_matched_word_is_returned_so_the_rejection_can_name_it():
    assert lf.blocked_word("buy followers now", {"followers"}) == "followers"


def test_a_clean_note_matches_nothing():
    assert lf.blocked_word("a perfectly ordinary note", {"spam", "followers"}) is None


def test_no_word_list_blocks_nothing():
    """Same default as the language set: an empty configuration must never reject."""
    for words in (set(), None, [], ""):
        assert lf.blocked_word("anything at all", words) is None


def test_an_empty_entry_in_the_list_does_not_match_everything():
    """`"" in low` is True for every string. One blank line in the admin textarea would otherwise
    reject every note the relay receives."""
    assert lf.blocked_word("a perfectly ordinary note", {"", "spam"}) is None


def test_empty_content_is_never_blocked():
    assert lf.blocked_word("", {"spam"}) is None
    assert lf.blocked_word(None, {"spam"}) is None


# --------------------------------------------------------------------------- doesn't crash


@pytest.mark.parametrize("text", ["", None, " ", "\n\n", "123 456", "!!!???", "🙂🙂🙂", "\x00"])
def test_odd_input_is_no_language_rather_than_an_exception(text):
    """This runs on every event the relay ingests, so an exception here is a relay that stops
    accepting writes."""
    assert lf.detect_languages(text) == set()
