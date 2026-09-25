"""Preview: Print, Share, and a name you can read -- in the real bundled client.

Reported: "Preview app has no print support on desktop or android", "and no share for android",
"opening a file in preview on android you can't see the file name when opened, the name and buttons
are all together". Measured here, in Chrome, against the shipped preview.js and client.css:

  * on a phone the file name has its OWN row above the actions and gets most of the width;
  * Print on the web/desktop prints a frame holding the file: the picture itself, or a PDF's pages
    drawn by pdf.js (every page an image, none blank);
  * on the APK (Capacitor present) Print goes to the native `Print` plugin with the file's bytes,
    and Share writes the file to the cache and opens Android's share sheet with it.
"""
import asyncio
from pathlib import Path

import pytest
from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


PHONE_OFF_OS = r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
'''

NAME = 'A really long holiday photo name from the beach, summer 2026.png'

# A real (tiny) PNG and a real one-page PDF, made in the page.
MAKE = r'''window.__png=async()=>{const c=document.createElement('canvas');c.width=300;c.height=200;
 const g=c.getContext('2d');g.fillStyle='#c00';g.fillRect(0,0,300,200);
 return await new Promise(r=>c.toBlob(r,'image/png'));};
window.__pdf=()=>{const objs=['<< /Type /Catalog /Pages 2 0 R >>','<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
 '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents 4 0 R >>'];
 const stream='0 0 1 rg 20 20 160 160 re f';objs.push('<< /Length '+stream.length+' >>\nstream\n'+stream+'\nendstream');
 let out='%PDF-1.4\n';const offs=[];objs.forEach((o,i)=>{offs.push(out.length);out+=(i+1)+' 0 obj\n'+o+'\nendobj\n';});
 const x=out.length;out+='xref\n0 '+(objs.length+1)+'\n0000000000 65535 f \n'+offs.map(o=>String(o).padStart(10,'0')+' 00000 n \n').join('')
 +'trailer\n<< /Size '+(objs.length+1)+' /Root 1 0 R >>\nstartxref\n'+x+'\n%%EOF';
 return new Blob([out],{type:'application/pdf'});};
// Record print() on whatever print frame Preview makes, before Preview's own load handler runs on.
if(!window.__printed){ window.__printed=[];
new MutationObserver(ms=>ms.forEach(m=>m.addedNodes.forEach(n=>{
  if(!(n.classList&&n.classList.contains('pv-print-frame')))return;
  // Wrap Preview's OWN onload, which runs first: its continuation calls print() straight after it,
  // so a listener added here would see the real print() go by before it could record it.
  const own=n.onload;
  n.onload=function(){const w=n.contentWindow;w.print=()=>{const d=w.document;
    __printed.push([...d.images].map(i=>({w:i.naturalWidth,h:i.naturalHeight})));};
    return own&&own.apply(this,arguments);};
}))).observe(document.body,{childList:true}); }
'''


async def _open(b, kind):
    await b.js(MAKE)
    blob = "await __png()" if kind == 'png' else "__pdf()"
    name = NAME if kind == 'png' else 'Quarterly report.pdf'
    await b.js(f"(async()=>{{const blob={blob};return PCPreview.open({{name:{name!r},blob}});}})()")
    await b.until("!!document.querySelector('.pv-host .pv-bar')")


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_phone_preview_shows_the_name_and_prints_a_picture_and_a_pdf():
    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride',
                     {'width': 390, 'height': 844, 'deviceScaleFactor': 2, 'mobile': True})
        await desktop.login(b)
        await _open(b, 'png')
        geo = await b.js(r'''(()=>{const r=s=>document.querySelector(s).getBoundingClientRect();
          const n=r('.pv-name'),a=r('.pv-acts'),x=r('.pv-x');
          return {nameW:n.width,nameBottom:n.bottom,actsTop:a.top,closeTop:x.top,nameTop:n.top,
                  print:!!document.querySelector('.pv-print')}})()''')
        assert geo['nameW'] >= 220, f"the name is squeezed to {geo['nameW']}px"
        assert geo['nameBottom'] <= geo['actsTop'] + 1, 'the name must sit on its own row above the actions'
        assert abs(geo['closeTop'] - geo['nameTop']) < 24, 'close belongs to the title row'
        assert geo['print'], 'a picture must offer Print'
        await b.js("document.querySelector('.pv-print').click()")
        await b.until("__printed.length===1")
        pages = await b.js("__printed[0]")
        assert len(pages) == 1 and pages[0]['w'] == 300, pages
        await b.js("PCPreview.close()")
        await _open(b, 'pdf')
        await b.until("!!document.querySelector('.pv-pdf-pages canvas')")
        await b.js("document.querySelector('.pv-print').click()")
        await b.until("__printed.length===2")
        pages = await b.js("__printed[1]")
        assert len(pages) == 1 and pages[0]['w'] > 0 and pages[0]['h'] > 0, f'PDF pages were not drawn: {pages}'

    asyncio.run(desktop.with_browser('online', '', check, PHONE_OFF_OS))


NATIVE = r'''(()=>{window.__cap={print:[],write:[],share:[]};
 window.Capacitor=Object.assign(window.Capacitor||{},{isNativePlatform:()=>true,
  Plugins:{Print:{print:async o=>{__cap.print.push({mime:o.mime,name:o.name,len:atob(o.data).length});return{ok:true}}},
           Filesystem:{writeFile:async o=>{__cap.write.push({path:o.path,len:atob(o.data).length});return{uri:'file:///cache/'+o.path}}},
           Share:{share:async o=>{__cap.share.push(o);return{}}}}});})()'''


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_on_the_apk_print_and_share_go_through_the_native_plugins():
    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride',
                     {'width': 390, 'height': 844, 'deviceScaleFactor': 2, 'mobile': True})
        await desktop.login(b)
        await b.js(NATIVE)
        await _open(b, 'png')
        assert await b.js("!!document.querySelector('.pv-share')"), 'the APK must offer Share'
        size = await b.js("(async()=>(await __png()).size)()")
        await b.js("document.querySelector('.pv-print').click()")
        await b.until("__cap.print.length===1")
        job = await b.js("__cap.print[0]")
        assert job['mime'] == 'image/png' and job['len'] == size and job['name'].endswith('.png'), job
        assert await b.js("!document.querySelector('.pv-print-frame')"), 'the APK must not fall back to window.print'
        await b.js("document.querySelector('.pv-share').click()")
        await b.until("__cap.share.length===1")
        wrote, shared = await b.js("[__cap.write[0], __cap.share[0]]")
        assert wrote['len'] == size, wrote
        assert shared['files'] == ['file:///cache/' + wrote['path']], shared

    asyncio.run(desktop.with_browser('online', '', check, PHONE_OFF_OS))
