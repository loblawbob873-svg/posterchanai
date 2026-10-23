"""Your profile and your follows are the SAME on every device -- an edit made on one shows on a fresh one, a
follow and an unfollow made on one hold on the other, and a slow relay never costs you what you had.

A kind 0 (profile) and a kind 3 (follow list) are REPLACEABLE: every save publishes the WHOLE document, so a
device that saves from an incomplete picture of it -- a fresh device whose read has not come back yet, an
open device that has not seen another one's unfollow -- writes its gap over the account for everybody. Each
scenario drives the SHIPPED client in real Chrome through the controls a person uses (Edit on the profile,
the profile's menu -> Follow / Unfollow) on tests/client/wallpaper_harness.py's real relay, every scenario
its own account, three at a time (the private-docs runner). "Fresh device" is a new Chrome profile with
nothing cached; "slow relay" is the home relay answering this account's kind-0 / kind-3 reads late
(STATE['slow_reqs']), later than the client waits.

Fail-first: PC_WALLPAPER_OVERRIDE="/static/js/client/app.js=/tmp/mutant/app.js" serves another copy of a
shipped file.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import wallpaper_harness as H
from tests.client.test_private_docs_save_with_a_silent_relay_full_app import no_errors, run_scenarios

CHROME = pytest.mark.skipif(not Path(H.CHROME).exists(), reason='Chrome required')
SCENARIOS = {}
FIRST_FILL = 120           # account keys 120, 121, ... -- never the private-docs suites' 40.. / 80..
PEOPLE = {'xavier': 201, 'yvonne': 202, 'zed': 203}     # other accounts to follow (their key fills)
COST = {}
SLOW = 12                  # seconds: later than every read window the client gives a kind 0 / kind 3


def scenario(fn):
    SCENARIOS[fn.__name__] = fn
    return fn


@pytest.fixture(scope='module')
def ran(request):
    return run_scenarios(request, SCENARIOS, COST, FIRST_FILL)


def _pubkey(fill):
    import coincurve
    return coincurve.PrivateKey(bytes([fill]) * 32).public_key.format(True)[1:].hex()


PK = {name: _pubkey(fill) for name, fill in PEOPLE.items()}


# ------------------------------------------------------------------------------------------------ helpers

def _newest(pk, kind):
    evs = [e for e in H.STATE['events'] if e.get('pubkey') == pk and e.get('kind') == kind]
    return max(evs, key=lambda e: e['created_at']) if evs else None


def _profile(pk):
    ev = _newest(pk, 0)
    return json.loads(ev['content']) if ev else None


def _follows(pk):
    ev = _newest(pk, 3)
    return {t[1] for t in ev['tags'] if t and t[0] == 'p'} if ev else None


async def until(fn, seconds, what, b=None):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while loop.time() < deadline:
        got = fn()
        if got:
            return got
        await asyncio.sleep(.2)
    raise AssertionError({'waiting for': what, 'toasts': b and await b.js('__shown'),
                          'errors': b and await b.js('__errors')})


SHOWN = r"""(()=>{if(window.__shown)return true;window.__shown=[];const root=document.querySelector('#toast-root');
  new MutationObserver(ms=>ms.forEach(m=>m.addedNodes.forEach(n=>{if(n.textContent)__shown.push(n.textContent)})))
    .observe(root,{childList:true});return true})()"""


async def device(S):
    """A device signed in to this scenario's account, recording every toast it SHOWS (`__shown`)."""
    b = await S.device()
    await b.js(SHOWN)
    return b


async def seed_people(b):
    """Each person to follow has a profile on the relay (their own signed kind 0), as anyone you follow does."""
    for name, fill in PEOPLE.items():
        if _newest(PK[name], 0):
            continue
        ev = await b.js("""NostrTools.finalizeEvent({kind:0,created_at:Math.floor(Date.now()/1000)-60,tags:[],
            content:JSON.stringify({name:%s})},new Uint8Array(32).fill(%d))""" % (json.dumps(name.title()), fill))
        H.RELAY._store(ev)


async def open_profile(b, pk):
    """Open a profile and wait for THAT profile's header (never the one already on screen) and its menu."""
    await b.js("document.querySelectorAll('.menu-pop,.pop-backdrop').forEach(p=>p.remove());"
               "__PC.openProfile(%s);true" % json.dumps(pk))
    await H.wait(b, "!!document.querySelector('#feed .prof #prof-nip05s[data-pk=\"%s\"]') && "
                    "!!document.querySelector('#feed #prof-menu')" % pk, 60)


async def header(b):
    return await b.js("""(()=>{const p=document.querySelector('#feed .prof');if(!p)return null;
      const img=p.querySelector('img.pav');
      return {name:(p.querySelector('.pbody h2')||{}).textContent||'',about:(p.querySelector('.about')||{}).textContent||'',
              pic:img?img.getAttribute('src'):'',picLoaded:!!(img&&img.complete&&img.naturalWidth>0)}})()""")


async def menu_label(b, pk):
    """Open the profile's menu (☰) and read its Follow / Unfollow item -- what the person is offered."""
    await open_profile(b, pk)
    await b.js("document.querySelector('#prof-menu').click();true")
    await H.wait(b, "!!document.querySelector('.menu-pop [data-m=\"follow\"]')")
    label = await b.js("document.querySelector('.menu-pop [data-m=\"follow\"]').textContent")
    await b.js("document.querySelectorAll('.menu-pop,.pop-backdrop').forEach(p=>p.remove());true")
    return label


async def press_follow(b, pk):
    """Profile -> ☰ -> Follow (or Unfollow): the item the person presses."""
    await open_profile(b, pk)
    await b.js("document.querySelector('#prof-menu').click();true")
    await H.wait(b, "!!document.querySelector('.menu-pop [data-m=\"follow\"]')")
    label = await b.js("document.querySelector('.menu-pop [data-m=\"follow\"]').textContent")
    n = await b.js("__shown.length")
    await b.js("document.querySelector('.menu-pop [data-m=\"follow\"]').click();true")
    # The press is over when the app says how it went (followed / unfollowed / why not) -- it then redraws
    # this profile, which must not land on top of the next screen the test opens.
    try:
        await H.wait(b, "__shown.length>%d" % n, 30)
    except AssertionError:
        pass
    return label


async def shows(b, pk, want, seconds=40):
    """Wait until this device's menu for `pk` offers `want` ('Unfollow' = it holds the follow, 'Follow' = not)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    label = None
    while loop.time() < deadline:
        label = await menu_label(b, pk)
        if ('Unfollow' in label) == (want == 'Unfollow'):
            return label
        await asyncio.sleep(.5)
    raise AssertionError({'menu for': pk[:8], 'wanted': want, 'shows': label, 'toasts': await b.js('__shown')})


async def follows_settled(b, S, want, seconds=40):
    """The relay holds exactly `want` as this account's follow list (and the device that pressed got its toast)."""
    await until(lambda: _follows(S.pk) == want, seconds, ('relay follow list', sorted(want), _follows(S.pk)), b)


def put_picture():
    """A real picture on the instance's Blossom, so the fresh device can be seen to LOAD it."""
    import base64
    import hashlib
    data = base64.b64decode(H.PNG_BLUE)
    sha = hashlib.sha256(data).hexdigest()
    H.STATE['blobs'][sha] = data
    return f'http://127.0.0.1:{H.HTTP.server_address[1]}/blossom/{sha}.png'


async def edit_profile(b, S, **fields):
    """Own profile -> Edit -> type into the fields asked for -> Save."""
    await open_profile(b, S.pk)
    await b.js("document.querySelector('#edit-prof').click();true")
    await H.wait(b, "!!document.querySelector('#pf-save') && !document.querySelector('#pf-save').disabled", 30)
    for fid, value in fields.items():
        await b.js("""(()=>{const x=document.querySelector('#pf-%s');x.value=%s;
          x.dispatchEvent(new Event('input',{bubbles:true}));return true})()""" % (fid, json.dumps(value)))
    form = await b.js("({name:document.querySelector('#pf-name').value,about:document.querySelector('#pf-about').value,"
                      "pic:document.querySelector('#pf-pic').value})")
    await b.js("document.querySelector('#pf-save').click();true")
    return form


# ------------------------------------------------------------------------------------------------ profile

@scenario
async def a_profile_edit_shows_on_a_fresh_device_and_an_edit_there_keeps_the_rest(S):
    a = await device(S)
    pic = put_picture()
    await edit_profile(a, S, name='Ada Lovelace', about='Analytical engines, mostly.', pic=pic)
    await until(lambda: (_profile(S.pk) or {}).get('name') == 'Ada Lovelace', 30, 'the profile on the relay', a)
    got = _profile(S.pk)
    assert got['about'] == 'Analytical engines, mostly.' and got['picture'] == pic, got
    await H.wait(a, "/Ada Lovelace/.test((document.querySelector('#feed .prof .pbody h2')||{}).textContent||'')")

    b = await device(S)                                   # a FRESH device: nothing cached
    await open_profile(b, S.pk)
    await H.wait(b, "(()=>{const i=document.querySelector('#feed .prof img.pav');"
                    "return !!i&&i.complete&&i.naturalWidth>0&&i.getAttribute('src')===%s})()" % json.dumps(pic))
    h = await header(b)
    assert 'Ada Lovelace' in h['name'] and h['about'] == 'Analytical engines, mostly.' and h['picLoaded'], h

    # The fresh device edits ONLY the About: the name and picture it never touched are kept.
    form = await edit_profile(b, S, about='Now with punch cards.')
    assert form['name'] == 'Ada Lovelace' and form['pic'] == pic, ('the editor opened without the profile', form)
    await until(lambda: (_profile(S.pk) or {}).get('about') == 'Now with punch cards.', 30, 'the second edit', b)
    got = _profile(S.pk)
    assert got['name'] == 'Ada Lovelace' and got['picture'] == pic, ('an About edit lost the rest', got)

    # ...and the first device, opening the profile again, shows the other device's edit.
    await open_profile(a, S.pk)
    await H.wait(a, "/Now with punch cards/.test((document.querySelector('#feed .prof .about')||{}).textContent||'')", 20,
                 state="({held:(Store.profile(__PC.me().pubkey)||{}).about,toasts:__shown,errors:__errors})")
    assert not no_errors(await a.js('__errors')) and not no_errors(await b.js('__errors'))


def test_a_profile_edit_shows_on_a_fresh_device_and_an_edit_there_keeps_the_rest(ran):
    ran('a_profile_edit_shows_on_a_fresh_device_and_an_edit_there_keeps_the_rest')


@scenario
async def a_fresh_device_on_a_slow_relay_never_saves_a_blank_profile_over_yours(S):
    a = await device(S)
    pic = put_picture()
    await edit_profile(a, S, name='Grace Hopper', about='Found the moth.', pic=pic)
    await until(lambda: (_profile(S.pk) or {}).get('name') == 'Grace Hopper', 30, 'the profile on the relay', a)

    H.STATE['slow_reqs'][(S.pk, 0)] = SLOW                 # the relay answers this account's profile reads late
    b = await device(S)
    await open_profile(b, S.pk)
    await b.js("document.querySelector('#edit-prof').click();true")
    # Whatever the editor does while it cannot know the profile, the person types a new About and presses Save.
    await H.wait(b, "!!document.querySelector('#pf-about') || __shown.length>0", 40)
    seen = await b.js("({form:(document.querySelector('#pf-name')||{}).value||null,shown:__shown})")
    # Either it found the profile (the form holds it) or it says plainly that it could not -- never a blank form.
    assert seen.get('form') == 'Grace Hopper' or any('load your profile' in t for t in seen['shown']), seen
    if await b.js("!!document.querySelector('#pf-about')"):
        await b.js("""(()=>{const x=document.querySelector('#pf-about');x.value='typed on the slow device';
          x.dispatchEvent(new Event('input',{bubbles:true}));const s=document.querySelector('#pf-save');
          if(s&&!s.disabled)s.click();return true})()""")
    # Watched until the relay's late answer has come and gone -- the window a blank publish would land in.
    loop = asyncio.get_running_loop()
    end = loop.time() + SLOW + 4
    while loop.time() < end:
        got = _profile(S.pk)
        assert got.get('name') == 'Grace Hopper' and got.get('picture') == pic, (
            'a device that never read the profile saved a blank one over it', got, await b.js('__shown'))
        await asyncio.sleep(.25)
    H.STATE['slow_reqs'].pop((S.pk, 0), None)


def test_a_fresh_device_on_a_slow_relay_never_saves_a_blank_profile_over_yours(ran):
    ran('a_fresh_device_on_a_slow_relay_never_saves_a_blank_profile_over_yours')


# ------------------------------------------------------------------------------------------------ follows

@scenario
async def a_follow_and_an_unfollow_reach_a_fresh_device(S):
    a = await device(S)
    await seed_people(a)
    assert 'Follow' in await press_follow(a, PK['xavier']) and 'Unfollow' not in await menu_label(a, PK['yvonne'])
    await follows_settled(a, S, {PK['xavier']})

    b = await device(S)
    await shows(b, PK['xavier'], 'Unfollow')               # the fresh device shows the follow
    assert 'Unfollow' not in await menu_label(b, PK['yvonne'])

    assert 'Unfollow' in await press_follow(a, PK['xavier'])
    await follows_settled(a, S, set())
    c = await device(S)                                   # a fresh device after the unfollow
    await H.wait(c, "(JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}').followsCache||null)!==null", 30)
    assert 'Unfollow' not in await menu_label(c, PK['xavier']), 'the unfollow did not reach a fresh device'


def test_a_follow_and_an_unfollow_reach_a_fresh_device(ran):
    ran('a_follow_and_an_unfollow_reach_a_fresh_device')


@scenario
async def an_unfollow_on_one_device_is_not_undone_by_a_follow_on_another(S):
    """Split: B is open with the old list; A unfollows X; B then follows Z. X must stay unfollowed."""
    a = await device(S)
    await seed_people(a)
    await press_follow(a, PK['xavier'])
    await follows_settled(a, S, {PK['xavier']})
    await press_follow(a, PK['yvonne'])
    await follows_settled(a, S, {PK['xavier'], PK['yvonne']})

    b = await device(S)
    await shows(b, PK['xavier'], 'Unfollow')
    await shows(b, PK['yvonne'], 'Unfollow')

    assert 'Unfollow' in await press_follow(a, PK['xavier'])
    await follows_settled(a, S, {PK['yvonne']})

    assert 'Follow' in await press_follow(b, PK['zed'])   # B never reloaded
    await until(lambda: PK['zed'] in (_follows(S.pk) or set()), 30, 'the follow from B', b)
    got = _follows(S.pk)
    assert PK['xavier'] not in got, 'the unfollow made on another device was undone by this follow'
    assert got == {PK['yvonne'], PK['zed']}, got
    assert 'Unfollow' not in await menu_label(b, PK['xavier']), 'B still shows the follow A removed'


def test_an_unfollow_on_one_device_is_not_undone_by_a_follow_on_another(ran):
    ran('an_unfollow_on_one_device_is_not_undone_by_a_follow_on_another')


@scenario
async def a_follow_on_a_fresh_device_with_a_slow_relay_keeps_every_other_follow(S):
    a = await device(S)
    await seed_people(a)
    await press_follow(a, PK['xavier'])
    await follows_settled(a, S, {PK['xavier']})
    await press_follow(a, PK['yvonne'])
    await follows_settled(a, S, {PK['xavier'], PK['yvonne']})

    H.STATE['slow_reqs'][(S.pk, 3)] = SLOW               # the relay answers this account's follow-list reads late
    b = await device(S)
    await press_follow(b, PK['zed'])
    # Either the follow lands ON the full list, or nothing is written -- never a list of one.
    loop = asyncio.get_running_loop()
    end = loop.time() + SLOW + 6
    while loop.time() < end:
        got = _follows(S.pk)
        assert {PK['xavier'], PK['yvonne']} <= got, ('a fresh device on a slow relay wiped the follow list', got,
                                                      await b.js('__shown'))
        if PK['zed'] in got:
            break
        await asyncio.sleep(.25)
    H.STATE['slow_reqs'].pop((S.pk, 3), None)
    if PK['zed'] not in _follows(S.pk):
        shown = await b.js('__shown')
        assert any('follow list' in t for t in shown), ('the follow silently did nothing', shown)
        # The relay answers again: the same press now lands, on the whole list.
        assert 'Unfollow' not in await press_follow(b, PK['zed'])
    await follows_settled(b, S, {PK['xavier'], PK['yvonne'], PK['zed']})


def test_a_follow_on_a_fresh_device_with_a_slow_relay_keeps_every_other_follow(ran):
    ran('a_follow_on_a_fresh_device_with_a_slow_relay_keeps_every_other_follow')
