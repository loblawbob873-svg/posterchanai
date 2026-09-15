"""Native browser JSON numbers stay exact through the shipped CORD boundaries."""
import asyncio
import json
from pathlib import Path

import pytest
from tests.client import test_desktop_offline_full_app as desktop

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_browser_lossless_membership_storage_and_invite_boundary():
    source = (ROOT / 'static/js/client/concord.js').read_text()
    helpers = source[source.index('  function cordJsonParse('):source.index('  function cordListMaterial(')]

    async def check(browser):
        await desktop.login(browser)
        await browser.js('''(async()=>{if(!window.PosterCordReader){await new Promise((resolve,reject)=>{const s=document.createElement('script');s.src='/static/js/client/cord-reader.js';s.onload=resolve;s.onerror=reject;document.head.append(s);});}})()''')
        result = await browser.js('(()=>{' + helpers + r'''
          const token='18446744073709551615';
          const doc=cordJsonParse('{"opaque":'+token+',"text":"'+token+'"}');
          localStorage.setItem('cord-number-fixture',JSON.stringify(doc));
          const restored=cordJsonParse(localStorage.getItem('cord-number-fixture'));
          localStorage.removeItem('cord-number-fixture');
          const R=PosterCordReader,invite=R.parseJoinMaterial('{"root_epoch":'+token+',"channels":[{"epoch":18446744073709551614}]}');
          const wire=R.stringifyJoinMaterial(invite);
          let refused=false;try{cordU64(Number(token));}catch(_){refused=true;}
          return {storage:JSON.stringify(restored),root:invite.root_epoch,wire,refused};
        })()''')
        assert result['storage'] == '{"opaque":18446744073709551615,"text":"18446744073709551615"}'
        assert result['root'] == '18446744073709551615'
        assert json.loads(result['wire']) == {'root_epoch': 18446744073709551615, 'channels': [{'epoch': 18446744073709551614}]}
        assert result['refused']

    asyncio.run(desktop.with_browser('online', '', check))
