"""A text arriving on a laptop must be noticeable, and must not overwrite the last one.

Reported: "we need notification for SMS messages on the client side too. easy to miss them" and
"on desktop, be good to know you got a SMS".

The notification existed. Three things made it miss:

* **ONE TAG FOR EVERY TEXT.** A notification tag REPLACES the card already under it — that is what a
  tag is for — so a literal `sms` meant the second person to text you silently overwrote the first,
  and a burst of three conversations left exactly one card on screen.
* **NO IN-APP HALF.** Direct messages raise both a toast and an OS notification; a toast needs no
  permission and is the only half that shows while you are on another screen of this app with OS
  notifications denied or ignored. Texts raised the OS one alone.
* **NOTHING TO CLICK THROUGH TO.** No route and no handler, so a click focused the app and left the
  reader to find the conversation — the same failure the DM route had to be taught about this week.
"""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
SMS = (ROOT / "static/js/client/sms.js").read_text(encoding="utf-8")
APP = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")
PREFS = (ROOT / "app/services/push_prefs.py").read_text(encoding="utf-8")
BODY = SMS.split("async function notifyNew(ev){", 1)[1].split("\n  }", 1)[0]


def test_each_conversation_gets_its_own_notification():
    assert "tag = 'sms:' +" in BODY, BODY[:400]
    assert "{ tag:'sms' }" not in SMS, "the single shared tag is what collapsed them"


def test_it_also_says_so_inside_the_app():
    """The half that works with OS notifications switched off."""
    assert "PC.notifToast" in BODY, BODY[:400]
    assert re.search(r"^\s*notifToast,", APP, re.M), "…and it has to be on the shared PC surface"


def test_the_notification_opens_that_conversation():
    assert "route:'texts'" in BODY, BODY[:400]
    assert "onClick: land" in BODY and "S.open = key(m.address)" in BODY, BODY[:400]


def test_a_text_is_a_notification_type_people_can_switch_off():
    """Every other kind is togglable; one that is not is one people turn off by turning off all."""
    assert "['sms','Text messages']" in APP
    assert '"sms"' in PREFS, "the push watcher shares ONE vocabulary with the client's toggles"


def test_the_type_resolves_from_a_per_conversation_tag():
    """`_notificationType` matched literal tags; the tag is now `sms:<address>`, so a prefix match
    is what keeps the toggle connected to the notification it governs."""
    fn = APP.split("function _notificationType(opts){", 1)[1].split("\n  }", 1)[0]
    assert "tag.startsWith('sms:')" in fn, fn


def test_it_still_refuses_the_cases_it_always_refused():
    """The phone posts its own; a message we sent is not news; and a first sync of a thousand
    messages must not fire a thousand notifications."""
    assert "if(await isPhone()) return;" in BODY
    assert "!m.incoming" in BODY
    assert "< S.since) return;" in BODY
