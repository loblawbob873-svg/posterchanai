"""Calendar deletes the relay REFUSES, driven end to end in the shipped client against the node's REAL calendar router.

  The node   app/routers/calendar.py mounted in-process (discover_flows_harness.Routers), over the REAL
             app/services/caldav_store -> nostr_store -> THIS node's relay (the shipped RelayServer over PosterChanDB):
             every event is an encrypted kind-30078 document, read back here with caldav_store.get_items exactly as
             the router reads it. Only the storage key is chosen by the test (one per account).
  Refusal    the home relay answering a NIP-09 deletion (kind 5) from the account's storage key with OK false, as a
             relay says no -- for every deletion, or for the first one only (one event of a calendar refused, the
             rest accepted).

What is held: a delete the relay refused is never reported as done. An event's Delete never toasts "deleted" over an
event that is still on the relay (the calendar's own offline queue keeps it and says so, and it drains once the relay
accepts); a calendar's Delete never toasts "calendar deleted" while any of its events is still on the relay, and never
leaves events behind with their calendar gone (data on the relay that no screen can show or delete).

Harness: tests/client/discover_flows_harness.py. Fail-first: copy an older app/routers/calendar.py /
app/services/caldav_store.py over the working tree (the routers run in this process).
"""
import asyncio
import datetime
import hashlib
import json

import pytest

from tests.client import discover_flows_harness as F
from tests.client import realnode_harness as R

CHROME = pytest.mark.skipif(not R.chrome_ok(), reason='Chrome required')
FEATURE_COVERS = ('calendar',)
FIRST_FILL = 200           # 200 .. (account keys are one byte; each module has its own node)
SCENARIOS = {}
COST = {}


def scenario(fn):
    SCENARIOS[fn.__name__] = fn
    return fn


def storage_sk(u):
    return hashlib.sha256(b'pc-test-storage:' + u.nostr_npub.encode()).digest()


class Ctx:
    def __init__(self, n):
        from app.routers import calendar
        from app.services import caldav_store, user_settings_table
        self.n = n
        self.mp = mp = pytest.MonkeyPatch()
        mp.setattr(caldav_store, 'enabled', lambda: True)
        mp.setattr(caldav_store, '_port', lambda db=None: n.home.port)
        mp.setattr(caldav_store, 'user_storage_seckey', lambda db, u: storage_sk(u))
        mp.setattr(calendar, '_forget', lambda username: None)       # no Radicale disk cache here

        async def no_password(db, uid, key):
            return False
        mp.setattr(user_settings_table, 'ahas', no_password)         # /config's "has a phone password" (no DB here)
        self.users = F.members(FIRST_FILL, SCENARIOS)
        self.api = F.Routers(mp, [(calendar.router, '')], self.users, paths=['/api/calendar/'])
        # storage pubkey -> how many more deletions (kind 5) to refuse (None: every one).
        self.refuse = {}
        orig = n.home.relay._dispatch

        async def dispatch(conn, raw):
            try:
                m = json.loads(raw)
            except (TypeError, ValueError):
                m = None
            if (isinstance(m, list) and len(m) > 1 and m[0] == 'EVENT' and isinstance(m[1], dict)
                    and m[1].get('kind') == 5 and m[1].get('pubkey') in self.refuse):
                left = self.refuse[m[1]['pubkey']]
                if left is None or left > 0:
                    if left is not None:
                        self.refuse[m[1]['pubkey']] = left - 1
                    await conn.send(json.dumps(['OK', m[1].get('id'), False, 'blocked: not now']))
                    return
            await orig(conn, raw)
        n.home.relay._dispatch = dispatch

    def storage_pk(self, pk):
        from app.services.nostr import bip340
        return bip340.pubkey_from_seckey(storage_sk(self.users[pk])).hex()

    def call(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.api.loop).result(60)

    def items(self, pk, cal):
        from app.services import caldav_store
        return sorted(r['uid'] for r in self.call(caldav_store.get_items(None, self.users[pk], cal, strict=True)))

    def calendars(self, pk):
        from app.services import caldav_store
        return [c['id'] for c in self.call(caldav_store.list_calendars(None, self.users[pk], strict=True))]

    def seed(self, pk, cal, events):
        """A calendar made by the node's own store (the producer every client's write goes through)."""
        from app.services import caldav_store
        u = self.users[pk]
        assert self.call(caldav_store.put_calendar(None, u, cal, {'displayname': cal.title(), 'kind': 'VCALENDAR'}))
        day = datetime.date.today().strftime('%Y%m%d')
        for uid, title in events:
            ics = ('BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\nBEGIN:VEVENT\r\nUID:%s\r\n'
                   'DTSTAMP:%sT000000Z\r\nDTSTART;VALUE=DATE:%s\r\nSUMMARY:%s\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n'
                   % (uid, day, day, title))
            assert self.call(caldav_store.put_item(None, u, cal, uid, ics, 'VEVENT'))

    def stop(self):
        self.api.stop()
        self.mp.undo()


@pytest.fixture(scope='module')
def ran(request):
    return F.run(request, SCENARIOS, COST, FIRST_FILL, setup=Ctx)


TOASTS = "(window.__toastLog||[])"
CONFIRM = ".uiconfirm-btns [data-uc=\"1\"]"


async def press(d, sel, what):
    """Find `sel` and click it in ONE evaluation, retried until it is there -- the screen repaints wholesale
    (innerHTML), so a node found by one call can be gone by the next."""
    await F.wait(d, "(e=>!!e&&(e.click(),true))(document.querySelector(%s))" % json.dumps(sel), 15, what)


async def open_calendar(d):
    await d.b.js("__PC.switchView('calendar');true")
    await F.wait(d, "!!document.querySelector('#cal-today')", 30, 'the Calendar screen')
    await press(d, '#cal-today', 'Today')


async def confirm(d, what):
    await press(d, CONFIRM, what)


async def delete_event(d, uid):
    edit = ".cal-edit[data-uid=\"%s\"]" % uid
    await F.wait(d, "!!document.querySelector(%s)" % json.dumps(edit), 30, 'the event on the day')
    await press(d, edit, 'the Edit button')
    await press(d, '#cev-del', 'Delete in the event editor')
    await confirm(d, 'the delete confirmation')


@scenario
async def an_event_delete_the_relay_refused_is_never_said_done(S):
    ctx, pk = S.ctx, S.pk(0)
    ctx.seed(pk, 'personal', [('ev-kept-1', 'Dentist')])
    d = await S.device(0)
    await open_calendar(d)
    ctx.refuse[ctx.storage_pk(pk)] = None
    await delete_event(d, 'ev-kept-1')
    await F.wait(d, "%s.some(t=>/delet/i.test(t))" % TOASTS, 20, 'what the delete says')
    await asyncio.sleep(1)
    toasts = await d.b.js(TOASTS)
    assert not [t for t in toasts if t.strip() == 'deleted'], ('"deleted" over a refused delete', toasts)
    assert ctx.items(pk, 'personal') == ['ev-kept-1'], 'the refused delete is not on the relay'
    # It is held as waiting to sync, and goes through once the relay accepts deletions again.
    await F.wait(d, "/waiting to sync/.test(document.querySelector('#feed').innerText)", 15, 'the pending pill')
    del ctx.refuse[ctx.storage_pk(pk)]
    await d.b.js("__PC.switchView('home');true")
    await open_calendar(d)
    await F.until(lambda: ctx.items(pk, 'personal') == [], 30, 'the queued delete reaching the relay', d)
    await F.wait(d, "!document.querySelector('.cal-edit[data-uid=\"ev-kept-1\"]')", 30, 'the event gone from the day')


@CHROME
def test_an_event_delete_the_relay_refused_is_never_said_done(ran):
    ran('an_event_delete_the_relay_refused_is_never_said_done')


@scenario
async def a_calendar_delete_the_relay_refused_leaves_nothing_behind(S):
    ctx, pk = S.ctx, S.pk(0)
    ctx.seed(pk, 'work', [('ev-a', 'Standup'), ('ev-b', 'Review')])
    d = await S.device(0)
    await open_calendar(d)
    await F.wait(d, "!!document.querySelector('.cal-edit[data-uid=\"ev-a\"]')", 30, 'the calendar drawn')
    # The relay refuses ONE deletion (the first of the calendar's documents) and accepts the rest.
    ctx.refuse[ctx.storage_pk(pk)] = 1
    await press(d, '#cal-menu', 'the Calendars menu button')
    await press(d, '.cal-del[data-id="work"]', 'Delete in the Calendars menu')
    await confirm(d, 'the delete confirmation')
    await F.wait(d, "%s.some(t=>/delet/i.test(t))" % TOASTS, 20, 'what the delete says')
    toasts = await d.b.js(TOASTS)
    left = ctx.items(pk, 'work')
    assert left, 'the relay refused one of the deletions, so something must still be there'
    assert 'work' in ctx.calendars(pk), ('events left on the relay under a calendar that is gone', left)
    assert not [t for t in toasts if 'calendar deleted' in t], ('"calendar deleted" over a refused delete', toasts)
    assert [t for t in toasts if 'could not delete' in t], toasts
    # Once the relay accepts, the same button finishes the job.
    del ctx.refuse[ctx.storage_pk(pk)]
    await press(d, '.cal-del[data-id="work"]', 'Delete again (the menu stays open)')
    await confirm(d, 'the delete confirmation again')
    await F.wait(d, "%s.some(t=>t.includes('calendar deleted'))" % TOASTS, 20, 'deleted once the relay accepts')
    assert ctx.items(pk, 'work') == [] and 'work' not in ctx.calendars(pk)


@CHROME
def test_a_calendar_delete_the_relay_refused_leaves_nothing_behind(ran):
    ran('a_calendar_delete_the_relay_refused_leaves_nothing_behind')
