#!/usr/bin/env python3
"""USER JOURNEYS -- what a member actually does, end to end, on a phone and on a desktop.

"This program has been turning to trash the past few days." Every report of 2026-10-10..13 was a path a person
walks every day -- send in Concord, vote, open an invitation, see Server Stats -- broken on a real node while every
test beside it passed, because each test drove ONE screen against a stub. This check walks the whole day in one
real Chrome session per device, against a FULL local node (tests/client/realnode_harness.py: the shipped
RelayServer over PosterChanDB as this node's relay, the Concord tunnel enforcing the shipped per-member share,
four relays elsewhere, a foreign Blossom server, the shipped web client), seeded like a real member:

  * an account following 300 people (30 of them posting), followed by 20, with a post of their own;
  * 16 Concord communities spread over 4 relays elsewhere (more live tunnels than the old 24-per-member share);
  * pictures on a Blossom server that is NOT PosterChan's, as their media server.

At 390x844 (a phone, touch) and then 1440x900 (a desktop, a second device of the same account), it:
sign in with an nsec -> read Home -> post -> reply -> react -> send a DM -> vote Bad Post of the Day -> send in a
Concord room while many room subscriptions are live -> open Files and see the pictures -> receive a mention and see
it on the bell and in Notifications -> All -> open it and land on the thread. Every step asserts what the person
SEES (rendered text, decoded images, the bell's number) and, where it is a write, what this node's relay or the
room's relays now hold. A failure names the step, what was expected and what was on screen.

KNOWN_BROKEN lists steps that fail TODAY for an open bug, with the reason: they are run and reported, do not fail
the check, and a known-broken step that starts passing FAILS the check (remove it from the list -- the same rule
as a strict xfail).

Exit 0 = every journey step passed, 1 = a step failed (printed), 2 = could not run (no Chrome, no app deps).
Env: PC_CHECK_PORT / PC_CHECK_PROFILE (the suite's per-check Chrome port and profile), PC_JOURNEY_ONLY=phone|desktop.
Run: venv-unified/bin/python scripts/check_user_journeys.py
"""
import asyncio
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Steps that fail today for an OPEN bug (name -> why). Keep this short and true; see the docstring.
KNOWN_BROKEN = {}

FOLLOWS, ACTIVE, FOLLOWERS = 300, 30, 20
ROOMS = 16


def _can_run():
    try:
        import importlib
        for mod in ('websockets', 'httpx', 'tests.client.realnode_harness'):
            importlib.import_module(mod)
    except Exception as e:      # noqa: BLE001
        return 'the app/test dependencies are not importable here: %s' % e
    if not Path('/opt/google/chrome/chrome').exists():
        return 'Chrome is not installed (/opt/google/chrome/chrome)'
    return ''


# ------------------------------------------------------------------------------------------------ the seed
def _sk(label: str) -> bytes:
    return hashlib.sha256(('pc-journey:' + label).encode()).digest()


def _sign(sk: bytes, kind: int, content='', tags=None, created_at=None):
    from app.services.nostr.event import build_event
    return build_event(sk, kind, content, tags or [], created_at)


class Seed:
    """The member and the world around them -- built once, shared by both devices."""

    def __init__(self, n, R):
        self.R = R
        now = int(time.time())
        self.me_sk = bytes([1]) * 32
        self.me = _sign(self.me_sk, 1, 'x')['pubkey']
        follows = [_sk('follow%d' % i) for i in range(FOLLOWS)]
        self.follow_pks = [_sign(sk, 1, 'x')['pubkey'] for sk in follows[:ACTIVE]]
        evs = [_sign(self.me_sk, 0, json.dumps({'name': 'journey member', 'about': 'walks every path'}), created_at=now - 86400)]
        all_pks = self.follow_pks + [hashlib.sha256(b'pk%d' % i).hexdigest() for i in range(FOLLOWS - ACTIVE)]
        evs.append(_sign(self.me_sk, 3, '', [['p', p] for p in all_pks], created_at=now - 86000))
        self.my_post = _sign(self.me_sk, 1, 'my first post on this node', created_at=now - 7200)
        evs.append(self.my_post)
        # The foreign Blossom server: three pictures of the member's own, and one a friend posted.
        self.pics = [n.foreign.put(R.png(c, 16)) for c in ((200, 40, 40), (40, 160, 60), (40, 60, 200))]
        n.foreign.owner = self.me
        friend_pic = n.foreign.put(R.png((230, 200, 30), 16), owned=False)
        self.friend_posts = []
        for i, sk in enumerate(follows[:ACTIVE]):
            evs.append(_sign(sk, 0, json.dumps({'name': 'friend %d' % i}), created_at=now - 86400))
            for j in range(2):
                body = 'post %d from friend %d' % (j, i)
                if i == 3 and j == 0:
                    body += ' look at this %s/%s.png' % (n.foreign.url, friend_pic)
                ev = _sign(sk, 1, body, created_at=now - 600 - i * 60 - j * 3000)
                if i == 3 and j == 0:
                    self.picture_post = ev
                evs.append(ev)
                self.friend_posts.append(ev)
        self.target = self.friend_posts[0]                     # 'post 0 from friend 0', the newest
        self.followers = [_sk('follower%d' % i) for i in range(FOLLOWERS)]
        for i, sk in enumerate(self.followers):
            evs.append(_sign(sk, 0, json.dumps({'name': 'follower %d' % i}), created_at=now - 86400))
            evs.append(_sign(sk, 3, '', [['p', self.me]], created_at=now - 80000 + i))
        n.home.add(evs)
        self.rooms = None


# ---------------------------------------------------------------------------------------------- the steps
class Journey:
    def __init__(self, n, seed, R, device_name):
        self.n, self.seed, self.R, self.dev = n, seed, R, device_name
        self.results = []          # (step, ok, detail, seconds)
        self.d = None

    async def step(self, name, fn):
        t = time.time()
        try:
            await fn()
            self.results.append((name, True, '', time.time() - t))
            return True
        except Exception as e:      # noqa: BLE001
            detail = getattr(e, 'args', [None])[0] if isinstance(e, AssertionError) else ''.join(
                traceback.format_exception_only(type(e), e)).strip()
            self.results.append((name, False, detail, time.time() - t))
            return False

    @property
    def b(self):
        return self.d.b

    async def js(self, expr):
        return await self.b.js(expr)

    async def wait(self, expr, seconds, what):
        await self.R.wait(self.b, expr, seconds, what)

    async def nav(self, view):
        """Tap the visible nav entry for `view` (the bottom bar on a phone, the sidebar on a desktop)."""
        ok = await self.js("(()=>{const b=[...document.querySelectorAll('.nav-item[data-view=%s]')].find(x=>x.getClientRects().length"
                           "&&getComputedStyle(x).visibility!=='hidden');if(!b)return false;b.click();return true})()" % json.dumps(view))
        if not ok:
            await self.js("__PC.switchView(%s)" % json.dumps(view))

    async def wait_relay(self, flt, what, seconds=20, where=None):
        end = time.time() + seconds
        while time.time() < end:
            got = [e for e in self.n.home.query(flt) if where is None or where(e)]
            if got:
                return got
            await asyncio.sleep(.4)
        mine = self.n.home.query({'authors': [self.seed.me], 'since': int(time.time()) - 300})
        raise AssertionError({'expected': what, 'seen': {
            'relay holds nothing matching': json.dumps(flt)[:200],
            'what this account published here in the last 5 min': [[e['kind'], e['content'][:40], e['tags'][:3]] for e in mine][:8],
            'toasts': (await self.d.toasts())[-5:]}})

    async def tap(self, selector, what):
        """A real pointer press at the element's centre (CDP Input), not a synthetic .click()."""
        await self.wait("!!document.querySelector(%s)" % json.dumps(selector), 15, what)
        xy = await self.js("(()=>{const e=document.querySelector(%s);e.scrollIntoView({block:'center'});"
                           "const r=e.getBoundingClientRect();return [r.left+r.width/2,r.top+r.height/2]})()" % json.dumps(selector))
        for t in ('mousePressed', 'mouseReleased'):
            await self.b.call('Input.dispatchMouseEvent', {'type': t, 'x': xy[0], 'y': xy[1], 'button': 'left', 'clickCount': 1})

    def mine(self, i):
        """This device's i-th friend post (the phone and the desktop never touch the same one)."""
        return self.seed.friend_posts[i * 2 + (1 if self.dev == 'desktop' else 0)]

    def card(self, ev_id):
        return "#feed .note[data-id=%s]" % json.dumps(ev_id)

    # --- 1
    async def sign_in(self):
        pre = ''
        if self.seed.rooms is not None:     # this account's communities, as another device of it already holds them
            pre = ("try{localStorage.setItem('pc.concord.rooms.v1.%s',%s);localStorage.setItem('pc.concord.active.v1.%s','0')}catch(_){}\n"
                   % (self.seed.me, json.dumps(json.dumps(self.seed.rooms)), self.seed.me))
        port = os.environ.get('PC_CHECK_PORT')
        prof = os.environ.get('PC_CHECK_PROFILE')
        if prof:
            prof = os.path.join(prof, self.dev)
            shutil.rmtree(prof, ignore_errors=True)
            os.makedirs(prof, exist_ok=True)
        self.d = self.R.Device(self.n, 1, phone=(self.dev == 'phone'), pre_init=pre,
                               port=int(port) if port else None, profile=prof)
        await self.d.__aenter__()
        name = await self.js("(__PC.me()||{}).pubkey||''")
        assert name == self.seed.me, {'expected': 'signed in as the nsec typed', 'seen': name}

    async def home_timeline(self):
        await self.nav('home')
        await self.wait("document.querySelector('#feed') && document.querySelector('#feed').innerText.includes('post 0 from friend 0')",
                        40, "Home shows the newest post of somebody this account follows ('post 0 from friend 0')")
        seen = await self.js("[...document.querySelectorAll('#feed .note')].filter(n=>n.getClientRects().length&&"
                             "parseFloat(getComputedStyle(n).opacity)>0.5).length")
        assert seen >= 3, {'expected': 'several VISIBLE cards on Home', 'seen': seen}

    async def picture_in_a_post(self):
        card = self.card(self.seed.picture_post['id'])
        await self.wait("!!document.querySelector(%s)" % json.dumps(card), 30, "friend 3's post with a picture on Home")
        await self.js("document.querySelector(%s).scrollIntoView({block:'center'});true" % json.dumps(card))
        await self.R.wait(self.b, "[...document.querySelectorAll(%s)].some(i=>i.complete&&i.naturalWidth>0)" % json.dumps(card + ' img:not(.avatar):not(.av)'),
                          20, 'the picture from a foreign Blossom server drawn in the post',
                          state="[...document.querySelectorAll(%s)].map(i=>[i.className,i.src.slice(0,90),i.naturalWidth])" % json.dumps(card + ' img'))

    # --- 2
    async def post(self):
        text = 'journey post from the %s %d' % (self.dev, int(time.time()))
        inline = await self.js("(()=>{const t=document.querySelector('#tl-cmp-ta');return !!(t&&t.getClientRects().length)})()")
        if inline:
            await self.js("(()=>{const t=document.querySelector('#tl-cmp-ta');t.focus();t.value=%s;"
                          "t.dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('#tl-cmp-post').click()})()"
                          % json.dumps(text))
        else:
            await self.js("(()=>{const b=[...document.querySelectorAll('#btn-compose-m,#btn-compose')].find(x=>x.getClientRects().length);b.click()})()")
            await self.wait("!!document.querySelector('#cmp') && !!document.querySelector('#cmp-send')", 15, 'the composer opened')
            await self.js("(()=>{const t=document.querySelector('#cmp');t.value=%s;t.dispatchEvent(new Event('input',{bubbles:true}));"
                          "document.querySelector('#cmp-send').click()})()" % json.dumps(text))
        await self.wait_relay({'kinds': [1], 'authors': [self.seed.me], 'search': None} if False else
                              {'kinds': [1], 'authors': [self.seed.me], 'since': int(time.time()) - 120},
                              'the new post on this node\'s relay')
        await self.wait("document.querySelector('#feed').innerText.includes(%s)" % json.dumps(text), 30,
                        'the new post on screen')

    # --- 3
    async def reply(self):
        t = self.mine(0)
        text = 'a reply from the %s %d' % (self.dev, int(time.time()))
        await self.nav('home')
        await self.wait("!!document.querySelector(%s)" % json.dumps(self.card(t['id']) + ' .act[data-a=reply]'), 30,
                        "the friend's post with its reply button")
        await self.js("document.querySelector(%s).click()" % json.dumps(self.card(t['id']) + ' .act[data-a=reply]'))
        await self.wait("!!document.querySelector('#cmp') && !!document.querySelector('#cmp-send')", 15, 'the reply composer opened')
        await self.js("(()=>{const t=document.querySelector('#cmp');t.value=%s;t.dispatchEvent(new Event('input',{bubbles:true}));"
                      "document.querySelector('#cmp-send').click()})()" % json.dumps(text))
        # Replies are NIP-22 comments (kind 1111: `E` = root, `e` = parent) since app.js replyKindFor; either shape counts.
        await self.wait_relay({'kinds': [1, 1111], 'authors': [self.seed.me], '#e': [t['id']]}, 'the reply, as typed, on this node\'s relay',
                              where=lambda e: e['content'] == text)
        # What the person sees: open the post and the reply is under it.
        await self.js("(()=>{const c=document.querySelector(%s);(c.querySelector('.txt')||c).click();return true})()"
                      % json.dumps(self.card(t['id'])))
        await self.wait("__PC.isView('thread') && document.querySelector('#feed').innerText.includes(%s)" % json.dumps(text),
                        30, 'the thread of the post shows the reply')

    # --- 4
    async def react(self):
        t = self.mine(0)
        await self.nav('home')
        btn = self.card(t['id']) + ' .act[data-a=react]'
        await self.wait("!!document.querySelector(%s)" % json.dumps(btn), 30, "the friend's post with its react button")
        await self.js("document.querySelector(%s).click()" % json.dumps(btn))
        await self.tap('.emoji-pop [data-e]', 'the reaction picker with its emoji')
        await self.wait_relay({'kinds': [7], 'authors': [self.seed.me], '#e': [t['id']]}, 'the reaction on this node\'s relay')
        await self.wait("!!document.querySelector(%s)" % json.dumps(btn + '.on'), 20, 'the post shows your reaction')

    # --- 5
    async def dm(self):
        t = self.mine(0)
        text = 'a private hello from the %s %d' % (self.dev, int(time.time()))
        await self.nav('home')
        name = self.card(t['id']) + ' .name[data-prof]'
        await self.wait("!!document.querySelector(%s)" % json.dumps(name), 30, "the friend's name on their post")
        await self.js("document.querySelector(%s).click()" % json.dumps(name))
        await self.wait("!!document.querySelector('#prof-menu')", 30, "the friend's profile")
        await self.js("document.querySelector('#prof-menu').click()")
        await self.wait("!!document.querySelector('.menu-pop [data-m=message]')", 10, "the profile menu offers Message")
        await self.js("document.querySelector('.menu-pop [data-m=message]').click()")
        await self.wait("!!document.querySelector('#dm-in') && !!document.querySelector('#dm-send')", 30, 'the conversation opened')
        await self.js("(()=>{const i=document.querySelector('#dm-in');i.focus();i.value=%s;i.dispatchEvent(new Event('input',{bubbles:true}));"
                      "document.querySelector('#dm-send').click()})()" % json.dumps(text))
        await self.wait("document.querySelector('#dm-msgs') && document.querySelector('#dm-msgs').innerText.includes(%s)" % json.dumps(text),
                        30, 'the message in the conversation')
        await self.wait_relay({'kinds': [1059], '#p': [t['pubkey']], 'since': int(time.time()) - 3 * 86400},
                              'a gift-wrapped DM for the friend on this node\'s relay')

    # --- 6
    async def badpost(self):
        t = self.mine(1)
        await self.nav('global')
        menu = self.card(t['id']) + ' .act[data-a=menu]'
        await self.wait("!!document.querySelector(%s)" % json.dumps(menu), 40, 'the post on the Nostrverse with its menu')
        await self.js("document.querySelector(%s).click()" % json.dumps(menu))
        await self.wait("!!document.querySelector('.menu-pop [data-m=badpost]')", 10, "the menu offers 'Vote Bad Post of the Day'")
        await self.js("document.querySelector('.menu-pop [data-m=badpost]').click()")
        await self.wait("(window.__toastLog||[]).some(t=>/Voted Bad Post/.test(t))", 20, "the toast 'Voted Bad Post of the Day'")
        await self.wait_relay({'kinds': [1985], 'authors': [self.seed.me], '#e': [t['id']]}, 'the vote on this node\'s relay')

    # --- 7
    async def concord(self):
        R, n = self.R, self.n
        if self.seed.rooms is None:
            self.seed.rooms = await R.seed_communities(n, self.d, ROOMS)
        n.home.tunnel_peak = sum(n.home.tunnel._open.values())      # this device's tunnels, not the last one's
        n.home.tunnel_refused = 0
        await self.js("__PC.switchMessagesTab('concord')")
        end = time.time() + 40
        while time.time() < end and n.home.tunnel_peak <= 24 and not n.home.tunnel_refused:
            await asyncio.sleep(.5)
        room0 = self.seed.rooms[0]['cord']['bundle']['relays']
        await R.open_room_channel(self.d)
        before = {f.url: len(f.writes) for f in n.far}
        text = 'concord hello from the %s %d' % (self.dev, int(time.time()))
        await R.concord_send(self.d, text)
        await self.wait("document.querySelector('.cc-app') && document.querySelector('.cc-app').innerText.includes(%s)" % json.dumps(text),
                        30, 'the message in the room')
        end = time.time() + 30
        while time.time() < end:
            if any(f.url in room0 and any(e.get('kind') == 1059 for e in f.writes[before[f.url]:]) for f in n.far):
                break
            await asyncio.sleep(.4)
        else:
            raise AssertionError({'expected': "the message delivered to the room's relays", 'seen': {
                'tunnels held at most': n.home.tunnel_peak, 'tunnels refused': n.home.tunnel_refused,
                'toasts': await self.d.toasts()}})
        bad = [t for t in await self.d.toasts() if 'Delivery needs attention' in t]
        assert not bad, {'expected': 'no delivery warning', 'seen': bad}

    # --- 8
    async def files(self):
        await self.js("ClientSettings.set('blossomEnabled',true);ClientSettings.set('mediaServer',%s);true" % json.dumps(self.n.foreign.url))
        await self.nav('blossom')
        await self.wait("!!document.querySelector('.fx-home-tile[data-folder=\"\"]')", 30, "Files opens on its home with 'All files'")
        await self.js("document.querySelector('.fx-home-tile[data-folder=\"\"]').click()")
        await self.wait("document.querySelectorAll('#bl-grid .file-card, #bl-grid .fx-tile, #bl-grid [data-sha]').length>=3", 30,
                        "All files lists the three pictures on the media server")

    async def files_thumbs(self):
        await self.R.wait(self.b, "[...document.querySelectorAll('#bl-grid img.ithumb')].filter(i=>i.complete&&i.naturalWidth>0).length>=3",
                        8, 'three picture thumbnails drawn',
                        state="({imgs:[...document.querySelectorAll('#bl-grid img')].map(i=>[i.src.replace(location.origin,''),i.naturalWidth]),"
                              "icons:document.querySelectorAll('#bl-grid .file-icon').length,"
                              "foreignHits:%s})" % json.dumps(self.n.foreign.hits[-6:]))

    # --- 9
    async def mention(self):
        sk = self.seed.followers[0]
        self.mention_text = 'hey @journey member, about your first post (%s) %d' % (self.dev, int(time.time()))
        ev = _sign(sk, 1, self.mention_text, [['e', self.seed.my_post['id'], '', 'root'], ['p', self.seed.me]])
        self.mention_ev = ev
        await self.nav('home')
        bell = ("Math.max(0,...[...document.querySelectorAll('#notif-badge,#notif-badge-m,#rb-notif-badge')].filter(b=>"
                "!b.classList.contains('hidden')&&b.getClientRects().length).map(b=>parseInt(b.textContent||'0')||0))")
        before = await self.js(bell)
        ok = self.n.home.publish(ev)
        assert ok[2] is True, {'expected': "the follower's mention accepted by this node's relay", 'seen': ok}
        await self.wait("%s>%d" % (bell, before), 30, 'the bell count goes up (it read %d before the mention)' % before)

    async def notifications_all(self):
        await self.nav('notifications')
        await self.wait("!!document.querySelector('.ntab[data-nf=all]')", 20, 'Notifications with its tabs')
        await self.js("document.querySelector('.ntab[data-nf=all]').click()")
        await self.wait("[...document.querySelectorAll('#feed .notif')].some(n=>n.innerText.includes(%s))"
                        % json.dumps(self.mention_text[:40]), 30, 'Notifications -> All lists the mention')

    async def open_notification(self):
        await self.js("[...document.querySelectorAll('#feed .notif')].find(n=>n.innerText.includes(%s)).click()"
                      % json.dumps(self.mention_text[:40]))
        await self.wait("__PC.isView('thread') && document.querySelector('#feed').innerText.includes('my first post on this node')"
                        " && document.querySelector('#feed').innerText.includes(%s)" % json.dumps(self.mention_text[:40]),
                        30, 'the thread: your post with the mention under it')

    async def run(self):
        if not await self.step('sign in with an nsec', self.sign_in):
            return
        try:
            await self.step('home shows the people you follow', self.home_timeline)
            await self.step('a picture on a foreign Blossom server shows in a post', self.picture_in_a_post)
            await self.step('post', self.post)
            await self.step('reply', self.reply)
            await self.step('react', self.react)
            await self.step('send a DM', self.dm)
            await self.step('vote Bad Post of the Day', self.badpost)
            await self.step('send in a Concord room with many rooms live', self.concord)
            if await self.step('files: open All files on the foreign media server', self.files):
                await self.step('files: pictures on the foreign media server show thumbnails', self.files_thumbs)
            if await self.step('a mention lights the bell', self.mention):
                if await self.step('Notifications -> All lists the mention', self.notifications_all):
                    await self.step('opening the notification lands on the thread', self.open_notification)
        finally:
            await self.d.__aexit__()


async def main():
    from tests.client import realnode_harness as R
    only = (os.environ.get('PC_JOURNEY_ONLY') or '').strip()
    devices = [d for d in ('phone', 'desktop') if not only or d == only]
    started = time.time()
    out = []
    with R.Node(far=4) as n:
        seed = Seed(n, R)
        for dev in devices:
            j = Journey(n, seed, R, dev)
            await j.run()
            out += [(dev,) + r for r in j.results]
    return out, time.time() - started


def report(results, took):
    fails, known, fixed = [], [], []
    print('USER JOURNEYS (%.0fs)' % took)
    for dev, name, ok, detail, secs in results:
        kb = name in KNOWN_BROKEN
        mark = ('PASS' if not kb else 'NOW PASSES (remove from KNOWN_BROKEN)') if ok else ('KNOWN BROKEN' if kb else 'FAIL')
        print('  %-7s %-58s %-12s %5.1fs' % (dev, name, mark, secs))
        if not ok and not kb:
            fails.append((dev, name, detail))
        elif not ok:
            known.append((dev, name, detail))
        elif kb:
            fixed.append((dev, name))
    for dev, name, detail in fails:
        print('\nFAIL [%s] %s\n  %s' % (dev, name, json.dumps(detail, default=str)[:1500] if not isinstance(detail, str) else detail[:1500]))
    for dev, name, detail in known:
        print('\nKNOWN BROKEN [%s] %s -- %s\n  seen: %s' % (dev, name, KNOWN_BROKEN[name], json.dumps(detail, default=str)[:600]))
    for dev, name in fixed:
        print('\n[%s] %s now PASSES: take it out of KNOWN_BROKEN in scripts/check_user_journeys.py' % (dev, name))
    return 1 if (fails or fixed) else 0


if __name__ == '__main__':
    why = _can_run()
    if why:
        print('SKIP: ' + why)
        sys.exit(2)
    tmp = tempfile.mkdtemp(prefix='pc-journeys-')
    os.environ.setdefault('TMPDIR', tmp)
    try:
        results, took = asyncio.run(main())
    except Exception:       # noqa: BLE001
        traceback.print_exc()
        print('FAIL: the journey harness itself broke (above)')
        sys.exit(1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if not results:
        print('SKIP: no journey ran')
        sys.exit(2)
    sys.exit(report(results, took))
