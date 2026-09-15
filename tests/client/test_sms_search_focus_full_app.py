"""Actual Texts input retains typing focus while debounced results and archive updates paint."""
import asyncio
import pytest
from tests.client import test_desktop_offline_full_app as desktop

@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.parametrize('native_window', [False, True])
def test_texts_search_keeps_focus_caret_and_cancels_stale_work(native_window):
    async def check(b):
        await desktop.login(b)
        await b.js("PCOpenNotificationRoute('texts')")
        await b.until("!!document.querySelector('#sms-q')")
        await b.js("""(()=>{
          const s=PCSms._state();s.msgs.clear();
          for(const [id,body] of [['1','alpha'],['2','beta']])s.msgs.set(id,{id,address:'+1555000'+id,body,date:100,incoming:true});
          PCSms.refreshNames();
          window.searchTimers=new Map();window.searchTimerId=900000;
          const set=window.setTimeout,clear=window.clearTimeout;
          window.setTimeout=(fn,ms,...args)=>ms===250?(searchTimers.set(++searchTimerId,()=>fn(...args)),searchTimerId):set(fn,ms,...args);
          window.clearTimeout=id=>searchTimers.delete(id)||clear(id);
          window.flushSearch=()=>{const jobs=[...searchTimers.values()];searchTimers.clear();jobs.forEach(fn=>fn());};
          document.querySelector('#sms-q').focus();window.originalSearch=document.activeElement;
        })()""")
        for ch in 'alpha':
            await b.call('Input.insertText', {'text': ch})
            assert await b.js("document.activeElement===originalSearch && originalSearch.isConnected")
        assert await b.js("({query:document.querySelector('#sms-q').value,rows:document.querySelectorAll('.sms-thread').length,timers:searchTimers.size})") == dict(query='alpha',rows=2,timers=1)
        await b.js('flushSearch()')
        assert await b.js("({focus:document.activeElement.id,caret:document.activeElement.selectionStart,rows:document.querySelectorAll('.sms-thread').length})") == dict(focus='sms-q',caret=5,rows=1)
        # A real archive/name repaint preserves a selection in the middle of the query.
        await b.js("document.activeElement.setSelectionRange(1,4);PCSms.refreshNames()")
        assert await b.js("[document.activeElement.id,document.activeElement.selectionStart,document.activeElement.selectionEnd]") == ['sms-q',1,4]
        await b.call('Input.insertText', {'text':'Z'})
        assert await b.js("document.querySelector('#sms-q').value") == 'aZa'
        await b.js("document.querySelector('#sms-q').value='';document.querySelector('#sms-q').dispatchEvent(new Event('input'));flushSearch()")
        assert await b.js("document.querySelectorAll('.sms-thread').length") == 2
        # Starting IME composition cancels an earlier Latin query timer; background refresh
        # leaves the composing field attached until composition ends.
        await b.call('Input.insertText', {'text':'a'})
        await b.js("document.querySelector('#sms-q').dispatchEvent(new CompositionEvent('compositionstart'));window.composingSearch=document.querySelector('#sms-q');PCSms.refreshNames();flushSearch()")
        assert await b.js("composingSearch.isConnected && document.activeElement===composingSearch && searchTimers.size===0")
        await b.js("composingSearch.value='beta';composingSearch.dispatchEvent(new InputEvent('input',{isComposing:true}));composingSearch.dispatchEvent(new CompositionEvent('compositionend'));flushSearch()")
        assert await b.js("({rows:document.querySelectorAll('.sms-thread').length,q:PCSms._state().q,value:document.querySelector('#sms-q').value,timers:searchTimers.size})") == dict(rows=1,q='beta',value='beta',timers=0)
        await b.js("document.querySelector('#sms-q').value='';document.querySelector('#sms-q').dispatchEvent(new Event('input'));flushSearch()")
        # Pending list search cannot replace a newly opened conversation or another application.
        await b.call('Input.insertText', {'text':'beta'})
        await b.js("document.querySelector('.sms-thread').click();flushSearch()")
        assert await b.js("!!document.querySelector('#sms-in') && !document.querySelector('#sms-q')")
        await b.js("document.querySelector('#sms-back').click()")
        await b.until("!!document.querySelector('#sms-q')")
        await b.js("document.querySelector('#sms-q').value='late';document.querySelector('#sms-q').dispatchEvent(new Event('input'));__PC.switchView('notes');flushSearch()")
        assert not await b.js("!!document.querySelector('#sms-q')")
        await b.js("PCOpenNotificationRoute('texts')")
        await b.until("!!document.querySelector('#sms-q')")
        assert await b.js("({query:document.querySelector('#sms-q').value,rows:document.querySelectorAll('.sms-thread').length})") == dict(query='late',rows=0)
        await b.js("document.querySelector('#sms-q').focus();document.querySelector('#sms-q').dispatchEvent(new CompositionEvent('compositionstart'));__PC.switchView('notes');PCOpenNotificationRoute('texts')")
        await b.until("!!document.querySelector('#sms-q')")
        await b.js("document.querySelector('#sms-q').focus();document.querySelector('#sms-q').value='alpha';document.querySelector('#sms-q').dispatchEvent(new Event('input'));flushSearch()")
        assert await b.js("({query:document.querySelector('#sms-q').value,rows:document.querySelectorAll('.sms-thread').length})") == dict(query='alpha',rows=1)
        await b.js("window.retiredSearch=document.querySelector('#sms-q');retiredSearch.value='private';retiredSearch.dispatchEvent(new Event('input'));window.retiredJobs=[...searchTimers.values()];__PC.ME.pubkey='b'.repeat(64);PCSms.render()")
        await b.until("!!document.querySelector('#sms-q') && document.querySelector('#sms-q')!==retiredSearch")
        await b.js("retiredJobs.forEach(fn=>fn());retiredSearch.dispatchEvent(new CompositionEvent('compositionend'));flushSearch()")
        assert await b.js("({query:document.querySelector('#sms-q').value,applied:PCSms._state().q})") == dict(query='',applied='')
    extra="localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"
    if native_window:
        extra += "window.pcShell.windowContext={role:'app',view:'texts'};window.pcShell.backgroundOwner=false;"
    asyncio.run(desktop.with_browser('online','',check,extra))
