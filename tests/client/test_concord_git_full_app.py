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


async def on_top(b, selector, where):
    """Present is not visible: at 2026-10-22 the Projects panel opened UNDER the windowed desktop (.os-root, z 300) and
    "Issues and Pull Requests does nothing on desktop" -- while every check here passed, because they ran with the
    desktop switched off and only asked whether the element existed. This asks what a click at its centre would hit."""
    hit = await b.js("""(()=>{const el=document.querySelector(%s);if(!el)return 'missing';const r=el.getBoundingClientRect();
      const top=document.elementFromPoint(r.x+r.width/2,r.y+Math.min(r.height/2,60));return el.contains(top)?'ok':(top?top.tagName+'.'+top.className:'nothing');})()""" % json.dumps(selector))
    assert hit == 'ok', f'{where}: {selector} is covered by {hit}'


async def _install(b, fx, os_mode):
    """A room with two repository channels and the fixture's issues, read through a stub relay door."""
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
          if(%s)__PC.switchView('concord');else __PC.switchMessagesTab('concord');
        })()''' % (json.dumps(fx), 'true' if os_mode else 'false'))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('width,os_mode', [(1280, False), (390, False), (1280, True)])
def test_repository_channel_panel_and_projects(width, os_mode):
    fx = fixture()

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', dict(width=width, height=860, deviceScaleFactor=1, mobile=width < 600))
        await desktop.login(b)
        assert await b.js('__PC.me().pubkey') == fx['viewer']
        await _install(b, fx, os_mode)
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
        await on_top(b, '#ccg-projects', 'Projects')
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

        await b.js("PCConcordGit.closeProjects()")
        await b.until("!document.querySelector('#ccg-projects')")
        if width < 600:   # back from the channel drawer into the room
            await press(b, '[data-cc-channel="ngit"]')
            await b.until("!!document.querySelector('#cc-head-more')&&document.querySelector('#cc-head-more').getBoundingClientRect().width>0")
        # The bug button (Issues and pull requests), reached the way a person reaches it: the room menu ☰.
        # 2026-10-21: "clicking bugs in the posterchan room does nothing, you did not test it".
        await press(b, '#cc-head-more')
        await b.until("[...document.querySelectorAll('button,[role=menuitem]')].some(x=>x.textContent.trim()==='Issues and pull requests'&&x.getBoundingClientRect().width>0)")
        row = await b.js("(()=>{const el=[...document.querySelectorAll('button,[role=menuitem]')].find(x=>x.textContent.trim()==='Issues and pull requests'&&x.getBoundingClientRect().width>0);el.setAttribute('data-t-issues','1');return true;})()")
        await press(b, '[data-t-issues]')
        await b.until("!!document.querySelector('#ccg-projects')")
        assert await b.js("document.querySelector('#ccg-projects [data-ccg-tab=\"issues\"]').getAttribute('aria-selected')") == 'true'
        await b.until("!!document.querySelector('#ccg-projects .ccg-item')")
        await on_top(b, '#ccg-projects', 'Issues and pull requests')
        if os_mode:
            assert await b.js("!!document.querySelector('.os-root')"), 'the windowed desktop never came up'

    extra = "localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:%s}));" % ('true' if os_mode else 'false')
    asyncio.run(desktop.with_browser('online', '', check, extra))


PICKER_FIXTURE = r"""
import fs from 'node:fs';import vm from 'node:vm';
globalThis.window=globalThis;vm.runInThisContext(fs.readFileSync('static/vendor/nostr/nostr.bundle.js','utf8'));
const NT=NostrTools;const viewer={sk:new Uint8Array(32).fill(1)};viewer.pk=NT.getPublicKey(viewer.sk);
const other={sk:new Uint8Array(32).fill(5)};const busy={sk:new Uint8Array(32).fill(6)};
const now=Math.floor(Date.now()/1000);
const ann=(who,d,at)=>NT.finalizeEvent({kind:30617,created_at:at,tags:[['d',d],['name',d],['relays','wss://git.fixture.invalid']],content:''},who.sk);
const events=[ann(viewer,'posterchanai',now-86400*400),ann(other,'oldrepo',now-86400*300)];
for(let i=0;i<600;i++)events.push(ann(busy,'busy-'+i,now-i));
console.log(JSON.stringify({events}));
"""


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_the_repository_picker_finds_an_old_repository():
    """2026-10-22, "posterchan not even found in Connect a git repository": every relay held its announcement, but the
    picker listed the NEWEST thousand (index.ngit.dev: 500) and only filtered that list as you typed. The stub relay
    here behaves the same way -- newest first, capped at 500, NIP-50 `search` answered -- with 600 newer
    announcements in front of the two being looked for."""
    r = subprocess.run(["node", "--input-type=module", "-e", PICKER_FIXTURE], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    fx = json.loads(r.stdout)

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', dict(width=1280, height=860, deviceScaleFactor=1, mobile=False))
        await desktop.login(b)
        await b.js(r'''(()=>{
          const FX=%s;
          const match=(ev,f)=>(!f.kinds||f.kinds.includes(ev.kind))&&(!f.authors||f.authors.includes(ev.pubkey))
            &&(!f.search||ev.tags.some(t=>t[0]==='name'&&t[1].toLowerCase().includes(f.search.toLowerCase())));
          const door={queryFrom:async(urls,filters)=>{const out=new Map();
              for(const f of filters){const hit=FX.events.filter(ev=>match(ev,f)).sort((a,b)=>b.created_at-a.created_at).slice(0,Math.min(f.limit||500,500));
                for(const ev of hit)out.set(ev.id,ev);}
              return [...out.values()];},
            publishTo:async()=>({ok:true,accepted:1}),subscribeFrom:()=>({close(){}})};
          Object.defineProperty(window,'PCConcordRelay',{configurable:true,get(){return door;}});
          __PC.switchMessagesTab('concord');
        })()''' % json.dumps(fx))
        await b.until("!!window.PCConcordGit")
        await b.js("window.__picked=null;PCConcordGit.pickRepository().then(v=>{window.__picked=v;});true")
        await b.until("!!document.querySelector('#ccg-pick-q')")
        for text, want in (('posterchan', 'posterchanai'), ('oldrepo', 'oldrepo')):
            await b.js("(()=>{const q=document.querySelector('#ccg-pick-q');q.value=%s;q.dispatchEvent(new Event('input'));})()" % json.dumps(text))
            await b.until("[...document.querySelectorAll('#ccg-pick-list .ccg-pick-row')].some(x=>x.textContent.includes(%s))" % json.dumps(want))

    extra = "localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"
    asyncio.run(desktop.with_browser('online', '', check, extra))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_projects_paints_from_the_repository_relays_without_waiting_for_the_index():
    """2026-10-22: "the projects page after you add a git repo is empty ... all 0's", then "finally loaded -- took
    forever". The refresh ran in strict sequence and its FIRST step asked index.ngit.dev -- an outside relay, i.e. a
    Tor-tunnelled dial -- for the announcement, waiting up to 8s before even asking the repository's own relays for
    its issues. Here those relays answer at once and the index only at its timeout: the issues must be on screen
    in well under the index's timeout."""
    fx = fixture()

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', dict(width=1280, height=860, deviceScaleFactor=1, mobile=False))
        await desktop.login(b)
        await b.js(r'''(()=>{
          const FX=%s;
          const ngit={id:'ch-ngit',name:'ngit',streamPubkeys:[],repositories:[{address:FX.coord,relayHints:['wss://git.fixture.invalid'],attachedAt:FX.attachedAt}]};
          const general={id:'ch-general',name:'general',streamPubkeys:[]};
          const room={name:'Git room',communityId:'c'.repeat(64),naddr:'fixture-community',url:'https://fixture.invalid/invite/x#s',
            channels:[general,ngit],cord:{bundle:{owner:FX.viewer,relays:['wss://fixture.invalid']},hydrated:true}};
          localStorage.setItem('pc.concord.rooms.v1.'+__PC.me().pubkey,JSON.stringify([room]));
          localStorage.setItem('pc.concord.active.v1.'+__PC.me().pubkey,'0');
          window.PosterCordReader={inspectControl:()=>({controlPubkeys:[],channels:[general,ngit]}),
            inspectChat:async()=>({messages:[],reactions:[],reactionIds:[]})};
          const match=(ev,f)=>(!f.kinds||f.kinds.includes(ev.kind))&&(!f.authors||f.authors.includes(ev.pubkey))
            &&Object.keys(f).filter(k=>k[0]==='#').every(k=>ev.tags.some(t=>t[0]===k.slice(1)&&f[k].includes(t[1])));
          // The index answers only when the caller gives up on it, as a slow tunnelled dial does.
          const door={queryFrom:async(urls,filters,o)=>{
              const fast=urls.filter(u=>!/index\.ngit\.dev/.test(u));
              if(!fast.length)await new Promise(r=>setTimeout(r,(o&&o.timeout)||8000));
              else if(fast.length<urls.length)await new Promise(r=>setTimeout(r,(o&&o.timeout)||8000));
              return FX.events.filter(ev=>filters.some(f=>match(ev,f)));},
            publishTo:async()=>({ok:true,accepted:1}),subscribeFrom:()=>({close(){}})};
          Object.defineProperty(window,'PCConcordRelay',{configurable:true,get(){return door;}});
          __PC.switchMessagesTab('concord');
        })()''' % json.dumps(fx))
        await b.until("!!document.querySelector('#ccg-projects-open')")
        t0 = await b.js("performance.now()")
        await b.js("document.querySelector('#ccg-projects-open').click()")
        await b.until("!!document.querySelector('#ccg-projects') && document.querySelector('#ccg-projects').innerText.includes('Push hangs on large packs')")
        took = await b.js("performance.now()") - t0
        print('projects painted after ms', round(took))
        assert took < 3000, ('Projects waited on the index relay before showing the repository\'s own issues', round(took))

    extra = "localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"
    asyncio.run(desktop.with_browser('online', '', check, extra))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_the_git_dialogs_buttons_do_what_they_say():
    """Event JSON → Copy hands the event's exact JSON to the clipboard helper; New issue → Attach opens the file picker
    and a picked file is uploaded and linked in the body, and Cancel publishes nothing; the repository picker's Cancel
    answers "nothing picked"; the channel chooser's Cancel attaches nothing."""
    fx = fixture()

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', dict(width=1280, height=860, deviceScaleFactor=1, mobile=False))
        await desktop.login(b)
        await b.js(r'''(()=>{
          const FX=%s;
          const ngit={id:'ch-ngit',name:'ngit',streamPubkeys:[],repositories:[{address:FX.coord,relayHints:['wss://git.fixture.invalid'],attachedAt:FX.attachedAt}]};
          const general={id:'ch-general',name:'general',streamPubkeys:[]};
          const room={name:'Git room',communityId:'c'.repeat(64),naddr:'fixture-community',url:'https://fixture.invalid/invite/x#s',
            channels:[general,ngit],cord:{bundle:{owner:FX.viewer,relays:['wss://fixture.invalid']},hydrated:true}};
          window.__room=room;window.__published=[];window.__copied=[];window.__uploads=[];window.__fileClicks=0;
          localStorage.setItem('pc.concord.rooms.v1.'+__PC.me().pubkey,JSON.stringify([room]));
          localStorage.setItem('pc.concord.active.v1.'+__PC.me().pubkey,'0');
          window.PosterCordReader={inspectControl:()=>({controlPubkeys:[],channels:[general,ngit]}),inspectChat:async()=>({messages:[],reactions:[],reactionIds:[]})};
          const match=(ev,f)=>(!f.kinds||f.kinds.includes(ev.kind))&&(!f.authors||f.authors.includes(ev.pubkey))
            &&Object.keys(f).filter(k=>k[0]==='#').every(k=>ev.tags.some(t=>t[0]===k.slice(1)&&f[k].includes(t[1])));
          const door={queryFrom:async(urls,filters)=>FX.events.filter(ev=>filters.some(f=>match(ev,f))),
            publishTo:async(urls,ev)=>{__published.push(ev);return {ok:true,accepted:1};},subscribeFrom:()=>({close(){}})};
          Object.defineProperty(window,'PCConcordRelay',{configurable:true,get(){return door;}});
          __PC.copyValue=v=>{__copied.push(String(v));return Promise.resolve(true);};
          __PC.uploadBlob=async(f,o)=>{__uploads.push({name:f.name,folder:o&&o.folder});return 'https://blobs.invalid/'+encodeURIComponent(f.name);};
          {const c=HTMLInputElement.prototype.click;HTMLInputElement.prototype.click=function(){if(this.id==='ccg-ni-file'){__fileClicks++;return;}return c.call(this);};}
          __PC.switchMessagesTab('concord');
        })()''' % json.dumps(fx))
        await b.until("!!document.querySelector('[data-cc-channel=\"ngit\"]')")
        await press(b, '[data-cc-channel="ngit"]')
        await b.until("!!document.querySelector('.ccg-ticket .ccg-subject')")

        # Event JSON → Copy.
        await press(b, '.ccg-ticket .ccg-subject')
        await b.until("!!document.querySelector('#ccg-panel [data-ccg-json]')")
        await b.js("document.querySelector('#ccg-panel [data-ccg-json]').click(); 1")
        await b.until("!!document.querySelector('#ccg-json-copy')")
        await b.js("document.querySelector('#ccg-json-copy').click(); 1")
        await b.until("__copied.length===1")
        assert json.loads(await b.js("__copied[0]"))['id'] == fx['ids']['issue']
        await b.js("__PC.closeModal(); PCConcordGit.closePanel(); 1")

        # New issue → Attach, then Cancel.
        await b.js("PCConcordGit.newIssue(__room); 1")
        await b.until("!!document.querySelector('#ccg-ni-attach')")
        await b.js("document.querySelector('#ccg-ni-attach').click(); 1")
        await b.until("__fileClicks===1")
        await b.js(r'''(()=>{const dt=new DataTransfer();dt.items.add(new File(['log line'],'crash.log',{type:'text/plain'}));
          const f=document.querySelector('#ccg-ni-file');f.files=dt.files;f.dispatchEvent(new Event('change',{bubbles:true}));})()''')
        await b.until("document.querySelector('#ccg-ni-body').value.includes('https://blobs.invalid/crash.log')")
        assert await b.js("__uploads") == [{'name': 'crash.log', 'folder': 'Posts'}]
        await b.js("document.querySelector('#ccg-ni-title').value='Never sent'; document.querySelector('#ccg-ni-cancel').click(); 1")
        await b.until("!document.querySelector('#ccg-ni-cancel')")
        assert not await b.js("__published.filter(e=>e.kind===1621).length"), 'Cancel published the issue'

        # Picker → Cancel answers null.
        await b.js("window.__picked='pending';PCConcordGit.pickRepository().then(v=>{window.__picked=v;}); 1")
        await b.until("!!document.querySelector('#ccg-pick-cancel')")
        await b.js("document.querySelector('#ccg-pick-cancel').click(); 1")
        await b.until("window.__picked===null")

        # Connect → pick → channel chooser → Cancel: nothing is attached.
        await b.js(r'''(()=>{window.__edits=[];const bare={...__room,channels:[{id:'ch-general',name:'general',streamPubkeys:[]}]};
          PCConcordGit.connectFlow(bare,async(id,ch)=>{__edits.push([id,ch]);});})()''')
        await b.until("!!document.querySelector('#ccg-pick-list [data-ccg-pick]:not([disabled])')")
        await b.js("document.querySelector('#ccg-pick-list [data-ccg-pick]:not([disabled])').click(); 1")
        await b.until("!!document.querySelector('#ccg-ch-cancel')")
        await b.js("document.querySelector('#ccg-ch-cancel').click(); 1")
        await b.until("!document.querySelector('#ccg-ch-cancel')")
        await asyncio.sleep(.3)
        assert await b.js("__edits.length") == 0, 'the channel chooser\'s Cancel attached the repository'
        assert not await b.js("__errors"), await b.js("__errors")

    extra = "localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"
    asyncio.run(desktop.with_browser('online', '', check, extra))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_android_back_closes_the_open_issue_first_then_follows_the_back_rule():
    """Git issue "Back Arrow Close Issue" (Vyram, 2026-10-27): on a phone, an issue opened from a community channel
    is a sheet over the channel; Android Back must close THAT, then the issues list, and only then do what the Back
    setting says (by default: the channel list). Back pressed through the same handler Android calls."""
    fx = fixture()
    got = {}

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', dict(width=390, height=860, deviceScaleFactor=1, mobile=True))
        await desktop.login(b)
        await _install(b, fx, False)
        await b.until("!!document.querySelector('.cc-channel-list')")
        await press(b, '[data-cc-channel="ngit"]')
        await b.until("document.querySelectorAll('.ccg-row').length>=3")
        await press(b, '.ccg-ticket .ccg-subject')
        await b.until("!!document.querySelector('#ccg-panel .ccg-md')")
        here = "({panel:!!document.querySelector('#ccg-panel'),projects:!!document.querySelector('#ccg-projects'),chat:!!document.querySelector('.cc-app.show-chat')})"
        got['one'] = (await b.js("__PC.concordBack()"), await b.js(here))
        await press(b, '#cc-head-more')
        await b.until("[...document.querySelectorAll('button,[role=menuitem]')].some(x=>x.textContent.trim()==='Issues and pull requests'&&x.getBoundingClientRect().width>0)")
        await b.js("[...document.querySelectorAll('button,[role=menuitem]')].find(x=>x.textContent.trim()==='Issues and pull requests'&&x.getBoundingClientRect().width>0).setAttribute('data-t-issues','1')")
        await press(b, '[data-t-issues]')
        await b.until("!!document.querySelector('#ccg-projects')")
        got['two'] = (await b.js("__PC.concordBack()"), await b.js(here))
        got['three'] = (await b.js("__PC.concordBack()"), await b.js(here))

    classic = "localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"
    asyncio.run(desktop.with_browser('online', '', check, classic))
    assert got['one'] == (True, {'panel': False, 'projects': False, 'chat': True}), ('Back did not close the issue', got)
    assert got['two'] == (True, {'panel': False, 'projects': False, 'chat': True}), ('Back did not close the issues list', got)
    assert got['three'] == (True, {'panel': False, 'projects': False, 'chat': False}), \
        ('after the issue, Back must follow the rule: channel -> channel list', got)
