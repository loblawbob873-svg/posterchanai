"""Navigate the shipped Settings renderer and check that every icon actually paints."""
import json
import re
import subprocess
import tempfile
from html import unescape
from pathlib import Path
import pytest
from .test_notes_new_draft_runtime import CHROME, ROOT


@pytest.mark.skipif(not Path(CHROME).exists(), reason='Chrome is not installed')
def test_settings_icons_survive_every_desktop_and_mobile_category_change():
    source = (ROOT / 'static/js/client/os.js').read_text()
    renderer = source[source.index('  async function renderSystemSettings(){'):source.index('  function openTaskManager')]
    icon = source[source.index('  const iconSvg ='):source.index('  // ---- the feed handoff')]
    setup = '''
const wins=[],enc=s=>String(s??''),PC=()=>({}),me=()=>null;
const settings=()=>({get:(k,v)=>v}),STYLE_KEY='style',UI_SCALE_CHOICES=[1,1.25],uiScaleEffective=()=>1;
const desktopEffectsMode=()=> 'auto';let _osSettingsPage='appearance';
const _settingsRead=async p=>({ok:true,value:await p});
window.pcDisplays={status:async()=>[]};window.pcPower={status:async()=>({})};
const clockCalls=[];let clockFail=false;
const hostClock={available:true,automatic:true,synchronized:false,canAutomatic:true,timezone:'America/Denver',now:1788912000000};
window.pcDateTime={status:async()=>({...hostClock}),timezones:async()=>['America/Denver','Asia/Tokyo'],
 setAutomatic:async on=>{if(clockFail)throw Error('Permission denied');clockCalls.push(['auto',on]);hostClock.automatic=on;return {...hostClock}},
 setTimezone:async zone=>{clockCalls.push(['zone',zone]);hostClock.timezone=zone;return {...hostClock}},
 setTime:async value=>{clockCalls.push(['time',value]);return {...hostClock}}};
window.pcSystem={snapshot:async()=>({})};window.pcPrinters={status:async()=>({available:false})};
'''
    exercise = '''
(async()=>{try{
 const check=()=>{
  const uses=[...document.querySelectorAll('#feed svg use')];
  if(uses.length<20)throw Error('settings icons missing from markup');
  for(const use of uses){
   const href=use.getAttribute('href'),symbol=document.querySelector(href);
   if(!symbol||symbol.tagName!=='symbol')throw Error('missing symbol '+href);
   const svg=use.closest('svg');
   if(svg.getBoundingClientRect().width && !use.getBBox().width)throw Error('blank visible icon '+href);
  }
 };
 await renderSystemSettings();check();
 const pages=[...document.querySelectorAll('[data-page]')].map(b=>b.dataset.page);
 for(const page of pages){
  document.querySelector('[data-page="'+page+'"]').click();
  await new Promise(r=>setTimeout(r,5));check();
  if(document.querySelector('[data-settings-page="'+page+'"]').hidden)throw Error('page did not open '+page);
 }
 for(const page of pages){
  const select=document.querySelector('[data-settings-mobile]');select.value='page:'+page;
  select.dispatchEvent(new Event('change'));await new Promise(r=>setTimeout(r,5));check();
 }
 const pause=()=>new Promise(r=>setTimeout(r,5));
 const choose=async page=>{document.querySelector('[data-page="'+page+'"]').click();await pause();};
 await choose('datetime');
 if(!document.querySelector('[data-settings-page="datetime"]').textContent.includes('waiting for synchronization'))throw Error('enabled NTP falsely shown synchronized');
 if(!document.querySelector('[data-clock-time]').disabled)throw Error('manual clock enabled during automatic time');
 let toggle=document.querySelector('[data-clock-auto]');toggle.checked=false;toggle.dispatchEvent(new Event('change'));await pause();
 if(hostClock.automatic||document.querySelector('[data-clock-time]').disabled)throw Error('automatic change failed');
 const zone=document.querySelector('[data-clock-zone]');zone.value='Asia/Tokyo';zone.dispatchEvent(new Event('change'));document.querySelector('[data-clock-zone-save]').click();await pause();
 document.querySelector('[data-clock-time]').value='2026-09-08T15:30';document.querySelector('[data-clock-time-save]').click();await pause();
 if(JSON.stringify(clockCalls)!==JSON.stringify([['auto',false],['zone','Asia/Tokyo'],['time','2026-09-08T15:30']]))throw Error('wrong clock changes '+JSON.stringify(clockCalls));
 await choose('appearance');await choose('datetime');
 if(document.querySelector('[data-clock-auto]').checked||document.querySelector('[data-clock-zone]').value!=='Asia/Tokyo')throw Error('saved clock settings did not reload');
 clockFail=true;toggle=document.querySelector('[data-clock-auto]');toggle.checked=true;toggle.dispatchEvent(new Event('change'));await pause();
 if(toggle.checked||toggle.disabled||!document.querySelector('[data-clock-status]').textContent.includes('Permission denied'))throw Error('failed write was not rolled back');
 hostClock.timezone='Factory';await choose('appearance');await choose('datetime');
 if(document.querySelector('[data-clock-zone]').value!=='Factory'||!document.querySelector('[data-clock-zone-save]').disabled)throw Error('Factory timezone was lost or offered as valid replacement');
 if(document.querySelector('[data-clock-current]').textContent==='—')throw Error('Factory timezone lost local clock display');
 document.getElementById('result').textContent=JSON.stringify({passed:true,pages:pages.length});
}catch(e){document.getElementById('result').textContent=JSON.stringify({error:e.stack})}})();
'''
    html = '<!doctype html><style>svg.ic{width:24px;height:24px}</style><div id="feed"></div><pre id="result"></pre>'
    html += '<script>' + (ROOT / 'static/js/client/sprite.js').read_text() + '</script>'
    html += '<script>' + setup + icon + renderer + exercise + '</script>'
    with tempfile.TemporaryDirectory(prefix='pc-settings-icons-') as directory:
        page = Path(directory) / 'test.html'; page.write_text(html)
        run = subprocess.run([CHROME, '--headless=new', '--no-sandbox', '--disable-gpu', '--virtual-time-budget=2000', '--dump-dom', page.as_uri()], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr[-1000:]
    result = re.search(r'<pre id="result">(.*?)</pre>', run.stdout, re.S)
    assert result, run.stdout[-1000:]
    assert json.loads(unescape(result.group(1))) == {'passed': True, 'pages': 12}
