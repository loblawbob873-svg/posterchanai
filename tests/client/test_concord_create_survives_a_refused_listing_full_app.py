"""A COMMUNITY THAT WAS MADE IS KEPT, EVEN WHEN ITS DISCOVER LISTING IS REFUSED.

Measured live (scripts/check_concord_federation_live.py, 3 creates in 10): "Create" failed with
"Community was created but its discovery announcement was not accepted". `mintPublicRoom` had
already written the owner's membership vault and published the community's genesis when the one
directory relay refused the kind-1 listing, and then it THREW -- so the creating device never saved
the room, while every other device of the account restored it from the vault: a live community
nobody asked for, invisible on the device that made it.

Driven in the shipped desktop bundle with the real Create button and the real PosterCord minting;
only relay I/O is a fixture (an instance relay that accepts, and a directory relay that refuses the
listing while `__refuseListing` is set). Asserted:

1. the listing is refused twice (it is retried once) and the community STAYS on the creating device,
   open, with a warning that names the problem and the way to retry -- not "creation failed";
2. "Publish to Armada Discover" lists it once the relay accepts, re-sending the same signed event;
3. a fresh device of the same account, restoring from the vault the creator wrote, finds exactly the
   community the creator has -- no second, invisible one;
4. a single transient refusal is absorbed by the retry: created and listed, no warning.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


NO_OS = ("localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse("
         "localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));")

DISCOVER = 'wss://relay.dreamith.to'

# Every publish is kept in __pub (the fresh device is served from it). A kind-1 sent to the directory
# relay is refused while __refuseListing > 0 (a number: how many more sends to refuse).
STUBS = r'''(()=>{const p=__PC;window.__copies=[];window.__pub=window.__pub||[];window.__toasts=[];window.__listingSends=0;
window.__refuseListing=window.__refuseListing||0;
const toListing=(relays,ev)=>ev&&ev.kind===1&&(relays||[]).includes(%s);
const refused=(relays,ev)=>{if(!toListing(relays,ev))return false;__listingSends++;if(__refuseListing>0){__refuseListing--;return true;}return false;};
const keep=ev=>{if(!__pub.some(x=>x.id===ev.id))__pub.push(ev);};
const toast=p.toast;p.toast=(m,...a)=>{__toasts.push(String(m));return toast&&toast.call(p,m,...a);};
p.copyValue=v=>{__copies.push(String(v));return true;};
p.relayPublishRoom=async(relays,ev)=>{if(refused(relays,ev))return {ok:false,msg:'blocked: fixture refuses the listing'};keep(ev);return {ok:true,relays};};
p.relayPublishTo=async(relays,ev,o)=>{if(refused(relays,ev))return o&&o.detailed?{ok:false,msg:'blocked'}:0;keep(ev);return o&&o.detailed?{ok:true,relays}:1;};
p.relayQueryFrom=async(relays,filters,options)=>{if(options&&options.report){options.report.ok=[...relays];}
  const out=[];for(const ev of (window.__served||[]))if(filters.some(f=>(!f.kinds||f.kinds.includes(ev.kind))&&(!f.authors||f.authors.includes(ev.pubkey))&&Object.keys(f).filter(k=>k[0]==='#').every(k=>(ev.tags||[]).some(t=>t[0]===k.slice(1)&&f[k].includes(t[1])))))out.push(ev);return out;};
p.relayQuery=async()=>[];return true;})()''' % json.dumps(DISCOVER)

ROOMS = "JSON.parse(localStorage.getItem('pc.concord.rooms.v1.'+__PC.me().pubkey)||'[]')"


async def wait(b, expression, seconds=20):
    for _ in range(int(seconds * 10)):
        if await b.js(expression):
            return True
        await asyncio.sleep(.1)
    return False


async def open_concord(b, refuse=0, served=None):
    await b.call('Emulation.setDeviceMetricsOverride', dict(width=1280, height=900, deviceScaleFactor=1, mobile=False))
    await desktop.login(b)
    await b.js('window.__refuseListing=%d;window.__served=%s;1' % (refuse, json.dumps(served or [])))
    await b.js(STUBS)
    await b.js("__PC.switchView('concord')")
    await b.until("!!window.PCConcord && !!window.PosterCord && !!window.PosterCordReader && document.body.classList.contains('concord-view')")


async def create(b, name):
    await b.until("!!document.querySelector('#cc-create-go')")
    await b.js("document.querySelector('#cc-community-name').value=%s;document.querySelector('#cc-create-go').click();1" % json.dumps(name))
    done = await wait(b, "__toasts.some(t=>/community created|creation failed/.test(t))", 60)
    return {'done': done, 'toasts': await b.js('__toasts'), 'rooms': await b.js(ROOMS),
            'active': await b.js("(document.querySelector('.cc-conversation header')||{}).textContent||''"),
            'sends': await b.js('__listingSends')}


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_refused_listing_keeps_the_community_and_offers_the_retry():
    got = {}

    async def creator(b):
        await open_concord(b, refuse=99)
        got.update(await create(b, 'Refused listing'))
        if not got['rooms']:
            got['pub'] = await b.js('__pub')
            return
        # The relay accepts again: the room's own "Publish to Armada Discover" lists it.
        await b.js('window.__refuseListing=0;__toasts.length=0;1')
        got['button'] = await wait(b, "!!document.querySelector('#cc-publish-listing')", 20)
        await b.js("document.querySelector('#cc-publish-listing').click();1")
        got['listed'] = await wait(b, "__toasts.some(t=>/published to Armada Discover|could not publish listing/.test(t))", 30)
        got['retry_toasts'] = await b.js('__toasts')
        got['listings'] = await b.js("__pub.filter(e=>e.kind===1&&e.tags.some(t=>t[0]==='t'&&t[1]==='concord')).map(e=>({id:e.id,content:e.content}))")
        got['pub'] = await b.js('__pub')
        got['errors'] = await b.js('__errors')

    asyncio.run(desktop.with_browser('online', '', creator, NO_OS))
    assert got['done'], ('the Create button never finished', got['toasts'])

    # A fresh device of the same account restores from the vault the creator wrote.
    fresh = {}

    async def other_device(b):
        await open_concord(b, served=got['pub'])
        fresh['restored'] = await wait(b, ROOMS + '.length>0', 30)
        await asyncio.sleep(2)  # anything else the vault would add arrives in the same sync
        fresh['rooms'] = await b.js(ROOMS)

    asyncio.run(desktop.with_browser('online', '', other_device, NO_OS))
    mine = [r['communityId'] for r in got['rooms']]
    theirs = [r['communityId'] for r in fresh['rooms']]
    assert theirs == mine, (
        'GHOST: the account\'s other device restored a community the creating device does not show '
        '(creator %r, fresh device %r); toasts: %r' % (mine, theirs, got['toasts']))
    assert len(mine) == 1, ('the community was not made at all', got['toasts'])
    room = got['rooms'][0]
    assert room['name'] == 'Refused listing'
    assert got['sends'] >= 2, ('the refused listing was not retried even once', got['sends'])
    assert not any('creation failed' in t for t in got['toasts']), got['toasts']
    warning = [t for t in got['toasts'] if 'community created' in t][-1]
    assert 'not listed in Discover' in warning and 'Publish to Armada Discover' in warning, warning
    assert 'Refused listing' in got['active'] or got['button'], 'the new community is not the open one'
    assert 'listingPending' not in room, 'a transient UI note was saved into the room'
    assert not got['errors'], got['errors']

    assert got['listed'] and any('published to Armada Discover' in t for t in got['retry_toasts']), got['retry_toasts']
    listings = [x for x in got['listings'] if room['url'] in x['content']]
    assert len({x['id'] for x in listings}) == 1, ('the retry signed a second listing (Amethyst spam hash)', listings)


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_one_transient_refusal_is_absorbed_by_the_retry():
    got = {}

    async def creator(b):
        await open_concord(b, refuse=1)
        got.update(await create(b, 'Transient refusal'))

    asyncio.run(desktop.with_browser('online', '', creator, NO_OS))
    assert got['done'], got['toasts']
    assert len(got['rooms']) == 1, got['toasts']
    assert got['sends'] == 2, got['sends']
    created = [t for t in got['toasts'] if 'community created' in t]
    assert created and 'not listed' not in created[-1], got['toasts']
