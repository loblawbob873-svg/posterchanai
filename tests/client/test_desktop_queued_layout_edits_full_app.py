"""A desktop change made before the layout read answers is HELD, shown at once, and applied on top.

Reported: with the layout already in the local cache, the PosterChanOS desktop draws its icons at
once and then REFUSES every change -- hide, drag, make folder, rename, "Add a program…" -- with
"still loading your desktop layout — try that again in a moment" until its own relay read answers.
The decision: such a change is QUEUED as an operation, drawn immediately, and when the read answers
it is REBASED onto whatever the relay returned (a newer layout from another device is never
overwritten -- the queued operation is re-applied on top of it) and saved once. A read that could not
be asked keeps the change queued and publishes nothing over a layout nobody has seen (the
replaceable-doc wipe); it is sent when a relay finally answers.

Runs the SHIPPED bundle. The fixtures are the boundaries only: a relay that remembers what is
published (localStorage-backed) and can HOLD the layout read, answering it late or never.

  * test_a_change_made_while_the_read_is_pending_lands_on_the_newer_remote_layout
      cached layout A (X hidden) on screen; the relay holds a NEWER layout B (X and Y hidden) and does
      not answer yet. Hide Z: Z disappears at once and nothing is published. Release the relay: ONE
      layout is published, and it hides X, Y and Z -- B's change and ours, neither lost.
  * test_a_failed_read_keeps_the_change_queued_and_publishes_nothing
      the relay never answers the layout read: Z is hidden on screen, the desktop says the change is
      kept on this device, and NOTHING is published; once the relay answers, it is sent, once.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


# A relay that REMEMBERS replaceable 30078s by d-tag, answers the desktop-layout REQ with ONLY that
# document, and can hold that one read: localStorage `__layHold` = 'late' (held until
# __releaseLayout()) or 'fail' (never answered while it says so).
RELAY = r'''
window.__publishOK=true;
window.__events=JSON.parse(localStorage.getItem('__relayEvents')||'[]');
window.__layPubs=[];window.__toasts=[];window.__held=[];
const _dOf=(ev)=>(ev.tags.find(t=>t[0]==='d')||[])[1];
window.__keep=(ev)=>{
  const d=_dOf(ev);
  window.__events=window.__events.filter(x=>!(d&&x.kind===ev.kind&&x.pubkey===ev.pubkey&&_dOf(x)===d));
  window.__events.push(ev);
  localStorage.setItem('__relayEvents',JSON.stringify(window.__events));
};
const _fixtureSend=WebSocket.prototype.send;
const _answerLayout=(sock,m)=>{
  for(const ev of window.__events){
    if(ev.kind===30078&&_dOf(ev)==='pcai:desktop'&&m.slice(2).some(f=>!f.authors||f.authors.includes(ev.pubkey)))
      sock.fire('message',['EVENT',m[1],ev]);
  }
  sock.fire('message',['EOSE',m[1]]);
};
window.__releaseLayout=()=>{localStorage.setItem('__layHold','');
  const h=window.__held.splice(0);for(const [s,m] of h)setTimeout(()=>_answerLayout(s,m),5);return h.length;};
WebSocket.prototype.send=function(raw){
  let m=null;try{ m=JSON.parse(raw); }catch(_){}
  if(Array.isArray(m)&&m[0]==='EVENT'&&m[1].kind===30078){
    __keep(m[1]);
    if(_dOf(m[1])==='pcai:desktop')__layPubs.push(m[1]);
  }
  if(Array.isArray(m)&&m[0]==='REQ'&&m.slice(2).some(f=>f&&Array.isArray(f['#d'])&&f['#d'].includes('pcai:desktop'))){
    const hold=localStorage.getItem('__layHold')||'';
    if(hold){window.__held.push([this,m]);return;}
    setTimeout(()=>_answerLayout(this,m),15);return;
  }
  return _fixtureSend.call(this,raw);
};
'''

SK = 'new Uint8Array(32).fill(1)'


async def wait(b, expression, seconds=40):
    """Poll for at least ten seconds (the gate runs browsers concurrently on a loaded node)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(10, seconds)
    while loop.time() < deadline:
        if await b.js(expression):
            return
        await asyncio.sleep(.2)
    raise AssertionError({'waiting': expression, 'state': await b.js(
        "({errors:__errors,toasts:__toasts,pubs:__layPubs.length,held:__held.length,"
        "icons:[...document.querySelectorAll('#os-root .os-icons .os-icon')].map(x=>x.dataset.view)})")})


async def vwait(b, expression, ms):
    """`wait`, on the PAGE's clock: virtual time is advanced in small steps until `expression` holds.
    Returns the virtual ms it took (at most `ms`)."""
    spent = 0
    while spent < ms:
        if await b.js(expression):
            return spent
        got = await desktop.advance(b, 100)
        assert got >= 99, ('the page clock did not advance', got)
        spent += got
    raise AssertionError({'waiting (virtual ms %d)' % ms: expression, 'state': await b.js(
        "({errors:__errors,toasts:__toasts,pubs:__layPubs.length,held:__held.length})")})


async def vrun(b, ms, step=100):
    """Run `ms` of the PAGE's clock in `step`-ms slices. One long budget would race through timers faster
    than work the page hands to its signer worker can come back (measured: a second save queued 1.5 s
    after the first never reached the relay inside a single 2 s budget); slices let both keep pace."""
    spent = 0
    while spent < ms:
        spent += await desktop.advance(b, min(step, ms - spent))
    return spent


IN_IDB = """(id=>new Promise(res=>{const r=indexedDB.open('posterchan-nostr');r.onerror=()=>res(false);
  r.onsuccess=()=>{try{const g=r.result.transaction('events').objectStore('events').get(id);
    g.onsuccess=()=>{res(!!g.result);r.result.close();};g.onerror=()=>{res(false);r.result.close();};}
    catch(_){res(false);r.result.close();}};}))"""


async def put_layout(b, doc):
    """Sign `doc` as this account's encrypted desktop layout, NEWER than any the relay holds, onto
    the relay only -- what another device's save looks like from here."""
    await b.js("""(async()=>{const pk=__PC.me().pubkey;
        const ct=await __PC.nip44enc(pk, %s);
        const ev=NostrTools.finalizeEvent({kind:30078,created_at:Math.max(0,...__events.filter(e=>e.kind===30078&&_dOf(e)==='pcai:desktop').map(e=>e.created_at))+2,
            tags:[['d','pcai:desktop']],content:ct}, %s);
        __keep(ev);return ev.id})()""" % (json.dumps(json.dumps(doc)), SK))


async def decoded(b, ev_expr):
    """The layout an event carries, decrypted with the account's key right here -- synchronously, since
    the page's own nip44dec waits on the page clock, which these tests leave paused."""
    return await b.js("(()=>{const N=NostrTools.nip44;"
                      "return JSON.parse(N.decrypt((%s).content,N.getConversationKey(%s,__PC.me().pubkey)));})()"
                      % (ev_expr, SK))


ICONS = "[...document.querySelectorAll('#os-root .os-icons .os-icon')].map(x=>x.dataset.view)"
DESK_UP = ("!!document.body && document.body.classList.contains('os-on') && "
           "!!document.querySelector('#os-root .os-icons')")


def has(view):
    return ("!!document.querySelector('#os-root .os-icons .os-icon[data-view=" + '"' + view + '"'
            + "]')")


async def pick_three(b):
    views = await b.js("[...document.querySelectorAll('#os-root .os-icons .os-icon')]"
                       ".filter(x=>!x.classList.contains('is-folder')&&!x.classList.contains('is-native'))"
                       ".map(x=>x.dataset.view)")
    assert len(views) >= 3, views
    return views[0], views[1], views[2]


async def hide_by_hand(b, view):
    """Right-click the icon → Hide from desktop, the way a person does it."""
    sel = "document.querySelector('#os-root .os-icons .os-icon[data-view=\"" + view + "\"]')"
    await b.js(sel + ".dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,clientX:60,clientY:60}))")
    await wait(b, "[...document.querySelectorAll('.os-ctx button')].some(x=>x.textContent.trim()==='Hide from desktop')")
    await b.js("[...document.querySelectorAll('.os-ctx button')].find(x=>x.textContent.trim()==='Hide from desktop').click()")


async def boot_with_cached_layout(b, hold):
    """Sign in and hide X by hand while the relay answers -- THIS device publishes layout A, which is
    what puts it in the client's cache -- then reload with the layout read held. Returns (X, Y, Z)."""
    await desktop.login(b)
    await wait(b, DESK_UP)
    x, y, z = await pick_three(b)
    for _ in range(60):                     # the first tries can land before the opening read answers
        if await b.js('__layPubs.length>0'):
            break
        if await b.js(has(x)):
            await hide_by_hand(b, x)
        await asyncio.sleep(.4)
    await wait(b, '__layPubs.length>0 && !' + has(x))
    # The Store batches its IndexedDB writes: wait until the published layout is on disk.
    await wait(b, '(%s)(__layPubs[__layPubs.length-1].id)' % IN_IDB)
    await b.js("localStorage.setItem('__layHold'," + json.dumps(hold) + ")")
    await b.call('Page.reload')
    await wait(b, DESK_UP)
    await wait(b, '!' + has(x) + ' && ' + has(y) + ' && ' + has(z))   # the CACHED layout is on screen
    await wait(b, '__held.length>0')        # the layout read went out, and is being held
    assert await b.js('__layPubs.length') == 0
    await b.js("(()=>{const t=__PC.toast;__PC.toast=function(m){__toasts.push(String(m));"
               "return t&&t.apply(this,arguments)}})()")
    return x, y, z


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_change_made_while_the_read_is_pending_lands_on_the_newer_remote_layout():
    async def check(b):
        x, y, z = await boot_with_cached_layout(b, 'late')
        # Meanwhile another device hid Y: a NEWER layout B is on the relay, not in this cache.
        await put_layout(b, {'v': 1, 'folders': [], 'order': [], 'hidden': [x, y], 'pos': {}})

        await hide_by_hand(b, z)
        # Shown at once, refused nowhere, written nowhere yet.
        await wait(b, '!' + has(z), 10)
        toasts = await b.js('__toasts')
        assert not any('try that again' in t for t in toasts), toasts
        assert await b.js('__layPubs.length') == 0, 'published before the layout was read'
        # From here on the PAGE's clock runs (Chrome virtual time): the quiet second and the "a second
        # write would have gone out by now" window cost the gate nothing. It stays paused between
        # advances, so every later wait is a vwait.
        quiet = await vrun(b, 1000)
        assert quiet >= 999, ('the page clock did not run the quiet second', quiet)
        assert await b.js('__layPubs.length') == 0, 'published before the layout was read'

        assert await b.js('__releaseLayout()') > 0
        await vwait(b, '__layPubs.length>0', 30000)
        after = await vrun(b, 2000)                   # a second write would have gone out by now
        assert after >= 1999, ('the page clock did not run the window', after)
        assert await b.js('__layPubs.length') == 1, await b.js('__layPubs.length')
        doc = await decoded(b, '__layPubs[0]')
        assert {x, y, z} <= set(doc.get('hidden', [])), doc
        # And the screen agrees with what was stored: B's change AND ours.
        await vwait(b, '!' + has(x) + ' && !' + has(y) + ' && !' + has(z), 10000)
        assert not await b.js('__errors'), await b.js('__errors')

    asyncio.run(desktop.with_browser('online', '', check, RELAY))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_failed_read_keeps_the_change_queued_and_publishes_nothing():
    async def check(b):
        x, y, z = await boot_with_cached_layout(b, 'fail')
        await hide_by_hand(b, z)
        await wait(b, '!' + has(z), 10)
        # The read gives up (no relay EOSEs): the change stays, said out loud, published nowhere. All of
        # it on the PAGE's clock (Chrome virtual time) -- the read's own timeouts are what took the gate
        # ~20 s; the page clock stays paused between advances, so every later wait is a vwait.
        await vwait(b, "__toasts.some(t=>t.includes('will sync when your relays answer'))", 60000)
        assert await b.js('__layPubs.length') == 0, 'published over a layout that was never read'
        assert not await b.js(has(z)), 'the queued change was dropped from the screen'

        # The relays come back: the queued change is sent, once, on top of what they hold.
        await b.js('__releaseLayout()')
        await vwait(b, '__layPubs.length>0', 90000)
        after = await vrun(b, 2000)                   # a second write would have gone out by now
        assert after >= 1999, ('the page clock did not run the window', after)
        assert await b.js('__layPubs.length') == 1, await b.js('__layPubs.length')
        doc = await decoded(b, '__layPubs[0]')
        assert {x, z} <= set(doc.get('hidden', [])), doc
        assert y not in doc.get('hidden', []), doc
        assert not await b.js('__errors'), await b.js('__errors')

    asyncio.run(desktop.with_browser('online', '', check, RELAY))
