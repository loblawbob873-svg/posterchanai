"""Real xterm selection and bracketed paste, including a delayed desktop clipboard."""
import base64
import os
from pathlib import Path

import pytest
from tests.client.test_emoji_pack_tabs_layout import chrome

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('delayed', [False, True])
def test_highlight_right_click_sends_text_once_with_bracketed_paste(chrome, delayed):
    source = Path(os.environ.get('PC_TERMINAL_JS', ROOT / 'static/js/client/term.js')).read_text()
    handlers = source[source.index('      /* HIGHLIGHT COPIES,'):source.index('      term.onData')]
    vendor = (ROOT / 'static/vendor/xterm/xterm.js').read_text()
    css = (ROOT / 'static/vendor/xterm/xterm.css').read_text()
    html = '<!doctype html><meta charset="utf-8"><style>' + css + '</style><div id="term" style="width:800px;height:400px"></div><script>' + vendor + '</script><script>' + r'''
let copied='[object Object]', finishCopy, input=[];
window.pcClip={write:s=>DELAY ? new Promise(r=>{finishCopy=()=>{copied=s;r(true);};}) : Promise.resolve((copied=s,true))};
window.pcClipRead={read:async()=>copied};
const PC={toast:()=>{},copyValue:async s=>{copied=s;}};
const box=document.querySelector('#term');
const term=new Terminal({cols:80,rows:20});term.open(box);term.onData(s=>input.push(s));
''' .replace('DELAY', str(delayed).lower()) + handlers + r'''
window.ready=new Promise(resolve=>term.write('echo café 日本語\r\nsecond line\x1b[?2004h',resolve));
</script>'''
    target = chrome.command('Target.createTarget', {'url': 'about:blank'})['targetId']
    chrome.session = chrome.command('Target.attachToTarget', {'targetId': target, 'flatten': True})['sessionId']
    try:
        chrome.command('Page.navigate', {'url': 'data:text/html;base64,' + base64.b64encode(html.encode()).decode()})
        chrome.evaluate('new Promise(r=>setTimeout(r,150))')
        chrome.evaluate('ready')
        chrome.evaluate('term.selectLines(0,1)')
        expected = chrome.evaluate('term.getSelection()')
        assert 'echo café 日本語' in expected and 'second line' in expected
        chrome.evaluate("box.dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,cancelable:true}))")
        chrome.evaluate('new Promise(r=>setTimeout(r,20))')
        if delayed:
            assert chrome.evaluate('input') == []
            chrome.evaluate('finishCopy()')
            chrome.evaluate('new Promise(r=>setTimeout(r,20))')
        assert chrome.evaluate('input') == ['\x1b[200~' + expected.replace('\r\n', '\n').replace('\n', '\r') + '\x1b[201~']
        assert chrome.evaluate('copied') == expected
    finally:
        chrome.session = None
        chrome.command('Target.closeTarget', {'targetId': target})
