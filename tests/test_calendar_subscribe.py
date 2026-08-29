"""Subscribing to somebody else's published calendar.

Run: venv-unified/bin/python -m pytest tests/test_calendar_subscribe.py

Asked for with a real URL — a school's term calendar — and that URL is what shaped the design, because
it broke two assumptions on contact:

  * it publishes no `X-WR-CALNAME`, so a name has to come from somewhere that is not the generated id
    ("www-canoncityschools-org" in a sidebar is an id leaking into a label);
  * its TLS chain ends at "ISRG Root YR", a Let's Encrypt root that neither certifi 2026.05 nor this
    OS carries yet. A perfectly valid certificate that every correct client refuses. Treating that
    like a typo would make the feature look broken for feeds that are fine.

The other half is that a subscription is a MIRROR, not an import: the remote end is the truth, so a
refresh must delete what the feed has dropped — and the moment code deletes on the strength of a read,
the read failing has to mean "do nothing" rather than "everything is gone".

Fetching a user-supplied URL from the server is SSRF, so that is asserted directly, including under
the certificate opt-in — the two are unrelated and conflating them would turn a cosmetic trust-store
gap into a way to read the cloud metadata endpoint.
"""
import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services import caldav_subscribe as cs   # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


# ---------------------------------------------------------------------------------------------
# the URL


@pytest.mark.parametrize("raw,want", [
    ("webcal://p24-caldav.icloud.com/published/2/x", "https://p24-caldav.icloud.com/published/2/x"),
    ("webcals://example.org/a.ics", "https://example.org/a.ics"),
    ("  https://example.org/a.ics ", "https://example.org/a.ics"),
])
def test_webcal_is_the_link_people_are_actually_given(raw, want):
    """Every calendar publisher links `webcal://`, and it is https with a scheme nobody implements.
    Refusing it would mean telling people to edit the link before pasting it."""
    assert cs.normalize_url(raw) == want


@pytest.mark.parametrize("url,want", [
    ("https://www.canoncityschools.org/schools/harrison/calendar/feed/ical.ics", "canoncityschools"),
    ("https://calendar.google.com/calendar/ical/x/basic.ics", "google"),
    ("https://sub.example.co.uk/a.ics", "example"),
])
def test_a_feed_with_no_name_still_gets_a_readable_one(url, want):
    """The real feed publishes no X-WR-CALNAME. Falling through to the id put
    "www-canoncityschools-org" in the sidebar as the calendar's NAME."""
    assert cs.host_label(url) == want


def test_the_feeds_own_name_wins_when_it_has_one():
    ics = "BEGIN:VCALENDAR\r\nX-WR-CALNAME:Harrison K-8 Athletics\r\nEND:VCALENDAR\r\n"
    assert cs.name_in(ics) == "Harrison K-8 Athletics"
    assert cs.name_in("BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n") == ""


# ---------------------------------------------------------------------------------------------
# SSRF — the reason this feature is dangerous at all


@pytest.mark.parametrize("bad", [
    "http://127.0.0.1/x.ics",
    "http://169.254.169.254/latest/meta-data/",          # cloud metadata
    "http://[::1]/x.ics",
    "http://192.168.0.1/cal.ics",
    "http://nas.lan/cal.ics",
    "file:///etc/passwd",
    "gopher://example.org/x",
])
@pytest.mark.parametrize("insecure", [False, True])
def test_a_private_address_is_never_fetched(bad, insecure):
    """…and the certificate opt-in does NOT relax this. `insecure` skips certificate VERIFICATION for
    one feed; the SSRF guard is what protects this server, and they are unrelated checks."""
    with pytest.raises(Exception) as e:
        _run(cs.fetch_ics(bad, insecure=insecure))
    assert not isinstance(e.value, cs.CertificateProblem)


def test_the_guard_runs_on_every_redirect_hop():
    """A public URL that 302s to 169.254.169.254 passes a check made only on the first one. That was
    a real hole in fetch_url_content, found the same way."""
    src = (ROOT / "app" / "services" / "caldav_subscribe.py").read_text(encoding="utf-8")
    body = src[src.index("async def fetch_ics("):src.index("async def refresh(")]
    assert "follow_redirects=False" in body, "httpx follows redirects itself, unchecked"
    assert body.count("is_safe_host") == 1 and "for _ in range(MAX_REDIRECTS" in body, (
        "the guard is not inside the redirect loop")
    i, j = body.index("for _ in range(MAX_REDIRECTS"), body.index("client.get(cur")
    assert body.index("is_safe_host") > i and body.index("is_safe_host") < j, (
        "the SSRF guard does not run before each hop's request")


# ---------------------------------------------------------------------------------------------
# the certificate case, which is the one with a second answer


def test_a_certificate_failure_is_its_own_kind_of_error():
    """Everything else here means "you cannot have this". A certificate that will not chain usually
    means the publisher's server is misconfigured or uses a root the trust stores have not shipped —
    not the reader's fault and not something they can fix."""
    assert issubclass(cs.CertificateProblem, ValueError)
    assert cs._is_cert_error(Exception("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"))
    assert not cs._is_cert_error(Exception("Connection refused"))


def test_the_router_marks_it_so_the_client_can_offer_a_choice():
    src = (ROOT / "app" / "routers" / "calendar.py").read_text(encoding="utf-8")
    assert '"certificate": True' in src, "the client cannot tell a certificate problem from a typo"
    js = (ROOT / "static" / "js" / "client" / "calendar.js").read_text(encoding="utf-8")
    assert "d.certificate" in js and "Subscribe anyway" in js


def test_a_structured_detail_survives_the_client_error_wrapper():
    """`new Error(anObject)` stringifies to "[object Object]" and drops the flag entirely — the
    certificate branch above would be a branch that could never run."""
    js = (ROOT / "static" / "js" / "client" / "calendar.js").read_text(encoding="utf-8")
    i = js.index("async function api(path, opts)")
    body = js[i:i + 1400]
    assert "e.detail = d;" in body, "the client throws away a structured error detail"


def test_the_opt_in_is_stored_per_feed_and_not_globally():
    src = (ROOT / "app" / "services" / "caldav_subscribe.py").read_text(encoding="utf-8")
    assert "insecure=bool(sub.get(\"insecure\"))" in src, (
        "a refresh does not carry the per-feed choice, so it would fail forever after the first fetch")
    assert "verify=not insecure" in src


# ---------------------------------------------------------------------------------------------
# mirror semantics


def test_a_refresh_that_cannot_read_what_is_here_does_not_prune():
    """The mirror deletes what the feed has dropped. An unreadable item list answers the same empty
    list as "this calendar is empty" — and deleting on the strength of that empties a calendar. Same
    shape as the replaceable-document wipe, with a different costume."""
    src = (ROOT / "app" / "services" / "caldav_subscribe.py").read_text(encoding="utf-8")
    i = src.index("have, prunable = {}, True")
    body = src[i:i + 1800]
    assert "prunable = False" in body
    assert "if prunable:" in body, "the prune runs whether or not the read succeeded"


def test_a_failed_fetch_deletes_nothing_and_records_why():
    """Stale events that look current are worse than an obviously broken calendar."""
    src = (ROOT / "app" / "services" / "caldav_subscribe.py").read_text(encoding="utf-8")
    i = src.index("    except Exception as e:\n        # The ERROR IS STORED")
    body = src[i:i + 600]
    assert "delete_item" not in body
    assert 'out["error"]' in body and "return {\"ok\": False" in body


def test_unsubscribing_keeps_the_events():
    """"Stop updating this" turning into "erase everything it ever gave me" is a surprise nobody
    wants — and the events are already on the person's phone. Deleting the calendar is a separate
    button that says what it does."""
    src = (ROOT / "app" / "routers" / "calendar.py").read_text(encoding="utf-8")
    i = src.index("async def unsubscribe(")
    body = src[i:src.index("@router.delete(\"/calendars/{cal_id}\")")]
    assert "delete_item" not in body and "delete_calendar" not in body
    assert 'k not in ("subscribe", "id")' in body, "unsubscribe does not actually drop the key"


def test_the_worker_refreshes_them_so_a_phone_sees_new_events():
    """These calendars are read on a PHONE, over CalDAV, by an app that never opens PosterChan. A
    refresh that only ran when somebody looked at the web UI would leave the phone confidently
    showing last term."""
    worker = (ROOT / "app" / "worker.py").read_text(encoding="utf-8")
    assert "start_calendar_subscriptions_scheduler" in worker
    src = (ROOT / "app" / "services" / "caldav_subscribe.py").read_text(encoding="utf-8")
    assert "def start_calendar_subscriptions_scheduler" in src
    assert "forget_user" in src, (
        "a phone reads through Radicale's cache; without invalidating it the new events are on the "
        "relay and invisible until the app restarts")


def test_a_user_whose_calendar_list_will_not_load_is_skipped_not_pruned():
    src = (ROOT / "app" / "services" / "caldav_subscribe.py").read_text(encoding="utf-8")
    i = src.index("            try:\n                cals = await caldav_store.list_calendars(db, user)")
    assert "continue" in src[i:i + 700]


def test_due_retries_a_failing_feed_instead_of_giving_up():
    """These fail because a school's web host is down for an afternoon. A subscription that quietly
    stops trying is the thing nobody notices until they miss something."""
    import time
    assert cs.due({"checked": 0}) is True
    assert cs.due({"checked": int(time.time())}) is False
    assert cs.due({"checked": int(time.time()) - 7 * 3600}) is True
    # A feed that has never SUCCEEDED but was checked a minute ago is not due again yet.
    assert cs.due({"checked": int(time.time()) - 60, "error": "boom"}) is False


def test_a_subscribed_calendar_refuses_to_be_edited():
    """A mirror does not merely LOSE an edit on the next refresh — it saves it first, so it looks like
    it worked, and the event disappears hours later with nothing to explain it."""
    js = (ROOT / "static" / "js" / "client" / "calendar.js").read_text(encoding="utf-8")
    i = js.index("function editEvent(ev)")
    body = js[i:i + 1400]
    assert "subOf(sc)" in body, "the editor opens on a calendar that follows a feed"
    assert "return;" in body[body.index("subOf(sc)"):]
    # …and the helper has to exist before the editor runs, not be declared beside the manager.
    assert js.index("const subOf =") < i


def test_the_worker_does_not_poll_every_account_forever():
    """Listing a user's calendars is a relay query. Doing that for every account on the node every
    half hour, for a feature almost nobody uses, is the steady background cost that only ever shows
    up as "why is the relay busy"."""
    src = (ROOT / "app" / "services" / "caldav_subscribe.py").read_text(encoding="utf-8")
    assert "_QUIET" in src and "_QUIET_EVERY" in src
    i = src.index("for user in users:")
    body = src[i:i + 900]
    assert "_QUIET.get(uid, 0)" in body, "every user is queried on every tick"
    assert "_QUIET.pop(uid, None)" in src, (
        "a user who ADDS a subscription would stay in the slow lane for hours")


def test_the_synthetic_id_is_not_written_back_into_the_document():
    """`id` comes from the document's d-tag; storing it inside makes the document claim an id it does
    not own, which is one rename away from the two disagreeing."""
    src = (ROOT / "app" / "services" / "caldav_subscribe.py").read_text(encoding="utf-8")
    i = src.index("async def _save_meta(")
    assert 'if k != "id"' in src[i:i + 600]


# ---------------------------------------------------------------------------------------------
# offline: the calendar catching up with Notes


CALJS = (ROOT / "static" / "js" / "client" / "calendar.js").read_text(encoding="utf-8")


def test_an_event_written_offline_is_kept_and_sent_later():
    """"What about calendar syncing like notes/music?" — the read cache landed first; this is the
    other half. Notes can be written on a train and published later; the calendar could not, because
    every write is an HTTP call and a failed one was a toast and nothing else. Adding an event is
    exactly what people do while offline, which makes it the worst one to lose."""
    assert "const CalQueue = {" in CALJS
    # The EDITOR's save, not the queue's own replay — both call jput, and the first occurrence in the
    # file is the replay.
    i = CALJS.index("closeModal(); toast('saved'); await load();")
    body = CALJS[i:i + 900]
    assert "CalQueue.add({ op:'put'" in body, "a failed save is still just a toast"
    assert "_applyLocal(" in body, (
        "the event would not appear on the grid, which reads as the save having failed")


def test_a_refusal_is_not_queued():
    """A 4xx means the server read it and said no. Queueing that retries a rejection for ever while
    telling the user it was saved."""
    for anchor in ("closeModal(); toast('saved'); await load();",
                   "closeModal(); toast('deleted'); await load();"):
        i = CALJS.index(anchor)
        body = CALJS[i:i + 900]
        assert "err.status < 500" in body, f"a refusal is queued as if it were a network failure ({anchor[:30]})"


def test_replaying_the_queue_is_safe_to_do_blindly():
    """An item is addressed by (calendar, uid) and put_item REPLACES, so re-sending is a no-op and a
    delete for something already gone answers 404. That is what makes a blind flush right here and
    wrong for Notes, whose queue has to check for a newer version first."""
    i = CALJS.index("async flush(){")
    body = CALJS[i:i + 1400]
    assert "err.status < 500" in body, "a permanently-refused write would never leave the queue"
    assert "left.push(op)" in body


def test_the_queue_drains_the_moment_the_server_answers():
    """The config call reaching the server IS the proof it is reachable, and draining before the read
    means what comes back already includes it."""
    # Account-switch generation guards deliberately split the await from the assignment. Anchor on
    # the request whose successful answer proves reachability, then require flush before discovery.
    i = CALJS.index("await api('/api/calendar/config')")
    flush = CALJS.index("CalQueue.flush()", i)
    read = CALJS.index("await api('/api/calendar/calendars')", i)
    assert i < flush < read


def test_the_screen_says_when_it_is_showing_a_saved_copy():
    """Silently showing stale data is the failure people cannot see."""
    assert "waiting to sync" in CALJS and "offline copy" in CALJS
    css = (ROOT / "static" / "css" / "client.css").read_text(encoding="utf-8")
    assert ".cal-pending{" in css


def test_editing_the_same_event_twice_offline_sends_once():
    i = CALJS.index("async add(op){")
    body = CALJS[i:i + 600]
    assert "x.op + '|' + x.cal + '|' + x.uid" in body, "the queue grows one entry per keystroke-save"


def test_a_write_the_server_later_refuses_is_not_dropped_in_silence():
    """The person was told "saved on this device — it will sync when you are back online". Dropping
    it quietly makes that a lie they find out about weeks later, when the appointment does not
    happen."""
    js = (ROOT / "static" / "js" / "client" / "calendar.js").read_text(encoding="utf-8")
    i = js.index("async flush(){")
    body = js[i:i + 2000]
    assert "refused.push(op)" in body, "a refused write vanishes"
    assert "refused by the server" in body, "nothing tells the person it was dropped"


def test_the_pending_badge_is_right_on_a_return_visit():
    """It is about the QUEUE, not the snapshot — so reading it after the "live data is already here"
    early return means it is only ever right on the first entry of a session."""
    js = (ROOT / "static" / "js" / "client" / "calendar.js").read_text(encoding="utf-8")
    i = js.index("async function loadCached(){")
    body = js[i:i + 900]
    assert body.index("CalQueue.read()") < body.index("if(S.ready || S.cals.length) return false;"), (
        "the queue depth is read after the early return")
