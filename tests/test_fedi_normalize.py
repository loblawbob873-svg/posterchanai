"""The fediverse text helpers the ActivityPub server relies on (fedi_normalize.py): HTML flattened to
text without eating what a post WROTE, and custom emoji turned into NIP-30 tags.

`_strip_html` unescapes entities AFTER removing tags. Do it the other way round and a post that
WROTE `&lt;b&gt;` about markup has that markup stripped out of its own text."""
import pytest

from app.services import fedi_normalize as norm


# --------------------------------------------------------------------------- _strip_html


def test_paragraphs_and_breaks_become_newlines():
    """Mastodon/Pleroma bodies are HTML. Dropping the structure instead of converting it turns
    every multi-paragraph post into one run-on line on Nostr."""
    assert norm._strip_html("<p>first</p><p>second</p>") == "first\nsecond"
    assert norm._strip_html("<p>one<br>two</p>") == "one\ntwo"
    assert norm._strip_html("<p>one<br />two<br/>three</p>") == "one\ntwo\nthree"


def test_tags_are_removed_but_their_text_is_kept():
    assert norm._strip_html('<a href="https://x.example">link text</a>') == "link text"
    assert norm._strip_html("<b>bold</b> and <i>italic</i>") == "bold and italic"


def test_entities_are_unescaped_after_the_tags_are_stripped():
    """ORDER IS THE WHOLE TEST. Unescaping first would turn `&lt;b&gt;` into a real tag and then
    delete it — so a post ABOUT html loses the thing it was about, with no error anywhere."""
    assert norm._strip_html("<p>use &lt;b&gt; for bold</p>") == "use <b> for bold"
    assert norm._strip_html("<p>a &amp; b</p>") == "a & b"
    assert norm._strip_html("<p>&quot;quoted&quot;</p>") == '"quoted"'


def test_a_bare_angle_bracket_in_prose_survives_as_prose():
    """`3 < 5` arrives entity-encoded from a well-behaved instance and must come back readable."""
    assert norm._strip_html("<p>3 &lt; 5</p>") == "3 < 5"


def test_it_tolerates_none_and_empty():
    assert norm._strip_html(None) == ""
    assert norm._strip_html("") == ""


def test_stripping_html_would_eat_a_kaomoji_which_is_why_display_names_skip_it():
    """Pinning the REASON for a split that otherwise looks like an oversight. `puppet_for` says:

        display_name is PLAIN TEXT on Mastodon/Pleroma (never HTML) — do NOT tag-strip it, or
        angle-bracket kaomoji like <(^o^)> get eaten.

    If someone "tidies" that by running display names through here too, this documents what
    happens. The bio IS html and must keep going through it."""
    assert norm._strip_html("<(^o^)>") == "", \
        "if this no longer eats kaomoji, the display_name/bio split can be revisited"


# --------------------------------------------------------------------------- the dedup key







# --------------------------------------------------------------------------- author / body






# --------------------------------------------------------------------------- media / quotes








# --------------------------------------------------------------------------- custom emoji


def test_the_list_shape_is_accepted():
    assert norm._emoji_url_map([{"shortcode": "blob", "url": "https://a.example/blob.png"}]) \
        == {"blob": "https://a.example/blob.png"}


def test_the_dict_shape_is_accepted():
    """"Both shapes are accepted — instances differ." Dropping either silently strips every custom
    emoji from that instance's posts."""
    assert norm._emoji_url_map({"blob": "https://a.example/blob.png"}) \
        == {"blob": "https://a.example/blob.png"}


def test_name_and_static_url_are_the_fallback_spellings():
    assert norm._emoji_url_map([{"name": "blob", "static_url": "https://a.example/b.png"}]) \
        == {"blob": "https://a.example/b.png"}


def test_an_emoji_with_no_url_is_dropped():
    assert norm._emoji_url_map([{"shortcode": "blob"}, {"url": "https://a.example/x.png"}]) == {}
    assert norm._emoji_url_map({"blob": None}) == {}


@pytest.mark.parametrize("junk", [None, "", 0, "blob", 7])
def test_a_junk_emoji_field_is_an_empty_map_not_a_crash(junk):
    assert norm._emoji_url_map(junk) == {}


# --------------------------------------------------------------------------- NIP-30 tags


def test_only_shortcodes_that_have_a_url_become_tags():
    """A NIP-30 tag naming an emoji with no url renders as a broken image in every Nostr client."""
    tags = norm.emoji_tags_for("hi :blob: :nope:", {"blob": "https://a.example/blob.png"})
    assert tags == [["emoji", "blob", "https://a.example/blob.png"]]


def test_a_repeated_shortcode_is_tagged_once():
    """Duplicate `emoji` tags for one shortcode are wasted bytes on every relay, on every post."""
    tags = norm.emoji_tags_for(":blob: :blob: :blob:", {"blob": "https://a.example/b.png"})
    assert tags == [["emoji", "blob", "https://a.example/b.png"]]


def test_the_tag_count_is_bounded():
    """An emoji-spam post must not turn into an event with a thousand tags."""
    emap = {f"e{i}": f"https://a.example/{i}.png" for i in range(100)}
    text = " ".join(f":e{i}:" for i in range(100))
    assert len(norm.emoji_tags_for(text, emap, limit=30)) == 30


def test_remote_shortcodes_with_a_host_are_matched():
    """Mastodon writes federated custom emoji as `:blob@other.example:`."""
    emap = {"blob@other.example": "https://a.example/b.png"}
    assert norm.emoji_tags_for(":blob@other.example:", emap) \
        == [["emoji", "blob@other.example", "https://a.example/b.png"]]


@pytest.mark.parametrize("text,emap", [("", {"a": "u"}), (None, {"a": "u"}),
                                       (":a:", {}), (":a:", None)])
def test_nothing_to_tag_is_an_empty_list(text, emap):
    assert norm.emoji_tags_for(text, emap) == []


