"""INSTALLING A NOSTR CLIENT MUST NOT SCATTER FOUR MORE ICONS THROUGH SOMEBODY'S APP DRAWER.

Reported as "posterchan apk is showing all the posterchan apps on peoples phones without it being
the default launcher".

`HomeRoles` already makes exactly this argument about the HOME component, in its own words:

    A CATEGORY_HOME activity makes Android offer this app in the "Select a Home app" chooser from
    the moment it is installed — including to people who installed a Nostr client and have no idea
    it can be a launcher. Disabled by default, enabled at the moment somebody asks, is what makes
    "opt-in" true rather than nearly true.

The HOME role got that treatment. The four LAUNCHER aliases beside it never did: `.sms.Messages`,
`.phone.Phone`, `.shortcut.MediaCenter` and `.shortcut.Email` shipped enabled, so every install put
Texts, Phone, Media Center and Email in the drawer — on phones where the shell was never opted into
and those screens do nothing at all.

The rule is DERIVED FROM THE MANIFEST rather than from the four names, because the failure is not
these four: it is that nothing stopped a fifth being added the same way.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "mobile/android/app/src/main/AndroidManifest.xml"
HOME_ROLES = (ROOT / "mobile/android/app/src/main/java/place/poster/app/home/HomeRoles.java").read_text()
HOME_PLUGIN = (ROOT / "mobile/android/app/src/main/java/place/poster/app/home/HomePlugin.java").read_text()
A = "{http://schemas.android.com/apk/res/android}"

#: The one component that must stay enabled — it IS the app, and an app with no icon cannot be opened.
THE_APP = ".MainActivity"


def _launcher_components():
    """(name, enabled) for every component that puts an icon in the drawer."""
    app = ET.parse(MANIFEST_PATH).getroot().find("application")
    out = []
    for el in app:
        if el.tag not in ("activity", "activity-alias"):
            continue
        if not any(c.get(A + "name") == "android.intent.category.LAUNCHER"
                   for f in el.findall("intent-filter") for c in f.findall("category")):
            continue
        out.append((el.get(A + "name"), el.get(A + "enabled")))
    return out


def test_the_manifest_still_declares_launcher_icons_at_all():
    """The check before the check: if the shape changes this reads nothing and passes vacuously."""
    found = _launcher_components()
    assert len(found) >= 2, f"only {len(found)} launcher components found — re-point this test"
    assert any(n == THE_APP for n, _ in found), "the app itself no longer has a launcher icon"


def test_only_the_app_itself_installs_an_icon():
    """THE RULE, and it is about the fifth alias as much as these four."""
    enabled = [n for n, en in _launcher_components() if n != THE_APP and en != "false"]
    assert not enabled, (
        "these put an icon in every user's app drawer on install, without anybody opting in: "
        + ", ".join(enabled)
        + ". Declare android:enabled=\"false\" and turn it on from HomeRoles.setDrawerIcon when the "
          "phone shell is enabled — the same thing the HOME component already does.")


def test_the_app_itself_is_never_disabled_by_that_rule():
    """The opposite mistake, which would be far worse: an APK that installs with no icon at all."""
    app = dict(_launcher_components()).get(THE_APP, None)
    assert app in (None, "true"), (
        "MainActivity is disabled — the app would install with no way to open it")


def test_the_toggle_list_matches_the_manifest():
    """Two lists that must agree, in two languages. A component disabled in the manifest and missing
    from `DRAWER_ICONS` is an icon nothing can ever turn on — the feature would be unreachable
    instead of opt-in, which is the same bug wearing the opposite sign."""
    block = HOME_ROLES.split("DRAWER_ICONS = {", 1)[1].split("};", 1)[0]
    listed = {n.rsplit(".", 1)[-1] for n in re.findall(r'"([^"]+)"', block)}
    manifest = {n.lstrip(".").rsplit(".", 1)[-1] for n, en in _launcher_components()
                if n != THE_APP and en == "false"}
    assert listed == manifest, (
        "HomeRoles.DRAWER_ICONS and the disabled launcher aliases disagree: "
        f"only in java={sorted(listed - manifest)}, only in manifest={sorted(manifest - listed)}")


def test_the_icons_follow_the_opt_in_in_both_directions():
    """Enabling the shell shows them; disabling it takes them away again. An icon left behind after
    the feature is switched off is the same complaint one step later."""
    on = HOME_PLUGIN.split("public void enableLauncher(", 1)[1].split("}", 1)[0]
    assert "setAllDrawerIcons(getContext(), true)" in on, (
        "opting into the phone shell no longer shows its icons, so the screens exist and nothing "
        "reaches them")
    off = HOME_PLUGIN.split("public void disableLauncher(", 1)[1][:600]
    assert "setAllDrawerIcons(getContext(), false)" in off, (
        "opting out leaves the icons in the drawer")


def test_enabled_is_read_from_the_component_not_from_a_stored_flag():
    """`DEFAULT` means 'whatever the manifest says', and the manifest now says false. Reading it as
    anything but OFF is how a settings screen claims an icon is showing when it is not — the trap
    `launcherComponentEnabled` documents directly above."""
    fn = HOME_ROLES.split("public static boolean drawerIconEnabled(", 1)[1].split("\n    }", 1)[0]
    assert "COMPONENT_ENABLED_STATE_ENABLED" in fn
    assert "getComponentEnabledSetting" in fn, "the answer is inferred rather than measured"
