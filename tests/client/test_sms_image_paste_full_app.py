"""Real Texts renderer: clipboard Files enter its existing draft/send ownership pipeline."""
import asyncio
from pathlib import Path
import pytest
from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


SETUP = r"""
window.pasteImage = (name='pasted.png', count=1, empty=false) => {
  const data=new DataTransfer();
  const bytes=Uint8Array.from(atob('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9ZlZkAAAAASUVORK5CYII='),c=>c.charCodeAt(0));
  for(let i=0;i<count;i++) data.items.add(new File([empty?'':bytes],name+i,{type:'image/png'}));
  const event=new ClipboardEvent('paste',{clipboardData:data,bubbles:true,cancelable:true});
  document.querySelector('#sms-in').dispatchEvent(event);
  return event.defaultPrevented;
};
window.draft=()=>PCSms._state().draft[PCSms._state().open];
window.toasts=[];__PC.toast=x=>toasts.push(x);
window.sends=[];window.uploads=[];
__PC.capPlugin=()=>null;
__PC.uploadEncFile=async file=>{uploads.push(file.name);return 'a'.repeat(64);};
__PC.publish=async (...args)=>{sends.push(args);return await new Promise(resolve=>window.finishSend=resolve);};
"""


async def open_texts(b):
    await desktop.login(b)
    await b.js("PCOpenNotificationRoute('texts:%2B15550100')")
    await b.until("!!document.querySelector('#sms-in') && !!window.PCSms")
    await b.js(SETUP)


@pytest.mark.parametrize('native_window', [False, True])
def test_pasted_image_previews_and_sends_once_through_existing_pipeline(native_window):
    async def check(b):
        await open_texts(b)
        await b.js("document.querySelector('#sms-in').value='caption';document.querySelector('#sms-in').dispatchEvent(new Event('input'))")
        assert await b.js("pasteImage()")
        await b.until("!!document.querySelector('.sms-draft-preview') && document.querySelector('.sms-draft-preview').naturalWidth>0")
        assert await b.js("({caption:document.querySelector('#sms-in').value,name:draft().file.name,sends:sends.length,uploads:uploads.length})") == dict(caption='caption',name='pasted.png0',sends=0,uploads=0)
        await b.js("window.__pastePreview=draft().previewUrl;document.querySelector('#sms-attach-clear').click()")
        assert await b.js("!!document.querySelector('.sms-draft-preview')") is False
        assert await b.js("new Promise(resolve=>{const image=new Image();image.onload=()=>resolve(false);image.onerror=()=>resolve(true);image.src=window.__pastePreview;})") is True
        await b.js("pasteImage();document.querySelector('#sms-send').click()")
        await b.until("sends.length===1")
        await b.js("PCSms.refreshNames();document.querySelector('#sms-send').click();document.querySelector('#sms-in').dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}));pasteImage('during-send.png')")
        assert await b.js("({sends:sends.length,uploads:uploads.length,name:draft().file.name})") == dict(sends=1,uploads=1,name='pasted.png0')
        await b.js("finishSend({ok:false})")
        await b.until("!PCSms._state().sending.size")
        assert await b.js("draft().file.name") == 'pasted.png0'
        assert await b.js("document.querySelector('#sms-in').value") == 'caption'
        # Failure preserves the draft; only a new explicit click retries.
        await b.js("document.querySelector('#sms-send').click()")
        await b.until("sends.length===2")
        await b.js("finishSend({ok:true})")
        await b.until("!document.querySelector('.sms-attachment-draft') && !PCSms._state().sending.size")
        assert await b.js("document.querySelector('#sms-in').value") == ''
    extra="localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"
    if native_window:
        extra += "window.pcShell.windowContext={role:'app',view:'texts'};window.pcShell.backgroundOwner=false;"
    asyncio.run(desktop.with_browser('online','?pcwin=texts' if native_window else '',check,extra))


def test_paste_replacement_and_recipient_account_boundaries():
    async def check(b):
        await open_texts(b)
        await b.js("pasteImage('original.png');__PC.uiConfirm=async()=>false;pasteImage('cancelled.png')")
        await b.until("draft().file.name==='original.png0'")
        assert await b.js("pasteImage('many.png',2)")
        assert await b.js("pasteImage('empty.png',1,true)")
        assert await b.js("draft().file.name") == 'original.png0'
        assert await b.js("toasts.some(x=>x.includes('one image')) && toasts.some(x=>x.includes('Could not read'))")
        await b.js("__PC.uiConfirm=async()=>true;pasteImage('accepted.png')")
        await b.until("draft().file.name==='accepted.png0'")
        # An async replacement confirmation belongs to its originating conversation.
        await b.js("__PC.uiConfirm=()=>new Promise(r=>window.answerPaste=r);pasteImage('late.png')")
        await b.until("typeof answerPaste==='function'")
        await b.js("PCOpenNotificationRoute('texts:%2B15550200')")
        await b.until("PCSms._state().open===PCSms._key('+15550200')")
        await b.js("answerPaste(true)")
        assert await b.js("!!document.querySelector('.sms-attachment-draft')") is False
        await b.js("PCOpenNotificationRoute('texts:%2B15550100')")
        await b.until("!!document.querySelector('.sms-attachment-draft')")
        assert await b.js("draft().file.name") == 'accepted.png0'
        # Ordinary text paste is not cancelled or treated as an attachment.
        assert await b.js("(()=>{const d=new DataTransfer();d.setData('text/plain','plain text');const e=new ClipboardEvent('paste',{clipboardData:d,bubbles:true,cancelable:true});document.querySelector('#sms-in').dispatchEvent(e);return e.defaultPrevented;})()") is False
        await b.js("window.__accountPreview=draft().previewUrl;pasteImage('old-account.png');window.oldOwner=__PC.ME.pubkey;__PC.ME.pubkey='b'.repeat(64);answerPaste(true)")
        assert await b.js("Object.values(PCSms._state().draft).every(d=>!d.file||d.file.name!=='old-account.png0')")
        assert await b.js("sends.length") == 0
        await b.js("PCSms.render()")
        assert await b.js("Object.keys(PCSms._state().draft).length") == 0
        assert await b.js("new Promise(resolve=>{const image=new Image();image.onload=()=>resolve(false);image.onerror=()=>resolve(true);image.src=window.__accountPreview;})") is True
    extra="localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"
    asyncio.run(desktop.with_browser('online','',check,extra))
