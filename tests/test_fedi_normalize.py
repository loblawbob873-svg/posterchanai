"""THE "PROVEN" MODULE NOTHING WAS PROVING.

CLAUDE.md says of `fedi_normalize.py`:

    extracted VERBATIM from the old bridge and is **proven** code — change it only with a very good
    reason; every bridge service depends on it.

It had ZERO test references. "Proven" was resting entirely on the fact that it used to work
somewhere else, which is a claim about the past, not a guard on the future. Three services import
it (`fedi_nostr_bridge_service`, `fedi_nostr_personal_service`, `fedi_bridge_identity`), so a
regression here is not one broken post — it is every mirrored post, every puppet profile and every
personal notification, all at once, and all of it silently: a mangled body still publishes.

The two that would be expensive and invisible:

  * `uri` is the CROSS-INSTANCE DEDUP KEY (CLAUDE.md: "`note_uri` (canonical AP URI) is the
    cross-instance dedup key, `note_id` the same-instance fallback"). Prefer `url` over `uri` by
    accident and federated copies of one post stop matching — the bridge mirrors each of them.
  * `_strip_html` unescapes entities AFTER removing tags. Do it the other way round and a post
    that WROTE `&lt;b&gt;` about markup has that markup stripped out of its own text.
"""
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


def test_the_canonical_uri_prefers_uri_over_url():
    """The cross-instance dedup key. `url` is the human page and differs per instance; `uri` is the
    AP id and is the same everywhere. Swap them and every federated copy of a post looks new."""
    post = {"uri": "https://a.example/objects/1", "url": "https://a.example/notice/1"}
    assert norm._canonical_uri("pleroma", "https://a.example", post) \
        == "https://a.example/objects/1"


def test_a_post_with_no_uri_is_none_not_an_empty_string():
    """`None` means "no dedup key, fall back to note_id". An empty string is a key that every
    keyless post shares — they would all dedup against each other and only the first would mirror."""
    assert norm._canonical_uri("pleroma", "https://a.example", {}) is None
    assert norm._canonical_uri("pleroma", "https://a.example", {"uri": ""}) is None


def test_the_normalised_uri_field_also_prefers_uri():
    s = {"uri": "https://a.example/objects/1", "url": "https://a.example/notice/1"}
    assert norm._norm_pleroma(s)["uri"] == "https://a.example/objects/1"


def test_the_human_url_field_prefers_url():
    """The mirror image: `url` is what a reader should be sent to, so this one must NOT be the
    AP id. The two fields are adjacent in the same dict and easy to transpose."""
    s = {"uri": "https://a.example/objects/1", "url": "https://a.example/notice/1"}
    assert norm._norm_pleroma(s)["url"] == "https://a.example/notice/1"


def test_each_falls_back_to_the_other():
    assert norm._norm_pleroma({"uri": "u"})["url"] == "u"
    assert norm._norm_pleroma({"url": "u"})["uri"] == "u"


# --------------------------------------------------------------------------- author / body


def test_the_author_falls_back_from_acct_to_username():
    assert norm._norm_pleroma({"account": {"username": "alice"}})["author"]["acct"] == "alice"
    assert norm._norm_pleroma({"account": {"acct": "alice@x.example", "username": "alice"}}) \
        ["author"]["acct"] == "alice@x.example"


def test_an_author_with_nothing_usable_is_marked_not_invented():
    assert norm._norm_pleroma({"account": {}})["author"]["acct"] == "?"


def test_a_status_with_no_account_at_all_still_normalises():
    """The bridge mirrors whatever the timeline hands it. A KeyError here stops the drain, and the
    cursor commits per page — so one malformed status would stall the whole mirror."""
    out = norm._norm_pleroma({"id": "1"})
    assert out["author"]["acct"] == "?"
    assert out["text"] == ""


def test_the_raw_html_is_kept_beside_the_flattened_text():
    s = {"content": "<p>hello <b>world</b></p>"}
    out = norm._norm_pleroma(s)
    assert out["text"] == "hello world"
    assert out["html"] == "<p>hello <b>world</b></p>"


# --------------------------------------------------------------------------- media / quotes


def test_media_without_a_url_is_dropped_rather_than_carried_as_none():
    """A `{"url": None}` attachment mirrored onto Nostr is a broken image in every client."""
    s = {"media_attachments": [{"url": "https://a.example/1.png"}, {"url": None}, {}]}
    assert norm._norm_pleroma(s)["media"] == [{"url": "https://a.example/1.png", "mime": ""}]


def test_missing_media_is_an_empty_list():
    assert norm._norm_pleroma({})["media"] == []


def test_a_quote_post_is_read_from_quote():
    s = {"quote": {"content": "<p>quoted</p>", "account": {"acct": "bob@x.example"}}}
    q = norm._norm_pleroma(s)["quote"]
    assert q["acct"] == "bob@x.example" and q["text"] == "quoted"


def test_a_boost_is_read_from_reblog():
    s = {"reblog": {"content": "<p>boosted</p>", "account": {"acct": "bob@x.example"}}}
    assert norm._norm_pleroma(s)["quote"]["text"] == "boosted"


def test_a_plain_post_has_no_quote():
    assert norm._norm_pleroma({"content": "<p>hi</p>"})["quote"] is None


def test_reply_and_count_fields_default_rather_than_vanish():
    out = norm._norm_pleroma({})
    assert out["in_reply_to_id"] is None
    assert out["replies_count"] == 0


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


def test_norm_dispatches_to_the_pleroma_normaliser():
    """`_norm` is the entry point the bridges call; it must stay wired to something."""
    assert norm._norm("pleroma", {"content": "<p>hi</p>"})["text"] == "hi"
