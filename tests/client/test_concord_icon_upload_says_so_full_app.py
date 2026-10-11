"""UPLOADING A COMMUNITY ICON SAYS WHAT IT IS DOING, AND SAVE NEVER RUNS AHEAD OF IT.

Reported from the APK: "Upload community image doesn't let you know the upload went through -- just
hit Save changes and hope it uploads." The picker put a `blob:` URL in the Icon box and nothing else;
the encrypt + Blossom upload ran inside Save, with no sign that it had started, finished or failed.

Driven in the shipped bundle at phone size (390x844, mobile emulation). The community is a real one
minted by PosterCord; only the Blossom upload (held open, then released or failed), relay I/O and the
file chooser are fixtures. What Save published is read back the way a MEMBER reads it (the control
stream folded with every wrap written), so the assertion is on the icon other people will see.

1. Pick an image -> the line under "Upload image…" says it is uploading, with the picture, and Save
   is held. Release the upload -> "uploaded", Save is free, and Save publishes the sealed icon whose
   url is the one the upload returned, without uploading the bytes a second time.
2. A failed upload says so with a Retry, Save stays held (no broken icon can be published), and a
   Retry that succeeds frees it.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


NO_OS = ("localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse("
         "localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));")

ROOM = r'''(async()=>{const p=__PC,ctx=p.cordDirectContext();
window.__pub=[];window.__uploads=[];window.__copies=[];
const made=await PosterCord.createCommunity({owner:ctx.pubkey,name:'Icon room',relays:['wss://fixture.invalid'],base:'https://fixture.invalid',signEvent:ctx.sign});
window.__served=made.events.filter(e=>e.kind===1059);
p.copyValue=v=>{__copies.push(v);return true;};
p.relayPublishRoom=async(relays,ev)=>{__pub.push(ev);return {ok:true,relays};};
p.relayPublishTo=async(relays,ev)=>{__pub.push(ev);return {ok:true,relays};};
p.relayQueryFrom=async(relays,filters,options)=>{if(options&&options.report)options.report.ok=[...relays];
  return __served.filter(ev=>filters.some(f=>(!f.kinds||f.kinds.includes(ev.kind))&&(!f.authors||f.authors.includes(ev.pubkey))));};
p.relayQuery=async()=>[];
/* The Blossom upload: held open until the test releases or fails it. */
p.uploadBlob=async blob=>{const n=__uploads.length;__uploads.push({size:blob.size,state:'open'});
  await new Promise((ok,no)=>{window.__release=()=>{__uploads[n].state='released';ok();};window.__fail=()=>{__uploads[n].state='failed';no(new Error('blossom refused the upload'));};});
  return 'https://fixture.invalid/blossom/'+'c'.repeat(64);};
const bundle=PosterCord.openInvite(made.url,made.events).bundle;
/* What Save published, as a MEMBER reads it: the control stream folded with every wrap written. */
window.__published=()=>{const wraps=__pub.filter(e=>e.kind===1059);if(!wraps.length)return [];
  const info=PosterCordReader.inspectControl({...bundle,control_root:made.secrets.controlRoot},[...__served,...wraps]);return [{icon:info.icon||null,wraps:wraps.length}];};
localStorage.setItem('pc.concord.rooms.v1.'+p.me().pubkey,JSON.stringify([{communityId:bundle.community_id,naddr:PCConcord.inviteParts(made.url).naddr,
  name:'Icon room',url:made.url,channels:[{name:'general',private:false,id:made.generalChannelId}],local:false,cord:{bundle:{...bundle,control_root:made.secrets.controlRoot}}}]));
/* The harness answers every non-static fetch with fixture JSON; a blob: read is local, so it gets the bytes. */
const fx=window.fetch;window.fetch=async(u,o)=>{if(!String(u).startsWith('blob:'))return fx(u,o);
  const data=await new Promise((ok,no)=>{const x=new XMLHttpRequest();x.open('GET',String(u));x.responseType='blob';x.onload=()=>ok(x.response);x.onerror=no;x.send();});
  return new Response(data,{status:200});};
/* The file chooser: capture the input the picker makes, so a real File can be handed to it. */
const click=HTMLInputElement.prototype.click;HTMLInputElement.prototype.click=function(){if(this.type==='file'){window.__picker=this;return;}return click.call(this);};
return true;})()'''

PICK = r'''(async()=>{const c=document.createElement('canvas');c.width=64;c.height=64;const g=c.getContext('2d');g.fillStyle='#e11';g.fillRect(0,0,64,64);
const blob=await new Promise(r=>c.toBlob(r,'image/png'));const file=new File([blob],'icon.png',{type:'image/png'});
window.__picker=null;document.querySelector('[data-cc-icon-pick="cc-settings-icon"]').click();
const dt=new DataTransfer();dt.items.add(file);__picker.files=dt.files;__picker.onchange();return true;})()'''

STATE = r'''(()=>{const line=document.querySelector('#cc-settings-icon-status'),save=document.querySelector('#cc-settings-save');
return {text:line&&!line.hidden?line.textContent:'',img:!!(line&&!line.hidden&&line.querySelector('img')),
  retry:!!(line&&line.querySelector('.cc-icon-retry')),saveDisabled:!!(save&&save.disabled),
  box:(document.querySelector('#cc-settings-icon')||{}).value||'',uploads:__uploads.map(u=>u.state),meta:__published(),
  dialogOpen:!!document.querySelector('#cc-settings-dialog:not(.hidden)'),toast:[...document.querySelectorAll('.toast')].map(t=>t.textContent).join(' | ')};})()'''


async def wait(b, expression, seconds=20):
    for _ in range(int(seconds * 10)):
        if await b.js(expression):
            return True
        await asyncio.sleep(.1)
    return False


async def open_settings(b):
    await b.call('Emulation.setDeviceMetricsOverride', dict(width=390, height=844, deviceScaleFactor=2, mobile=True))
    await desktop.login(b)
    await b.js("__PC.switchView('concord')")
    await b.until("!!window.PCConcord && !!window.PosterCord && document.body.classList.contains('concord-view')")
    await b.js(ROOM)
    await b.js("__PC.switchView('concord')")
    await b.until("!!document.querySelector('[data-cc-server]')")
    await b.js("document.querySelector('[data-cc-server]').click()")
    await b.until("!!document.querySelector('#cc-edit-icon')")
    await b.js("document.querySelector('#cc-edit-icon').click()")
    await b.until("!!document.querySelector('#cc-settings-dialog:not(.hidden)')")


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_picked_icon_shows_its_upload_and_save_waits_for_it():
    got = {}

    async def check(b):
        await open_settings(b)
        await b.js(PICK)
        got['started'] = await wait(b, "__uploads.length===1", 15)
        got['uploading'] = await b.js(STATE)
        # Save while the upload is still open: it must not publish anything yet.
        await b.js("document.querySelector('#cc-settings-save').click()")
        await asyncio.sleep(.5)
        got['after_early_save'] = await b.js(STATE)
        await b.js("__release()")
        got['done_seen'] = await wait(b, "(document.querySelector('#cc-settings-icon-status')||{}).textContent.includes('uploaded')", 15)
        got['done'] = await b.js(STATE)
        await b.js("document.querySelector('#cc-settings-save').click()")
        got['saved'] = await wait(b, "__published().length>0", 15)
        got['final'] = await b.js(STATE)
    asyncio.run(desktop.with_browser('online', '', check, NO_OS))

    assert got['started'], 'picking the image did not start an upload (it waited for Save)'
    up = got['uploading']
    assert up['box'].startswith('blob:'), up
    assert 'Uploading' in up['text'] and up['img'], ('no visible upload state after the pick', up)
    assert up['saveDisabled'], ('Save is offered while the icon is still uploading', up)
    assert got['after_early_save']['meta'] == [], ('Save published before the icon upload finished', got['after_early_save'])
    assert got['done_seen'] and got['done']['img'], ('the finished upload was never shown', got['done'])
    assert not got['done']['saveDisabled'], ('Save stayed blocked after the upload finished', got['done'])
    assert got['saved'], 'Save did not publish the community profile: ' + repr(got['final'])
    ref = got['final']['meta'][-1].get('icon') or {}
    assert ref.get('url') == 'https://fixture.invalid/blossom/' + 'c' * 64, ('the saved icon is not the uploaded one', got['final']['meta'])
    assert got['final']['uploads'] == ['released'], ('the icon bytes were uploaded more than once', got['final']['uploads'])


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_failed_icon_upload_says_so_and_never_saves_a_broken_icon():
    got = {}

    async def check(b):
        await open_settings(b)
        await b.js(PICK)
        got['started'] = await wait(b, "__uploads.length===1", 15)
        if not got['started']:
            return
        await b.js("__fail()")
        got['failed_seen'] = await wait(b, "!!document.querySelector('#cc-settings-icon-status .cc-icon-retry')", 15)
        got['failed'] = await b.js(STATE)
        await b.js("document.querySelector('#cc-settings-save').click()")
        await asyncio.sleep(.5)
        got['after_save'] = await b.js(STATE)
        await b.js("document.querySelector('#cc-settings-icon-status .cc-icon-retry').click()")
        await wait(b, "__uploads.length===2", 15)
        await b.js("__release()")
        got['retried'] = await wait(b, "(document.querySelector('#cc-settings-icon-status')||{}).textContent.includes('uploaded')", 15)
        got['final'] = await b.js(STATE)
    asyncio.run(desktop.with_browser('online', '', check, NO_OS))

    assert got['started'], 'picking the image did not start an upload (it waited for Save)'
    assert got['failed_seen'], ('a failed upload was not reported with a Retry', got['failed'])
    assert 'fail' in got['failed']['text'].lower(), got['failed']
    assert got['failed']['saveDisabled'], ('Save is offered over an icon that did not upload', got['failed'])
    assert got['after_save']['meta'] == [], ('a community profile was published with a failed icon', got['after_save'])
    assert got['retried'] and not got['final']['saveDisabled'], ('Retry did not recover the upload', got['final'])
