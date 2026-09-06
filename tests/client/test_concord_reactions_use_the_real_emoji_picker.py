"""CONCORD'S REACTION BUTTON HAD ITS OWN PICKER, WITH EIGHT FACES AND NONE OF THE SERVER'S.

Reported as "concord: not displaying custom emojis on the server", alongside "react broken".

The app already has an emoji popover with the instance's packs, search, recents and thousands of
custom emoji; the composer immediately beside this button uses it, and the popover's own comment
says it was written to serve reactions. The reaction button ignored all of that and rendered eight
hardcoded unicode faces into a bespoke element with a second, separate placement implementation --
the one that put the picker in the corner of the screen.

The read side never needed changing: `reactionSummary` has always drawn `reactionUrls[emoji]` as an
`<img>`. Only the picker could not produce a custom emoji, and a reaction published without the
NIP-30 `emoji` tag reaches every other client as the literal text ":partyblob:".

Both halves RUN here. A source assertion would pass against a comment mentioning the popover.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SIM = ROOT / "tests/client/concord_reaction_emoji_sim.mjs"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _sim(**plan):
    out = subprocess.run(["node", str(SIM), json.dumps(plan)], cwd=ROOT, text=True,
                         capture_output=True, timeout=60)
    assert out.returncode == 0, out.stderr[:2000]
    return json.loads(out.stdout)


def test_reacting_opens_the_app_picker_with_the_servers_emoji():
    assert _sim(what="picker", hasPopover=True)["picker"] == "app"


def test_an_older_shell_still_gets_a_picker_rather_than_a_dead_button():
    """A bundle predating the popover loses eight faces; it must not lose the button."""
    assert _sim(what="picker", hasPopover=False)["picker"] == "inline"


def test_a_custom_emoji_publishes_the_nip30_tag_that_carries_its_picture():
    """Without it the reaction arrives everywhere else as the literal text ':partyblob:'."""
    r = _sim(what="tags", emoji=":partyblob:", known=True)
    assert r["tags"] == [["emoji", "partyblob", "https://emoji.example/partyblob.png"]], r
    assert r["localUrl"].startswith("https://"), r


def test_a_unicode_emoji_carries_no_tag():
    """👍 needs no picture, and an empty emoji tag is a malformed event."""
    assert _sim(what="tags", emoji="👍", known=True)["tags"] == []


def test_a_shortcode_this_instance_does_not_know_publishes_no_broken_tag():
    """Better a shortcode nobody renders than an `emoji` tag pointing nowhere."""
    assert _sim(what="tags", emoji=":unknown:", known=False)["tags"] == []


def test_a_shortcode_whose_url_is_not_http_is_refused():
    """The URL goes into an <img src> on every reader's screen."""
    assert _sim(what="tags", emoji=":x:", known=False)["tags"] == []
