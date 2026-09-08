"""Measure the real bundled sign-in panel and QR at desktop/scaled/mobile sizes."""
import asyncio
from pathlib import Path
import pytest
from tests.client import test_desktop_offline_full_app as desktop

@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()

@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('width,height,scale', [(1440,900,1),(1024,768,1),(960,540,2),(390,844,1),(320,640,1)])
def test_phone_signer_qr_stays_inside_panel(width,height,scale):
    async def check(b):
        await b.until("document.body.classList.contains('guest')")
        await b.js("__PC.showAuth()")
        await desktop.login(b)
        await b.until("document.readyState==='complete' && PCOS.isOn() && !document.querySelector('#app').classList.contains('hidden') && document.querySelector('#auth-gate').classList.contains('hidden')")
        await b.call('Emulation.setDeviceMetricsOverride',dict(width=width,height=height,deviceScaleFactor=scale,mobile=False))
        # Select the production phone-signer pane; replace only the handshake image payload.
        await b.js("""__PC.showAuth();
          document.querySelectorAll('.auth-pane').forEach(el=>el.classList.add('hidden'));
          document.querySelector('#auth-amber').classList.remove('hidden');
          document.querySelector('#amber-nc-box').classList.remove('hidden');
          const qr=document.querySelector('#amber-nc-qr');
          qr.src='data:image/svg+xml,'+encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="404" height="404"><rect width="404" height="404" fill="white"/></svg>');
          qr.classList.remove('hidden');""")
        await asyncio.sleep(.1)
        result=await b.js("""(()=>{const qr=document.querySelector('#amber-nc-qr'),q=qr.getBoundingClientRect(),box=qr.parentElement.getBoundingClientRect(),card=qr.closest('.auth-card').getBoundingClientRect();return {width:q.width,height:q.height,left:q.left,right:q.right,boxLeft:box.left,boxRight:box.right,cardLeft:card.left,cardRight:card.right,viewport:innerWidth}})()""")
        assert result['width']>100 and abs(result['width']-result['height'])<1,result
        assert result['left']>=result['boxLeft'] and result['right']<=result['boxRight']+1,result
        assert result['cardLeft']>=0 and result['cardRight']<=result['viewport']+1,result
        if width>=1024: assert result['width']>=370,result
    asyncio.run(desktop.with_browser('online','',check))
