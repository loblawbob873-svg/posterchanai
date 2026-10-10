"""OFFICE ON THIS MACHINE IS NOT A SERVER APP: NO MEMBERSHIP CHECK STANDS IN FRONT OF IT.

Reported as "office broken on desktop: instance membership settings not loaded" and "office should
always just work on PosterChanOS". The desktop ships its own Collabora (desktop/office-local.js,
reached through the `pcOfficeLocal` bridge) and opens the person's own files on their own machine —
yet the Office SCREEN was still behind the instance's membership gate (PCInstanceAccess), so when the
server was restarting, unreachable, its settings not yet hydrated, or simply said no, the person got
"Could not verify instance membership" instead of the Office home, and a document on this computer
that fell through to the server surfaced the server's raw 503 detail ("Instance membership settings
are not loaded yet").

The real client, the desktop bundle, the preload bridges stubbed the way preload injects them; the
instance answers the membership question with each of its failure shapes. A local document must open
in the local editor every time, and the instance must never be asked to open it.
"""
import asyncio
from pathlib import Path

import pytest
from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


NOT_LOADED = 'Instance membership settings are not loaded yet'

BOUNDARIES = r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
window.__officeAsked=0;window.__accessAsked=0;window.__opened=[];window.__writes=[];window.__toasts=[];
window.pcHost={
 roots:async()=>[{path:'/home/test',name:'Home',kind:'home'}],
 list:async(path)=>({path,parent:'/home',entries:[{name:'report.odt',path:'/home/test/report.odt',dir:false,size:4,mtime:100}]}),
 read:async()=>new TextEncoder().encode('test'),
 pickFile:async()=>({name:'report.odt',path:'/home/test/report.odt',type:'application/vnd.oasis.opendocument.text',mtime:100,data:new TextEncoder().encode('picked')}),
 writeBytes:async(path,bytes)=>{__writes.push([path,new TextDecoder().decode(bytes)]);return {ok:true,mtime:200};},
 open:async()=>{throw Error('must open Office inside the app');},
};
const HAVE_LOCAL=LOCAL;
window.pcOfficeLocal={installed:()=>HAVE_LOCAL,available:async()=>HAVE_LOCAL,
 open:async(bytes,name,mode)=>{__opened.push([new TextDecoder().decode(new Uint8Array(bytes)),name,mode]);
   return {ok:true,id:'local1',token:'tok',expires:9999999999,editor_url:location.origin+'/__local-editor'};},
 contents:async()=>({ok:true,bytes:new TextEncoder().encode('edited')}),
 export:async()=>({ok:false,error:'not in this test'}),
 close:async()=>({ok:true,closed:true})};
const json=(body,status)=>new Response(JSON.stringify(body),{status,headers:{'Content-Type':'application/json'}});
const innerFetch=window.fetch;
window.fetch=async function(url,opts={}){
 const u=String(url);
 if(u.includes('/api/instance-welcome/access')){__accessAsked++;
   if('ANSWER'==='refused')return json({qualified:false,reason:'unregistered',pubkey:window.__PC?.me()?.pubkey,domain:'fixture.invalid'},200);
   if('ANSWER'==='unreachable')throw new TypeError('Failed to fetch');
   return json({detail:'NOT_LOADED'},503);}
 if(u.includes('/client/office/')){__officeAsked++;return json({detail:'NOT_LOADED'},503);}
 return innerFetch(url,opts);
};
document.addEventListener('DOMContentLoaded',()=>{const r=document.getElementById('toast-root');
 if(r)new MutationObserver(()=>r.querySelectorAll('.toast').forEach(t=>{if(!__toasts.includes(t.textContent))__toasts.push(t.textContent)})).observe(r,{childList:true});});
'''

ANSWERS = ['unavailable', 'refused', 'unreachable']


def boundaries(answer, local=True):
    return (BOUNDARIES.replace('ANSWER', answer).replace('NOT_LOADED', NOT_LOADED)
            .replace('LOCAL', 'true' if local else 'false'))


chrome_required = pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')


@chrome_required
@pytest.mark.parametrize('answer', ANSWERS)
def test_the_office_screen_opens_a_local_document_whatever_the_instance_says(answer):
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.js("__PC.switchView('office')")
        # The Office home, not a membership panel: the person is opening their own files.
        await b.until("!!document.querySelector('#office-open-local') || !!document.querySelector('.instance-app-gate .ia-retry')")
        got['gate'] = await b.js("(document.querySelector('.instance-app-gate')||{}).textContent||''")
        if got['gate']:
            return
        await b.js("document.querySelector('#office-open-local').click()")
        await b.until("!!document.querySelector('.office-view #office-save')")
        # A late membership answer must not paint over the editor.
        await asyncio.sleep(1.5)
        got.update(await b.js("({editor:!!document.querySelector('.office-view #office-save'),"
                              "gate:(document.querySelector('.instance-app-gate')||{}).textContent||'',"
                              "action:document.querySelector('.office-launch').getAttribute('action'),"
                              "opened:__opened,asked:__officeAsked,text:document.body.innerText,errors:__errors})"))

    asyncio.run(desktop.with_browser('offline', '', check, boundaries(answer)))
    assert not got['gate'], f'the Office screen was refused by the instance ({answer}): {got["gate"]!r}'
    assert got['editor'] and not got['gate'], got
    assert got['opened'] == [['picked', 'report.odt', 'edit']], got
    assert got['action'].endswith('/__local-editor'), got
    assert got['asked'] == 0, 'the instance was asked to open a document on this computer'
    assert NOT_LOADED not in got['text'], got['text'][-400:]


@chrome_required
def test_with_no_local_editor_the_server_refusal_is_not_a_settings_message():
    """No Office on this machine: the instance is the only editor, and it is down. Say THAT —
    never the server's internal "settings are not loaded yet"."""
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.js("__PC.switchView('blossom')")
        await b.until("!!document.querySelector('[data-host=\"1\"]')")
        await b.js("document.querySelector('[data-host=\"1\"]').click()")
        await b.until("!!document.querySelector('[data-p=\"/home/test/report.odt\"]')")
        await b.js("document.querySelector('[data-p=\"/home/test/report.odt\"]').click()")
        await b.until("!!document.querySelector('[data-ow=office]')")
        await b.js("document.querySelector('[data-ow=office]').click()")
        await b.until("__toasts.some(t=>/office/i.test(t))")
        got.update(await b.js("({toasts:__toasts,asked:__officeAsked})"))

    asyncio.run(desktop.with_browser('offline', '', check, boundaries('unavailable', local=False)))
    said = ' | '.join(got['toasts'])
    assert got['asked'] == 1, got
    assert 'not loaded' not in said.lower(), said
    assert 'not installed on this computer' in said, said


@chrome_required
@pytest.mark.parametrize('local', [True, False])
def test_with_no_instance_at_all_office_is_there_exactly_when_this_computer_has_it(local):
    """The terminal's rule, for documents: a machine with its own editor needs no server to open
    its own files. Without a local suite and without an instance there is nothing behind the screen,
    and the row stays hidden exactly as before."""
    got = {}
    no_instance = "window.pcShell.instanceSync='';window.pcShell.getInstance=async()=>'';"

    async def check(b):
        await desktop.login(b)
        await asyncio.sleep(.5)
        got['row_hidden'] = await b.js("(()=>{const r=document.querySelector('.nav-item[data-view=\"office\"]');return !r||r.classList.contains('hidden')})()")
        await b.js("__PC.switchView('office')")
        await asyncio.sleep(.5)
        got['home'] = await b.js("!!document.querySelector('#office-open-local')")
        if got['home']:
            await b.js("document.querySelector('#office-open-local').click()")
            await b.until("!!document.querySelector('.office-view #office-save')")
        got.update(await b.js("({opened:__opened,asked:__officeAsked,access:__accessAsked})"))

    asyncio.run(desktop.with_browser('offline', '', check, boundaries('unavailable', local=local) + no_instance))
    if local:
        assert not got['row_hidden'], 'Office is missing from a standalone desktop that has it installed'
        assert got['home'] and got['opened'] == [['picked', 'report.odt', 'edit']], got
    else:
        assert got['row_hidden'] and not got['home'], got
    assert got['asked'] == 0 and got['access'] == 0, got


@chrome_required
def test_a_late_preload_answer_still_lets_office_in():
    """preload fills `installed()` from an IPC reply that can land after the screen was first drawn;
    the gate must not leave a membership panel up when the machine's answer is yes."""
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.js("__PC.switchView('office')")
        await b.until("!!document.querySelector('#office-open-local')")
        got.update(await b.js("({access:__accessAsked,text:document.querySelector('#feed').innerText})"))

    late = "window.pcOfficeLocal.installed=()=>false;"
    asyncio.run(desktop.with_browser('offline', '', check, boundaries('unavailable') + late))
    assert NOT_LOADED not in got['text'] and 'membership' not in got['text'], got
