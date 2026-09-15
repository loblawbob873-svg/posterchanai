"""The real desktop centre expires cached reminders, including offline reloads."""
import asyncio
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
        await browser.call('Page.reload')
        await browser.until("!!window.__PC?.me() && !!document.querySelector('#os-bell')")
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
        await browser.call('Page.reload')
        await browser.until("!!window.__PC?.me() && !!document.querySelector('#os-bell')")
        assert await browser.js('__PC.notifUnread()') == 0
        await browser.js("document.querySelector('#os-bell').click()")
        await browser.until("document.querySelectorAll('#os-noti .reminder-notif').length===1")
        await browser.js("document.querySelector('#os-noti .reminder-notif').click()")
        await browser.until("__PC.isView('calendar')")
        assert await browser.js("document.querySelector('#os-noti')===null")
        assert await browser.js("!!document.querySelector('#feed') && document.querySelector('#feed').textContent.length>0")
    asyncio.run(desktop.with_browser('online', '', check))
