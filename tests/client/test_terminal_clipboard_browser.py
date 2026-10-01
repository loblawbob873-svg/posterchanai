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


def test_highlight_copies_even_when_a_program_has_turned_on_mouse_reporting(chrome):
    """"terminal won't let me copy by highlight when claude is in screen session": screen and Claude Code
    turn on mouse reporting, after which xterm sent every drag to the PROGRAM and selecting needed Shift.
    A REAL mouse drag (CDP Input events) must select and copy, and send no mouse report to the program;
    Alt+click must still reach the program."""
    source = Path(os.environ.get('PC_TERMINAL_JS', ROOT / 'static/js/client/term.js')).read_text()
    handlers = source[source.index('      /* HIGHLIGHT COPIES,'):source.index('      term.onData')]
    vendor = (ROOT / 'static/vendor/xterm/xterm.js').read_text()
    css = (ROOT / 'static/vendor/xterm/xterm.css').read_text()
    html = '<!doctype html><meta charset="utf-8"><style>body{margin:0}' + css + '</style><div id="term" style="width:800px;height:400px"></div><script>' + vendor + '</script><script>' + r'''
let copied='', input=[];
window.pcClip={write:s=>Promise.resolve((copied=s,true))};
const PC={toast:()=>{},copyValue:async s=>{copied=s;}};
const box=document.querySelector('#term');
const term=new Terminal({cols:80,rows:20});term.open(box);term.onData(s=>input.push(s));
''' + handlers + r'''
// What screen / Claude Code do: SGR mouse reporting on, then draw text.
window.ready=new Promise(resolve=>term.write('\x1b[?1000h\x1b[?1002h\x1b[?1006hclaude says hello world\r\n',resolve));
window.cell=()=>{const r=box.querySelector('.xterm-screen').getBoundingClientRect();return {x:r.left,y:r.top,w:r.width/term.cols,h:r.height/term.rows};};
</script>'''
    target = chrome.command('Target.createTarget', {'url': 'about:blank'})['targetId']
    chrome.session = chrome.command('Target.attachToTarget', {'targetId': target, 'flatten': True})['sessionId']

    def mouse(kind, x, y, mods=0, buttons=1):
        chrome.command('Input.dispatchMouseEvent', {'type': kind, 'x': x, 'y': y, 'button': 'left',
                                                    'buttons': buttons, 'clickCount': 1, 'modifiers': mods})
    try:
        chrome.command('Page.navigate', {'url': 'data:text/html;base64,' + base64.b64encode(html.encode()).decode()})
        chrome.evaluate('new Promise(r=>setTimeout(r,150))')
        chrome.evaluate('ready')
        assert chrome.evaluate('term.modes.mouseTrackingMode') != 'none', 'the fixture did not turn mouse reporting on'
        c = chrome.evaluate('cell()')
        y = c['y'] + c['h'] / 2
        x0, x1 = c['x'] + c['w'] * 0.5, c['x'] + c['w'] * 17.5      # "claude says hello"
        mouse('mouseMoved', x0, y, buttons=0)
        mouse('mousePressed', x0, y)
        for i in range(1, 9):
            mouse('mouseMoved', x0 + (x1 - x0) * i / 8, y)
        mouse('mouseReleased', x1, y, buttons=0)
        chrome.evaluate('new Promise(r=>setTimeout(r,50))')
        sel = chrome.evaluate('term.getSelection()')
        assert sel.startswith('claude says hello'), ('a drag did not select while mouse reporting was on', sel)
        assert chrome.evaluate('copied') == sel, 'the selection was not copied'
        reports = [s for s in chrome.evaluate('input') if '\x1b[<' in s or '\x1b[M' in s]
        assert reports == [], ('the drag was sent to the program as mouse reports', reports)

        chrome.evaluate('input.length=0;term.clearSelection()')
        mouse('mousePressed', x0, y, mods=1)                          # Alt
        mouse('mouseReleased', x0, y, mods=1, buttons=0)
        chrome.evaluate('new Promise(r=>setTimeout(r,50))')
        assert any('\x1b[<' in s for s in chrome.evaluate('input')), 'Alt+click no longer reaches the program'
    finally:
        chrome.session = None
        chrome.command('Target.closeTarget', {'targetId': target})
