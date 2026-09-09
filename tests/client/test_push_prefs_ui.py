"""The Notifications tab: one place for everything that decides when you are interrupted.

Two separate things are asserted, because they fail differently:

* the per-device PUSH preference layer, driven under node against a stub localStorage — it defaults
  on, persists, hydrates, is scoped per account, and is INDEPENDENT of the account-wide set. That
  last one is a negative and no source assertion can see it;
* the pane's own wiring, which is source-level because it is markup: both sets hydrate from their
  OWN store, the push section is only revealed where push exists, and the mirror to the server
  always names a device.
"""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]
APP = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")


def _pane():
    return APP[APP.index("  function _notificationPane("):APP.index("  function _paintNotificationSettings(")]


def test_the_per_device_push_layer_persists_hydrates_and_stays_separate():
    result = subprocess.run(['node', str(ROOT / 'tests/client/push_prefs_runtime.mjs')],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr


def test_both_sets_hydrate_from_their_own_store():
    """Painting the push boxes from `notificationPreference` would silently re-merge the two lists:
    every box would read right on first open and the phone's choices would be the account's."""
    paint = APP[APP.index("  function _paintNotificationSettings(){"):
                APP.index("  function _wireNotificationSettings(")]
    assert "el.checked=notificationPreference(el.dataset.notificationType)" in paint
    assert "el.checked=pushPreference(el.dataset.pushType)" in paint


def test_the_push_section_is_hidden_until_this_device_actually_has_push():
    """Offering 'what reaches you when the app is closed' on a device that has never registered is
    a control that cannot do anything."""
    assert 'id="us-push-section" hidden' in _pane()
    wire = APP[APP.index("  function _wireNotificationSettings("):
               APP.index("  let _notificationLastSound")]
    assert "await pushState()" in wire and "section.hidden=" in wire


def test_the_mirror_always_names_a_device():
    """Unscoped, the server applies the preferences to EVERY device of the account — which would put
    the desktop's push back under the phone's choices, the exact thing this split prevents. The
    refusal is explicit rather than a fallback."""
    fn = APP[APP.index("  async function mirrorPushPrefs("):APP.index("  function notificationPreference(")]
    assert "body.device_id=" in fn and "body.endpoint=" in fn
    assert "if(!body.device_id && !body.endpoint)return false;" in fn, \
        "the mirror must refuse to send rather than fall back to every device"
    assert "sign(27235,'push-prefs'" in fn, "the write is authenticated, and bound to its purpose"


def test_a_newly_registered_device_is_told_its_preferences_at_once():
    """Without this the device receives everything until the next toggle happens to mirror them."""
    # BOTH transports: the APK registers natively and a browser registers a Web Push subscription,
    # and the phone — the one this feature is for — takes the first of those.
    native = APP[APP.index("  async function _enablePushNative("):APP.index("  async function _registerPushSub(")]
    web = APP[APP.index("  async function _registerPushSub("):APP.index("  async function _pushTestWait(")]
    assert "mirrorPushPrefs(" in native, "a freshly registered PHONE would receive everything"
    assert "mirrorPushPrefs(" in web


def test_the_telegram_notification_settings_moved_and_did_not_get_duplicated():
    """Two copies of one id is worse than the wrong tab: the global Save reads by id, so whichever
    the DOM hands back first silently wins and the other looks like a control that does nothing."""
    assert APP.count('id="us-tg-notif"') == 1
    assert APP.count('id="us-social-notif"') == 1
    pane = _pane()
    assert 'id="us-tg-notif"' in pane and 'id="us-social-notif"' in pane
    telegram = APP[APP.index('<div class="us-pane" data-pane="telegram">'):
                   APP.index('<div class="us-pane" data-pane="social">')]
    assert "us-tg-notif" not in telegram
    # Linking is an ACCOUNT action, not a notification preference, and stays where it was.
    assert "us-tg-key" in telegram


def test_the_relocated_server_settings_stay_out_of_a_serverless_build():
    """They belong to an account on an instance. _standalone() has no such account, and the pane
    already drops the notification-email field for the same reason."""
    pane = _pane()
    i = pane.index('id="us-tg-notif"')
    assert "_standalone()?''" in pane[:i], \
        "the Telegram delivery block must be inside a _standalone() guard"
