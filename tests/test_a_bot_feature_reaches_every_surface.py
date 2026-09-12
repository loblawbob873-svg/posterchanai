"""A BOT FEATURE HAS FIVE SURFACES, AND MISSING ONE MAKES IT INVISIBLE RATHER THAN BROKEN.

The Concord work shipped a bridge, a listener and a config key — and no FIELD to type the invite
into. Reported as "i still do not see a way to make a concord bot respond when mentioned" and then
"we need a field in the bot manager to add the invite link or whatever so it can join". Both times
the code was there; the way in was not.

The five, and what each one's absence looks like:

  1. `main.py` argparse flag        the manager spawns a mode argparse rejects → EVERY mode of that
                                    bot dies at startup with a usage error
  2. `BOT_FEATURES` in admin-bots.js the checkbox exists but ticking it adds no mode → silently does
                                    nothing, and unticking it is equally silent
  3. a checkbox in bots.html        `_g(cid).checked` throws on a missing element, which takes
                                    _buildModes and therefore SAVE with it
  4. `BOT_KNOWN_KEYS` for its config the key is dropped on the next save of an existing bot — the
                                    one record of what that bot was configured to do
  5. an <input> the operator types into   the feature is unreachable, which is this report

This is the bot-manager twin of test_every_view_reaches_every_surface.py. Same failure, different
four surfaces, both found the same way: by a user saying "I can't see it".
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "static/js/admin-bots.js").read_text(encoding="utf-8")
HTML = (ROOT / "templates/admin/tabs/bots.html").read_text(encoding="utf-8")
MAIN = (ROOT / "botframework/main.py").read_text(encoding="utf-8")
MANAGER = (ROOT / "app/services/bot_manager_service.py").read_text(encoding="utf-8")


def _features() -> dict[str, str]:
    """checkbox id → main.py flag, from the shipped map."""
    block = JS[JS.index("const BOT_FEATURES = {"):]
    block = block[:block.index("};")]
    return dict(re.findall(r"(bot_ft_[a-z0-9_]+)\s*:\s*'(--[a-z0-9-]+)'", block))


def _argparse_flags() -> set[str]:
    return set(re.findall(r'"(--[a-z0-9-]+)", action="store_true"', MAIN))


def _known_keys() -> set[str]:
    block = JS[JS.index("const BOT_KNOWN_KEYS = ["):]
    block = block[:block.index("];")]
    return set(re.findall(r"'([a-z0-9_]+)'", block))


def test_the_lists_are_all_readable():
    """The check before the check: a parse that silently returns nothing makes every rule below
    pass about an empty set, which is how a guard stops guarding unnoticed."""
    assert len(_features()) >= 8, "BOT_FEATURES no longer parses"
    assert len(_argparse_flags()) >= 15, "main.py's flags no longer parse"
    assert len(_known_keys()) >= 10, "BOT_KNOWN_KEYS no longer parses"


def test_every_feature_checkbox_exists_in_the_form():
    """`_buildModes` does `_g(cid).checked` with no null guard, so a checkbox named here and absent
    from the template throws — and takes SAVE with it, for every bot, not just this feature."""
    missing = [cid for cid in _features() if f'id="{cid}"' not in HTML]
    assert not missing, (
        "these features are in BOT_FEATURES but have no checkbox in bots.html, so saving ANY bot "
        "throws: %s" % ", ".join(missing))


def test_every_feature_maps_to_a_mode_main_py_accepts():
    """A mode argparse does not know is not a no-op: `main.py --nostr --nope` exits on a usage
    error, so one bad entry here stops that bot doing ANYTHING."""
    flags, unknown = _argparse_flags(), {}
    for cid, flag in _features().items():
        if flag not in flags:
            unknown[cid] = flag
    assert not unknown, (
        "these features spawn modes main.py rejects, which kills the whole bot at startup: %r"
        % unknown)


def test_every_checkbox_in_the_form_is_wired_to_something():
    """The reverse: a checkbox nobody reads is a control that lies. `bot_ft_reply` and
    `bot_ft_stats` are deliberate exceptions — the first is the bot's own platform (`--<platform>`)
    and the second is a CONFIG flag the app acts on, not a main.py mode."""
    wired = set(_features()) | {"bot_ft_reply", "bot_ft_stats"}
    present = set(re.findall(r'id="(bot_ft_[a-z0-9_]+)"', HTML))
    orphans = present - wired
    assert not orphans, (
        "these checkboxes are in the form but reach no mode and no config flag, so ticking them "
        "does nothing: %s" % ", ".join(sorted(orphans)))


CONFIGURED = {
    # feature → (config key it needs, the input the operator types it into)
    "bot_ft_concord": ("concord_invite", "bot_f_concord_invite"),
}


@pytest.mark.parametrize("feature,expected", sorted(CONFIGURED.items()))
def test_a_feature_that_needs_a_value_has_somewhere_to_type_it(feature, expected):
    """THE ONE THAT WAS MISSED. The key survived in BOT_KNOWN_KEYS, which is what keeps it out of
    the Advanced JSON box — so it looked wired from the code's side and there was no way in from
    the operator's."""
    key, field = expected
    assert feature in _features(), f"{feature} is no longer a feature"
    assert key in _known_keys(), (
        f"{key} is not in BOT_KNOWN_KEYS, so it is dropped from an existing bot's config on the "
        "next save")
    assert f'id="{field}"' in HTML, (
        f"{feature} needs {key} and there is no input to type it into — the feature is unreachable")
    # The generic save/load is `bot_f_<key>`, so the id has to BE that or nothing reads it.
    assert field == "bot_f_" + key, (
        f"the input is {field}; the generic config save/load reads bot_f_{key}, so this value is "
        "never stored")


def test_the_invite_field_is_visible_without_hunting_for_a_tickbox_first():
    """REPORTED TWICE. The field existed and was gated on `bot_ft_concord`, which is one checkbox
    among fifteen in a wrapped grid — so "we need a field in the bot manager to add the invite
    link" was answered with a field you could only reach by finding a tickbox, and the follow-up
    was "still don't see concord bot settings in admin bot ui".

    A place to PUT a value is not the same thing as the switch that uses it. The nsec and the
    profile fields are not hidden behind their features either.
    """
    block = JS[JS.index("function onBotFormChange("):]
    block = block[:block.index("\n}")]
    m = re.search(r"show\('bot_grp_concord',\s*([^)]*)\)", block)
    assert m, "the Concord group is no longer shown or hidden by the form logic"
    condition = m.group(1)
    assert "bot_ft_concord" not in condition, (
        "the invite field is hidden until the Concord feature is ticked; that is the report. Show "
        "it for any nostr bot: " + condition)
    assert "isNostr" in condition, (
        "the invite field shows for non-nostr bots, which cannot join a room at all: " + condition)


def test_a_nostr_only_feature_is_hidden_on_a_fediverse_bot():
    """A checkbox a platform cannot honour is a control that lies — and `showFeat` UNCHECKS what it
    hides, so a feature missing from both lists can also be saved onto a bot that cannot run it."""
    block = JS[JS.index("function onBotFormChange("):]
    nostr_only = re.search(r"\[([^\]]*)\]\.forEach\(f => showFeat\(f, isNostr\)\)", block)
    assert nostr_only, "the per-platform feature lists moved — re-point this test"
    assert "'concord'" in nostr_only.group(1), (
        "Concord is a Nostr-only feature and is offered on Pleroma bots: " + nostr_only.group(1))


def test_the_invite_field_comes_before_the_features_that_use_it():
    """Its own tooltip says "paste the room's invite link above". If the field moved below the
    checkbox, that instruction points at nothing."""
    assert HTML.index('id="bot_grp_concord"') < HTML.index('id="bot_grp_features"'), (
        "the invite field is now below the Features list it is referenced from")


def test_a_saved_room_means_the_bot_joins_it():
    """THE TRAP THIS FILE EXISTS TO CATCH, AND THE ONE THE FIRST FIX FOR IT CREATED.

    Making the invite field always visible left the SWITCH somewhere else — the feature checkbox —
    so pasting a link and pressing Save produced a bot holding a room it never opened. Measured on
    the real row: `concord_invite` set with its fragment intact, "Test join" answering 200, and
    `modes='--nostr'`. Nothing broken, nothing logged, every surface saying it was fine. Reported as
    "i don't see bot in room despite what the UI says".

    The first fix derived the mode inside `_buildModes`, i.e. at SAVE. That worked and produced the
    opposite complaint — "In bots, concord can never be unchecked, wtf is this" — because a bot that
    has ever held an invite could not be saved without the listener: unticking the box was undone by
    the save it triggered. Two bugs, one cause: the decision was being made somewhere the operator
    could not see.

    So the derivation happens at the FORM, visibly. Pasting an invite TICKS the box (once — the
    ordinary request, one less click), the box is never locked, and Save reads the box and nothing
    else. If the two end up disagreeing, the form SAYS so instead of silently correcting either way.
    That keeps the original bug impossible — a pasted invite still produces a joining bot — while
    leaving the operator in charge of the switch.
    """
    form = JS[JS.index("function onBotFormChange("):]
    form = form[:form.index("\n  }\n")] if "\n  }\n" in form else form

    # Pasting an invite ticks the box, so the mode IS derived from the field — on screen.
    assert "bot_f_concord_invite" in form and "bot_ft_concord" in form, (
        "the form no longer reads the invite alongside the Concord checkbox, so pasting a link "
        "leaves the listener off and the bot never joins the room it was given")
    assert "checked = true" in form, "pasting an invite no longer ticks the Concord box"

    # ...but it is a default, never a lock. These two are what the second complaint was about.
    assert "pcSeenInvite" in form, (
        "the tick is no longer one-time, so unticking the box is undone on the next form change")
    assert "box.disabled = false" in form, "the Concord checkbox is being disabled again"

    # And Save must read the BOX, never the field — otherwise the tick is decorative.
    block = JS[JS.index("function _buildModes("):]
    block = block[:block.index("\n}")]
    # `--concord` reaches the command line through the feature table, which is the single place
    # every checkbox is mapped; asserting a literal here would just pin where the string is typed.
    assert "bot_ft_concord: '--concord'" in JS, "the Concord feature lost its mode flag"
    assert "BOT_FEATURES" in block, "Save no longer reads the feature checkboxes at all"
    assert "bot_f_concord_invite" not in block, (
        "Save derives --concord from the invite field again, which is what made the checkbox "
        "impossible to untick")

    # Nothing is silently ignored: a saved room with the listener off has to say so.
    assert "will not " in form and "join that room" in form, (
        "a bot holding a room it is not listening to no longer explains itself")


def test_the_checkbox_cannot_disagree_with_what_save_will_do():
    """A control showing OFF while the thing is about to be turned ON is how the trap above stayed
    invisible. The form keeps the two in step and explains why the tickbox is locked."""
    block = JS[JS.index("function onBotFormChange("):]
    block = block[:block.index("\n}")]
    assert "bot_ft_concord" in block and "box.checked = true" in block, (
        "the Concord feature no longer reflects a saved invite, so the form can show OFF for a bot "
        "that is about to be saved with the listener ON")
    assert "box.disabled" in block, (
        "the tickbox can be unticked while an invite is saved, which Save then overrides — a "
        "control that silently refuses its own setting")


def test_test_join_does_not_claim_the_bot_has_joined():
    """It proves the LINK opens from this node. It was read as "the bot is in the room", because
    that is what "✓ room — 3 channels" sounds like."""
    block = JS[JS.index("async function testBotConcord("):]
    block = block[:block.index("\n}")]
    assert "Save the bot" in block, (
        "the Test join result does not say that joining still requires a save")


def test_the_invite_reaches_the_bot_process_as_an_env_var():
    """A config key the manager does not inject is a value the bot never sees."""
    assert 'setif("concord_invite", "CONCORD_INVITE")' in MANAGER, (
        "the Concord invite is no longer passed to the bot process")


def test_a_credential_field_is_masked():
    """The `#` fragment IS the room key, so that input grants read access to everything the room has
    ever said. It is masked for the same reason the nsec is, and it is checked because the default
    for a text input is the opposite."""
    block = HTML[HTML.index('id="bot_f_concord_invite"') - 200:HTML.index('id="bot_f_concord_invite"') + 200]
    assert 'type="password"' in block, "the Concord invite is rendered as plain text"
