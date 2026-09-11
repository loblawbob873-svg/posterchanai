"""THE PHONE'S BOTTOM BAR HAS A BUDGET, AND ANYTHING TAKEN OUT OF IT KEEPS ITS BADGE.

Reported as "you added Rooms to the android toolbar on the bottom now it's too bloated". Splitting
Communities out of Messages gave it a nav button, which took the bar to SEVEN items on a 360px
screen — Home, Social, compose, Alerts, DMs, Rooms, More. Every one of them still cleared the 44px
touch minimum, so no layout check objected; it was simply too much, which is a judgement a person
makes and a number nobody had written down. It is written down here.

THE SECOND RULE IS THE ONE THAT MATTERS MORE. `#cc-badge-m` — the only thing on a phone that said a
community wanted you — lived on that button, and `#more-badge-m` counted drafts and nothing else.
Deleting the button would have deleted the notification with it. A control that disappears is a
feature that MOVED; a notification that disappears is a feature that was SWITCHED OFF, and the
difference is invisible in a screenshot. So the ☰ badge sums everything behind the ☰.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SHELL = (ROOT / "templates/client.html").read_text(encoding="utf-8")
APP = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")

#: What a 360px bar holds without becoming a row of guesses. Raising this is a product decision,
#: not a refactor — a reviewer should have to argue for it.
MAX_ITEMS = 6


def _bar() -> str:
    i = SHELL.index('<nav class="mobilenav')
    return SHELL[i:SHELL.index("</nav>", i)]


def _items() -> list[str]:
    return re.findall(r'<button class="nav-item[^"]*"([^>]*)>', _bar())


def test_the_bar_parses():
    """The check before the check: a parse that finds nothing makes the budget pass about zero."""
    assert len(_items()) >= 4, "the phone bar no longer parses"


def test_the_bar_is_within_its_item_budget():
    items = _items()
    assert len(items) <= MAX_ITEMS, (
        "the phone bar is up to %d items; %d is the budget. On a 360px screen each extra one makes "
        "every target smaller and every label shorter. Put the new view in the ☰ More sheet — and "
        "if it carries an unread count, carry that to the More badge (see below)."
        % (len(items), MAX_ITEMS))


def test_every_badge_in_the_bar_is_painted_by_something():
    """A badge nothing writes to is a permanent blank, which reads as 'nothing is waiting'."""
    for badge in re.findall(r'<i id="([a-z0-9-]+)" class="badge', _bar()):
        assert f"#{badge}" in APP or badge in APP, (
            f"{badge} is in the phone bar and nothing in app.js ever paints it")


def test_communities_unread_still_reaches_the_phone():
    """THE REPORT'S OTHER HALF. Rooms left the bar; its unread count must not have left with it."""
    assert 'data-view="concord"' not in _bar(), (
        "Rooms is back in the bar — if that is deliberate, raise MAX_ITEMS and say why")
    fn = APP[APP.index("function moreBadgeCount()"):]
    fn = fn[:fn.index("function bumpDraft(")]
    assert "Drafts.live()" in fn, "the More badge stopped counting drafts"
    assert "unreadRooms" in fn, (
        "the More badge no longer counts unread communities, so on a phone nothing shows that a "
        "community is waiting — the Rooms button that used to carry it is gone")
    assert "bumpMoreBadge" in (ROOT / "static/js/client/concord.js").read_text(encoding="utf-8"), (
        "concord.js repaints its own badge without refreshing the ☰ badge, so a newly unread room "
        "is invisible until something else happens to repaint it")


def test_the_sheet_explains_the_number_on_the_badge():
    """The ☰ badge is a SUM. Opening the sheet has to show what it is made of, or the number is a
    riddle: the sheet already prints per-row counts for drafts and mail, and Communities joined the
    sum when Rooms left the bar."""
    i = APP.index("function moreMenu(")
    head = APP[i:i + 3000]
    m = re.search(r"const counts=\{([^}]*)\}", head)
    assert m, "the More sheet's per-row counts moved — re-point this test"
    for key in ("drafts", "mail", "concord"):
        assert key in m.group(1), (
            f"the ☰ badge counts {key} but the sheet never shows it, so the total is unexplained: "
            + m.group(1))


def test_a_view_that_left_the_bar_is_still_reachable_in_one_tap():
    """Removing a button is only acceptable because the ☰ sheet still offers it directly."""
    i = APP.index("function moreMenu(")
    m = re.search(r"const items=\[(.*?)\n\s*\.filter\(", APP[i:], re.S)
    assert m, "the More sheet's item list moved — re-point this test"
    assert "'concord'" in m.group(1), (
        "Communities is in neither the phone bar nor the More sheet, so on a phone there is no way "
        "to open it at all")
