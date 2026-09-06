"""Exercise the shipped controls, including layout after the real picker bridge runs."""
import json
import re
import shutil
import subprocess
from html import unescape
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CHROME = shutil.which('google-chrome-stable') or shutil.which('chromium')


def browser(tmp_path, body, script, width=1280, css=''):
    if not CHROME:
        pytest.skip('Chrome unavailable')
    page = tmp_path / 'regression.html'
    page.write_text('<!doctype html><html><meta name="viewport" content="width=device-width,initial-scale=1">'
                    '<style>' + css + '</style><body>' + body + '<pre id="result"></pre>'
                    '<script>' + script + '</script></body></html>')
    out = subprocess.run([CHROME, '--headless=new', '--no-sandbox', '--disable-gpu',
                          f'--window-size={width},900', '--virtual-time-budget=2000',
                          '--dump-dom', page.as_uri()], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr[-1000:]
    found = re.search(r'<pre id="result">(.*?)</pre>', out.stdout, re.S)
    assert found and found[1], out.stdout[-2000:]
    return json.loads(unescape(found[1]))


@pytest.mark.parametrize('enabled,solo,visible', [(False, False, False), (True, False, True), (False, True, True)])
def test_guest_signup_obeys_registration(tmp_path, enabled, solo, visible):
    source = (ROOT / 'static/js/client/app.js').read_text()
    helper = source[source.index('  function _registrationOpen('):source.index('  function applyInstanceGating(')]
    guest = source[source.index('  function _guestCardHtml('):source.index('  function _timelineHeaderHtml(')]
    result = browser(tmp_path, '<div id="guest"></div>', f'''
const CFG={{registration_enabled:{json.dumps(enabled)}}}, _standalone=()=>{json.dumps(solo)};
const enc=String, LOGO='data:image/gif;base64,R0lGODlhAQABAIAAAP///wAAACH5BAEAAAAALAAAAAABAAEAAAICRAEAOw==', SOURCE_URL='';
{helper}
{guest}
document.querySelector('#guest').innerHTML=_guestCardHtml();
document.querySelector('#result').textContent=JSON.stringify({{signup:!!document.querySelector('#guest-signup'),login:!!document.querySelector('#guest-login2')}});
''')
    assert result == {'signup': visible, 'login': True}


@pytest.mark.parametrize('width', [390, 1280])
@pytest.mark.parametrize('zoom', [0.85, 1.25])
def test_shared_concord_picker_uses_message_position_after_toolbar_hides(tmp_path, width, zoom):
    app = (ROOT / 'static/js/client/app.js').read_text()
    cord = (ROOT / 'static/js/client/concord.js').read_text()
    placer = app[app.index('  function _placePop('):app.index('  // Keyboard navigation for the flat')]
    popover = app[app.index('  function openEmojiPopover('):app.index('  // Tapping the react button when')]
    dismiss = re.search(r'const dismissPointer=e=>.*', cord).group(0)
    handler = cord[cord.index("    $$('[data-cc-react]').forEach"):cord.index("    $$('[data-cc-zap]').forEach")]
    css = (ROOT / 'static/css/client.css').read_text()
    css += f'body{{zoom:{zoom}}}.cc-message{{position:absolute;left:100px;top:400px}}.cc-message:not(.cc-actions-open) button{{display:none}}'
    result = browser(tmp_path, '<div class="cc-message cc-actions-open"><button data-cc-react="message">React</button></div>', f'''
const $=(s,r=document)=>r.querySelector(s), $$=(s,r=document)=>[...r.querySelectorAll(s)];
const enc=String, REACTION_EMOJIS=['👍','❤️'], InstEmoji={{list:[{{s:'party',p:'pack',t:''}}],load:async()=>[1]}};
const _emojiBtn=e=>'<button data-e="thumb">👍</button>',_emojiRecent=()=>[],_emojiRemember=()=>{{}},_popKeys=()=>()=>{{}};
let reactionTarget=null,picked=null;
const closeMessageActions=()=>{{$('.cc-message').classList.remove('cc-actions-open');reactionTarget=null;}};
const toggleReaction=(id,emoji)=>{{picked={{id,emoji}};}};
{placer}
{popover}
const p={{openEmojiPopover}};
{dismiss}
document.addEventListener('pointerdown',dismissPointer);
{handler}
const button=$('[data-cc-react]'),anchor=button.getBoundingClientRect();button.click();
setTimeout(()=>{{
 const pop=$('.emoji-pop'),rect=pop.getBoundingClientRect();
 const fits=rect.left>=0&&rect.right<=innerWidth+1&&rect.top>=0&&rect.bottom<=innerHeight+1;
 const near=Math.min(Math.abs(rect.top-anchor.bottom),Math.abs(rect.bottom-anchor.top))<20;
 pop.querySelector('[data-e]').dispatchEvent(new PointerEvent('pointerdown',{{bubbles:true}}));
 pop.querySelector('[data-e]').dispatchEvent(new MouseEvent('mousedown',{{bubbles:true}}));
 document.querySelector('#result').textContent=JSON.stringify({{fits,near,picked,closed:!$('.emoji-pop')}});
}},50);
''', width, css)
    assert result['fits'] and result['near'], result
    assert result['picked'] == {'id': 'message', 'emoji': 'thumb'} and result['closed'], result


@pytest.mark.parametrize('width', [390, 1280])
def test_populated_jellyfin_device_names_have_readable_width(tmp_path, width):
    source = (ROOT / 'static/js/client/app.js').read_text()
    card = source[source.index('<details id="mc-jellyfin"'):]
    card = card[:card.index('</details>') + len('</details>')]
    card = card.replace('Loading devices…', '<div class="mc-device-row"><div><strong>Living Room Roku</strong>'
                        '<small>Jellyfin Roku · Last used today</small></div><button class="btn btn-ghost">Revoke</button></div>')
    css = (ROOT / 'static/css/client.css').read_text() + (ROOT / 'static/css/media-center-ui.css').read_text()
    script = (ROOT / 'static/js/client/media-center-ui.js').read_text()
    result = browser(tmp_path, '<div class="mc-gallery"><div class="xdc-gal-top"><h2>Media Center</h2></div>'
                     '<div class="mc-tools">' + card + '</div></div>', script + '''
setTimeout(()=>{
document.querySelectorAll('details').forEach(d=>d.open=true);
const info=document.querySelector('.mc-device-row>div'),heading=document.querySelector('h4');
const rect=info.getBoundingClientRect(),h=heading.getBoundingClientRect();
document.querySelector('#result').textContent=JSON.stringify({width:rect.width,height:rect.height,headingHeight:h.height});
},100);
''', width, css)
    assert result['width'] >= 100 and result['height'] < 100 and result['headingHeight'] < 50, result


@pytest.mark.parametrize('fail', [False, True])
def test_policy_first_preview_waits_for_loading_and_preserves_edits(tmp_path, fail):
    source = (ROOT / 'static/js/admin.js').read_text().split('// Relay access policy has its own save action;')[1].split('\n', 1)[1]
    body = '<button class="tab-btn" data-tab="relay">Relay</button>' + ''.join(
        '<input type="checkbox" id="access-policy-' + key + '">' for key in ['enabled', 'fedi'])
    body += ''.join('<button id="access-policy-' + key + '">' + key + '</button>' for key in ['preview', 'run', 'save'])
    body += '<p id="access-policy-status"></p><ul id="access-policy-affected"></ul>'
    result = browser(tmp_path, body, f'''
let release,calls=[];const ready=new Promise(r=>release=r);
const csrfFetch=async(url,opts)=>{{calls.push({{url,method:opts.method,body:opts.body}});
if(opts.method==='GET'){{await ready;return {{ok:true,json:async()=>({{enabled:false,exempt_fediverse:true}})}};}}
return {{ok:{json.dumps(not fail)},json:async()=>({{detail:'Preview unavailable',domain:'test',accounts:1,ai:0,blossom:0,streaming:1,whitelist:0,affected_accounts:[{{name:'<img src=x onerror=alert(1)>',streaming:true}}]}})}};}};
const pcConfirm=async()=>false;
{source}
document.querySelector('.tab-btn').click();
const fedi=document.querySelector('#access-policy-fedi');fedi.checked=false;fedi.dispatchEvent(new Event('change'));
document.querySelector('#access-policy-preview').click();release();
setTimeout(()=>{{document.querySelector('#result').textContent=JSON.stringify({{calls,status:document.querySelector('#access-policy-status').textContent,disabled:document.querySelector('#access-policy-preview').disabled,unsafe:!!document.querySelector('#access-policy-affected img')}});}},100);
''')
    assert len(result['calls']) == 2 and result['calls'][1]['url'].endswith('/preview'), result
    assert json.loads(result['calls'][1]['body'])['exempt_fediverse'] is False
    assert not result['disabled'] and not result['unsafe']
    assert ('Preview unavailable' if fail else '1 Live Streaming grants') in result['status']
