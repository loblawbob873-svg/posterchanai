"""The real presentation module preserves form handlers and accessible menu behavior."""
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


@pytest.mark.parametrize('width', [390, 1280])
@pytest.mark.parametrize('theme', CLIENT_THEMES)
def test_menu_breadcrumbs_and_folder_tiles(tmp_path, width, theme):
    css = (ROOT/'static/css/client.css').read_text() + (ROOT/'static/css/media-center-ui.css').read_text()
    js = (ROOT/'static/js/client/media-center-ui.js').read_text()
    cards = ''.join('<details class="mc-tool-card"><summary>'+name+'</summary><form class="mc-tool-body"><input required value="fixture"><button>Save</button></form></details>'
                    for name in ['Add a server folder','Bandwidth & resources','Connect an app'])
    folders = ''.join('<button class="mc-directory"><span class="mc-directory-art"></span><span class="mc-directory-label"><b>'+name+'</b><small>Browse folder</small></span></button>'
                      for name in ['Movies','A long directory name that must wrap on a phone'])
    path = tmp_path/'menu.html'
    path.write_text(f'''<!doctype html><html data-theme="{theme}"><meta name="viewport" content="width=device-width,initial-scale=1"><style>{css}</style>
<body><button id="outside">Outside</button><div class="mc-gallery"><div class="xdc-gal-top"><h2>Media Center</h2></div>
<div class="mc-tools">{cards}</div><div class="mc-folder-trail"><button>Library</button><button>Movies</button></div>
<div class="mc-directory-grid">{folders}</div></div><pre id="result"></pre>
<script>let submitted=0;document.querySelector('form').onsubmit=e=>{{e.preventDefault();submitted++;}};</script>
<script>{js}</script><script>setTimeout(()=>{{
const menu=document.querySelector('.mc-actions-menu'),toggle=menu.querySelector('summary'),tools=menu.querySelector('.mc-tools');
const closed=!menu.open;toggle.click();const opened=menu.open;
const rect=tools.getBoundingClientRect();const fits=rect.left>=0&&rect.right<=innerWidth+1;
const card=tools.querySelector('details');card.open=true;card.querySelector('form').requestSubmit();
menu.dispatchEvent(new KeyboardEvent('keydown',{{key:'Escape',bubbles:true}}));
const escaped=!menu.open&&document.activeElement===toggle;
toggle.click();document.getElementById('outside').dispatchEvent(new PointerEvent('pointerdown',{{bubbles:true}}));
const outsideClosed=!menu.open;
const columns=getComputedStyle(document.querySelector('.mc-directory-grid')).gridTemplateColumns.split(' ').length;
const crumb=document.querySelector('[aria-current=page]').textContent;
document.getElementById('result').textContent=JSON.stringify({{closed,opened,fits,escaped,outsideClosed,columns,crumb,submitted,
menus:document.querySelectorAll('.mc-actions-menu').length,actions:tools.querySelectorAll('.mc-tool-card').length}});
}},250);</script></body></html>''')
    result = subprocess.run([CHROME,'--headless=new','--no-sandbox','--disable-gpu',f'--window-size={width},900',
        '--force-device-scale-factor=1','--virtual-time-budget=1500','--dump-dom',path.as_uri()],capture_output=True,text=True,timeout=30)
    assert result.returncode == 0, result.stderr[-1000:]
    match = re.search(r'<pre id="result">(.*?)</pre>',result.stdout,re.S)
    assert match and match.group(1), result.stdout[-1000:]
    data = json.loads(unescape(match.group(1)))
    assert all(data[key] for key in ['closed','opened','fits','escaped','outsideClosed']), data
    assert data['submitted'] == 1 and data['actions'] == 3 and data['menus'] == 1
    assert data['crumb'] == 'Movies'
    assert data['columns'] >= 2
