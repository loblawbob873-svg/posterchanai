"""Budget and the password Vault, driven end to end on a REAL node (the shipped RelayServer over PosterChanDB).

Both are documents only their author can read: Budget is ONE kind-30078 `pcai:budget` NIP-44-encrypted to the
account's own key (every write a read-modify-write of the whole document), the Vault one kind-30078 per login
(`pcai:pw:<id>`, AES-GCM under a vault key that is itself `pcai:pwkey`, NIP-44-wrapped to the account).

  budget  bills added with the Add row, one ticked paid, survive a reload and open on a FRESH device with the same
          totals; two devices with Budget open at once both keep their change (the doc is replaceable -- a device
          that saves from the copy it loaded an hour ago publishes over the other's bill); a slow relay is a spinner
          that turns into the budget; a fresh device nobody answers says so with a Retry, publishes nothing, and the
          Retry shows the budget once the relay is back.
  vault   a login with a password, saved in the editor, opens decrypted on a fresh device and an edit made there
          shows on the first device; a deletion sticks; a fresh device whose relay answers the vault key but never
          the list must not say "No passwords yet" (it holds nothing because it was told nothing) and must write
          nothing.

Harness: tests/client/discover_flows_harness.py. Fail-first: PC_JOURNEY_OVERRIDE="/static/js/client/budget.js=/tmp/x.js".
"""
import asyncio
import json

import pytest

from tests.client import discover_flows_harness as F
from tests.client import realnode_harness as R

CHROME = pytest.mark.skipif(not R.chrome_ok(), reason='Chrome required')
FEATURE_COVERS = ('budget', 'vault')
FIRST_FILL = 200
SCENARIOS = {}
COST = {'two_devices_with_budget_open_both_keep_their_bill': 30,
        'a_fresh_device_nobody_answers_says_so_and_retry_shows_the_budget': 30,
        'a_login_opens_decrypted_on_a_fresh_device_and_an_edit_comes_back': 30}


def scenario(fn):
    SCENARIOS[fn.__name__] = fn
    return fn


class Ctx:
    def __init__(self, n):
        self.n = n
        self.gate = F.Gate(n.home)

    def stop(self):
        pass


@pytest.fixture(scope='module')
def ran(request):
    return F.run(request, SCENARIOS, COST, FIRST_FILL, setup=Ctx)


def docs(n, pk, d):
    return F.stored(n, {'kinds': [30078], 'authors': [pk], '#d': [d]})


async def reload(d):
    await d.b.js("window.__beforeReload=1;true")
    await d.b.call('Page.navigate', {'url': d.node.origin + '/client'})
    await R.wait(d.b, "!window.__beforeReload && !!window.__PC && !!__PC.me && !!__PC.me() && Relay.status==='ok'", 60,
                 'reopened and signed in')


# ------------------------------------------------------------------------------------------------ budget
async def open_budget(d, seconds=40):
    await d.b.js("__PC.switchView('budget');true")
    await F.wait(d, "!!document.querySelector('#bg-n') || !!document.querySelector('.bg-err')", seconds, 'Budget')


async def add_bill(d, name, amount, income=False):
    """The Add row: name, amount, (Income), Add -- as a person adds a bill."""
    await d.b.js("""(()=>{document.querySelector('#bg-n').value=%s;document.querySelector('#bg-a').value=%s;
      document.querySelector('#bg-inc').checked=%s;document.querySelector('#bg-save').click();return true})()"""
                 % (json.dumps(name), json.dumps(amount), 'true' if income else 'false'))
    await F.wait(d, "[...document.querySelectorAll('.bg-row .bg-name')].some(x=>x.textContent.includes(%s))"
                 % json.dumps(name), 20, 'the bill listed')


TILES = ("Object.fromEntries([...document.querySelectorAll('.bg-tile')].map(t=>[t.querySelector('.bg-tl').textContent,"
         "t.querySelector('.bg-tv').textContent]))")
BILLS = "[...document.querySelectorAll('.bg-row .bg-name')].map(x=>x.firstChild.textContent.trim())"


async def budget_on_relay(d, n):
    """The account's newest pcai:budget as the RELAY holds it, decrypted with the page's own key: the bill names."""
    pk = await d.b.js('__PC.me().pubkey')
    got = docs(n, pk, 'pcai:budget')
    if not got:
        return None
    ev = max(got, key=lambda e: e['created_at'])
    doc = await d.b.js("(async()=>JSON.parse(await __PC.nip44dec(__PC.me().pubkey,%s)))()" % json.dumps(ev['content']))
    return sorted(b['name'] for b in doc.get('bills') or [])


@scenario
async def bills_and_a_paid_mark_survive_a_reload_and_open_on_a_fresh_device(S):
    a = await S.device(0)
    await open_budget(a)
    await add_bill(a, 'Salary', '2000', income=True)
    await add_bill(a, 'Electric company', '42.50')
    await add_bill(a, 'Water', '30')
    for _ in range(100):                              # every add is stored BEFORE the tick, so only the tick's own
        if await budget_on_relay(a, S.n) == ['Electric company', 'Salary', 'Water']:   # save can carry the paid mark
            break
        await asyncio.sleep(.2)
    assert await budget_on_relay(a, S.n) == ['Electric company', 'Salary', 'Water']
    await a.b.js("[...document.querySelectorAll('.bg-row')].find(r=>r.textContent.includes('Electric company'))"
                 ".querySelector('[data-act=paid]').click();true")
    want = {'Income': '$2,000.00', 'Bills due': '$30.00', 'Paid': '$42.50', 'Remaining': '$1,927.50'}
    await F.wait(a, "JSON.stringify(%s)===%s" % (TILES, json.dumps(json.dumps(want, separators=(',', ':')))), 20,
                 'the totals after ticking a bill paid')
    await F.until(lambda: docs(S.n, S.pk(0), 'pcai:budget'), 30, 'pcai:budget on the home relay', a)
    stored = max(docs(S.n, S.pk(0), 'pcai:budget'), key=lambda e: e['created_at'])
    assert 'Electric' not in stored['content'], 'the budget went to the relay in plaintext'
    await asyncio.sleep(1.5)                          # the last save (the paid mark) is serialized behind the adds
    await reload(a)
    await open_budget(a)
    await F.wait(a, "JSON.stringify(%s)===%s" % (TILES, json.dumps(json.dumps(want, separators=(',', ':')))), 30,
                 'the same totals after a reload')
    b = await S.device(0)
    await open_budget(b)
    await F.wait(b, "JSON.stringify(%s)===%s" % (TILES, json.dumps(json.dumps(want, separators=(',', ':')))), 30,
                 'the same totals on a fresh device')
    assert await b.b.js("[...document.querySelectorAll('.bg-row.done')].some(r=>r.textContent.includes('Electric company'))")


@CHROME
def test_bills_and_a_paid_mark_survive_a_reload_and_open_on_a_fresh_device(ran):
    ran('bills_and_a_paid_mark_survive_a_reload_and_open_on_a_fresh_device')


@scenario
async def two_devices_with_budget_open_both_keep_their_bill(S):
    a = await S.device(0)
    await open_budget(a)
    await add_bill(a, 'Rent', '900')
    await F.until(lambda: docs(S.n, S.pk(0), 'pcai:budget'), 30, 'the first bill on the relay', a)
    b = await S.device(0)                             # the phone, while the laptop still has Budget open
    await open_budget(b)
    await F.wait(b, "%s.includes('Rent')" % BILLS, 30, 'the laptop\'s bill on the phone')
    await add_bill(b, 'Phone plan', '25')
    for _ in range(100):
        if await budget_on_relay(b, S.n) == ['Phone plan', 'Rent']:
            break
        await asyncio.sleep(.2)
    assert await budget_on_relay(b, S.n) == ['Phone plan', 'Rent'], await budget_on_relay(b, S.n)
    await asyncio.sleep(1.1)                          # a later second, as a real "later" is
    await add_bill(a, 'Internet', '60')               # the laptop, never having left the screen
    for _ in range(100):
        got = await budget_on_relay(a, S.n)
        if got and 'Internet' in got:
            break
        await asyncio.sleep(.2)
    await asyncio.sleep(1)
    got = await budget_on_relay(a, S.n)
    assert got == ['Internet', 'Phone plan', 'Rent'], ('a save from a device holding an older copy dropped a bill', got)
    await F.wait(a, "%s.includes('Phone plan')" % BILLS, 20, 'the phone\'s bill shown on the laptop too')


@CHROME
def test_two_devices_with_budget_open_both_keep_their_bill(ran):
    ran('two_devices_with_budget_open_both_keep_their_bill')


@scenario
async def a_slow_relay_is_a_spinner_that_becomes_the_budget(S):
    a = await S.device(0)
    await open_budget(a)
    await add_bill(a, 'Gym', '35')
    await F.until(lambda: docs(S.n, S.pk(0), 'pcai:budget'), 30, 'the bill on the relay', a)
    tag, pk = 'bgs%d' % S.base, S.pk(0)
    rule = S.ctx.gate.add(lambda fl, c: 4 if F.device_of(c) == tag and F.asks(fl, kinds=(30078,), authors=(pk,)) else None)
    try:
        b = await S.device(0, tag=tag)
        await b.b.js("__PC.switchView('budget');true")
        await F.wait(b, "!!document.querySelector('#feed .spinner')", 10, 'a spinner while the relay is slow')
        await F.wait(b, "%s.includes('Gym')" % BILLS, 40, 'the budget, once the slow answer landed')
        assert not await b.b.js("!!document.querySelector('.bg-err')")
    finally:
        S.ctx.gate.drop(rule)


@CHROME
def test_a_slow_relay_is_a_spinner_that_becomes_the_budget(ran):
    ran('a_slow_relay_is_a_spinner_that_becomes_the_budget')


@scenario
async def a_fresh_device_nobody_answers_says_so_and_retry_shows_the_budget(S):
    a = await S.device(0)
    await open_budget(a)
    await add_bill(a, 'Insurance', '120')
    for _ in range(100):                              # the bill's save, not the month-rollover save before it
        if await budget_on_relay(a, S.n) == ['Insurance']:
            break
        await asyncio.sleep(.2)
    assert await budget_on_relay(a, S.n) == ['Insurance']
    before = docs(S.n, S.pk(0), 'pcai:budget')[0]['id']
    tag, pk = 'bgn%d' % S.base, S.pk(0)
    rule = S.ctx.gate.add(lambda fl, c: 'never' if F.device_of(c) == tag and F.asks(fl, kinds=(30078,), authors=(pk,))
                          else None)
    try:
        b = await S.device(0, tag=tag)
        await open_budget(b, 60)
        assert await b.b.js("!!document.querySelector('.bg-err') && !document.querySelector('#bg-n')"), \
            await b.b.js("document.querySelector('#feed').innerText")
        assert docs(S.n, pk, 'pcai:budget')[0]['id'] == before, 'a device that never read the budget wrote one'
        S.ctx.gate.drop(rule)
        await b.b.js("document.querySelector('#bg-retry').click();true")
        await F.wait(b, "%s.includes('Insurance')" % BILLS, 40, 'Retry showed the budget')
    finally:
        S.ctx.gate.drop(rule)


@CHROME
def test_a_fresh_device_nobody_answers_says_so_and_retry_shows_the_budget(ran):
    ran('a_fresh_device_nobody_answers_says_so_and_retry_shows_the_budget')


# ------------------------------------------------------------------------------------------------ vault
MEMBER = "PCInstanceAccess.accept({pubkey:__PC.me().pubkey,qualified:true},__PC.me().pubkey);true"
ITEMS = "[...document.querySelectorAll('.pv-item b')].map(x=>x.textContent)"


async def open_vault(d, seconds=40):
    await d.b.js(MEMBER)
    await d.b.js("__PC.switchView('vault');true")
    await F.wait(d, "!!document.querySelector('.pv-new') || !!document.querySelector('.pv-locked')", seconds, 'the vault')


SET = """((sel,v)=>{const x=document.querySelector(sel);x.value=v;x.dispatchEvent(new Event('input',{bubbles:true}));
  return true})"""


async def type_into(d, sel, value):
    await d.b.js(SET + '(%s,%s)' % (json.dumps(sel), json.dumps(value)))


async def new_login(d, title, user, password):
    await d.b.js("document.querySelector('.pv-new').click();true")
    await F.wait(d, "!!document.querySelector('.pv-editor .pv-title')", 20, 'the entry editor')
    await type_into(d, '.pv-editor .pv-title', title)
    await type_into(d, '.pv-editor .pv-user', user)
    await type_into(d, '.pv-editor .pv-pass', password)
    await F.wait(d, "/^saved$/.test(document.querySelector('.pv-editor .pv-state').textContent)", 20, 'the entry saved')
    await d.b.js("document.querySelector('.pv-back').click();true")


async def open_entry(d, title):
    await F.wait(d, "%s.includes(%s)" % (ITEMS, json.dumps(title)), 40, 'the login listed')
    await d.b.js("[...document.querySelectorAll('.pv-item')].find(x=>x.querySelector('b').textContent===%s).click();true"
                 % json.dumps(title))
    await F.wait(d, "!!document.querySelector('.pv-editor .pv-pass')", 20, 'the entry open')


@scenario
async def a_login_opens_decrypted_on_a_fresh_device_and_an_edit_comes_back(S):
    pk = S.pk(0)
    a = await S.device(0)
    await open_vault(a)
    await new_login(a, 'Bank of Testing', 'alice@example.invalid', 'correct horse battery staple')
    await F.until(lambda: [e for e in F.stored(S.n, {'kinds': [30078], 'authors': [pk], '#l': ['pcai-pw']})
                           if e['content'] and 'pcai:pw:' in json.dumps(e['tags'])], 30, 'the login on the relay', a)
    assert not [e for e in F.stored(S.n, {'kinds': [30078], 'authors': [pk], '#l': ['pcai-pw']})
                if 'battery staple' in e['content'] or 'Bank of Testing' in e['content']], 'the login went out in plaintext'
    b = await S.device(0)
    await open_vault(b)
    await open_entry(b, 'Bank of Testing')
    assert await b.b.js("document.querySelector('.pv-editor .pv-pass').value") == 'correct horse battery staple'
    assert await b.b.js("document.querySelector('.pv-editor .pv-user').value") == 'alice@example.invalid'
    assert len(docs(S.n, pk, 'pcai:pwkey')) == 1, 'the fresh device minted a second vault key'
    # Edited on the phone: the laptop, still showing the vault, follows.
    await type_into(b, '.pv-editor .pv-title', 'Bank of Testing (joint)')
    await F.wait(b, "/^saved$/.test(document.querySelector('.pv-editor .pv-state').textContent)", 20, 'the edit saved')
    await F.wait(a, "%s.includes('Bank of Testing (joint)')" % ITEMS, 40, 'the edit on the first device')


@CHROME
def test_a_login_opens_decrypted_on_a_fresh_device_and_an_edit_comes_back(ran):
    ran('a_login_opens_decrypted_on_a_fresh_device_and_an_edit_comes_back')


@scenario
async def a_deleted_login_stays_deleted_on_a_fresh_device(S):
    a = await S.device(0)
    await open_vault(a)
    await new_login(a, 'Old forum', 'bob', 'hunter2hunter2')
    await new_login(a, 'Mail', 'bob@example.invalid', 'tr0ub4dor&3xyz')
    await open_entry(a, 'Old forum')
    await a.b.js("document.querySelector('.pv-editor .pv-del').click();true")
    await F.wait(a, "!!document.querySelector('.uiconfirm-bg [data-uc=\"1\"]')", 20, 'the confirm')
    await a.b.js("document.querySelector('.uiconfirm-bg [data-uc=\"1\"]').click();true")
    await F.wait(a, "!%s.includes('Old forum')" % ITEMS, 20, 'the login gone here')
    await asyncio.sleep(1)
    b = await S.device(0)
    await open_vault(b)
    await F.wait(b, "%s.includes('Mail')" % ITEMS, 40, 'the kept login')
    await asyncio.sleep(2)
    assert 'Old forum' not in await b.b.js(ITEMS), 'a deleted login is back on a fresh device'


@CHROME
def test_a_deleted_login_stays_deleted_on_a_fresh_device(ran):
    ran('a_deleted_login_stays_deleted_on_a_fresh_device')


def _asks_vault_list(fl, pk):
    return any(30078 in (f.get('kinds') or []) and pk in (f.get('authors') or []) and 'pcai-pw' in (f.get('#l') or [])
               for f in fl)


@scenario
async def a_fresh_device_whose_list_read_is_never_answered_does_not_say_no_passwords(S):
    pk = S.pk(0)
    a = await S.device(0)
    await open_vault(a)
    await new_login(a, 'Work VPN', 'carol', 'p4ss-w0rd-long-enough')
    await F.until(lambda: docs(S.n, pk, 'pcai:pwkey'), 30, 'the vault key on the relay', a)
    tag = 'pv%d' % S.base
    rule = S.ctx.gate.add(lambda fl, c: 'never' if F.device_of(c) == tag and _asks_vault_list(fl, pk) else None)
    try:
        b = await S.device(0, tag=tag)
        await open_vault(b)
        n_before = len(F.stored(S.n, {'kinds': [30078], 'authors': [pk], '#l': ['pcai-pw']}))
        await F.wait(b, "!!document.querySelector('.pv-unreachable')", 40,
                     'the vault said it could not load the list')
        text = await b.b.js("document.querySelector('#feed').innerText")
        assert 'No passwords yet' not in text, ('an unanswered read told the person their vault is empty', text)
        assert len(F.stored(S.n, {'kinds': [30078], 'authors': [pk], '#l': ['pcai-pw']})) == n_before
        S.ctx.gate.drop(rule)
        await b.b.js("document.querySelector('#pv-list-retry').click();true")
        await F.wait(b, "%s.includes('Work VPN')" % ITEMS, 40, 'Try again listed the login')
    finally:
        S.ctx.gate.drop(rule)


@CHROME
def test_a_fresh_device_whose_list_read_is_never_answered_does_not_say_no_passwords(ran):
    ran('a_fresh_device_whose_list_read_is_never_answered_does_not_say_no_passwords')
