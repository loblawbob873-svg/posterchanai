"""Run the actual welcome module and styles in Chrome at phone and desktop widths."""
import json
import re
import shutil
import subprocess
from html import unescape
from pathlib import Path

import pytest
from app.schemas import CLIENT_THEMES

ROOT = Path(__file__).resolve().parents[2]
CHROME = shutil.which('google-chrome-stable') or shutil.which('chromium')
pytestmark = pytest.mark.skipif(not CHROME, reason='Chrome unavailable')


def render(tmp_path, theme='professional', width=390, eligible=True, pending=False, action='apply'):
    css = (ROOT / 'static/css/client.css').read_text() + (ROOT / 'static/css/instance-welcome.css').read_text()
    script = (ROOT / 'static/js/client/instance-welcome.js').read_text()
    config = json.dumps({'eligible': eligible, 'pending': pending, 'site_name': 'Example Community'})
    page = tmp_path / 'welcome.html'
    page.write_text(f'''<!doctype html><html data-theme="{theme}"><meta name="viewport" content="width=device-width,initial-scale=1">
<style>{css}</style><body><pre id="result"></pre><script>
let applies=0, account='a'.repeat(64), tick, desktop='{action}'==='already_desktop';
window.PCOS={{isOn:()=>desktop}};
window.PCOSWin={{isWindow:()=>'{action}'==='child_window'}};
if('{action}'==='first_run'){{const setup=document.createElement('div');setup.id='osfr';document.body.append(setup);}}
window.__PC_BOOTED=true;
window.__PC={{viewer:()=>({{pubkey:account}}), standalone:()=>false, LOGO:'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg"/>',
signTemplate:async template=>template}};
window.setInterval=fn=>{{tick=fn;}};
window.fetch=async(url)=>({{ok:true,json:async()=>{{if(url.endsWith('/apply')){{applies++;return {{ok:true,pending:true}};}}if('{action}'==='enter_before_status') desktop=true;return {config};}}}});
</script><script>{script}</script><script>
setTimeout(async()=>{{
const d=document.querySelector('dialog'), button=d?.querySelector('.iw-apply');
const shown=!!d?.open, focused=document.activeElement===button;
const rect=d?.getBoundingClientRect(), style=d?getComputedStyle(d):null;
const columns=d?getComputedStyle(d.querySelector('.iw-benefits')).gridTemplateColumns.split(' ').length:0;
if(button&&'{action}'==='apply'){{button.click();button.click();await new Promise(r=>setTimeout(r,50));}}
if('{action}'==='enter_open'){{desktop=true;document.body.append(document.createElement('div'));await new Promise(r=>setTimeout(r,50));}}
if('{action}'==='logout'){{account='';await tick();}}
document.getElementById('result').textContent=JSON.stringify({{shown,focused,columns,applies,
closed:!document.querySelector('dialog[open]'),status:d?.querySelector('.iw-status').textContent,
fits:!d||(rect.left>=0&&rect.right<=innerWidth+1&&d.scrollWidth<=d.clientWidth+1),
background:style?.backgroundColor,color:style?.color}});
}},250);
</script></body></html>''')
    done = subprocess.run([CHROME, '--headless=new', '--no-sandbox', '--disable-gpu',
        f'--window-size={width},900', '--force-device-scale-factor=1', '--virtual-time-budget=2000',
        '--dump-dom', page.as_uri()], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr[-1500:]
    match = re.search(r'<pre id="result">(.*?)</pre>', done.stdout, re.S)
    assert match and match.group(1), done.stdout[-1500:]
    return json.loads(unescape(match.group(1)))


@pytest.mark.parametrize('theme', CLIENT_THEMES)
@pytest.mark.parametrize('width', [390, 1280])
def test_welcome_fits_theme_and_viewport_and_submits_once(tmp_path, theme, width):
    got = render(tmp_path, theme, width)
    assert got['shown'] and got['focused'] and got['fits'], got
    assert got['columns'] == (1 if width == 390 else 3), got
    assert got['applies'] == 1 and 'application is saved' in got['status'], got
    assert got['background'] != got['color']


@pytest.mark.parametrize('eligible,pending', [(False, False), (True, True)])
def test_existing_members_and_pending_applicants_are_not_prompted(tmp_path, eligible, pending):
    got = render(tmp_path, eligible=eligible, pending=pending)
    assert not got['shown'] and got['applies'] == 0


def test_logout_removes_the_previous_accounts_splash(tmp_path):
    got = render(tmp_path, action='logout')
    assert got['shown'] and got['closed'] and got['applies'] == 0


@pytest.mark.parametrize('action', ['already_desktop', 'child_window', 'first_run', 'enter_before_status'])
def test_splash_does_not_cover_desktop_or_setup(tmp_path, action):
    result = render(tmp_path, action=action)
    assert not result['shown'] and result['applies'] == 0, result


def test_entering_desktop_closes_an_already_open_splash(tmp_path):
    result = render(tmp_path, action='enter_open')
    assert result['shown'] and result['closed'] and result['applies'] == 0, result
