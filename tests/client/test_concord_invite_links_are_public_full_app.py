"""A CONCORD INVITE MINTED IN THE APP POINTS AT A HOST SOMEBODY ELSE CAN REACH.

Reported from the APK: every invite link read `https://localhost/invite/naddr1…#…`. Both mint
paths in concord.js passed `base: location.origin`, which in a bundled app is the app's OWN origin
(`https://localhost` on Android, `app://posterchan` on the desktop) -- so the link sent to a friend
opened the friend's own phone. The host is not part of the secret (the naddr and the # fragment are
the whole invitation), which is also why a link already minted that way must keep working.

Driven in the shipped desktop bundle served from a loopback origin (the APK/desktop shape: the page
is NOT the instance) with `__PC_API_BASE__` set by the bundle's own shim. Only relay I/O and the
clipboard are fixtures; minting is the real PosterCord + the real Create button.

1. With an instance: the link copied after "Create" starts with the INSTANCE's origin.
2. With NO instance: it starts with the canonical public CORD-05 host, never the page's origin.
3. An invite already minted against https://localhost still previews when pasted, and copying that
   room's link hands out the public host instead.
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
NO_INSTANCE = "window.pcShell.instanceSync='';"

# Relay I/O answers as an accepting, reachable relay; the clipboard is captured.
STUBS = r'''(()=>{const p=__PC;window.__copies=[];window.__pub=[];
p.copyValue=v=>{__copies.push(String(v));return true;};
p.relayPublishRoom=async(relays,ev)=>{__pub.push(ev);return {ok:true,relays};};
p.relayPublishTo=async(relays,ev)=>{__pub.push(ev);return {ok:true,relays};};
p.relayQueryFrom=async(relays,filters,options)=>{if(options&&options.report){options.report.ok=[...relays];}
  const out=[];for(const ev of (window.__served||[]))if(filters.some(f=>(!f.kinds||f.kinds.includes(ev.kind))&&(!f.authors||f.authors.includes(ev.pubkey))))out.push(ev);return out;};
p.relayQuery=async()=>[];return true;})()'''


async def wait(b, expression, seconds=20):
    for _ in range(int(seconds * 10)):
        if await b.js(expression):
            return True
        await asyncio.sleep(.1)
    return False


async def open_concord(b):
    await b.call('Emulation.setDeviceMetricsOverride', dict(width=390, height=844, deviceScaleFactor=2, mobile=True))
    await desktop.login(b)
    await b.js("__PC.switchView('concord')")
    await b.until("!!window.PCConcord && !!window.PosterCord && document.body.classList.contains('concord-view')")
    await b.js(STUBS)


async def create_and_copy(b):
    await open_concord(b)
    await b.until("!!document.querySelector('#cc-create-go')")
    await b.js("document.querySelector('#cc-community-name').value='Public link test';document.querySelector('#cc-create-go').click()")
    ok = await wait(b, "__copies.length>0", 30)
    return {'ok': ok, 'copies': await b.js('__copies'),
            'toast': await b.js("(document.querySelector('.toast')||{}).textContent||''"),
            'base': await b.js('window.__PC_API_BASE__'), 'origin': await b.js('location.origin')}


def _check_public(got, expected_prefix):
    assert got['ok'], ('the Create button never copied an invite', got)
    link = got['copies'][-1]
    assert '/invite/naddr1' in link and '#' in link, link
    assert link.startswith(expected_prefix + '/invite/'), (
        f'the minted invite points at {link.split("/invite/")[0]!r}, not {expected_prefix!r} '
        f'(page origin {got["origin"]!r}, instance {got["base"]!r})')
    assert got['origin'] not in link and 'localhost' not in link and 'app://' not in link, link


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_an_invite_minted_in_the_app_points_at_the_instance():
    got = {}

    async def check(b):
        got.update(await create_and_copy(b))
    asyncio.run(desktop.with_browser('online', '', check, NO_OS))
    assert got['base'] == 'https://fixture.invalid', got
    _check_public(got, 'https://fixture.invalid')


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_an_invite_minted_with_no_instance_points_at_the_public_cord_host():
    got = {}

    async def check(b):
        got.update(await create_and_copy(b))
    asyncio.run(desktop.with_browser('online', '', check, NO_OS + NO_INSTANCE))
    assert got['base'] == '', got
    _check_public(got, 'https://armada.buzz')


# A real invite minted with the OLD base, whose 33301 bundle the stub relay serves.
LOCALHOST_INVITE = r'''(async()=>{const ctx=__PC.cordDirectContext();
const made=await PosterCord.createCommunity({owner:ctx.pubkey,name:'Old localhost room',relays:['wss://fixture.invalid'],base:'https://localhost',signEvent:ctx.sign});
window.__served=made.events;window.__oldUrl=made.url;
const bundle=PosterCord.openInvite(made.url,made.events).bundle;
localStorage.setItem('pc.concord.rooms.v1.'+__PC.me().pubkey,JSON.stringify([{communityId:bundle.community_id,naddr:PCConcord.inviteParts(made.url).naddr,
  name:'Old localhost room',url:made.url,channels:[{name:'general',private:false}],local:false,cord:{bundle:{...bundle,control_root:made.secrets.controlRoot}}}]));
return made.url;})()'''


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_an_invite_already_minted_with_localhost_still_opens_and_is_shared_publicly():
    got = {}

    async def check(b):
        await open_concord(b)
        url = await b.js(LOCALHOST_INVITE)
        got['url'] = url
        # Pasting it: the preview must find the community (the host is ignored, the path is read).
        await b.js("PCConcord.openInvite(%s)" % json.dumps(url))
        # The invitation opens on its OWN sheet (never inside the paste-a-link dialog).
        got['preview'] = await wait(b, "!!document.querySelector('#cc-invite-sheet:not(.hidden) #cc-invite-preview') && document.querySelector('#cc-invite-preview').textContent.includes('Old localhost room') && document.querySelector('#cc-join').classList.contains('hidden')", 30)
        got['join'] = await b.js("(document.querySelector('#cc-invite-sheet')||{}).textContent||''")
        # Copying that room's link later hands out a host the recipient can reach.
        await b.js("document.querySelector('#cc-invite-close')&&document.querySelector('#cc-invite-close').click()")
        await b.js("__PC.switchView('concord')")
        await b.until("!!document.querySelector('[data-cc-server]')")
        await b.js("document.querySelector('[data-cc-server]').click()")
        got['copy_btn'] = await wait(b, "!!document.querySelector('#cc-copy-link')", 20)
        await b.js("__copies.length=0;document.querySelector('#cc-copy-link')&&document.querySelector('#cc-copy-link').click()")
        await wait(b, '__copies.length>0', 15)
        got['copies'] = await b.js('__copies')
    asyncio.run(desktop.with_browser('online', '', check, NO_OS))
    assert got['url'].startswith('https://localhost/invite/naddr1'), got['url']
    assert got['preview'], ('a localhost invite no longer previews', got['join'][-600:])
    assert got['copy_btn'], 'the room view never offered its link'
    assert got['copies'] and got['copies'][-1].startswith('https://fixture.invalid/invite/'), got['copies']
    assert got['copies'][-1].split('/invite/')[1] == got['url'].split('/invite/')[1], 'the path or the # secret changed'


# The same class outside Concord: "Copy link" on a post builds `_serverOrigin() + '/' + nevent`, and
# with no instance `_serverOrigin()` is '' -- so it copied a bare `/nevent1…`, relative to whatever page
# the recipient is on. With no instance the only address every client understands is `nostr:`.
POST = r'''window.pcClip={write:async v=>{(window.__clip=window.__clip||[]).push(String(v));return true;}};
window.__events=[];addEventListener('DOMContentLoaded',()=>{const k=new Uint8Array(32).fill(1);
window.__events=[NostrTools.finalizeEvent({kind:1,created_at:Math.floor(Date.now()/1000),content:'share me',tags:[]},k)];});'''


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_copying_a_post_link_with_no_instance_is_never_a_relative_path():
    got = {}

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', dict(width=390, height=844, deviceScaleFactor=2, mobile=True))
        await desktop.login(b)
        await b.js("__PC.switchView('global')")
        got['note'] = await wait(b, "!!document.querySelector('.note [data-a=menu]')", 20)
        await b.js("document.querySelector('.note [data-a=menu]').click()")
        await wait(b, "!!document.querySelector('.menu-pop [data-m=copyid]')", 15)
        await b.js("document.querySelector('.menu-pop [data-m=copyid]').click()")
        await wait(b, '(window.__clip||[]).length>0', 15)
        got['clip'] = await b.js('window.__clip||[]')
    asyncio.run(desktop.with_browser('online', '', check, NO_OS + NO_INSTANCE + POST))
    assert got['note'], 'the post never rendered'
    assert got['clip'], 'nothing was copied'
    assert got['clip'][-1].startswith('nostr:nevent1'), got['clip']
