"""Contacts (the address book), driven end to end in the shipped client against the node's REAL contacts router.

  The node   app/routers/contacts.py mounted in-process (discover_flows_harness.Routers), over the REAL
             app/services/caldav_store -> nostr_store -> THIS node's relay (the shipped RelayServer over PosterChanDB):
             every card is an encrypted kind-30078 document, read back here with caldav_store.get_items exactly as
             the router reads it. Only the storage key is chosen by the test (one per account).
  Flows      a new addressbook, add / edit / delete a contact through the editor's own buttons, each step checked on
             the relay; a FRESH device of the same account sees the address book; a second account sees none of it.
  Failures   the node answering 503, dropping the connection, or answering late -- for the book list and for one
             book's cards -- on a fresh device (nothing cached). The screen must say what happened (never "No
             addressbook yet" / "No contacts in this book yet" over an address book it could not read) and must not
             offer to make a new addressbook over one it could not list. A save the node refuses keeps the form.

The phone's own Contacts app (the APK's ContactSyncPlugin) is NOT driven here: its writes go through Android's
ContactsContract, which has no JVM implementation. Its guards are RUN by tests/test_android_contact_sync.py (javac +
java) and the client half against a stub phone by tests/client/test_contacts_phonebook_guard.py.

Harness: tests/client/discover_flows_harness.py. Fail-first: PC_JOURNEY_OVERRIDE="/static/js/client/contacts.js=...".
"""
import asyncio
import hashlib
import json

import pytest

from tests.client import discover_flows_harness as F
from tests.client import realnode_harness as R

CHROME = pytest.mark.skipif(not R.chrome_ok(), reason='Chrome required')
FEATURE_COVERS = ('contacts',)
FIRST_FILL = 8             # 8 .. 31 (account keys are one byte; each module has its own node)
SCENARIOS = {}
COST = {'add_edit_and_delete_reach_the_relay_and_a_fresh_device': 40}


def scenario(fn):
    SCENARIOS[fn.__name__] = fn
    return fn


# ------------------------------------------------------------------------------------------------ the node
class Ctx:
    def __init__(self, n):
        from app.routers import contacts
        from app.services import caldav_store
        self.n = n
        self.mp = mp = pytest.MonkeyPatch()
        mp.setattr(caldav_store, 'enabled', lambda: True)
        mp.setattr(caldav_store, '_port', lambda db=None: n.home.port)
        # The account's server-held storage key (keystore.get_storage_seckey on a node): one per account.
        mp.setattr(caldav_store, 'user_storage_seckey',
                   lambda db, u: hashlib.sha256(b'pc-test-storage:' + u.nostr_npub.encode()).digest())
        mp.setattr(contacts, '_forget', lambda username: None)       # no Radicale disk cache here
        self.users = F.members(FIRST_FILL, SCENARIOS)
        self.api = F.Routers(mp, [(contacts.router, '')], self.users, paths=['/api/contacts/'])
        # The home relay REFUSING a deletion (NIP-09 kind 5) from these storage keys -- OK false, as a relay says it.
        self.refuse_deletes = set()
        orig = n.home.relay._dispatch

        async def dispatch(conn, raw):
            try:
                m = json.loads(raw)
            except (TypeError, ValueError):
                m = None
            if (isinstance(m, list) and len(m) > 1 and m[0] == 'EVENT' and isinstance(m[1], dict)
                    and m[1].get('kind') == 5 and m[1].get('pubkey') in self.refuse_deletes):
                await conn.send(json.dumps(['OK', m[1].get('id'), False, 'blocked: not now']))
                return
            await orig(conn, raw)
        n.home.relay._dispatch = dispatch

    def storage_pk(self, pk):
        from app.services.nostr import bip340
        return bip340.pubkey_from_seckey(hashlib.sha256(b'pc-test-storage:' + pk.encode()).digest()).hex()

    def call(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.api.loop).result(60)

    def cards(self, pk, book):
        """The account's cards in `book`, as the node reads them from the relay."""
        from app.services import caldav_store
        return self.call(caldav_store.get_items(None, self.users[pk], book, strict=True))

    def books(self, pk):
        from app.services import caldav_store
        return self.call(caldav_store.list_addressbooks(None, self.users[pk], strict=True))

    def seed(self, pk, book, people):
        """An address book made by the node's own router (the producer every client's write goes through)."""
        from app.services import caldav_store
        u = self.users[pk]
        self.call(caldav_store.put_calendar(None, u, book, {'displayname': 'Contacts', 'kind': 'VADDRESSBOOK'}))
        for uid, name, tel in people:
            vcf = 'BEGIN:VCARD\r\nVERSION:3.0\r\nUID:%s\r\nFN:%s\r\nTEL;TYPE=cell:%s\r\nEND:VCARD\r\n' % (uid, name, tel)
            assert self.call(caldav_store.put_item(None, u, book, uid, vcf, 'VCARD'))

    def stop(self):
        self.api.stop()
        self.mp.undo()


@pytest.fixture(scope='module')
def ran(request):
    return F.run(request, SCENARIOS, COST, FIRST_FILL, setup=Ctx)


# ------------------------------------------------------------------------------------------------ a person
NAMES = "[...document.querySelectorAll('#feed .ct-row .ct-name')].map(e=>e.textContent.trim())"
FEED = "((document.querySelector('#feed')||{}).innerText||'')"


async def open_contacts(d, ready="!!document.querySelector('#feed .ct-wrap') && !document.querySelector('#feed .ct-wrap > .spinner')"):
    await d.b.js("__PC.switchView('contacts');true")
    await F.wait(d, ready, 30, 'the Contacts screen')


async def fill_and_save(d, given=None, family=None, phone=None):
    await F.wait(d, "!!document.querySelector('#cc-save')", 15, 'the contact editor')
    sets = []
    if given is not None:
        sets.append("['#cc-given',%s]" % json.dumps(given))
    if family is not None:
        sets.append("['#cc-family',%s]" % json.dumps(family))
    if phone is not None:
        sets.append("['#cc-tels .ct-mv',%s]" % json.dumps(phone))
    await d.b.js("(()=>{for(const [s,v] of [%s]){const i=document.querySelector(s);i.value=v;"
                 "i.dispatchEvent(new Event('input',{bubbles:true}));}document.querySelector('#cc-save').click();return true})()"
                 % ','.join(sets))


async def open_row(d, name):
    sel = "[...document.querySelectorAll('#feed .ct-row')].find(r=>r.querySelector('.ct-name').textContent.trim()===%s)" % json.dumps(name)
    await F.wait(d, "!!" + sel, 20, 'the row for ' + name)
    await d.b.js(sel + ".click();true")
    await F.wait(d, "!!document.querySelector('#cc-save')", 15, 'the editor for ' + name)


def js_list(v):
    return json.dumps(json.dumps(v, separators=(',', ':')))


def by_fn(cards):
    out = {}
    for c in cards:
        fn = next((ln[3:] for ln in c['ics'].replace('\r\n', '\n').split('\n') if ln.startswith('FN:')), '')
        out[fn] = c['ics']
    return out


# ------------------------------------------------------------------------------------------------ scenarios
@scenario
async def add_edit_and_delete_reach_the_relay_and_a_fresh_device(S):
    ctx, pk = S.ctx, S.pk(0)
    d = await S.device(0)
    await open_contacts(d)
    # A brand-new account: no addressbook yet -> "New addressbook" -> Create.
    await F.wait(d, "!!document.querySelector('#ct-mkbook')", 20, 'the no-addressbook offer')
    await d.b.js("document.querySelector('#ct-mkbook').click();true")
    await F.wait(d, "!!document.querySelector('#ctb-save')", 15, 'the New addressbook form')
    await d.b.js("document.querySelector('#ctb-save').click();true")
    await F.wait(d, "!!document.querySelector('#ct-new')", 30, 'the new, empty addressbook')
    books = await F.until(lambda: ctx.books(pk), 20, 'the addressbook on the relay', d)
    book = books[0]['id']
    # Add.
    await d.b.js("document.querySelector('#ct-new').click();true")
    await fill_and_save(d, 'Ada', 'Lovelace', '+15550100')
    await F.wait(d, "%s.includes('Ada Lovelace')" % NAMES, 30, 'the new contact listed')
    got = by_fn(ctx.cards(pk, book))
    assert 'Ada Lovelace' in got and '+15550100' in got['Ada Lovelace'], got
    # Edit: a new number replaces the old one, on the relay.
    await open_row(d, 'Ada Lovelace')
    await fill_and_save(d, phone='+15550199')
    await F.until(lambda: '+15550199' in by_fn(ctx.cards(pk, book)).get('Ada Lovelace', ''), 20, 'the edit on the relay', d)
    assert '+15550100' not in by_fn(ctx.cards(pk, book))['Ada Lovelace']
    # A second contact, then a FRESH device of the same account reads both back through the node.
    await d.b.js("document.querySelector('#ct-new').click();true")
    await fill_and_save(d, 'Grace', 'Hopper', '+15550200')
    await F.wait(d, "%s.includes('Grace Hopper')" % NAMES, 30, 'the second contact listed')
    fresh = await S.device(0)
    await open_contacts(fresh)
    await F.wait(fresh, "JSON.stringify(%s)===%s" % (NAMES, js_list(['Grace Hopper', 'Ada Lovelace'])), 30,
                 'both contacts on the fresh device (by family name)')
    await open_row(fresh, 'Ada Lovelace')
    assert await fresh.b.js("document.querySelector('#cc-tels .ct-mv').value") == '+15550199'
    await fresh.b.js("__PC.closeModal ? __PC.closeModal() : document.querySelector('.modal-bg,.modal')?.remove();true")
    # Delete (confirmed) on the first device: gone from the relay and from the list.
    await open_row(d, 'Ada Lovelace')
    await d.b.js("document.querySelector('#cc-del').click();true")
    await F.wait(d, "!!document.querySelector('.uiconfirm-btns [data-uc=\"1\"]')", 10, 'the delete confirmation')
    await d.b.js("document.querySelector('.uiconfirm-btns [data-uc=\"1\"]').click();true")
    await F.wait(d, "JSON.stringify(%s)===%s" % (NAMES, js_list(['Grace Hopper'])), 30, 'the deleted contact gone')
    assert list(by_fn(ctx.cards(pk, book))) == ['Grace Hopper']


@CHROME
def test_add_edit_and_delete_reach_the_relay_and_a_fresh_device(ran):
    ran('add_edit_and_delete_reach_the_relay_and_a_fresh_device')


@scenario
async def switching_account_on_one_device_never_shows_the_other_address_book(S):
    """ONE device, two accounts: A's address book (painted from this device's copy before the network answers)
    must never be drawn under B -- B's own book list is answered late, so a cached paint would be on screen."""
    ctx, api, tag = S.ctx, S.ctx.api, 'ctx%d' % S.base
    ctx.seed(S.pk(0), 'contacts', [('k1', 'Katherine Johnson', '+15550300')])
    d = await S.device(0, tag=tag)
    await open_contacts(d)
    await F.wait(d, "%s.includes('Katherine Johnson')" % NAMES, 30, "A's contact")
    await F.add_account(d, S.fill(1))
    api.fail('/api/contacts/books', 3, tag)          # B's read is slow: whatever is drawn meanwhile is from the device
    await d.b.js("__PC.switchView('contacts');true")
    seen = []
    loop = __import__('asyncio').get_running_loop()
    end = loop.time() + 6
    while loop.time() < end:
        seen.append(await d.b.js(FEED))
        if 'No addressbook yet' in seen[-1]:
            break
        await __import__('asyncio').sleep(.1)
    assert not [t for t in seen if 'Katherine' in t], [t for t in seen if 'Katherine' in t][:1]
    await F.wait(d, "!!document.querySelector('#ct-mkbook')", 20, "B's own (empty) contacts")
    # And back to A: A's book again, and B's (empty) state did not leak the other way.
    api.heal('/api/contacts/books', tag)
    await F.switch_account(d, S.fill(0))
    await open_contacts(d)
    await F.wait(d, "%s.includes('Katherine Johnson')" % NAMES, 30, "A's contact after switching back")
    assert [b['id'] for b in ctx.books(S.pk(1))] == []


@CHROME
def test_switching_account_on_one_device_never_shows_the_other_address_book(ran):
    ran('switching_account_on_one_device_never_shows_the_other_address_book')


@scenario
async def a_fresh_device_whose_book_list_failed_says_so_and_offers_nothing_to_overwrite(S):
    ctx, api, tag, pk = S.ctx, S.ctx.api, 'ctb%d' % S.base, S.pk(0)
    ctx.seed(pk, 'contacts', [('h1', 'Hedy Lamarr', '+15550400')])
    for how in (503, 'drop'):
        api.fail('/api/contacts/books', how, tag)
        d = await S.device(0, tag=tag)
        await open_contacts(d)
        await F.wait(d, "!!document.querySelector('#feed .ws-err')", 30, 'the book list error (%s)' % how)
        text = await d.b.js(FEED)
        assert 'No addressbook yet' not in text and not await d.b.js("!!document.querySelector('#ct-mkbook,#ct-new')"), text
        # The node answers again: the screen must be able to ask again without leaving it.
        api.heal('/api/contacts/books', tag)
        await F.wait(d, "!!document.querySelector('#ct-retry')", 10, 'a way to try again (%s)' % how)
        await d.b.js("document.querySelector('#ct-retry').click();true")
        await F.wait(d, "%s.includes('Hedy Lamarr')" % NAMES, 30, 'the address book after Try again (%s)' % how)
        await S.close()
        S.open = []
    assert [b['id'] for b in ctx.books(pk)] == ['contacts']


@CHROME
def test_a_fresh_device_whose_book_list_failed_says_so_and_offers_nothing_to_overwrite(ran):
    ran('a_fresh_device_whose_book_list_failed_says_so_and_offers_nothing_to_overwrite')


@scenario
async def a_book_whose_cards_did_not_load_is_not_an_empty_book(S):
    ctx, api, tag, pk = S.ctx, S.ctx.api, 'ctc%d' % S.base, S.pk(0)
    ctx.seed(pk, 'contacts', [('m1', 'Margaret Hamilton', '+15550500'), ('m2', 'Mary Jackson', '+15550501')])
    api.fail('/api/contacts/cards', 503, tag)
    d = await S.device(0, tag=tag)
    await open_contacts(d)
    await F.wait(d, "!!document.querySelector('#ct-new') || !!document.querySelector('#feed .ws-err')", 30,
                 'the book drawn')
    text = await d.b.js(FEED)
    assert 'No contacts in this book yet' not in text and '0 contacts' not in text, text
    assert 'could' in text.lower() or 'try again' in text.lower(), text
    api.heal('/api/contacts/cards', tag)
    await F.wait(d, "!!document.querySelector('#ct-retry')", 10, 'a way to try again')
    await d.b.js("document.querySelector('#ct-retry').click();true")
    await F.wait(d, "JSON.stringify(%s)===%s" % (NAMES, js_list(['Margaret Hamilton', 'Mary Jackson'])), 30,
                 'the contacts after Try again')
    assert 'could' not in (await d.b.js(FEED)).lower()


@CHROME
def test_a_book_whose_cards_did_not_load_is_not_an_empty_book(ran):
    ran('a_book_whose_cards_did_not_load_is_not_an_empty_book')


@scenario
async def a_slow_node_shows_a_spinner_then_the_contacts(S):
    ctx, api, tag, pk = S.ctx, S.ctx.api, 'cts%d' % S.base, S.pk(0)
    ctx.seed(pk, 'contacts', [('r1', 'Radia Perlman', '+15550600')])
    api.fail('/api/contacts/cards', 3, tag)
    d = await S.device(0, tag=tag)
    await d.b.js("__PC.switchView('contacts');true")
    await F.wait(d, "!!document.querySelector('#feed .ct-wrap .spinner')", 10, 'the spinner while it waits')
    assert 'No addressbook' not in await d.b.js(FEED)
    await F.wait(d, "%s.includes('Radia Perlman')" % NAMES, 30, 'the slow answer drawn')
    api.heal('/api/contacts/cards', tag)


@CHROME
def test_a_slow_node_shows_a_spinner_then_the_contacts(ran):
    ran('a_slow_node_shows_a_spinner_then_the_contacts')


@scenario
async def a_save_or_delete_the_node_refused_is_said_and_kept(S):
    ctx, api, tag, pk = S.ctx, S.ctx.api, 'ctw%d' % S.base, S.pk(0)
    ctx.seed(pk, 'contacts', [])
    d = await S.device(0, tag=tag)
    await open_contacts(d)
    await F.wait(d, "!!document.querySelector('#ct-new')", 30, 'the empty book')
    api.fail('/api/contacts/cards', 503, tag)
    await d.b.js("document.querySelector('#ct-new').click();true")
    await fill_and_save(d, 'Annie', 'Easley', '+15550700')
    await F.wait(d, "(window.__toastLog||[]).some(t=>/could not save/i.test(t))", 20, 'the refusal said')
    # The form is still open with what was typed; nothing reached the relay.
    assert await d.b.js("document.querySelector('#cc-given') && document.querySelector('#cc-given').value") == 'Annie'
    assert ctx.cards(pk, 'contacts') == []
    api.heal('/api/contacts/cards', tag)
    await d.b.js("document.querySelector('#cc-save').click();true")
    await F.wait(d, "%s.includes('Annie Easley')" % NAMES, 30, 'saved on the second press')
    assert list(by_fn(ctx.cards(pk, 'contacts'))) == ['Annie Easley']
    # A delete the relay refuses: said, and the contact is still there -- never "deleted".
    ctx.refuse_deletes.add(ctx.storage_pk(pk))
    await open_row(d, 'Annie Easley')
    await d.b.js("document.querySelector('#cc-del').click();true")
    await F.wait(d, "!!document.querySelector('.uiconfirm-btns [data-uc=\"1\"]')", 10, 'the delete confirmation')
    await d.b.js("document.querySelector('.uiconfirm-btns [data-uc=\"1\"]').click();true")
    await F.wait(d, "(window.__toastLog||[]).some(t=>/could not delete/i.test(t))", 20, 'the refused delete said')
    assert not [t for t in await d.toasts() if t.strip() == 'deleted'], await d.toasts()
    assert list(by_fn(ctx.cards(pk, 'contacts'))) == ['Annie Easley']
    ctx.refuse_deletes.discard(ctx.storage_pk(pk))
    await d.b.js("document.querySelector('#cc-del').click();true")
    await F.wait(d, "!!document.querySelector('.uiconfirm-btns [data-uc=\"1\"]')", 10, 'the delete confirmation again')
    await d.b.js("document.querySelector('.uiconfirm-btns [data-uc=\"1\"]').click();true")
    await F.wait(d, "!!document.querySelector('#feed .empty')", 30, 'deleted once the relay accepts it')
    assert ctx.cards(pk, 'contacts') == []


@CHROME
def test_a_save_or_delete_the_node_refused_is_said_and_kept(ran):
    ran('a_save_or_delete_the_node_refused_is_said_and_kept')
