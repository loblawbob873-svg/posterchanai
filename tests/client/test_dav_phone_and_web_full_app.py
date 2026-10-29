"""A PHONE (CalDAV/CardDAV, DAVx5's requests) and the WEB CLIENT on a fresh device, on the same account.

  The phone  tests/dav_wire_harness.Phone against the node's REAL Radicale mount + storage plugin (no stand-in).
  The web    the shipped client in Chrome on a FRESH profile, against the node's real calendar/contacts routers
             (discover_flows_harness.Routers) -- both over this node's relay, as on a node.

Flows: the phone makes a calendar (MKCALENDAR) and an event under its OWN href, and an address book (MKCOL) with a
contact; a fresh device's Calendar and Contacts screens show them (by the names the phone gave); the person edits
each in the web editor (Edit -> title -> Save, the contact row -> name -> Save); the phone's next sync gets the edit
under the href it already holds -- one copy, nothing deleted -- and its address book is still an address book.

Fail-first: copy an older app/services/caldav/storage.py / caldav_store.py over the working tree (the href and the
addressbook kind were both lost there).
"""
import asyncio
import datetime
import json

import pytest

from tests import dav_wire_harness as D
from tests.client import discover_flows_harness as F
from tests.client import realnode_harness as R

CHROME = pytest.mark.skipif(not R.chrome_ok(), reason='Chrome required')
FEATURE_COVERS = ('calendar', 'contacts')
FIRST_FILL = 160
SCENARIOS = {}
COST = {}


def scenario(fn):
    SCENARIOS[fn.__name__] = fn
    return fn


class Ctx:
    def __init__(self, n):
        from app.routers import calendar, contacts
        self.mp = mp = pytest.MonkeyPatch()
        self.users = F.members(FIRST_FILL, SCENARIOS)
        # The DAV side first: it installs the account table, the settings rows and the storage key both halves use.
        self.dav = D.Node(mp, n.home.port, list(self.users.values()), routers=False)
        self.api = F.Routers(mp, [(calendar.router, ''), (contacts.router, '')], self.users,
                             paths=['/api/calendar/', '/api/contacts/'])

    def phone(self, pk):
        return D.Phone(self.dav, self.users[pk].username)

    def stop(self):
        self.api.stop()
        self.dav.stop()
        self.mp.undo()


@pytest.fixture(scope='module')
def ran(request):
    return F.run(request, SCENARIOS, COST, FIRST_FILL, setup=Ctx)


async def press(d, sel, what, seconds=20):
    await F.wait(d, "(e=>!!e&&(e.click(),true))(document.querySelector(%s))" % json.dumps(sel), seconds, what)


async def in_thread(fn, *a):
    return await asyncio.get_running_loop().run_in_executor(None, fn, *a)


MKCAL = ('<?xml version="1.0" encoding="UTF-8"?><CAL:mkcalendar xmlns="DAV:" xmlns:CAL="urn:ietf:params:xml:ns:caldav">'
         '<set><prop><displayname>Familie</displayname><CAL:supported-calendar-component-set><CAL:comp name="VEVENT"/>'
         '</CAL:supported-calendar-component-set></prop></set></CAL:mkcalendar>')
MKBOOK = ('<?xml version="1.0" encoding="UTF-8"?><mkcol xmlns="DAV:" xmlns:CARD="urn:ietf:params:xml:ns:carddav"><set>'
          '<prop><resourcetype><collection/><CARD:addressbook/></resourcetype><displayname>Handy</displayname></prop>'
          '</set></mkcol>')


@scenario
async def an_event_from_the_phone_shows_on_the_web_and_the_web_edit_reaches_the_phone(S):
    ctx, pk = S.ctx, S.pk(0)
    ph = ctx.phone(pk)
    home = '/caldav/%s/' % ctx.users[pk].username
    day = datetime.date.today().strftime('%Y%m%d')
    href = home + 'familie/0E5A9C1B-phone-minted.ics'
    ics = ('BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//DAVx5//EN\r\nBEGIN:VEVENT\r\nUID:ev-fam-1\r\n'
           'DTSTAMP:%sT080000Z\r\nDTSTART;VALUE=DATE:%s\r\nSUMMARY:Oma Geburtstag\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n'
           % (day, day))

    def phone_writes():
        ph.discover()
        assert ph.req('MKCALENDAR', home + 'familie/', MKCAL).status_code == 201
        assert ph.put(href, ics, create=True).status_code == 201
        ph.sync(home + 'familie/')
    await in_thread(phone_writes)
    d = await S.device(0)                                   # a fresh profile: nothing cached
    await d.b.js("__PC.switchView('calendar');true")
    await F.wait(d, "!!document.querySelector('#cal-today')", 30, 'the Calendar screen')
    await press(d, '#cal-today', 'Today')
    edit = '.cal-edit[data-uid="ev-fam-1"]'
    await F.wait(d, "!!document.querySelector(%s) && document.querySelector('#feed').innerText.includes('Oma Geburtstag')"
                 % json.dumps(edit), 30, "the phone's event on the web")
    assert 'Familie' in await d.b.js("document.querySelector('#feed').innerText"), 'the calendar shows under its id'
    await press(d, edit, 'Edit')
    await F.wait(d, "!!document.querySelector('#cev-title')", 15, 'the event editor')
    await d.b.js("(()=>{const t=document.querySelector('#cev-title');t.value='Oma Geburtstag (Kuchen!)';"
                 "t.dispatchEvent(new Event('input',{bubbles:true}));return true})()")
    await press(d, '#cev-save', 'Save')

    def phone_sees():
        got = ph.sync(home + 'familie/')
        return got if got['changed'] or got['deleted'] else None
    got = None
    for _ in range(60):
        got = await in_thread(phone_sees)
        if got:
            break
        await asyncio.sleep(.5)
    assert got and list(got['changed']) == [href] and got['deleted'] == [], ('a second copy or a delete', got)
    assert 'SUMMARY:Oma Geburtstag (Kuchen!)' in D.lines(got['changed'][href])
    assert sorted(ph.state[home + 'familie/']['data']) == [href]


@scenario
async def a_contact_from_the_phone_shows_on_the_web_and_the_web_edit_reaches_the_phone(S):
    ctx, pk = S.ctx, S.pk(0)
    ph = ctx.phone(pk)
    home = '/caldav/%s/' % ctx.users[pk].username
    book = home + 'handy/'
    href = book + '7f3e-phone-card.vcf'
    vcf = ('BEGIN:VCARD\r\nVERSION:3.0\r\nPRODID:-//DAVx5//EN\r\nUID:card-handy-1\r\nFN:Anna Müller\r\n'
           'N:Müller;Anna;;;\r\nTEL;TYPE=CELL:+49 170 1234567\r\nEND:VCARD\r\n')

    def phone_writes():
        ph.discover()
        assert ph.req('MKCOL', book, MKBOOK).status_code == 201
        assert ph.put(href, vcf, create=True).status_code == 201
        ph.sync(book)
    await in_thread(phone_writes)
    d = await S.device(0)
    await d.b.js("__PC.switchView('contacts');true")
    names = "[...document.querySelectorAll('#feed .ct-row .ct-name')].map(e=>e.textContent.trim())"
    await F.wait(d, "%s.includes('Anna Müller')" % names, 30, "the phone's contact on the web")
    await d.b.js("[...document.querySelectorAll('#feed .ct-row')].find(r=>r.querySelector('.ct-name').textContent"
                 ".trim()==='Anna Müller').click();true")
    await F.wait(d, "!!document.querySelector('#ctc-edit')", 15, "the contact's card")
    await d.b.js("document.querySelector('#ctc-edit').click();true")
    await F.wait(d, "!!document.querySelector('#cc-save') && !!document.querySelector('#cc-family')", 15, 'the editor')
    await d.b.js("(()=>{const i=document.querySelector('#cc-family');i.value='Müller-Lüdenscheidt';"
                 "i.dispatchEvent(new Event('input',{bubbles:true}));return true})()")
    await press(d, '#cc-save', 'Save')

    def phone_sees():
        got = ph.sync(book)
        return got if got['changed'] or got['deleted'] else None
    got = None
    for _ in range(60):
        got = await in_thread(phone_sees)
        if got:
            break
        await asyncio.sleep(.5)
    assert got and list(got['changed']) == [href] and got['deleted'] == [], ('a second copy or a delete', got)
    assert any(l.startswith('FN:') and 'Müller-Lüdenscheidt' in l for l in D.lines(got['changed'][href])), got
    cols = await in_thread(ph.collections, home)
    assert cols[book]['type'] == 'addressbook', cols


@CHROME
def test_an_event_from_the_phone_shows_on_the_web_and_the_web_edit_reaches_the_phone(ran):
    ran('an_event_from_the_phone_shows_on_the_web_and_the_web_edit_reaches_the_phone')


@CHROME
def test_a_contact_from_the_phone_shows_on_the_web_and_the_web_edit_reaches_the_phone(ran):
    ran('a_contact_from_the_phone_shows_on_the_web_and_the_web_edit_reaches_the_phone')
