"""Concord × git in the real bundled client, at phone and desktop width.

A room whose #ngit channel carries Armada's repository attachment. The repository's relay is a fixture
behind `window.PCConcordRelay` (the door every Concord relay call goes through) answering with REAL
signed NIP-34 events — the module checks every signature, so unsigned fixtures would prove nothing.

Checked, each by what is on screen and what was published:
  * the channel shows the repository's issue, comment and status rows between its chat messages, and a
    detached interval's activity does not appear;
  * the subject opens the ticket panel: body, comments, status actions for a maintainer, a composer
    whose comment goes out as Armada's NIP-22 shape;
  * Projects lists the issue, filters by state, and New issue publishes Armada's 1621 with labels;
  * another channel's git activity raises its unread dot;
  * nothing scrolls sideways, every control we press is actually hittable, and the panels fit the
    viewport — at 390px and at 1280px.
"""
import asyncio
import base64
import json
import os
import subprocess
from pathlib import Path

import pytest
from tests.client import test_desktop_offline_full_app as desktop

ROOT = Path(__file__).resolve().parents[2]

FIXTURE = r"""
import fs from 'node:fs';import vm from 'node:vm';
globalThis.window=globalThis;vm.runInThisContext(fs.readFileSync('static/vendor/nostr/nostr.bundle.js','utf8'));
const NT=NostrTools,hex=b=>Buffer.from(b).toString('hex');
const viewer={sk:new Uint8Array(32).fill(1)};viewer.pk=NT.getPublicKey(viewer.sk);
const owner={sk:new Uint8Array(32).fill(2)};owner.pk=NT.getPublicKey(owner.sk);
const alice={sk:new Uint8Array(32).fill(3)};alice.pk=NT.getPublicKey(alice.sk);
const now=Math.floor(Date.now()/1000),attachedAt=now-7200;
const coord='30617:'+owner.pk+':ngit',other='30617:'+owner.pk+':tools';
const s=(who,kind,tags,content,at)=>NT.finalizeEvent({kind,created_at:at,tags,content},who.sk);
const ann=s(owner,30617,[['d','ngit'],['name','ngit'],['description','nostr git'],['relays','wss://git.fixture.invalid'],['maintainers',viewer.pk],['clone','https://git.fixture.invalid/ngit.git'],['web','https://git.fixture.invalid/ngit']],'',now-9000);
const annTools=s(owner,30617,[['d','tools'],['name','tools'],['relays','wss://git.fixture.invalid']],'',now-9000);
const issue=s(alice,1621,[['a',coord],['subject','Push hangs on large packs'],['t','bug']],'Steps:\n\n1. push a **big** repo\n<script>window.__xss=1</script>',now-3600);
const comment=s(owner,1111,[['E',issue.id,'',alice.pk],['K','1621'],['P',alice.pk],['e',issue.id,'',alice.pk],['k','1621'],['p',alice.pk]],'Confirmed on 3.0.3',now-3000);
const status=s(owner,1632,[['e',issue.id,'','root'],['a',coord]],'',now-2400);
const old=s(alice,1621,[['a',coord],['subject','Before the channel existed']],'',now-86400*3);
const toolsIssue=s(alice,1621,[['a',other],['subject','Tools issue']],'',now-600);
console.log(JSON.stringify({viewer:viewer.pk,coord,other,attachedAt,ids:{issue:issue.id,old:old.id,tools:toolsIssue.id},
  events:[ann,annTools,issue,comment,status,old,toolsIssue]}));
"""


def fixture():
    r = subprocess.run(["node", "--input-type=module", "-e", FIXTURE], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


async def shot(b, name):
    """PC_SHOTS=<dir> saves what the test saw, for a person to look at."""
    out = os.environ.get('PC_SHOTS')
    if out:
        data = await b.call('Page.captureScreenshot', dict(format='png'))
        Path(out, name + '.png').write_bytes(base64.b64decode(data['data']))


async def press(b, selector, scope='document'):
    target = await b.js('''(()=>{const el=%s.querySelector(%s);if(!el)return {missing:true};el.scrollIntoView({block:'center'});
      const r=el.getBoundingClientRect(),x=r.x+r.width/2,y=r.y+r.height/2;
      return {x,y,hit:r.width>0&&r.height>0&&el.contains(document.elementFromPoint(x,y))};})()''' % (scope, json.dumps(selector)))
    assert not target.get('missing'), f'{selector} is not on screen'
    assert target['hit'], f'{selector} is hidden, clipped or covered'
    for kind in ('mousePressed', 'mouseReleased'):
        await b.call('Input.dispatchMouseEvent', dict(type=kind, x=target['x'], y=target['y'], button='left', clickCount=1))


async def no_sideways_scroll(b, where):
    """What a person can actually scroll sideways: the page, the message pane, and our panels. (The
    theme's decorative skyline is wider than the body by design and is clipped, so body.scrollWidth
    is not the question.)"""
    w = await b.js("""(()=>{const out={page:[document.scrollingElement.scrollWidth,innerWidth]};
      for(const s of ['.cc-messages','#ccg-panel .ccg-panel-body','#ccg-projects .ccg-proj-body']){const e=document.querySelector(s);if(e&&e.getBoundingClientRect().width)out[s]=[e.scrollWidth,e.clientWidth];}
      return out;})()""")
    for k, (sw, cw) in w.items():
        if sw > cw + 1:
            wide = await b.js("[...document.querySelectorAll(%s)].filter(e=>e.getBoundingClientRect().right>innerWidth+1).slice(0,6).map(e=>e.tagName+'.'+e.className+' '+Math.round(e.getBoundingClientRect().right))" % json.dumps((k if k != 'page' else 'body') + ' *'))
            assert False, f'{where}: {k} scrolls sideways {sw}>{cw} {wide}'


async def inside_viewport(b, selector, where):
    r = await b.js("(()=>{const r=document.querySelector(%s).getBoundingClientRect();return [r.left,r.top,r.right,r.bottom,innerWidth,innerHeight];})()" % json.dumps(selector))
    assert r[0] >= -1 and r[1] >= -1 and r[2] <= r[4] + 1 and r[3] <= r[5] + 1, f'{where}: {selector} leaves the viewport {r}'


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('width', [1280, 390])
def test_repository_channel_panel_and_projects(width):
    fx = fixture()

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', dict(width=width, height=860, deviceScaleFactor=1, mobile=width < 600))
        await desktop.login(b)
        assert await b.js('__PC.me().pubkey') == fx['viewer']
        await b.js(r'''(()=>{
          const FX=%s;
          const ngit={id:'ch-ngit',name:'ngit',streamPubkeys:[],repositories:[{address:FX.coord,relayHints:['wss://git.fixture.invalid'],attachedAt:FX.attachedAt}]};
          const tools={id:'ch-tools',name:'tools',streamPubkeys:[],repositories:[{address:FX.other,relayHints:['wss://git.fixture.invalid'],attachedAt:FX.attachedAt}]};
          const general={id:'ch-general',name:'general',streamPubkeys:[]};
          const room={name:'Git room',communityId:'c'.repeat(64),naddr:'fixture-community',url:'https://fixture.invalid/invite/x#s',
            channels:[general,ngit,tools],cord:{bundle:{owner:FX.viewer,relays:['wss://fixture.invalid']},hydrated:true}};
          localStorage.setItem('pc.concord.rooms.v1.'+__PC.me().pubkey,JSON.stringify([room]));
          localStorage.setItem('pc.concord.active.v1.'+__PC.me().pubkey,'0');
          window.PosterCordReader={inspectControl:()=>({controlPubkeys:[],channels:[general,ngit,tools]}),
            inspectChat:async()=>({messages:[],reactions:[],reactionIds:[]})};
          window.__published=[];window.__reads=[];
          const match=(ev,f)=>(!f.kinds||f.kinds.includes(ev.kind))&&(!f.authors||f.authors.includes(ev.pubkey))
            &&Object.keys(f).filter(k=>k[0]==='#').every(k=>ev.tags.some(t=>t[0]===k.slice(1)&&f[k].includes(t[1])));
          const door={queryFrom:async(urls,filters)=>{__reads.push(urls);const all=[...FX.events,...__published];return all.filter(ev=>filters.some(f=>match(ev,f)));},
            publishTo:async(urls,ev)=>{__published.push(ev);return {ok:true,accepted:1};},
            subscribeFrom:()=>({close(){}})};
          Object.defineProperty(window,'PCConcordRelay',{configurable:true,get(){return door;}});
          __PC.switchMessagesTab('concord');
        })()''' % json.dumps(fx))
        await b.until("!!document.querySelector('.cc-channel-list')")
        # The channel list marks repository channels and offers Projects.
        assert await b.js("document.querySelectorAll('.ccg-chan-badge').length") == 2
        assert await b.js("!!document.querySelector('#ccg-projects-open')")
        # The tools channel's issue (by someone else, never read) shows as unread once fetched.
        await b.until("[...document.querySelectorAll('[data-cc-channel=\"tools\"]')].some(x=>x.classList.contains('unread'))")

        await press(b, '[data-cc-channel="ngit"]')
        await b.until("document.querySelectorAll('.ccg-row').length>=3")
        rows = await b.js("[...document.querySelectorAll('.ccg-row')].map(r=>r.className.split(' ')[1])")
        assert rows == ['ccg-ticket', 'ccg-comments', 'ccg-notice'], rows
        text = await b.js("document.querySelector('.cc-messages').innerText")
        assert 'Push hangs on large packs' in text and 'Confirmed on 3.0.3' in text and 'closed' in text
        assert 'Before the channel existed' not in text, 'opened before the attachment yet shown in the channel'
        assert await b.js("!!document.querySelector('#ccg-head-issues')") or width < 600
        await no_sideways_scroll(b, 'channel with git rows')
        await shot(b, f'{width}-1-channel')

        # The ticket panel.
        await press(b, '.ccg-ticket .ccg-subject')
        await b.until("!!document.querySelector('#ccg-panel .ccg-md')")
        await inside_viewport(b, '#ccg-panel', 'ticket panel')
        await shot(b, f'{width}-2-panel')
        assert await b.js("document.querySelector('#ccg-panel h3').textContent") == 'Push hangs on large packs'
        assert await b.js("!!document.querySelector('#ccg-panel .ccg-md b')"), 'markdown not rendered'
        assert not await b.js("!!window.__xss||!!document.querySelector('#ccg-panel script')"), 'ticket body markup executed'
        assert await b.js("document.querySelector('#ccg-panel .ccg-status').textContent") == 'Closed'
        # The viewer is a maintainer of the newest announcement: Reopen is offered.
        assert await b.js("[...document.querySelectorAll('#ccg-panel [data-ccg-status]')].map(x=>x.textContent)") == ['Reopen']
        await b.js("document.querySelector('#ccg-reply').value='Fixed in main'")
        await press(b, '[data-ccg-send]', 'document.querySelector("#ccg-panel")')
        await b.until("__published.some(e=>e.kind===1111)")
        sent = await b.js("__published.find(e=>e.kind===1111)")
        assert [t[:2] for t in sent['tags'][:6]] == [['E', fx['ids']['issue']], ['K', '1621'], ['P', sent['tags'][2][1]], ['e', fx['ids']['issue']], ['k', '1621'], ['p', sent['tags'][5][1]]]
        await b.until("document.querySelector('#ccg-panel').innerText.includes('Fixed in main')")
        await press(b, '[data-ccg-status]', 'document.querySelector("#ccg-panel")')
        await b.until("__published.some(e=>e.kind===1630)")
        await b.until("document.querySelector('#ccg-panel .ccg-status').textContent==='Open'")
        await press(b, '[data-ccg-close]', 'document.querySelector("#ccg-panel")')
        await b.until("!document.querySelector('#ccg-panel')")

        # Projects (the phone reaches it from the channel drawer).
        if width < 600:
            await press(b, '#cc-back-channels')
            await b.until("!!document.querySelector('#ccg-projects-open')&&document.querySelector('#ccg-projects-open').getBoundingClientRect().width>0")
        await press(b, '#ccg-projects-open')
        await b.until("!!document.querySelector('#ccg-projects .ccg-stats')")
        await inside_viewport(b, '#ccg-projects', 'Projects')
        await no_sideways_scroll(b, 'Projects')
        await shot(b, f'{width}-3-projects')
        await press(b, '[data-ccg-tab="issues"]', 'document.querySelector("#ccg-projects")')
        await b.until("!!document.querySelector('#ccg-projects .ccg-item')")
        await shot(b, f'{width}-4-issues')
        subjects = await b.js("[...document.querySelectorAll('#ccg-projects .ccg-item b')].map(x=>x.textContent)")
        assert 'Before the channel existed' in subjects, 'Projects must ignore attachment intervals'
        await press(b, '[data-ccg-new-issue]', 'document.querySelector("#ccg-projects")')
        await b.until("!!document.querySelector('#ccg-ni-title')")
        await b.js("document.querySelector('#ccg-ni-title').value='Docs typo';document.querySelector('#ccg-ni-body').value='In README'")
        await press(b, '[data-ccg-addlabel="documentation"]')
        await shot(b, f'{width}-5-new-issue')
        await press(b, '#ccg-ni-send')
        await b.until("__published.some(e=>e.kind===1621)")
        issue = await b.js("__published.find(e=>e.kind===1621)")
        assert issue['tags'][1] == ['subject', 'Docs typo'] and ['t', 'documentation'] in issue['tags']
        assert issue['tags'][0][0] == 'a' and issue['tags'][0][1] in (fx['coord'], fx['other'])
        assert ['client', 'PosterChan'] in issue['tags']
        await b.until("[...document.querySelectorAll('#ccg-projects .ccg-item b')].some(x=>x.textContent==='Docs typo')")
        await no_sideways_scroll(b, 'Projects after a new issue')
        # Every relay this ever asked is the repository's, never the community's own.
        reads = await b.js("[...new Set(__reads.flat())]")
        assert 'wss://fixture.invalid' not in reads, reads

    extra = "localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"
    asyncio.run(desktop.with_browser('online', '', check, extra))
