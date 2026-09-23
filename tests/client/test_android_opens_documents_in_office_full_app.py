"""A document opened from Android lands in PosterChan Office, fits the phone, and Save goes back to it.

The native half (the manifest entry, DocIntent, OpenDocPlugin) is tests/test_android_open_documents.py.
This drives the SHIPPED bundle at a phone's size with the page's two boundaries as fixtures: the
`OpenDoc` plugin (what Android handed over) and the instance's /client/office endpoints. Checked:

  * a .docx arrival opens the Office editor, NOT a composer: the upload carries its bytes and name;
  * "make sure the office UI looks good on mobile and is sized right": the editor frame takes most
    of the screen, its Save row is on screen with thumb-sized buttons, nothing runs off the side;
  * Save writes the edited bytes BACK through OpenDoc.save with the arrival's nonce, and on a
    read-only source saves a copy and says so instead of failing quietly;
  * a resume that re-reads the same arrival does not open it twice; a PDF arrival opens Preview.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


FIXTURE = r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
window.__doc={nonce:1,kind:'office',name:'Quarterly Report.docx',
  mime:'application/vnd.openxmlformats-officedocument.wordprocessingml.document',writable:true,data:btoa('ORIGINAL-DOCX')};
window.__docSaves=[];window.__officeUploads=[];window.__pendingCalls=0;
// Installed AFTER sign-in: present at boot, a Capacitor object sends the page down
// the native sign-in path this harness cannot drive.
window.__installOpenDoc=()=>{ window.Capacitor={Plugins:{OpenDoc:{
  pending:async()=>{__pendingCalls++;return JSON.parse(JSON.stringify(__doc));},
  save:async(o)=>{ if(!__doc.writable) throw new Error('read-only'); __docSaves.push(o); return {ok:true}; }}}}; };
(()=>{const inner=window.fetch;
 const ok=(j,h)=>new Response(typeof j==='string'?j:JSON.stringify(j),{status:200,headers:h||{'Content-Type':'application/json'}});
 window.fetch=async function(url,opts={}){
  const u=String(url);
  if(u.includes('/client/office/session/') && u.includes('/contents')) return ok('EDITED-DOCX',{'Content-Type':'application/octet-stream'});
  if(u.includes('/client/office/session/')) return ok({});
  if(u.includes('/client/office/session')){
    const f=opts.body&&opts.body.get&&opts.body.get('file');
    __officeUploads.push({name:f&&f.name, text:f?await f.text():''});
    return ok({id:'s'+__officeUploads.length,token:'t',expires:600,editor_url:location.origin+'/static/js/client/sprite.js'});
  }
  return inner(url,opts);
 };})();
'''


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_document_from_android_opens_in_office_fits_the_phone_and_saves_back():
    got = {}

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', {'width': 390, 'height': 844, 'deviceScaleFactor': 3, 'mobile': True})
        await desktop.login(b)
        await b.js("__installOpenDoc();__PC.consumeOpenDoc();true")
        await b.until("__officeUploads.length===1 && !!document.querySelector('.office-frame')")
        await asyncio.sleep(.6)
        got['upload'] = await b.js("__officeUploads[0]")
        got['composer'] = await b.js("!!document.querySelector('.modal .compose, #compose-text')")
        got['fit'] = await b.js(r'''(()=>{const vw=document.documentElement.clientWidth, vh=innerHeight;
          const f=document.querySelector('.office-frame').getBoundingClientRect();
          const btns=[...document.querySelectorAll('.office-actions .btn')].map(x=>x.getBoundingClientRect());
          const over=[...document.querySelectorAll('.office-win, .office-win *')].filter(e=>e.getClientRects().length&&e.getBoundingClientRect().right>vw+1).map(e=>e.className||e.tagName);
          return {vw, vh, frameH:Math.round(f.height), frameW:Math.round(f.width), frameTop:Math.round(f.top),
            btnsOnScreen: btns.every(r=>r.top>=0&&r.bottom<=vh+1&&r.left>=0&&r.right<=vw+1),
            // ON screen is not REACHABLE: the tab bar is drawn over the bottom 62px, and that is
            // where this row sat. What is under the middle of each button must be that button.
            covered: [...document.querySelectorAll('.office-actions .btn')].filter(x=>{const r=x.getBoundingClientRect();
              const hit=document.elementFromPoint(r.left+r.width/2, r.top+r.height/2); return !(hit && x.contains(hit));}).map(x=>x.id),
            clipped: [...document.querySelectorAll('.office-actions .btn')].filter(x=>x.scrollWidth>x.clientWidth+1).map(x=>x.id),
            headLeft: Math.round(document.querySelector('.office-head h3').getBoundingClientRect().left),
            minBtnH: Math.round(Math.min(...btns.map(r=>r.height))), buttons: btns.length,
            over, pageScroll: document.documentElement.scrollWidth>vw+1}})()''')
        # A resume re-reads the same arrival: no second editor.
        await b.js("__PC.consumeOpenDoc();true")
        await asyncio.sleep(1)
        got['uploads_after_resume'] = await b.js("__officeUploads.length")

        await b.js("document.getElementById('office-save').click();true")
        await b.until("__docSaves.length===1")
        got['save'] = await b.js("({nonce:__docSaves[0].nonce, text:atob(__docSaves[0].data)})")

        # A second document, from an app that did not grant write access.
        await b.js("__doc={...__doc,nonce:2,name:'Budget.xlsx',writable:false,"
                   "mime:'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',data:btoa('XLSX')};"
                   "__PC.consumeOpenDoc();true")
        await b.until("__officeUploads.length===2")
        await asyncio.sleep(.5)
        got['second'] = await b.js("__officeUploads[1].name")
        await b.js("document.getElementById('office-save').click();true")
        await b.until("/Saved a copy/.test(document.body.innerText)")
        got['saves_after_readonly'] = await b.js("__docSaves.length")

        # A PDF opens in Preview.
        await b.js("__doc={nonce:3,kind:'pdf',name:'Manual.pdf',mime:'application/pdf',writable:false,data:btoa('%PDF-1.4 x')};"
                   "__PC.consumeOpenDoc();true")
        await b.until("!!(window.PCPreview&&PCPreview.isOpen&&PCPreview.isOpen())")
        got['pdf_uploads'] = await b.js("__officeUploads.length")

    asyncio.run(desktop.with_browser('online', '', check, FIXTURE))
    assert got['upload'] == {'name': 'Quarterly Report.docx', 'text': 'ORIGINAL-DOCX'}, got['upload']
    assert not got['composer'], 'the document was attached to a post'
    f = got['fit']
    assert f['frameW'] >= f['vw'] - 24, f
    assert f['frameH'] >= f['vh'] * 0.6, 'the editor is a small box on a phone: %r' % f
    assert f['btnsOnScreen'] and f['buttons'] == 4 and f['minBtnH'] >= 36, f
    assert f['covered'] == [], 'the tab bar covers the Save row: %r' % f
    assert f['clipped'] == [], 'a button label is cut off: %r' % f
    assert f['headLeft'] >= 6, 'the file name touches the edge of the screen: %r' % f
    assert not f['over'] and not f['pageScroll'], f
    assert got['uploads_after_resume'] == 1, 'a resume opened the same document again'
    assert got['save'] == {'nonce': 1, 'text': 'EDITED-DOCX'}, got['save']
    assert got['second'] == 'Budget.xlsx'
    assert got['saves_after_readonly'] == 1, 'a read-only source was written to'
    assert got['pdf_uploads'] == 2, 'a PDF went to the Office editor instead of Preview'
