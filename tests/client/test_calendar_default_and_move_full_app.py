"""Real Calendar dialogs persist a default and submit safe moves through the API boundary."""
import asyncio
from pathlib import Path
import pytest
from tests.client import test_desktop_offline_full_app as desktop

@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()

BOUNDARIES = r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
localStorage.setItem('pc_cal_nostr','0');
const day=new Date(), date=[day.getFullYear(),String(day.getMonth()+1).padStart(2,'0'),String(day.getDate()).padStart(2,'0')].join('');
window.__originalIcs=['BEGIN:VCALENDAR','VERSION:2.0','X-WR-CALNAME:Original','BEGIN:VEVENT','UID:original','DTSTART;VALUE=DATE:'+date,'SUMMARY:Original appointment','RRULE:FREQ=DAILY','X-PRIVATE:preserve','ATTENDEE:mailto:friend@example.test','BEGIN:VALARM','ACTION:DISPLAY','TRIGGER:-PT15M','DESCRIPTION:Reminder','END:VALARM','END:VEVENT','END:VCALENDAR',''].join('\r\n');
window.__calendarData={a:[{uid:'original',ics:__originalIcs,component:'VEVENT'}],b:[],feed:[]};
window.__calendarCalls=[];window.__moveFailures=0;
window.__calendarList=[{id:'a',displayname:'Personal'},{id:'b',displayname:'Work'},{id:'feed',displayname:'Subscribed',subscribe:{url:'https://fixture.invalid/feed.ics'}}];
const calendarFetch=window.fetch;
window.fetch=async function(url,opts={}){
 const u=new URL(String(url),location.href);
 const reply=(body,status=200)=>Promise.resolve(new Response(JSON.stringify(body),{status,headers:{'Content-Type':'application/json'}}));
 if(u.pathname==='/api/calendar/config')return reply({enabled:true});
 if(u.pathname==='/api/calendar/calendars')return reply({calendars:__calendarList});
 if(u.pathname==='/api/calendar/items/move'){
   const body=JSON.parse(opts.body);__calendarCalls.push({op:'move',...body});
   const existing=__calendarData[body.target].find(r=>r.uid===body.uid);
   if(existing && existing.ics!==body.ics)return reply({detail:'Destination conflict'},409);
   if(!existing)__calendarData[body.target].push({uid:body.uid,ics:body.ics,component:'VEVENT'});
   if(__moveFailures-->0)return reply({detail:'Copied to the destination, but the original could not be removed. Retry to finish the move.'},502);
   __calendarData[body.cal]=__calendarData[body.cal].filter(r=>r.uid!==body.uid);
   return reply({ok:true});
 }
 if(u.pathname==='/api/calendar/items'){
   if(opts.method==='PUT'){const body=JSON.parse(opts.body);__calendarCalls.push({op:'put',...body});__calendarData[body.cal].push(body);return reply({ok:true});}
   return reply({items:__calendarData[u.searchParams.get('cal')]||[]});
 }
 return calendarFetch(url,opts);
};
'''

async def calendar(browser):
    await desktop.login(browser)
    await browser.js("__PC.switchView('calendar')")
    await browser.until("document.querySelector('#cal-pick')?.options.length===3")

@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(),reason='Chrome required')
def test_default_survives_reload_and_removed_or_subscribed_default_falls_back():
    async def check(b):
        await calendar(b)
        await b.js("document.querySelector('#cal-menu').click()")
        assert await b.js("Array.from(document.querySelector('#cal-default').options,o=>o.value)")==['a','b']
        await b.js("document.querySelector('#cal-default').value='b';document.querySelector('#cal-default').dispatchEvent(new Event('change'));__PC.closeModal();document.querySelector('#cal-new').click()")
        assert await b.js("document.querySelector('#cev-cal').value")=='b'
        await b.js("document.querySelector('#cev-title').value='New work event';document.querySelector('#cev-save').click()")
        await b.until("__calendarCalls.length===1 && !document.querySelector('#cev-save')")
        assert await b.js("__calendarCalls[0].cal")=='b'
        # A full document load must restore the preference without preserving module state.
        url=await b.js("location.origin+'/index.html'")
        await b.call('Page.navigate',{'url':url})
        await b.until("!!window.__PC && !!__PC.me()")
        await b.js("__PC.switchView('calendar')")
        await b.until("document.querySelector('#cal-pick')?.options.length===3")
        await b.js("document.querySelector('#cal-new').click()")
        assert await b.js("document.querySelector('#cev-cal').value")=='b'
        await b.js("__PC.closeModal();__calendarList=__calendarList.filter(c=>c.id!=='b');PCCalendar.reload()")
        await b.js("document.querySelector('#cal-new').click()")
        assert await b.js("document.querySelector('#cev-cal').value")=='a'
        await b.js("__PC.closeModal();localStorage.setItem('pc_cal_default:'+__PC.me().pubkey,'feed');document.querySelector('#cal-new').click()")
        assert await b.js("document.querySelector('#cev-cal').value")=='a'
    asyncio.run(desktop.with_browser('online','',check,BOUNDARIES))

@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(),reason='Chrome required')
@pytest.mark.parametrize('edit_title',[False,True])
def test_move_keeps_original_ics_and_retries_exact_edited_payload(edit_title):
    async def check(b):
        await calendar(b)
        await b.until("!!document.querySelector('.cal-edit[data-uid=original]')")
        await b.js("document.querySelector('.cal-edit[data-uid=original]').click()")
        assert await b.js("document.querySelector('#cev-cal').value")=='a'
        if edit_title:
            await b.js("document.querySelector('#cev-title').value='Edited appointment'")
        await b.js("__moveFailures=1;document.querySelector('#cev-cal').value='b';document.querySelector('#cev-save').click()")
        await b.until("__calendarCalls.length===1 && !document.querySelector('#cev-save').disabled")
        assert await b.js("__calendarData.a.length") == 1
        assert await b.js("__calendarCalls[0].op")=='move'
        if not edit_title:
            assert await b.js("__calendarCalls[0].ics===__originalIcs")
        else:
            assert await b.js("__calendarCalls[0].ics.includes('X-PRIVATE:preserve') && __calendarCalls[0].ics.includes('BEGIN:VALARM') && __calendarCalls[0].ics.includes('RRULE:FREQ=DAILY')")
        # The formatter rounds DTSTAMP to minutes. Advance its clock across a minute boundary,
        # without delaying the suite for a real minute or changing any event input.
        await b.js("window.__RealDate=Date;window.Date=class extends __RealDate{constructor(...a){super(...(a.length?a:[__RealDate.now()+120000]));}static now(){return __RealDate.now()+120000;}}")
        await asyncio.sleep(1.1)
        await b.js("document.querySelector('#cev-save').click()")
        await b.until("__calendarCalls.length===2 && !document.querySelector('#cev-save')")
        assert await b.js("JSON.stringify(__calendarCalls[0])===JSON.stringify(__calendarCalls[1])")
        assert await b.js("__calendarData.a.length") == 0
        assert await b.js("__calendarData.b.length") == 1
        # The list repaints AFTER the dialog closes. Asserted immediately, this lost the race on the
        # slower CI runner (desktop build for 90f75c97) with the move itself already correct above.
        await b.until("!!document.querySelector('.cal-edit[data-cal=b][data-uid=original]')")
    asyncio.run(desktop.with_browser('online','',check,BOUNDARIES))

@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(),reason='Chrome required')
def test_default_is_account_scoped_and_stale_menu_cannot_write_new_account():
    async def check(b):
        await calendar(b)
        await b.js("window.__originalMe=__PC.me;window.__firstAccount=__PC.me().pubkey;document.querySelector('#cal-menu').click();document.querySelector('#cal-default').value='b';document.querySelector('#cal-default').dispatchEvent(new Event('change'))")
        # Only the authentication boundary changes; the actual menu callback remains open.
        await b.js("__PC.me=()=>({pubkey:'other-calendar-account'});document.querySelector('#cal-default').value='a';document.querySelector('#cal-default').dispatchEvent(new Event('change'))")
        assert await b.js("localStorage.getItem('pc_cal_default:other-calendar-account')") is None
        assert await b.js("localStorage.getItem('pc_cal_default:'+__firstAccount)")=='b'
        await b.js("__PC.closeModal();document.querySelector('#cal-new').click()")
        assert await b.js("document.querySelector('#cev-cal').value")=='a'
        await b.js("__PC.closeModal();__PC.me=__originalMe;document.querySelector('#cal-new').click()")
        assert await b.js("document.querySelector('#cev-cal').value")=='b'
    asyncio.run(desktop.with_browser('online','',check,BOUNDARIES))

@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(),reason='Chrome required')
def test_pending_offline_edit_blocks_move_without_deleting_or_queueing_more():
    async def check(b):
        await calendar(b)
        await b.js(r'''new Promise((resolve,reject)=>{
          const request=indexedDB.open('pccal',1);
          request.onsuccess=()=>{const db=request.result,tx=db.transaction('cal','readwrite');
            tx.objectStore('cal').put([{op:'put',cal:'a',uid:'original',ics:__originalIcs}], 'queue:'+__PC.me().pubkey);
            tx.oncomplete=()=>{db.close();resolve();};tx.onerror=()=>reject(tx.error);};request.onerror=()=>reject(request.error);
        })''')
        await b.js("document.querySelector('.cal-edit[data-uid=original]').click();document.querySelector('#cev-cal').value='b';document.querySelector('#cev-save').click()")
        await b.until("!!document.querySelector('#cev-save') && !document.querySelector('#cev-save').disabled && document.body.textContent.includes('pending offline changes')")
        assert await b.js('__calendarCalls')==[]
        assert await b.js('__calendarData.a.length')==1
        assert await b.js('__calendarData.b.length')==0
    asyncio.run(desktop.with_browser('online','',check,BOUNDARIES))
