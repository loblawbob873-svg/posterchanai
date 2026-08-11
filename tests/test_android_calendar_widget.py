"""The home-screen calendar widget.

Run: venv-unified/bin/python -m pytest tests/test_android_calendar_widget.py

Asked for as "add nice calendar widget for android devices for desktop/homescreen thing", then
narrowed: "should show events for the next few days like the desktop widget".

None of it can be driven here (no device; Gradle runs on CI), so what is guarded is the WIRING and
the two decisions that make a calendar widget either useful or quietly wrong:

  * WHICH DAY IS TODAY is decided at DRAW time, not at push time. A widget that trusted when it was
    written shows yesterday's list from the first midnight after the app was last opened — and a
    calendar that is confidently wrong is worse than one that admits it is stale. That is also why
    ACTION_DATE_CHANGED is registered: without it nothing redraws at midnight.
  * THE DATA IS PUSHED, not computed natively. A calendar item is an encrypted document and the
    launcher has no key — but the sharper reason is that a second iCalendar parser and a second
    recurrence expander in Java is how the widget and the app end up disagreeing about what day
    something is on.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ANDROID = os.path.join(ROOT, "mobile", "android", "app")
JAVA = os.path.join(ANDROID, "src", "main", "java", "place", "poster", "app")


def _read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as fh:
        return fh.read()


MANIFEST = _read(ANDROID, "src", "main", "AndroidManifest.xml")
MAIN = _read(JAVA, "MainActivity.java")
WIDGET = _read(JAVA, "calendar", "CalendarWidget.java")
PLUGIN = _read(JAVA, "calendar", "CalendarPlugin.java")
CALJS = _read(ROOT, "static", "js", "client", "calendar.js")
APPJS = _read(ROOT, "static", "js", "client", "app.js")


def test_the_widget_and_its_plugin_are_declared():
    assert re.search(r'android:name="\.calendar\.CalendarWidget"', MANIFEST), "no widget receiver"
    assert "@xml/calendar_widget_info" in MANIFEST
    assert "registerPlugin(place.poster.app.calendar.CalendarPlugin.class);" in MAIN
    assert 'name = "CalendarWidget"' in PLUGIN
    assert "PC.capPlugin('CalendarWidget', 'push')" in CALJS, "the JS asks for a different name"
    for f in ("res/layout/widget_calendar.xml", "res/xml/calendar_widget_info.xml"):
        assert os.path.exists(os.path.join(ANDROID, "src", "main", f)), f"{f} is missing"


def test_the_plugin_lookup_can_find_a_java_only_plugin():
    """`Capacitor.Plugins.<name>` is EMPTY for a plugin registered in Java with no JS package of its
    own — `registerPlugin(name)` is what finds it. A module reaching for the map directly gets null
    on the one build the plugin exists in, does nothing, and says nothing."""
    assert "capPlugin: _capPlugin," in APPJS, "the shared lookup is not on the bridge"
    i = APPJS.index("function _capPlugin(")
    assert "cap.registerPlugin" in APPJS[i:i + 500]


def test_which_day_is_today_is_decided_when_it_is_drawn():
    """A widget that trusted the push time shows yesterday's list from the first midnight after the
    app was last opened."""
    i = WIDGET.index("private static RemoteViews build(")
    body = WIDGET[i:]
    assert "Calendar.getInstance()" in body, "the widget does not read the clock when it draws"
    assert 'KEY_DAYS' in WIDGET and "optJSONArray(k)" in body, (
        "the stored data is not keyed by date, so it cannot be re-read for a different day")


def test_midnight_redraws_it():
    assert "android.intent.action.DATE_CHANGED" in MANIFEST, (
        "nothing redraws the widget at midnight — it shows yesterday until something else happens")
    assert "Intent.ACTION_DATE_CHANGED.equals(a)" in WIDGET


def test_it_fills_its_rows_from_the_days_AFTER_today():
    """"should show events for the next few days like the desktop widget" — an empty day is the
    common case, and "nothing on today" by itself is less use than what is actually coming."""
    i = WIDGET.index("int shown = 0, more = 0;")
    body = WIDGET[i:i + 1800]
    assert "for (int d = 0; d < WINDOW_DAYS" in body, "the widget only ever looks at today"
    assert "cur.add(Calendar.DAY_OF_YEAR, 1)" in body
    assert 'SimpleDateFormat("EEE"' in body, (
        "a later day's row is not labelled, so it reads as one of today's")


def test_the_client_pushes_the_same_window_the_widget_reads():
    """Two constants for one number is how the widget ends up with six days and the app sends five."""
    assert "public void window(PluginCall call)" in PLUGIN
    assert "WINDOW_DAYS" in PLUGIN
    assert "P.window()" in CALJS, "the client hardcodes its own window instead of asking"


def test_the_push_happens_after_the_data_lands():
    """Pushing before S.items is filled blanks a correct widget for as long as the load takes."""
    i = CALJS.index("async function load(){")
    body = CALJS[i:CALJS.index("// ---- rendering", i)]
    assert "pushWidget()" in body
    assert body.index("S.items = items") < body.index("pushWidget()")


def test_recurrence_is_expanded_for_the_widget_too():
    """The month grid shipped once placing only DTSTART, and 59 of a real 707-event calendar — every
    weekly delivery, every birthday — drew exactly once. A widget with that bug shows an empty week."""
    i = CALJS.index("async function pushWidget()")
    body = CALJS[i:i + 2200]
    assert "I.occurrences(" in body and "I.parseResource(" in body


def test_a_finished_event_is_dimmed_rather_than_dropped():
    i = CALJS.index("async function pushWidget()")
    assert "p: !o.allDay && o.start < now" in CALJS[i:i + 2200]
    assert "setTextColor" in WIDGET, "the widget has no way to show one differently"


def test_an_empty_widget_says_which_kind_of_empty_it_is():
    """"Nothing on" and "this has never been given anything" look identical as a blank box — and the
    second is the one a person should act on."""
    assert "Open PosterChan to load your calendar." in WIDGET
    i = WIDGET.index("if (shown == 0)")
    assert "known ?" in WIDGET[i:i + 400]


def test_every_pending_intent_is_immutable():
    """On Android 12+ a mutable PendingIntent throws the moment the widget is built."""
    assert re.search(r"int f = PendingIntent\.FLAG_UPDATE_CURRENT[\s\S]{0,160}FLAG_IMMUTABLE", WIDGET)
    for args in re.findall(r"PendingIntent\.get(?:Activity|Service|Broadcast)\(([\s\S]{0,200}?)\);", WIDGET):
        # The capture can end on the call's own closing paren when it spans a line.
        assert re.search(r",\s*f\s*\)?$", args.strip()), f"a PendingIntent is not immutable: {args}"


def test_tapping_it_opens_the_calendar_and_the_press_is_consumed():
    """It can launch the app cold OR foreground it through onNewIntent, and either way the press is
    an Intent extra rather than a JS event. Consumed, so a resume cannot replay it."""
    assert "EXTRA_OPEN_CALENDAR" in WIDGET and "EXTRA_OPEN_CALENDAR" in PLUGIN
    assert "removeExtra(EXTRA_OPEN_CALENDAR)" in PLUGIN, "the press replays on every resume"
    assert "_reCal" in APPJS and "switchView('calendar')" in APPJS


def test_the_widget_never_polls():
    """The platform clamps updatePeriodMillis to 30 minutes, so polling would be a wake-up an hour to
    redraw the same list. Everything that changes the display arrives as a broadcast."""
    info = _read(ANDROID, "src", "main", "res", "xml", "calendar_widget_info.xml")
    assert 'android:updatePeriodMillis="0"' in info


def test_the_widget_survives_the_app_not_being_opened_for_weeks():
    """The push is one pass over data the client already decrypted and a few KB of preferences. The
    cost of the window running out is a widget that goes blank on a phone whose owner has not opened
    the app since the holidays."""
    assert "WINDOW_DAYS = 31;" in WIDGET, "the widget only caches a week"
    i = CALJS.index("async function pushWidget()")
    body = CALJS[i:i + 2400]
    assert "Math.min(62, span)" in body, (
        "an unbounded window from the plugin would expand recurrence over an arbitrary range")


def test_the_calendar_screen_works_with_no_network():
    """Calendar items come from /api/calendar/*, so with no server the screen was a spinner and then
    an error — on the app that keeps Notes, Passwords and the timeline working offline."""
    assert "const CalCache = {" in CALJS
    assert "indexedDB.open(this.DB" in CALJS, (
        "the cache is not in IndexedDB — localStorage is a shared ~5MB quota for the whole origin, "
        "and a real calendar is hundreds of KB of raw iCalendar")
    i = CALJS.index("function render(){")
    body = CALJS[i:i + 900]
    assert "loadCached()" in body and body.index("loadCached()") < body.index("load()", body.index("loadCached()")), (
        "the cache is not painted before the network is asked")


def test_a_stale_calendar_says_so_instead_of_reporting_a_failure():
    """The month you are looking at is real, it is just not fresh."""
    assert "showing your saved calendar" in CALJS
    i = CALJS.index("S.enabled = /off on this node/i")
    assert "S.cals.length" in CALJS[i:i + 500], (
        "an error is reported the same way whether or not there is a cache behind it")


def test_the_cache_holds_only_what_the_server_can_already_read():
    """A real trade, and worth stating: cached items are readable to anything that can read this
    device's IndexedDB. The calendar is explicitly the one part of this app the SERVER can read too
    (a CalDAV client sends plaintext), so a device-local copy is not a new exposure — which is
    precisely why Notes and the vault, which the server CANNOT read, are not cached this way."""
    i = CALJS.index("const CalCache = {")
    head = CALJS[max(0, i - 1400):i]
    assert "docs/CALENDAR.md" in head or "SERVER can read" in head, (
        "the cache does not say what it exposes")
    for f in ("notes.js", "vault.js"):
        src = _read(ROOT, "static", "js", "client", f)
        assert "CalCache" not in src


def test_the_widget_is_fed_without_opening_the_calendar_screen():
    """pushWidget runs at the end of load(), and load() only runs when the Calendar is RENDERED — so
    somebody who adds events on a laptop and only ever glances at the phone's widget would see one
    that was never filled at all."""
    assert "async function widgetTick(" in CALJS
    assert "widgetTick" in APPJS, "nothing calls it outside the Calendar screen"
    i = APPJS.index("PCCalendar.widgetTick()")
    assert "setTimeout" in APPJS[max(0, i - 200):i], (
        "it runs during the first paint, which is the one moment nothing is waiting for it")


def test_it_spends_disk_before_it_spends_the_network():
    """The snapshot is already there. A widget filled from a four-hour-old copy beats an empty one
    while a request is in flight, and beats it entirely if the request fails."""
    i = CALJS.index("async function widgetTick(")
    body = CALJS[i:i + 1800]
    assert body.index("CalCache.read()") < body.index("await load()")
    assert body.index("pushWidget()") < body.index("await load()"), (
        "the widget waits for the network before drawing anything")
    assert "age >= (maxAgeH == null ? 6 : maxAgeH)" in body, (
        "it fetches on every call rather than when the snapshot is actually stale")


def test_a_resume_does_not_become_a_request():
    """Resuming is frequent. The staleness window there is deliberately wider than at startup."""
    i = APPJS.index("PCCalendar.widgetTick(12)")
    assert "st.isActive" in APPJS[max(0, i - 600):i]


def test_it_is_a_no_op_off_the_packaged_app():
    i = CALJS.index("async function widgetTick(")
    body = CALJS[i:i + 400]
    assert "PC.capPlugin('CalendarWidget', 'push')" in body and "return;" in body
