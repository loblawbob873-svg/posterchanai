"""Apply themes through Settings and measure the actual browser and native Files views."""
import asyncio
import base64
import os
from pathlib import Path

import pytest
from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_office_close_files_full_app import BOUNDARIES


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('native_window', [False, True])
def test_files_follow_selected_theme_with_readable_controls(native_window):
    async def check(browser):
        await desktop.login(browser)
        for theme in ('cyberpunk', 'professional', 'dark', 'cherryblossom', 'win98', 'winxp', 'animegirl', 'sovietgothic', 'monero'):
            await browser.js("__PC.switchView('settings')")
            await browser.until("!!document.querySelector('#us-theme')")
            await browser.js(f"(()=>{{const picker=document.querySelector('#us-theme');picker.value='{theme}';picker.dispatchEvent(new Event('change',{{bubbles:true}}));}})()")
            assert await browser.js("document.documentElement.getAttribute('data-theme')||'cyberpunk'") == theme
            await browser.js("__PC.switchView('blossom')")
            await browser.until("!!document.querySelector('[data-host=\"1\"]')")
            await browser.js("document.querySelector('[data-host=\"1\"]').click()")
            await browser.until("!!document.querySelector('[data-p=\"/home/test/Reports\"]')")
            metrics = await browser.js(r'''(()=>{
              const style=s=>getComputedStyle(document.querySelector(s));
              const main=style('.fx-main'),bar=style('#host-pane .fx-bar');
              const token=name=>{const el=document.createElement('i');el.style.color='var('+name+')';document.body.append(el);const c=getComputedStyle(el).color;el.remove();return c;};
              const rect=el=>{const r=el.getBoundingClientRect();return {x:r.x,y:r.y,w:r.width,h:r.height,right:r.right,bottom:r.bottom};};
              const luminance=color=>{const canvas=document.createElement('canvas'),ctx=canvas.getContext('2d');ctx.fillStyle=color;ctx.fillRect(0,0,1,1);const c=[...ctx.getImageData(0,0,1,1).data].slice(0,3).map(v=>v/255).map(v=>v<=.04045?v/12.92:((v+.055)/1.055)**2.4);return c[0]*.2126+c[1]*.7152+c[2]*.0722;};
              const contrast=(a,b)=>{a=luminance(a);b=luminance(b);return (Math.max(a,b)+.05)/(Math.min(a,b)+.05);};
              const controls=[...document.querySelectorAll('#host-pane .fx-bar button:not(:disabled), #host-pane .fx-bar input')].filter(el=>el.getBoundingClientRect().width>0);
              return {main:main.backgroundColor,bar:bar.backgroundColor,text:main.color,bg:token('--bg'),ink:token('--text'),
                image:bar.backgroundImage,icon:style('.fx-file-folder').color,iconFilter:style('.fx-file-folder').filter,
                controls:controls.map(el=>({...rect(el),hit:el.contains(document.elementFromPoint(el.getBoundingClientRect().x+el.getBoundingClientRect().width/2,el.getBoundingClientRect().y+el.getBoundingClientRect().height/2))})),
                chromeBottom:document.querySelector('#pc-oswin-chrome')?.getBoundingClientRect().bottom||0,
                scale:parseFloat(getComputedStyle(document.body).zoom)||1,
                width:innerWidth,height:innerHeight,overflow:document.documentElement.scrollWidth>innerWidth,
                textContrast:contrast(main.color,token('--bg')),iconContrast:contrast(style('.fx-file-folder').color,token('--bg')),
                active:!!document.querySelector('[data-fxtoggle=computer].active')};
            })()''')
            directory = os.environ.get('PC_FILES_THEME_SCREENSHOTS')
            if directory and native_window:
                Path(directory).mkdir(parents=True, exist_ok=True)
                shot = await browser.call('Page.captureScreenshot', {'format': 'png'})
                Path(directory, theme + '.png').write_bytes(base64.b64decode(shot['data']))
            assert metrics['main'] == metrics['bg'], (theme, metrics)
            assert metrics['bar'] == metrics['bg'], (theme, metrics)
            assert metrics['text'] == metrics['ink'], (theme, metrics)
            assert metrics['textContrast'] >= 4.5 and metrics['iconContrast'] >= 3, (theme, metrics)
            assert metrics['active'] and not metrics['overflow'], (theme, metrics)
            assert metrics['controls'], (theme, metrics)
            assert all(c['hit'] and c['w'] >= 28*metrics['scale'] and c['h'] >= 28*metrics['scale'] and c['x'] >= 0 and c['y'] >= metrics['chromeBottom'] and c['right'] <= metrics['width'] and c['bottom'] <= metrics['height'] for c in metrics['controls']), (theme, metrics)
            assert ('linear-gradient' in metrics['image']) == (theme == 'cyberpunk'), (theme, metrics)
            if theme != 'cyberpunk':
                assert metrics['iconFilter'] == 'none', (theme, metrics)

    extra = BOUNDARIES + r"const themeFetch=window.fetch;window.fetch=(url,opts)=>String(url).includes('/api/auth/api-keys')?Promise.resolve(new Response('[]',{headers:{'Content-Type':'application/json'}})):themeFetch(url,opts);"
    if native_window:
        extra += "window.pcShell.windowContext={role:'app',view:'blossom'};window.pcShell.backgroundOwner=false;"
    asyncio.run(desktop.with_browser('online', '?pcwin=blossom' if native_window else '', check, extra))
