"""An invitation is a question, and it had no "no".

`#cc-join-go` was LABELLED "Preview invite" and joined outright — hydrate the bundle, push it into
the room list, switch to it, publish the membership. And `openInviteLink` auto-clicked that button,
so tapping a link somebody put in a chat message enrolled you in their community before you had seen
its name. Being sent an invitation was the same act as accepting it.

DECLINING IS A DISMISSAL, NOT A BLACKLIST. concord.js already carries a `left.v2` key because an
earlier build wrote communities into a permanent local refusal and `wasLocallyLeft` then hid one for
ever ("my concord community for posterchan is no longer appearing in Concord"), with no way to
remove the records already on disk. So a declined invitation is remembered only so the same link
stops re-asking, it never touches that ledger, and opening the invite again clears it.
"""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
SRC = (ROOT / "static/js/client/concord.js").read_text(encoding="utf-8")
CSS = (ROOT / "static/css/concord.css").read_text(encoding="utf-8")


def test_opening_an_invite_link_no_longer_joins_by_itself():
    """It fills the join surface and stops. Nothing in it may enrol anybody: the rule is about what
    the function DOES, not about a flag, because a flag whose only caller passes it is not a rule."""
    fn = SRC.split("function openInviteLink(", 1)[1].split("\n  }", 1)[0]
    for joining in ("acceptInvite", "persistArmadaMembership", "save(a)", "hydrateInvite"):
        assert joining not in fn, "openInviteLink must not join: " + fn
    assert "forgetDeclinedInvite" in fn, "opening it again is reconsidering it"


def test_the_preview_offers_both_answers():
    html = SRC.split("function invitePreviewHtml(", 1)[1].split("\n  }", 1)[0]
    assert "cc-invite-accept" in html and "cc-invite-decline" in html, html[:400]
    # And it says what accepting will do, since joining publishes something.
    assert "publishes your membership" in html


def test_a_community_name_cannot_close_the_markup():
    """An invite is written by whoever sent it, and the community name is their text."""
    html = SRC.split("function invitePreviewHtml(", 1)[1].split("\n  }", 1)[0]
    assert "p.enc(name)" in html, html[:400]
    assert re.search(r"p\.enc\(desc", html), html[:400]


def test_declining_never_touches_the_left_communities_ledger():
    """That ledger is what `wasLocallyLeft` reads, and a record in it hides a community."""
    fn = SRC.split("function declineInvite(", 1)[1].split("\n  }", 1)[0]
    assert "DECLINED_INVITES_KEY" in fn, fn
    assert "LEFT_COMMUNITIES_KEY" not in fn and "noteLeftFromVault" not in fn, fn


def test_a_decline_can_be_taken_back():
    assert "function forgetDeclinedInvite(" in SRC
    body = SRC.split("function forgetDeclinedInvite(", 1)[1].split("\n  }", 1)[0]
    assert "filter(" in body, "it must REMOVE the record, not add another: " + body


def test_the_decline_ledger_is_bounded():
    """A record that only grows is the failure this file already carries a v2 key because of."""
    fn = SRC.split("function declineInvite(", 1)[1].split("\n  }", 1)[0]
    assert re.search(r"\.slice\(0,\s*\d+\)", fn), fn


def test_the_preview_is_rendered_and_bound_like_every_other_dialog():
    """A first version injected the card with insertAdjacentHTML and wired it by hand. That is a
    second way to build a screen: it draws over whatever the next repaint puts there, and the
    runtime harness — which drives this flow through render() — could not see the buttons at all.
    The card is part of the join dialog's markup and its two buttons are bound in bind()."""
    assert "insertAdjacentHTML" not in SRC.split("invitePreviewHtml", 1)[1][:4000], (
        "the preview must be rendered, not injected")
    shell = SRC.split('id="cc-join"', 1)[1][:1200]
    assert "invitePreviewHtml(p,pendingInvite)" in shell, shell[:600]
    for control in ("#cc-invite-accept", "#cc-invite-decline"):
        assert "$('%s')" % control in SRC, control + " is never bound"


def test_answering_clears_the_pending_invite_before_it_repaints():
    """Both answers repaint. An answer that leaves `pendingInvite` set draws the card again over
    its own result — the invitation you just declined, back on screen."""
    for handler in ("cc-invite-accept", "cc-invite-decline"):
        fn = SRC.split("$('#%s'); if(" % handler, 1)[1].split("};", 1)[0]
        assert "pendingInvite=null" in fn, fn


def test_the_card_is_styled_and_the_sheet_version_moved():
    assert ".cc-invite-card" in CSS
    version = re.search(r"concord\.css\?v=(\d+)", SRC)
    assert version and int(version.group(1)) >= 19, "a CSS change needs its cache-busting bump"
