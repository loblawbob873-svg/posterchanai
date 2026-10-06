"""An incoming fediverse post keeps its LINKS and its LISTS.

Reported: "the blockbot for pleroma is all messed up now ... top posts ... no link, bad formatting".
Pleroma's daily Top Posts is an <ol> of entries each ending `🔗 <a href=…>View post</a>`. The
ActivityPub inbox flattened HTML by deleting every tag, so the relay stored ten "🔗 View post"s with
no address anywhere and the numbering gone. The shape below is that post's (names changed).
"""
from app.services.activitypub import convert
from app.services.fedi_normalize import html_to_text

TOP = ('<p>Top Posts of the Day - 2026-10-04</p><ol>'
       '<li><p><span class="h-card"><a class="u-url mention" href="https://riot.example/users/alice" rel="ugc">'
       '@<span>alice</span></a></span> - 38 pts (14 likes, 12 boosts) [No text content] 🔗 '
       '<a href="https://riot.example/objects/4e63">View post</a></p></li>'
       '<li><p><span class="h-card"><a class="u-url mention" href="https://riot.example/users/bob" rel="ugc">'
       '@<span>bob</span></a></span> - 32 pts (8 likes, 12 boosts) '
       '<a class="hashtag" data-tag="caturday" href="https://riot.example/tag/caturday" rel="tag ugc">#Caturday</a> 🔗 '
       '<a href="https://riot.example/objects/b9e6">View post</a></p></li></ol>')


def _note(html):
    return convert.note_content({"type": "Note", "content": html}, local_actors={})[0]


def test_every_view_post_keeps_its_address_and_the_list_its_numbers():
    text = _note(TOP)
    assert text.splitlines() == [
        "Top Posts of the Day - 2026-10-04",
        "1. @alice - 38 pts (14 likes, 12 boosts) [No text content] 🔗 View post https://riot.example/objects/4e63",
        "2. @bob - 32 pts (8 likes, 12 boosts) #Caturday 🔗 View post https://riot.example/objects/b9e6",
    ], text


def test_mentions_and_hashtags_stay_words_for_the_mention_rewriter():
    """activitypub/mentions.py finds `@alice` in the TEXT and links it to a person; a profile URL glued
    on after it would survive that rewrite as noise."""
    text = _note(TOP)
    assert "users/alice" not in text and "tag/caturday" not in text


def test_a_link_that_shows_its_own_address_is_not_doubled():
    mastodon = ('<p>see <a href="https://example.com/blog/post" rel="nofollow"><span class="invisible">https://'
                '</span><span class="ellipsis">example.com/blog/po</span><span class="invisible">st</span></a></p>')
    assert html_to_text(mastodon) == "see https://example.com/blog/post"
    assert html_to_text('<p><a href="https://x.com/a?s=20">https://x.com/a?s=20</a></p>') == "https://x.com/a?s=20"
    assert html_to_text('<p><a href="https://example.com">example.com</a></p>') == "https://example.com"


def test_paragraphs_breaks_bullets_and_unsafe_links():
    assert html_to_text("<p>a<br>b</p><p>c</p><ul><li>x</li><li>y</li></ul>") == "a\nb\nc\n• x\n• y"
    assert html_to_text('<p><a href="javascript:alert(1)">click</a></p>') == "click", \
        "a non-web link must not be carried into the post"
    assert html_to_text("&lt;b&gt; &amp; 5 &gt; 3") == "<b> & 5 > 3"
