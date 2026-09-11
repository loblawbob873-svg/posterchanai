"""CREATING A CONCORD COMMUNITY WAS REACHABLE ONLY WHILE YOU HAD NONE.

Reported as "i don;t see a way to create a new COncord room" — and the feature was there the whole
time, which is the point of this file.

"Create community" is painted on the `state.community == null` discover pane. The moment you join or
create anything, `state.community` is a number, that pane is replaced by the message list, and the
button ceases to EXIST. The only way back was `◎` in the 62px community rail — an unlabelled glyph
whose title reads "Discover public communities", which is not what somebody looking to make one
would read as the way. The rail's `+` was titled "Join a community" and only ever opened Join.

So this is not a bug in creating a community; it is a bug in being able to say so. What it needs is
a rule that survives the next layout change: THE CONTROLS THAT REACH `openCreate` MUST INCLUDE ONE
THAT IS PAINTED IN EVERY STATE, not only in the empty one.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONCORD = (ROOT / "static/js/client/concord.js").read_text(encoding="utf-8")
CSS = (ROOT / "static/css/concord.css").read_text(encoding="utf-8")


def _openers():
    """The selectors wired to the shared create opener, read from the wiring itself — the array
    literal that is fanned out onto `openCreate`, not everything that happens to be nearby."""
    m = re.search(r"\[([^\]]*)\]\.forEach\(\s*sel\s*=>\s*\{[^}]*onclick\s*=\s*openCreate", CONCORD)
    assert m, "re-point this test: nothing fans a selector list onto openCreate any more"
    return set(re.findall(r"'#(cc-[\w-]+)'", m.group(1)))


def _empty_state_pane():
    """The markup painted ONLY when `state.community == null`."""
    at = CONCORD.index('<div class="cc-discover">')
    return CONCORD[at:CONCORD.index('</section></div>`', at)]


def _always_painted():
    """The chrome painted in every state: the rail and the sheets after the conversation."""
    body = CONCORD[CONCORD.index('feed.innerHTML=`<div class="cc-app'):]
    rail = body[body.index('<aside class="cc-communities">'):body.index('</aside>')]
    sheets = body[body.index('</main></div>'):body.index('\n    retainCommunityRail')]
    return rail + sheets


def test_there_is_more_than_one_way_to_start_creating():
    openers = _openers()
    assert "cc-create" in openers, "the empty-state Create button is no longer wired"
    assert len(openers) >= 2, (
        "creating a community is reachable from one control again: %s. That control is on the "
        "empty-state pane, which stops existing the moment you have a community." % sorted(openers))


def test_at_least_one_of_them_survives_having_a_community():
    """THE RULE. An opener that only exists on the zero-communities screen is not an opener."""
    always = _always_painted()
    reachable = [s for s in _openers() if 'id="%s"' % s in always]
    assert reachable, (
        "every control that opens the create dialog is painted only on the empty-state pane, so "
        "once you have one community there is no way to make another: %s" % sorted(_openers()))


def test_the_empty_state_button_really_is_the_conditional_one():
    """Proof the test above is measuring something: `cc-create` is in the pane that disappears."""
    assert 'id="cc-create"' in _empty_state_pane()
    assert 'id="cc-create"' not in _always_painted()


def test_the_rail_plus_says_what_it_reaches():
    """It was titled "Join a community" while being the only + in the app. A control whose label
    names half of what it does is how a feature becomes invisible."""
    rail = _always_painted()
    plus = rail[rail.index('id="cc-add"') - 120:rail.index('id="cc-add"') + 180]
    assert re.search(r'title="[^"]*[Cc]reate', plus), (
        "the rail's + does not mention creating, so nothing on screen says a community can be made")
    assert re.search(r'aria-label="[^"]*[Cc]reate', plus), "…and a screen reader is told even less"


def test_the_create_dialog_itself_is_always_in_the_document():
    """The opener only removes a `hidden` class, so the sheet has to be painted regardless of state
    — otherwise the new entry points would open nothing."""
    assert 'id="cc-create-dialog"' in _always_painted()


def test_the_second_way_in_is_a_real_control_not_a_footnote():
    """A text link under a primary button is a way to say a feature exists without offering it. The
    sheet is reached by a `+` that advertises both, so the two halves are presented as two
    choices — and the create one has to be a real, thumb-sized control."""
    assert 'id="cc-join-create"' in CONCORD
    row = CONCORD[CONCORD.index("cc-join-alt"):]
    row = row[:row.index("</div>") + 6]
    assert 'class="btn' in row, "creating a community is offered as a text link again"
    assert ".cc-join-alt{" in CSS, "the row has no stylesheet"
    rule = CSS[CSS.index(".cc-join-alt .btn{"):]
    rule = rule[:rule.index("}") + 1]
    assert "min-height:46px" in rule, "below the touch-target floor this phone layout uses"


def test_the_sheet_says_it_can_do_both():
    """It was headed "Join a Concord community" while being the only route to creating one."""
    head = CONCORD[CONCORD.index('id="cc-join"'):]
    head = head[:head.index("</h2>")]
    assert re.search(r"[Cc]reate", head), (
        "the sheet still only advertises joining, so nobody opens it looking to create")
