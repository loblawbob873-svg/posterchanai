"""The real desktop centre expires cached reminders, including offline reloads."""
import asyncio
import json
from pathlib import Path

import pytest
from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_desktop_calendar_history_expires_offline_and_opens_calendar():
    async def check(browser):
        await desktop.login(browser)
        await browser.until("!!document.querySelector('#os-bell')")
        await browser.js("""(()=>{
          const owner=String(window.__PC_API_BASE__||location.origin).replace(/\\/+$/,'')+':'+__PC.me().pubkey;
          const now=Math.floor(Date.now()/1000);
          const row=(id,age,content)=>({type:'reminder',id:'reminder:'+id,created_at:now-age,content,route:'calendar',alerted:true});
          localStorage.setItem('pc_reminder_history:'+owner,JSON.stringify([
            row('old-one',10*86400,'Expired calendar appointment one'),
            row('old-two',30*86400,'Expired calendar appointment two'),
            row('current',60,'Recent calendar appointment')]));
          localStorage.setItem('pc_reminder_history:'+owner+':another-account',JSON.stringify([
            row('foreign',30,'Another account calendar appointment')]));
          localStorage.removeItem('pc_reminder_history_days:'+owner);
          localStorage.setItem('pc_notif_seen','0');
          localStorage.setItem('__offlineMode','reject');
        })()""")
        previous_document = await browser.js('__documentIdentity')
        await browser.call('Page.reload')
        await browser.until("window.__documentIdentity!=="+json.dumps(previous_document)+" && !!window.__PC?.me() && !!document.querySelector('#os-bell')")
        assert await browser.js("__instanceMode") == 'reject'
        unread = await browser.js('__PC.notifUnread()')
        await browser.js("document.querySelector('#os-bell').click()")
        await browser.until("!!document.querySelector('#os-noti')")
        rows = await browser.js("[...document.querySelectorAll('#os-noti .reminder-notif')].map(x=>x.textContent)")
        assert len(rows) == 1 and 'Recent calendar appointment' in rows[0], rows
        assert unread == 1, 'expired cached reminders inflated the unread count'
        assert await browser.js('__PC.notifUnread()') == 0
        assert await browser.js("document.querySelector('#os-bell .os-dot')===null")
        # A second offline document must retain both expiry and the acknowledgement.
        previous_document = await browser.js('__documentIdentity')
        await browser.call('Page.reload')
        await browser.until("window.__documentIdentity!=="+json.dumps(previous_document)+" && !!window.__PC?.me() && !!document.querySelector('#os-bell')")
        # The shell may paint before the fixture's asynchronous open callback is attempted.
        # Wait for that prerequisite; its held event still cannot initialize notification watching.
        await browser.until('window.__heldReminderOpens?.length>0')
        assert await browser.js('__PC.notifUnread()') == 0, await browser.js("({seen:localStorage.getItem('pc_notif_seen'),rows:__PC.notifItems(60)})")
        await browser.js("document.querySelector('#os-bell').click()")
        await browser.until("document.querySelectorAll('#os-noti .reminder-notif').length===1")
        # Restore the fixture server for the uncached Calendar module before following its route.
        await browser.js("__heldReminderOpens.splice(0).forEach(open=>open());__restoreInstance()")
        await browser.js("document.querySelector('#os-noti .reminder-notif').click()")
        await browser.until("__PC.isView('calendar')")
        assert await browser.js("document.querySelector('#os-noti')===null")
        await browser.until("document.body.textContent.includes('The calendar server is off on this node')")
    asyncio.run(desktop.with_browser('online', '', check, extra_init=r'''
// Keep offline relay startup pending: the desktop and cached reminders are available before
// watchNotifications runs. Persisted read markers must already work during that interval.
const ReminderSocket=window.WebSocket;
window.__heldReminderOpens=[];
window.WebSocket=class extends ReminderSocket {
  fire(type,data){
    if(type==='open' && localStorage.getItem('__offlineMode')==='reject'){
      this.readyState=0;
      __heldReminderOpens.push(()=>{this.readyState=1;super.fire(type,data);});
      return;
    }
    return super.fire(type,data);
  }
};
'''))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('popup', [False, True], ids=['shell', 'native-popup'])
def test_open_notification_centre_refreshes_expired_rows_without_losing_focus(popup):
    async def check(browser):
        await desktop.login(browser)
        await browser.js(r"""(()=>{
          const owner=String(window.__PC_API_BASE__||location.origin).replace(/\/+$/,'')+':'+__PC.me().pubkey;
          const now=Math.floor(Date.now()/1000);
          const rows=Array.from({length:35},(_,i)=>({type:'reminder',id:'reminder:current-'+i,created_at:now-60-i,content:'Current appointment '+i,route:'calendar'}));
          rows.unshift({type:'reminder',id:'reminder:expiring',created_at:now-2*86400,content:'Appointment outside shortened history',route:'calendar'});
          localStorage.setItem('pc_reminder_history:'+owner,JSON.stringify(rows));
          localStorage.setItem('pc_reminder_history_days:'+owner,'7');
          localStorage.setItem('__holdReminderHistory','1');
        })()""")
        route = '?pcpopup=noti' if popup else ''
        await browser.js('location.href=location.origin+"/index.html"+'+json.dumps(route))
        await browser.until('!!window.__PC?.me() && !!window.PCOS')
        if not popup:
            await browser.until("!!document.querySelector('#os-bell')")
            await browser.js("document.querySelector('#os-bell').click()")
        await browser.until("document.querySelectorAll('#os-noti .reminder-notif').length===36")
        await browser.js("""(()=>{
          const list=document.querySelector('#os-noti .os-noti-list');
          list.scrollTop=120;
          window.__historyScroll=list.scrollTop;
          document.querySelector('#os-noti-ding').focus({preventScroll:true});
          window.__unchangedPanel=document.querySelector('#os-noti');
          PCOS.notifChanged();
        })()""")
        # Wait for the real debounce without a speed assertion; unchanged updates keep DOM identity.
        await browser.js('new Promise(resolve=>setTimeout(resolve,300))')
        assert await browser.js("document.querySelector('#os-noti')===__unchangedPanel")
        await browser.until("typeof window.__releaseReminderHistory==='function'")
        await browser.js('__releaseReminderHistory()')
        await browser.until("document.querySelectorAll('#os-noti .reminder-notif').length===35")
        assert await browser.js("!document.querySelector('#os-noti').textContent.includes('Appointment outside shortened history')")
        assert await browser.js("document.activeElement.id==='os-noti-ding'")
        assert await browser.js("__historyScroll>0 && document.querySelector('#os-noti .os-noti-list').scrollTop===__historyScroll")
        assert await browser.js("document.querySelectorAll('#os-noti').length===1")
        await browser.js("document.querySelector('#os-noti-ding').click()")
        assert await browser.js("document.activeElement.id==='os-noti-ding'")
        assert await browser.js("document.querySelector('#os-noti .os-noti-list').scrollTop===__historyScroll")
        assert await browser.js("document.querySelector('#os-noti-ding').title==='Unmute the arrival sound'")
        await browser.js("document.querySelector('#os-noti-perm').focus({preventScroll:true});document.querySelector('#os-noti-perm').click()")
        await browser.until("!document.querySelector('#os-noti-perm')")
        assert await browser.js("document.activeElement.id==='os-noti-all'")
        assert await browser.js("document.querySelector('#os-noti .os-noti-list').scrollTop===__historyScroll")


    asyncio.run(desktop.with_browser('online', '', check, extra_init=r'''
window.Notification=class {
  static permission='default';
  static async requestPermission(){this.permission='granted';return 'granted';}
  close(){}
};
const historyFetch=window.fetch;
window.fetch=function(url,opts){
  if(localStorage.getItem('__holdReminderHistory') && String(url).includes('/api/auth/reminder-notifications')){
    return new Promise(resolve=>{window.__releaseReminderHistory=()=>resolve(new Response(
      JSON.stringify({items:[],history_days:1}),{status:200,headers:{'Content-Type':'application/json'}}));});
  }
  return historyFetch(url,opts);
};
'''))
