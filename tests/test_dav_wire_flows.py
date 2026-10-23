"""CALDAV/CARDDAV AS A PHONE SYNCS IT: discovery, create/edit/delete from the phone, edits from the web reaching the
phone, refusals, recurrence + timezones, and a large calendar -- against the node's REAL Radicale mount, storage
plugin, caldav_store and relay, with the request bodies DAVx5 sends (tests/dav_wire_harness.py). The web side is the
web client's own API (app/routers/calendar.py, contacts.py), read from the relay on every call -- a fresh device.

Bugs these found, each fixed and each failing without its fix:
  * a DELETE the relay REFUSED was answered 200 -- the phone dropped the event, the relay kept it, and the next
    reconcile served it back to every device (the storage plugin mirrored loosely, after the disk delete);
  * a PUT the relay refused was answered 201 -- the event existed only in the disk cache, never reached the web, and
    the reconcile later removed it as "not on the relay": a phone's appointment silently lost;
  * an event a phone stored under its own href (not the UID -- DAVx5 and iOS mint their own) came back from a web
    edit under the UID: the phone got a SECOND copy, then a delete of its own;
  * an addressbook the phone created (MKCOL) was mirrored as Radicale's `tag`, read only as `kind`, so it was a
    CALENDAR: missing from Contacts, shown in Calendar, and served back to the phone as a calendar whose cards were
    rewritten as unparseable `.ics` -- the address book vanished from the phone that made it;
  * a calendar the phone named showed on the web under its directory id.

Fail-first: copy an older app/services/caldav/storage.py / caldav_store.py over the working tree.
"""
import json
import shutil
import tempfile
import time

import pytest

from tests import dav_wire_harness as D
from tests.client import realnode_harness as R

DAY = time.strftime('%Y%m%d', time.gmtime(time.time() + 86400))


@pytest.fixture(scope='module')
def env():
    from app.services.nostr import bip340
    mp = pytest.MonkeyPatch()
    tmp = tempfile.mkdtemp(prefix='pc-davflows-')
    loop = R._Loop()
    home = R.Home(loop, tmp)
    users = [D.user(i) for i in range(1, 30)]
    node = D.Node(mp, home.port, users)
    refuse = {}             # storage pubkey -> set of event kinds the relay refuses from it
    orig = home.relay._dispatch

    async def dispatch(conn, raw):
        try:
            m = json.loads(raw)
        except (TypeError, ValueError):
            m = None
        if (isinstance(m, list) and len(m) > 1 and m[0] == 'EVENT' and isinstance(m[1], dict)
                and m[1].get('kind') in refuse.get(m[1].get('pubkey'), ())):
            await conn.send(json.dumps(['OK', m[1].get('id'), False, 'blocked: not now']))
            return
        await orig(conn, raw)
    home.relay._dispatch = dispatch
    ns = type('Env', (), {})()
    ns.node, ns.home, ns.users, ns.loop = node, home, iter(users), loop
    ns.storage_pk = lambda u: bip340.pubkey_from_seckey(D.storage_sk(u)).hex()
    ns.refuse = refuse
    yield ns
    node.stop()
    home.close()
    mp.undo()
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.rmtree(node.tmp, ignore_errors=True)


def account(env, cal='personal', book='friends'):
    """A fresh account with a calendar and an addressbook made on the web, and a phone that has discovered them."""
    u = next(env.users)
    if cal:
        assert env.node.web(u, 'POST', '/api/calendar/calendars', {'name': cal.title(), 'id': cal})[0] == 200
    if book:
        assert env.node.web(u, 'POST', '/api/contacts/books', {'name': book.title(), 'id': book})[0] == 200
    ph = D.Phone(env.node, u.username)
    ph.discover()
    return u, ph, '/caldav/%s/%s/' % (u.username, cal), '/caldav/%s/%s/' % (u.username, book)


def vevent(uid, summary, extra=''):
    return ('BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//DAVx5//ical4j//EN\r\nBEGIN:VEVENT\r\nUID:%s\r\n'
            'DTSTAMP:20261023T100000Z\r\nDTSTART:%sT100000Z\r\nDTEND:%sT110000Z\r\nSUMMARY:%s\r\n%s'
            'END:VEVENT\r\nEND:VCALENDAR\r\n' % (uid, DAY, DAY, summary, extra))


def vcard(uid, fn, tel='+49 170 1234567'):
    return ('BEGIN:VCARD\r\nVERSION:3.0\r\nPRODID:-//DAVx5//EN\r\nUID:%s\r\nFN:%s\r\nN:%s;;;;\r\n'
            'TEL;TYPE=CELL:%s\r\nitem1.EMAIL;TYPE=INTERNET:anna@example.test\r\nitem1.X-ABLABEL:Arbeit\r\n'
            'END:VCARD\r\n' % (uid, fn, fn, tel))


def web_items(env, u, cal):
    code, got = env.node.web(u, 'GET', '/api/calendar/items?cal=' + cal)
    assert code == 200, got
    return {i['uid']: i['ics'] for i in got['items']}


def web_cards(env, u, book):
    code, got = env.node.web(u, 'GET', '/api/contacts/cards?book=' + book)
    assert code == 200, got
    return {i['uid']: i['ics'] for i in got['cards']}


def has_line(text, line):
    return line in D.lines(text)


# ------------------------------------------------------------------------------------------------ discovery
def test_a_phone_discovers_the_principal_and_both_kinds_of_collection(env):
    u, ph, cal, book = account(env)
    principal, homes = ph.discover()
    assert principal == '/caldav/%s/' % u.username
    assert homes == {('c', 'calendar-home-set'): principal, ('card', 'addressbook-home-set'): principal}
    cols = ph.collections(principal)
    assert cols[cal]['type'] == 'calendar' and cols[cal]['name'] == 'Personal' and 'VEVENT' in cols[cal]['components']
    assert cols[book]['type'] == 'addressbook' and cols[book]['name'] == 'Friends'
    assert all(c['ctag'] and c['token'] for c in cols.values())
    # The wrong password is refused, and another account's collections are not this phone's to read.
    assert D.Phone(env.node, u.username, password='wrong').req('PROPFIND', principal, D.PRINCIPAL,
                                                                {'Depth': '0'}).status_code == 401
    other = next(env.users)
    r = D.Phone(env.node, other.username).req('PROPFIND', cal, D.ETAGS, {'Depth': '1'})
    assert r.status_code in (403, 404), r.status_code


def test_collections_the_phone_creates_are_the_right_kind_everywhere(env):
    u, ph, _, _ = account(env, cal=None, book=None)
    home = '/caldav/%s/' % u.username
    mkcal = ('<?xml version="1.0" encoding="UTF-8"?><CAL:mkcalendar xmlns="DAV:" xmlns:CAL="urn:ietf:params:xml:ns:caldav" '
             'xmlns:ICAL="http://apple.com/ns/ical/"><set><prop><displayname>Familie</displayname>'
             '<ICAL:calendar-color>#FF8800FF</ICAL:calendar-color><CAL:supported-calendar-component-set>'
             '<CAL:comp name="VEVENT"/></CAL:supported-calendar-component-set></prop></set></CAL:mkcalendar>')
    mkbook = ('<?xml version="1.0" encoding="UTF-8"?><mkcol xmlns="DAV:" xmlns:CARD="urn:ietf:params:xml:ns:carddav">'
              '<set><prop><resourcetype><collection/><CARD:addressbook/></resourcetype><displayname>Handy</displayname>'
              '</prop></set></mkcol>')
    assert ph.req('MKCALENDAR', home + 'familie/', mkcal).status_code == 201
    assert ph.req('MKCOL', home + 'handy/', mkbook).status_code == 201
    assert ph.put(home + 'handy/c1.vcf', vcard('c1', 'Anna Müller'), create=True).status_code == 201
    cals = {c['id']: c for c in env.node.web(u, 'GET', '/api/calendar/calendars')[1]['calendars']}
    books = {b['id']: b for b in env.node.web(u, 'GET', '/api/contacts/books')[1]['books']}
    assert list(cals) == ['familie'] and cals['familie']['displayname'] == 'Familie', cals
    assert list(books) == ['handy'] and books['handy']['displayname'] == 'Handy', (books, cals)
    assert list(web_cards(env, u, 'handy')) == ['c1']
    # ...and after the node re-reads this account from the relay (any web write does that), the phone still has
    # an ADDRESSBOOK holding its card.
    from app.services.caldav import storage
    storage.forget_user(u.username)
    cols = ph.collections(home)
    assert cols[home + 'handy/']['type'] == 'addressbook' and cols[home + 'familie/']['type'] == 'calendar', cols
    got = ph.sync(home + 'handy/')
    assert list(got['changed']) == [home + 'handy/c1.vcf'] and 'FN:Anna Müller' in D.lines(got['changed'][home + 'handy/c1.vcf'])


# ------------------------------------------------------------------------------------------------ phone -> web
def test_an_event_from_the_phone_is_created_edited_and_deleted_on_the_web(env):
    u, ph, cal, _ = account(env)
    href = cal + 'f81d4fae-7dec-11d0-a765-00a0c91e6bf6.ics'          # DAVx5 names its own resources
    r = ph.put(href, vevent('ev-1@phone', 'Zahnarzt'), create=True)
    assert r.status_code == 201, r.text
    etag = r.headers['etag']
    assert has_line(web_items(env, u, 'personal')['ev-1@phone'], 'SUMMARY:Zahnarzt')
    # An edit with the etag the phone holds; one with a stale etag is refused (412), never applied.
    r = ph.put(href, vevent('ev-1@phone', 'Zahnarzt (verschoben)'), etag=etag)
    assert r.status_code in (200, 204), r.text
    assert ph.put(href, vevent('ev-1@phone', 'stale'), etag=etag).status_code == 412
    assert has_line(web_items(env, u, 'personal')['ev-1@phone'], 'SUMMARY:Zahnarzt (verschoben)')
    assert ph.delete(href, r.headers['etag']).status_code in (200, 204)
    assert web_items(env, u, 'personal') == {}


def test_a_contact_from_the_phone_is_created_edited_and_deleted_on_the_web(env):
    u, ph, _, book = account(env)
    href = book + '6c3f9a1e-2b5d-4e8f-9a7c-0d1e2f3a4b5c.vcf'
    r = ph.put(href, vcard('card-1', 'Jürgen Groß'), create=True)
    assert r.status_code == 201, r.text
    card = web_cards(env, u, 'friends')['card-1']
    assert has_line(card, 'FN:Jürgen Groß') and has_line(card, 'item1.X-ABLABEL:Arbeit'), card
    r = ph.put(href, vcard('card-1', 'Jürgen Groß', tel='+49 30 999'), etag=r.headers['etag'])
    assert r.status_code in (200, 204)
    assert has_line(web_cards(env, u, 'friends')['card-1'], 'TEL;TYPE=CELL:+49 30 999')
    assert ph.delete(href).status_code in (200, 204)
    assert web_cards(env, u, 'friends') == {}


# ------------------------------------------------------------------------------------------------ web -> phone
def test_a_web_edit_reaches_the_phone_on_its_next_sync_under_the_phones_own_name(env):
    u, ph, cal, book = account(env)
    ev_href, card_href = cal + 'A1B2C3D4-phone-made.ics', book + 'phone-card-77.vcf'
    assert ph.put(ev_href, vevent('ev-77', 'Elternabend'), create=True).status_code == 201
    assert ph.put(card_href, vcard('card-77', 'Petra Schmidt'), create=True).status_code == 201
    ph.sync(cal), ph.sync(book)
    before = {c: ph.ctag(c) for c in (cal, book)}
    # The web client saves (calendar.js / contacts.js send the whole component).
    assert env.node.web(u, 'PUT', '/api/calendar/items', {'cal': 'personal', 'uid': 'ev-77',
                                                          'ics': vevent('ev-77', 'Elternabend 19 Uhr')})[0] == 200
    assert env.node.web(u, 'PUT', '/api/contacts/cards', {'book': 'friends', 'uid': 'card-77',
                                                          'vcf': vcard('card-77', 'Petra Schmidt-Weber')})[0] == 200
    for c in (cal, book):
        ctag, token = ph.ctag(c)
        assert ctag != before[c][0] and token != before[c][1], ('ctag/sync-token did not move', c)
    got = ph.sync(cal)
    assert list(got['changed']) == [ev_href] and got['deleted'] == [], ('a second copy, or a delete', got)
    assert has_line(got['changed'][ev_href], 'SUMMARY:Elternabend 19 Uhr')
    got = ph.sync(book)
    assert list(got['changed']) == [card_href] and got['deleted'] == [], got
    assert has_line(got['changed'][card_href], 'FN:Petra Schmidt-Weber')
    assert sorted(ph.state[cal]['data']) == [ev_href] and sorted(ph.state[book]['data']) == [card_href]
    # A web delete is a delete on the phone's next sync.
    assert env.node.web(u, 'DELETE', '/api/calendar/items?cal=personal&uid=ev-77')[0] == 200
    got = ph.sync(cal)
    assert got['deleted'] == [ev_href] and got['changed'] == {}, got


def test_an_event_made_on_the_web_appears_on_the_phone(env):
    u, ph, cal, _ = account(env)
    ph.sync(cal)
    assert env.node.web(u, 'PUT', '/api/calendar/items', {'cal': 'personal', 'uid': 'web-1',
                                                          'ics': vevent('web-1', 'Vom Browser')})[0] == 200
    got = ph.sync(cal)
    assert list(got['changed']) == [cal + 'web-1.ics'] and has_line(got['changed'][cal + 'web-1.ics'],
                                                                    'SUMMARY:Vom Browser')


# ------------------------------------------------------------------------------------------------ refusals
def test_a_delete_the_relay_refuses_is_not_reported_to_the_phone_as_done(env):
    u, ph, cal, book = account(env)
    assert ph.put(cal + 'keep.ics', vevent('keep', 'Bleibt'), create=True).status_code == 201
    assert ph.put(book + 'keep.vcf', vcard('keep', 'Bleibt Da'), create=True).status_code == 201
    ph.sync(cal), ph.sync(book)
    env.refuse[env.storage_pk(u)] = {5}
    try:
        for href in (cal + 'keep.ics', book + 'keep.vcf'):
            r = ph.delete(href)
            assert r.status_code >= 500, ('the phone was told the delete happened', href, r.status_code)
        r = ph.req('DELETE', cal)                    # the whole calendar
        assert r.status_code >= 500, r.status_code
    finally:
        env.refuse.pop(env.storage_pk(u), None)
    assert list(web_items(env, u, 'personal')) == ['keep'] and list(web_cards(env, u, 'friends')) == ['keep']
    # The phone's next sync still has both: nothing it was told is gone, and nothing gone comes back.
    from app.services.caldav import storage
    storage.forget_user(u.username)
    assert ph.sync(cal) == {'changed': {}, 'deleted': [], 'full': False}
    assert ph.sync(book) == {'changed': {}, 'deleted': [], 'full': False}
    # Once the relay accepts, the phone's retry goes through.
    assert ph.delete(cal + 'keep.ics').status_code in (200, 204) and web_items(env, u, 'personal') == {}


def test_a_write_the_relay_refuses_is_not_reported_to_the_phone_as_saved(env):
    u, ph, cal, book = account(env)
    ph.sync(cal)
    env.refuse[env.storage_pk(u)] = {30078}
    try:
        r = ph.put(cal + 'lost.ics', vevent('lost', 'Wäre verloren'), create=True)
        assert r.status_code >= 500, ('the phone was told an event the relay refused was saved', r.status_code)
        r = ph.put(book + 'lost.vcf', vcard('lost', 'Wäre Weg'), create=True)
        assert r.status_code >= 500, r.status_code
    finally:
        env.refuse.pop(env.storage_pk(u), None)
    assert ph.sync(cal)['changed'] == {}, 'the node serves an event it never stored'
    r = ph.put(cal + 'lost.ics', vevent('lost', 'Wäre verloren'), create=True)    # the phone's retry
    assert r.status_code == 201 and 'lost' in web_items(env, u, 'personal')


# ------------------------------------------------------------------------------------------------ recurrence + tz
TZ_EVENT = '\r\n'.join([
    'BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//Mozilla.org/NONSGML Mozilla Calendar V1.1//EN',
    'BEGIN:VTIMEZONE', 'TZID:Europe/Berlin', 'BEGIN:DAYLIGHT', 'TZOFFSETFROM:+0100', 'TZOFFSETTO:+0200',
    'TZNAME:CEST', 'DTSTART:19700329T020000', 'RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU', 'END:DAYLIGHT',
    'BEGIN:STANDARD', 'TZOFFSETFROM:+0200', 'TZOFFSETTO:+0100', 'TZNAME:CET', 'DTSTART:19701025T030000',
    'RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU', 'END:STANDARD', 'END:VTIMEZONE',
    'BEGIN:VEVENT', 'UID:weekly-standup@thunderbird', 'DTSTAMP:20261001T080000Z',
    'DTSTART;TZID=Europe/Berlin:20261005T093000', 'DTEND;TZID=Europe/Berlin:20261005T100000',
    'RRULE:FREQ=WEEKLY;BYDAY=MO,WE;UNTIL=20271231T225959Z', 'EXDATE;TZID=Europe/Berlin:20261012T093000',
    'SUMMARY:Standup', 'BEGIN:VALARM', 'ACTION:DISPLAY', 'TRIGGER:-PT10M', 'DESCRIPTION:Standup', 'END:VALARM',
    'END:VEVENT',
    'BEGIN:VEVENT', 'UID:weekly-standup@thunderbird', 'DTSTAMP:20261001T080000Z',
    'RECURRENCE-ID;TZID=Europe/Berlin:20261014T093000', 'DTSTART;TZID=Europe/Berlin:20261014T110000',
    'DTEND;TZID=Europe/Berlin:20261014T113000', 'SUMMARY:Standup (verlegt)', 'END:VEVENT',
    'END:VCALENDAR', ''])


def test_a_recurring_event_with_a_timezone_round_trips_both_ways(env):
    u, ph, cal, _ = account(env)
    href = cal + 'weekly-standup.ics'
    assert ph.put(href, TZ_EVENT, create=True).status_code == 201
    stored = web_items(env, u, 'personal')['weekly-standup@thunderbird']
    for line in ('TZID:Europe/Berlin', 'RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU',
                 'DTSTART;TZID=Europe/Berlin:20261005T093000', 'RRULE:FREQ=WEEKLY;BYDAY=MO,WE;UNTIL=20271231T225959Z',
                 'EXDATE;TZID=Europe/Berlin:20261012T093000', 'RECURRENCE-ID;TZID=Europe/Berlin:20261014T093000',
                 'SUMMARY:Standup (verlegt)', 'TRIGGER:-PT10M'):
        assert has_line(stored, line), (line, stored)
    ph.sync(cal)
    # The web client edits the series title (calendar.js re-emits the rule, EXDATEs and overrides verbatim).
    edited = stored.replace('SUMMARY:Standup\r\n', 'SUMMARY:Daily Standup\r\n')
    assert edited != stored
    assert env.node.web(u, 'PUT', '/api/calendar/items', {'cal': 'personal', 'uid': 'weekly-standup@thunderbird',
                                                          'ics': edited})[0] == 200
    got = ph.sync(cal)
    assert list(got['changed']) == [href], got
    back = got['changed'][href]
    for line in ('SUMMARY:Daily Standup', 'TZID:Europe/Berlin', 'DTSTART;TZID=Europe/Berlin:20261005T093000',
                 'RRULE:FREQ=WEEKLY;BYDAY=MO,WE;UNTIL=20271231T225959Z', 'EXDATE;TZID=Europe/Berlin:20261012T093000',
                 'RECURRENCE-ID;TZID=Europe/Berlin:20261014T093000', 'SUMMARY:Standup (verlegt)'):
        assert has_line(back, line), (line, back)
    assert D.unfold(back).count('BEGIN:VEVENT') == 2 and D.unfold(back).count('BEGIN:VTIMEZONE') == 1


# ------------------------------------------------------------------------------------------------ a large calendar
N_LARGE = 1500


def test_a_large_calendar_syncs_fast(env, record_property):
    """1,500 events (a few years of a busy calendar). Measured: the phone's first full sync, the node re-reading the
    account after a web edit (every web write invalidates it), and a no-change sync. Budgets are ~5x what this
    machine measures, so a regression to per-item relay round trips or an O(n^2) reconcile fails."""
    from app.services import caldav_store, nostr_store
    from app.services.caldav import storage
    from app.services.nostr import nip44
    from app.services.nostr.event import build_event
    u, ph, cal, _ = account(env, book=None)
    sk = D.storage_sk(u)
    evs = []
    for i in range(N_LARGE):
        rec = {'cal': 'personal', 'uid': 'bulk-%04d' % i, 'component': 'VEVENT', 'ts': time.time(),
               'ics': vevent('bulk-%04d' % i, 'Termin %d' % i)}
        # The producer's own seal (nostr_store.put_doc's): NIP-44 to the storage key, kind 30078, d-tag.
        evs.append(build_event(sk, nostr_store.APP_KIND, nip44.encrypt_self(sk, json.dumps(rec, separators=(',', ':'))),
                               tags=[['d', '%spersonal:bulk-%04d' % (caldav_store.NS_ITEM, i)]]))
    env.home.add(evs)
    storage.forget_user(u.username)
    t = time.monotonic()
    got = ph.sync(cal)
    first_sync = time.monotonic() - t
    assert len(got['changed']) == N_LARGE, len(got['changed'])
    # A web edit: the next phone sync carries exactly that one event.
    assert env.node.web(u, 'PUT', '/api/calendar/items', {'cal': 'personal', 'uid': 'bulk-0007',
                                                          'ics': vevent('bulk-0007', 'Geändert')})[0] == 200
    t = time.monotonic()
    got = ph.sync(cal)
    after_edit = time.monotonic() - t
    assert list(got['changed']) == [cal + 'bulk-0007.ics'] and got['deleted'] == [], (len(got['changed']), got['deleted'][:3])
    t = time.monotonic()
    got = ph.sync(cal)
    idle = time.monotonic() - t
    assert got == {'changed': {}, 'deleted': [], 'full': False}
    for k, v in (('first_full_sync_s', first_sync), ('sync_after_web_edit_s', after_edit), ('idle_sync_s', idle)):
        record_property(k, round(v, 3))
    print('\n[dav-large] %d events: first full sync %.2fs, after a web edit %.2fs, idle %.3fs'
          % (N_LARGE, first_sync, after_edit, idle))
    assert first_sync < 20 and after_edit < 3 and idle < 1.5, (first_sync, after_edit, idle)
