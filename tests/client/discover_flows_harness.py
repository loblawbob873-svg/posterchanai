"""Shared plumbing for the Discover / content flow tests (search, trending, articles, analytics, stats, news, web
search, code, office): ONE real node per test module (tests/client/realnode_harness.py -- the shipped RelayServer
over PosterChanDB, the instance, real Chrome per device), the node's own API routers mounted IN-PROCESS, and the
failure conditions a person meets, injected at the real boundaries:

  Gate     the HOME RELAY's REQ handling (RelayServer._dispatch, wrapped): a rule may keep a REQ unanswered for
           ever ('never' -- no EVENT, no EOSE) or answer it N seconds late. Everything else is the shipped relay.
  Routers  app/routers/* mounted on a FastAPI app reached over httpx's ASGI transport, behind the instance; a
           per-path FAULT answers 503 (the app's own Unavailable shape), drops the connection, or is SLOW.
  run      every selected scenario of a module on its own accounts, LANES at a time, on one node.

The data is always made by a producer that ships: the posts, reactions, articles and drafts are published by the
shipped client through its own composer / react button / editor, never hand-built events.

Fail-first: PC_JOURNEY_OVERRIDE="/static/js/client/discover.js=/tmp/old/discover.js,..." serves another copy of
a shipped client file (realnode_harness.Node reads it).
"""

import asyncio
import json
import threading
from types import SimpleNamespace

from tests.client import realnode_harness as R
from tests.client import wallpaper_harness as H

LANES = 3


# --------------------------------------------------------------------------------------------- the home relay
class Gate:
    """Wrap the home relay's REQ handling. `rules` are callables (filters, conn) -> None | 'never' | seconds.
    `device_of(conn)` names the device a socket belongs to (Scenario.device(tag=...)), so a rule can make the relay
    silent for ONE device while every other scenario on the node is answered as usual."""

    def __init__(self, home):
        self.home, self.rules, self.held = home, [], []
        orig = home.relay._dispatch

        async def gated(conn, raw):
            try:
                m = json.loads(raw) if isinstance(raw, (str, bytes)) else None
            except ValueError:
                m = None
            if isinstance(m, list) and m and m[0] == 'REQ':
                filters = [f for f in m[2:] if isinstance(f, dict)]
                for rule in list(self.rules):
                    verdict = rule(filters, conn)
                    if verdict == 'never':
                        self.held.append(filters)
                        return
                    if verdict:
                        async def late(v=verdict):
                            await asyncio.sleep(v)
                            await orig(conn, raw)
                        asyncio.ensure_future(late())
                        return
            await orig(conn, raw)
        home.relay._dispatch = gated

    def add(self, rule):
        self.rules.append(rule)
        return rule

    def drop(self, rule):
        try:
            self.rules.remove(rule)
        except ValueError:
            pass


def device_of(conn):
    try:
        path = conn.request.path or ''
    except Exception:
        return ''
    return path.split('pcdev=', 1)[1].split('&', 1)[0] if 'pcdev=' in path else ''


# The page names its device on every socket to the home relay (a query string the relay ignores), so a Gate rule
# can tell one device's reads from another's (and a cookie names it to the instance, for Routers.fail(tag=)). The
# client's own idea of the url is untouched.
TAG_JS = r'''
(()=>{const W=window.WebSocket;if(!W||W.__pcTagged)return;
  const T=function(u,p){u=String(u);if(/^ws:\/\/127\.0\.0\.1:__HOME__\/?(\?|$)/.test(u))u+=(u.includes('?')?'&':(u.endsWith('/')?'?':'/?'))+'pcdev=__TAG__';
    return p===undefined?new W(u):new W(u,p);};
  T.prototype=W.prototype;T.CONNECTING=0;T.OPEN=1;T.CLOSING=2;T.CLOSED=3;T.__pcTagged=true;T.__pcLoopback=W.__pcLoopback;window.WebSocket=T;
  try{document.cookie='pcdev=__TAG__; path=/';}catch(_){}})();
'''


def stored(n, flt):
    """What the home relay's STORE holds for `flt` -- private (NIP-78) documents included, which a socket read
    without the owner's AUTH is rightly not given."""
    st = n.home.store
    return n.loop.run(st._s(st._query_sync, [flt], 2000))


def asks(filters, kinds=(), authors=(), search=None, ids=False):
    """Does this REQ ask for any of `kinds` (by any of `authors`, when given)? `search`: only NIP-50 reads;
    `ids`: only reads by event id."""
    for f in filters:
        if kinds and not set(f.get('kinds') or []) & set(kinds):
            continue
        if authors and not set(f.get('authors') or []) & set(authors):
            continue
        if search is True and not f.get('search'):
            continue
        if ids and not f.get('ids'):
            continue
        return True
    return False


# --------------------------------------------------------------------------------------------- the node's API
class Routers:
    """The node's own routers, REAL, in this process. `users`: pubkey -> the account the routers see (every one a
    member, as allow_keys makes them). `paths`: the url prefixes these routers answer. `faults`: path prefix -> 503 |
    'drop' | seconds (answered that late)."""

    def __init__(self, mp, routers, users, paths, overrides=None, forward=()):
        """`forward`: lowercase header names (or prefixes) passed through both ways besides the defaults -- e.g.
        `host` and `x-wopi-` for a WOPI host whose URLs are built from the request and whose answers are headers."""
        import httpx
        from fastapi import FastAPI, HTTPException, Request
        from app import auth as app_auth
        from app.database import get_db
        from app.routers import auth as router_auth
        from tests.member_fixtures import allow_keys

        self.users, self.faults, self.seen, self.faulted = users, {}, [], []
        self.forward = tuple(forward)
        allow_keys(mp, *users)
        app = FastAPI()
        for router, prefix in routers:
            app.include_router(router, prefix=prefix)
        self.prefixes = list(paths)         # the url prefixes the node answers here; everything else is the instance

        def who(request: Request):
            tok = (request.headers.get('authorization') or '').removeprefix('Bearer ').strip()
            if not tok.startswith('tok-') or tok[4:] not in self.users:
                raise HTTPException(401, 'not signed in')
            return self.users[tok[4:]]
        for dep in (app_auth.get_current_user, router_auth.get_current_user):
            app.dependency_overrides[dep] = who
        app.dependency_overrides[get_db] = lambda: None
        for dep, fn in (overrides or {}).items():
            app.dependency_overrides[dep] = fn
        ready = threading.Event()

        def run():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://node', timeout=90)
            ready.set()
            self.loop.run_forever()
        threading.Thread(target=run, daemon=True).start()
        ready.wait(10)
        H.ROUTES.append(self.route)

    def stop(self):
        try:
            H.ROUTES.remove(self.route)
        except ValueError:
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)

    def fail(self, prefix, how, tag=None):
        """Make `prefix` answer `how` (503 | 'drop' | seconds | (503, seconds)) -- for ONE device (its tag) or, with no tag, for all.
        Scenarios run side by side, so a fault for everybody would land on the neighbours' requests too."""
        self.faults[(prefix, tag)] = how

    def heal(self, prefix, tag=None):
        self.faults.pop((prefix, tag), None)

    def fault(self, path, h=None):
        tag = ''
        if h is not None:
            for part in (h.headers.get('Cookie') or '').split(';'):
                k, _, v = part.strip().partition('=')
                if k == 'pcdev':
                    tag = v
        for (pre, who), f in list(self.faults.items()):
            if path.startswith(pre) and (who is None or who == tag):
                self.faulted.append((path, tag))
                return f
        return None

    def route(self, h, method, path, body):
        p = path.split('?')[0]
        if p == '/api/auth/nostr-login' and method == 'POST':
            pk = (json.loads(body or b'{}') or {}).get('pubkey') or ''
            if pk not in self.users:
                return False
            u = self.users[pk]
            h._json({'access_token': 'tok-' + pk, 'token_type': 'bearer',
                     'user': {'id': u.id, 'username': u.username, 'nostr_npub': pk, 'is_admin': False}})
            return True
        if p == '/api/instance-welcome/access':
            tok = (h.headers.get('Authorization') or '').removeprefix('Bearer ').strip()
            if tok[4:] not in self.users:
                h._json({'detail': 'not signed in'}, 403)
            else:
                h._json({'pubkey': tok[4:], 'qualified': True})
            return True
        if not any(p.startswith(pre) for pre in self.prefixes):
            return False
        self.seen.append((method, p))
        f = self.fault(p, h)
        if isinstance(f, tuple):                    # (503, seconds): a node that is slow to refuse
            import time
            time.sleep(f[1])
            f = f[0]
        if f == 503:
            h._json({'detail': 'Temporarily unavailable; try again shortly'}, 503)   # app/main.py's Unavailable
            return True
        if f == 'drop':
            h.close_connection = True
            return True
        headers = {k: v for k, v in h.headers.items() if k.lower() in ('authorization', 'content-type', 'accept')
                   or (self.forward and k.lower().startswith(self.forward))}

        async def call():
            if isinstance(f, (int, float)) and f:
                await asyncio.sleep(f)
            return await self.client.request(method, path, headers=headers, content=body)
        try:
            r = asyncio.run_coroutine_threadsafe(call(), self.loop).result(120)
        except Exception:
            h.close_connection = True
            return True
        try:
            h.send_response(r.status_code)
            for k, v in r.headers.items():
                if k.lower() in ('content-type', 'x-document-extension', 'content-disposition', 'cache-control') or (
                        self.forward and k.lower().startswith(self.forward) and k.lower() != 'host'):
                    h.send_header(k, v)
            h.send_header('Content-Length', str(len(r.content)))
            h.end_headers()
            h.wfile.write(r.content)
        except OSError:
            pass
        return True


def member(pk, i):
    return SimpleNamespace(id=3000 + i, username='m%d' % i, nostr_npub=pk, is_admin=False, email='m%d@example.invalid' % i)


def members(first_fill, scenarios):
    """Every account a module's scenarios sign in with (4 per scenario, see run), as the node's members."""
    fills = range(first_fill, first_fill + 4 * len(scenarios))
    return {R.pubkey(f): member(R.pubkey(f), f) for f in fills}


# --------------------------------------------------------------------------------------------- the runner
class Scenario:
    """One scenario: its own accounts (`fill(i)`), its devices (closed at the end), the shared node `n`."""

    def __init__(self, n, name, base):
        self.n, self.name, self.base, self.open = n, name, base, []

    def fill(self, i=0):
        return self.base + i

    def pk(self, i=0):
        return R.pubkey(self.fill(i))

    async def device(self, i=0, phone=False, pre_init='', tag='', login=True):
        """A device signed in to account `i` (a FRESH Chrome profile each time). `tag` names its relay sockets
        for Gate rules (device_of)."""
        js = pre_init + CLIENT_INIT
        if tag:
            js += TAG_JS.replace('__HOME__', str(self.n.home.port)).replace('__TAG__', tag)
        d = R.Device(self.n, self.fill(i), phone=phone, pre_init=js, login=login)
        await d.__aenter__()
        self.open.append(d)
        return d

    async def close(self):
        for d in reversed(self.open):
            try:
                await d.__aexit__()
            except Exception:
                pass


# A plain (non-desktop) client: these screens are driven in the ordinary shell, as on a phone or the web.
CLIENT_INIT = r'''
try{localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));}catch(_){}
'''


def run(request, scenarios, cost, first_fill, setup=None, lanes=LANES, node_kw=None):
    """Run every SELECTED scenario of the calling module (each on accounts first_fill + 4*i ..), `lanes` at a time,
    on ONE node; returns check(name), which raises that scenario's own failure."""
    wanted = [i.name[len('test_'):] for i in request.session.items
              if getattr(i, 'module', None) is request.module and i.name.startswith('test_')]
    missing = [w for w in wanted if w not in scenarios]
    assert not missing, ('a test with no scenario', missing)
    bases = {name: first_fill + 4 * i for i, name in enumerate(scenarios)}
    queue = sorted(wanted, key=lambda x: -cost.get(x, 15))
    outcome = {}
    with R.Node(**(node_kw or {})) as n:
        ctx = setup(n) if setup else None
        try:
            async def lane():
                while queue:
                    name = queue.pop(0)
                    S = Scenario(n, name, bases[name])
                    S.ctx = ctx
                    try:
                        await scenarios[name](S)
                        outcome[name] = None
                    except BaseException as e:      # noqa: B036 -- re-raised by that scenario's own test
                        outcome[name] = e
                    finally:
                        await S.close()

            async def main():
                await asyncio.gather(*(lane() for _ in range(lanes)))
            asyncio.run(main())
        finally:
            if ctx is not None and hasattr(ctx, 'stop'):
                ctx.stop()

    def check(name):
        assert name in outcome, ('scenario never ran', name)
        if outcome[name] is not None:
            raise outcome[name]
    return check


# --------------------------------------------------------------------------------------------- what a person does
STATE = ("({toasts:(window.__toastLog||[]).slice(-8),errors:(window.__errors||[]).slice(-6),"
         "feed:((document.querySelector('#feed')||{}).innerText||'').slice(0,700)})")


async def wait(d, expr, seconds=30, what=None):
    await R.wait(d.b, expr, seconds, what, state=STATE)


async def until(fn, seconds, what, d=None):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while loop.time() < deadline:
        got = fn()
        if got:
            return got
        await asyncio.sleep(.2)
    raise AssertionError({'waiting for': what, 'seen': d and await d.b.js(STATE)})


async def view(d, name, ready, seconds=30):
    await d.b.js("__PC.switchView(%s);true" % json.dumps(name))
    await wait(d, ready, seconds, 'the %s screen' % name)


async def post_note(d, text):
    """Home -> the ＋ button -> type -> Send, as a person posts. Returns the event the relay stored."""
    n = d.node
    await view(d, 'home', "!!document.querySelector('#tl-fab')")
    await d.b.js("document.querySelector('#tl-fab').click();true")
    await wait(d, "!!document.querySelector('#cmp') && !!document.querySelector('#cmp-send')", 20, 'the composer')
    await d.b.js("(()=>{const t=document.querySelector('#cmp');t.focus();t.value=%s;"
                 "t.dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('#cmp-send').click();return true})()"
                 % json.dumps(text))
    pk = await d.b.js('__PC.me().pubkey')
    got = await until(lambda: [e for e in n.home.query({'kinds': [1], 'authors': [pk]}) if e['content'] == text],
                      30, 'the post on the home relay', d)
    return got[0]


CARD = "document.querySelector('.note[data-id=\"%s\"]')"


async def react(d, note_id, emoji='🔥'):
    """The post's react button -> an emoji in the picker (a NIP-25 kind 7 from the shipped client)."""
    card = CARD % note_id
    await wait(d, "!!%s && !!%s.querySelector('.act[data-a=react]')" % (card, card), 30, 'the post card')
    await d.b.js("%s.querySelector('.act[data-a=react]').click();true" % card)
    sel = ".emoji-pop [data-e=\"%s\"]" % emoji
    await wait(d, "!!document.querySelector(%s)" % json.dumps(sel), 15, 'the emoji picker')
    await d.b.js("document.querySelector(%s).dispatchEvent(new MouseEvent('mousedown',{bubbles:true,cancelable:true}));true"
                 % json.dumps(sel))
    pk = await d.b.js('__PC.me().pubkey')
    await until(lambda: [e for e in d.node.home.query({'kinds': [7], 'authors': [pk], '#e': [note_id]})], 30,
                'the reaction on the home relay', d)


async def open_note(d, note_id):
    await d.b.js("__PC.openThread(%s);true" % json.dumps(note_id))
    await wait(d, "!!%s" % (CARD % note_id), 30, 'the post opened')


# --------------------------------------------------------------------------------------------- accounts
async def add_account(d, sk_fill):
    """The account card -> "＋ Add an account" -> the login screen -> type that account's nsec, as a person does.
    The device then holds BOTH accounts in its remembered list (Session.accounts), signed in as the new one."""
    await d.b.js("document.querySelector('#me-card').click();true")
    await wait(d, "!!document.querySelector('.acct-pop [data-act=add]')", 15, 'the account menu')
    await d.b.js("document.querySelector('.acct-pop [data-act=add]').click();true")
    await wait(d, "!!window.__PC && document.body.classList.contains('guest') && !!document.querySelector('#nsec-input')",
               60, 'the login screen for another account')
    await d.b.js("(()=>{const key=new Uint8Array(32).fill(%d);document.querySelector('#nsec-input').value="
                 "NostrTools.nip19.nsecEncode(key);document.querySelector('#btn-nsec-login').click();return true})()" % sk_fill)
    await wait(d, "!!__PC.me() && __PC.me().pubkey===%s && Relay.status==='ok'" % json.dumps(R.pubkey(sk_fill)), 60,
               'signed in as the added account')


async def switch_account(d, sk_fill, before_reload=''):
    """The account card -> the other account's row (the page reloads signed in as it). `before_reload`: JS run
    right after the click, before the reload lands -- "a change made just before a switch"."""
    pk = R.pubkey(sk_fill)
    await d.b.js("document.querySelector('#me-card').click();true")
    row = ".acct-pop .acct-p[data-pk=\"%s\"]" % pk
    await wait(d, "!!document.querySelector(%s)" % json.dumps(row), 15, 'the other account in the menu')
    await d.b.js("window.__pcBeforeSwitch=true;document.querySelector(%s).click();%s;true" % (json.dumps(row), before_reload))
    await wait(d, "!window.__pcBeforeSwitch && !!window.__PC && !!__PC.me() && __PC.me().pubkey===%s && Relay.status==='ok'"
               % json.dumps(pk), 60, 'signed in as the account switched to')
