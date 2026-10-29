"""The menu colours are drawn on a fresh load after the app UPDATES -- not only after a colour is changed again.

Git issue "Menu Separate Color Class" (Vyram), his reply after the fix shipped: "Once I updated social, notifications,
& messages weren't following the menu color. But once I changed the color now they all do."

The menus became a section of Settings -> Appearance (`sec.menu`, apptheme.js) on the web first; the APK and the desktop
app picked it up with their next build. A build from before that read the account's synced look (`appLook` in
`pcai:client-prefs`) through its own `clean()`, which keeps only the sections IT knew -- so its local copy lost `menu`
(and every section's `text`) while keeping the edit's own `at`. After the update, the restore compared clocks only:
the synced copy was not NEWER than the local one (it was the same edit), so it was never taken, and the menus followed
each section's accent -- until a colour was changed, which rewrote the local copy from scratch.

Driven through the shipped client in Chrome (tests/client/test_client_prefs_sync_full_app.py's in-page relay, which
keeps what it accepted across a reload): a look with menu + section colours is saved and synced; the device's local
copy is then replaced with what the older build left behind (the same look, same `at`, without the fields it did not
know); a reload with NOTHING touched must draw the bottom bar and the More sheet in the menu colours.
"""
import asyncio
import json

from tests.client.test_client_prefs_sync_full_app import device, needs_chrome, reload, restored, sent, wait

MENU_ACCENT, MENU_TEXT, SOCIAL_ACCENT, MESSAGES_ACCENT = "#3366ff", "#ffcc00", "#ff0000", "#00b06f"
# The three views Vyram named -- Social (the timeline), Notifications, Messages -- and the section each belongs to.
VIEWS = (("home", "social", SOCIAL_ACCENT), ("notifications", "social", SOCIAL_ACCENT),
         ("messages", "messages", MESSAGES_ACCENT))
VAR = "getComputedStyle(document.querySelector({sel})).getPropertyValue({name}).trim()"

# What a build from before the Menus section wrote to its own storage on adopting this look: its clean() kept only the
# sections it knew and only their accent/background -- and the edit's own `at`.
OLD_BUILD_COPY = """(()=>{const pk=__PC.me().pubkey;
  for(const k of ['pc_look_v1:last','pc_look_v1.'+pk]){const v=JSON.parse(localStorage.getItem(k));
    delete v.sec.menu; for(const s of Object.values(v.sec)) delete s.text;
    localStorage.setItem(k, JSON.stringify(v));}
  return JSON.parse(localStorage.getItem('pc_look_v1.'+pk));})()"""


def _var(sel, name):
    return VAR.format(sel=json.dumps(sel), name=json.dumps(name))


@needs_chrome
def test_menu_colours_survive_an_update_from_a_build_without_them():
    got = {}

    async def act(b):
        await b.call("Emulation.setDeviceMetricsOverride", dict(width=390, height=900, deviceScaleFactor=1, mobile=True))
        await restored(b)
        await b.js(f"PCLook.setSection('social',{{accent:'{SOCIAL_ACCENT}'}});"
                   f"PCLook.setSection('messages',{{accent:'{MESSAGES_ACCENT}'}});"
                   f"PCLook.setSection('menu',{{accent:'{MENU_ACCENT}',text:'{MENU_TEXT}'}})")
        # Three saves in a row: wait for the one carrying the last edit to be the account's copy.
        await wait(b, "(d=>!!d&&!!d.appLook&&d.appLook.at===PCLook.get().at)(__fx.prefsDoc(__PC.me().pubkey))", 30,
                   "the look to reach the account")
        await sent(b)
        got["synced"] = await b.js("__fx.prefsDoc(__PC.me().pubkey).appLook")
        got["old_copy"] = await b.js(OLD_BUILD_COPY)
        await reload(b)
        await restored(b)
        got["views"] = {}
        for view, _, _ in VIEWS:
            await b.js(f"__PC.switchView('{view}')")
            await asyncio.sleep(.5)
            got["views"][view] = {
                "sec": await b.js("document.documentElement.getAttribute('data-pcsec')"),
                "neon": await b.js(_var(":root", "--neon")),
                "nav_neon": await b.js(_var(".mobilenav", "--neon")),
                "nav_text": await b.js(_var(".mobilenav", "--text")),
                # The menu BUTTON the person sees lit: the bottom bar's selected item, drawn in --neon.
                "lit": await b.js("(e=>e?getComputedStyle(e).color:'')(document.querySelector('.mobilenav .nav-item.active'))")}
        await b.js("__PC.switchView('home')")
        await asyncio.sleep(.3)
        await b.js("document.querySelector('.mobilenav [data-v=\"more\"], .mobilenav .more, .mobilenav button:last-child').click()")
        await wait(b, "!!document.querySelector('.modal .more-grid')", 15, "the More sheet")
        got["more"] = {"neon": await b.js(_var(".modal:has(.more-grid)", "--neon")),
                       "label": await b.js("getComputedStyle(document.querySelector('.more-item')).color")}
        got["look"] = await b.js("PCLook.get()")

    asyncio.run(device(act))
    assert got["synced"]["sec"]["menu"]["accent"] == MENU_ACCENT, ("the menu colour never reached the account", got)
    assert "menu" not in got["old_copy"]["sec"] and got["old_copy"]["at"] == got["synced"]["at"], got
    lit = []
    for view, sec, accent in VIEWS:
        v = got["views"][view]
        assert v["sec"] == sec and v["neon"] == accent, (view, "the section's own accent", got)
        assert v["nav_neon"] == MENU_ACCENT and v["nav_text"] == MENU_TEXT, \
            (view, "after the update the bottom bar took the section's colours, not the menu's", got)
        if v["lit"]:
            lit.append(view)
            assert v["lit"] == "rgb(51, 102, 255)", (view, "the selected menu button is not the menu colour", got)
    assert lit, ("no bottom-bar button was lit on any of the three views -- nothing visible was checked", got)
    assert got["more"]["neon"] == MENU_ACCENT and got["more"]["label"] == "rgb(255, 204, 0)", ("the More sheet", got)
