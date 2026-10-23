"""Monero Wallet, Webxdc and Markets, driven end to end in the shipped client -- each against the node's REAL
router, and each with the backend slow, unreachable or not answering.

Same harness and runner as tests/client/test_private_docs_cross_device_full_app.py (wallpaper_harness: a real
relay, a real instance, real Chrome; `run_scenarios`: every scenario its own account, three at a time).

  Monero Wallet  the member's own wallet (`app/routers/monero_user_wallet.py` + the real UserWallets service)
                 over HTTP to a FAKE monero-wallet-rpc in this process (`Rpc`), one per account, that can
                 answer, answer too slowly (the node's own read budget runs out -> 503 "busy") or not be
                 there at all (a closed port). The node wallet (`app/routers/monero_wallet.py`) is mounted
                 too and refuses a member exactly as on a node (403, get_admin_user). Asserted: the balance,
                 confirming amount, address and history render from the RPC's known state; the send sheet
                 refuses a wrong address / zero / too many decimals / more than is unlocked WITHOUT asking
                 the wallet to move anything, and a valid send reaches `transfer_split` with the exact
                 atomic amount from this user's account; a wallet that is down or busy says so -- never the
                 operator's admin-only refusal, never a spinner -- and Retry recovers.
  Webxdc         the gallery against the real relay: a relay that never answers ends in the empty state with
                 Refresh usable (no spinner for ever); once it answers, Refresh lists the app another
                 account posted, with its move count; Play fetches exactly the posted archive.
  Markets        `app/routers/markets.py` over the real `markets_service.get_report` (its in-process report
                 set to a known one -- generating it is the AI backend's job and is not exercised): the
                 digest renders with real prices, Share puts it in the composer, "building" heals by
                 itself, and a server that errors, a connection that dies and a node with no AI backend
                 each get their own explicit sentence.

Fail-first: PC_WALLPAPER_OVERRIDE="/static/js/client/monero-wallet.js=/tmp/x.js,..." serves another copy.
"""
import asyncio
import hashlib
import io
import json
import socket
import threading
import time
import zipfile
from contextvars import ContextVar
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.client import wallpaper_harness as H
from tests.client.test_private_docs_save_with_a_silent_relay_full_app import no_errors, run_scenarios

CHROME = pytest.mark.skipif(not Path(H.CHROME).exists(), reason='Chrome required')
FEATURE_COVERS = ('wallet', 'xdc', 'markets')
SCENARIOS = {}
FIRST_FILL = 120           # 120, 121, ... -- no other suite's accounts
OTHER_FILL = 7             # the account that posted the mini app
COST = {'markets_digest_share_and_every_failure_state': 45, 'the_gallery_survives_a_silent_relay_then_lists_and_plays': 35,
        'a_member_wallet_that_is_down_says_so_and_retry_recovers': 30,
        'refresh_on_a_busy_wallet_never_shows_the_operators_refusal': 30,
        'a_member_wallet_renders_validates_and_sends': 30,
        'a_tip_asked_while_the_wallet_loads_does_not_cost_the_screen': 25}

B58 = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
DEST = '5' + 'D' * 94                     # a well-formed stagenet address nobody holds
XMR = 10 ** 12


def scenario(fn):
    SCENARIOS[fn.__name__] = fn
    return fn


def _pubkey(fill):
    import coincurve
    return coincurve.PrivateKey(bytes([fill]) * 32).public_key.format(True)[1:].hex()


# ----------------------------------------------------------------------------- a fake monero-wallet-rpc

def _closed_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Rpc:
    """monero-wallet-rpc's JSON-RPC, one wallet per test account (the account is the first path segment).
    `mode[pk]`: 'ok' | 'lag' (answers, 0.8s late -- inside the budget) | 'slow' (answers after the node's read
    budget) | 'down' (the node is pointed at a closed port). Every call is recorded in `calls[pk]`, so "nothing was sent" is read off what the wallet was ASKED."""

    def __init__(self):
        self.mode, self.calls, self.wallets, self.started = {}, {}, {}, {}
        self.dead = _closed_port()
        rpc = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                pk = self.path.strip('/').split('/')[0]
                body = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)) or b'{}')
                rpc.started[pk] = rpc.started.get(pk, 0) + 1
                if rpc.mode.get(pk) == 'slow':
                    time.sleep(4)
                elif rpc.mode.get(pk) == 'lag':
                    time.sleep(.8)
                out = rpc.answer(pk, body.get('method'), body.get('params') or {})
                data = json.dumps({'jsonrpc': '2.0', 'id': body.get('id'), 'result': out}).encode()
                try:
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:
                    pass
        self.http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.http.daemon_threads = True
        threading.Thread(target=self.http.serve_forever, daemon=True).start()

    def url(self, pk):
        port = self.dead if self.mode.get(pk) == 'down' else self.http.server_address[1]
        return f'http://127.0.0.1:{port}/{pk}/json_rpc'

    def wallet(self, pk):
        return self.wallets.setdefault(pk, {'accounts': [{'account_index': 0, 'label': 'Primary account',
                                                          'base_address': '5' + 'P' * 94}],
                                            'balance': 0, 'unlocked': 0, 'blocks': 0, 'transfers': {}})

    def answer(self, pk, method, params):
        w = self.wallet(pk)
        self.calls.setdefault(pk, []).append((method, params))
        if method == 'get_accounts':
            return {'subaddress_accounts': w['accounts']}
        if method == 'create_account':
            idx = len(w['accounts'])
            w['accounts'].append({'account_index': idx, 'label': params.get('label'),
                                  'base_address': '5' + B58[idx + 20] * 94})
            return {'account_index': idx}
        if method == 'get_balance':
            return {'balance': w['balance'], 'unlocked_balance': w['unlocked'], 'blocks_to_unlock': w['blocks'],
                    'per_subaddress': [{'num_unspent_outputs': 2}]}
        if method == 'get_transfers':
            return w['transfers']
        if method == 'transfer_split':
            return {'tx_hash_list': ['e' * 64], 'amount_list': [d['amount'] for d in params['destinations']],
                    'fee_list': [30000000]}
        return {}

    def spends(self, pk):
        return [p for m, p in self.calls.get(pk, []) if m in ('transfer', 'transfer_split', 'sweep_all')]


# ------------------------------------------------------------------------------------- the node's own API

CUR = ContextVar('pc_test_account', default='')
MARKETS = {'mode': 'real'}       # 'real' (the router answers) | 'error' (503, the app's own JSON) | 'drop'


class Node:
    """The node's markets and Monero routers, REAL, in this process (cf. the private-docs Node)."""

    def __init__(self, pubkeys):
        self.users = {pk: SimpleNamespace(id=2000 + i, username='w%d' % i, nostr_npub=pk, is_admin=False,
                                          email='w%d@example.invalid' % i) for i, pk in enumerate(pubkeys)}
        self.loop = self.client = None
        self.rpc = Rpc()

    def start(self, mp):
        import httpx
        from fastapi import FastAPI, HTTPException, Request
        from app import auth as app_auth
        from app.database import get_db
        from app.routers import auth as router_auth
        from app.routers import markets, monero_user_wallet, monero_wallet
        from app.services import markets_service, monero_user_wallets
        from tests.member_fixtures import allow_keys

        rpc = self.rpc
        w = monero_user_wallets.user_wallets

        class PerAccount(type(w)):
            """The pooled wallet's RPC URL, per test account -- so one scenario's wallet can be down while
            another's answers. Everything else is the shipped UserWallets."""
            url = property(lambda self: rpc.url(CUR.get()), lambda self, v: None)
        mp.setattr(w, '__class__', PerAccount)
        mp.setattr(w, 'user', 'u')
        mp.setattr(w, 'password', 'p')
        mp.setattr(w, 'network', 'stagenet')
        mp.setattr(w, 'timeout', 2.0)                  # the node's read budget; 'slow' answers after 4s
        mp.setattr(w, '_lock', asyncio.Lock())
        mp.setattr(monero_user_wallets, 'zap_fee_percent', lambda: Decimal(0))
        mp.setattr(monero_user_wallet, 'zap_fee_percent', lambda: Decimal(0))
        mp.setattr(monero_user_wallet, 'explorer_tx_base', lambda _n=None: '')
        mp.setattr(markets_service, '_ai_available', lambda: True)
        mp.setattr(markets_service, '_launch', lambda *_a, **_k: None)     # never generate (that is the AI)
        mp.setattr(markets_service, '_memo', None)
        mp.setattr(markets_service, '_gen_task', None)
        mp.setattr(markets_service, '_degraded', False)
        allow_keys(mp, *self.users)

        app = FastAPI()
        app.include_router(markets.router)
        app.include_router(monero_wallet.router, prefix='/api/wallet/xmr')
        app.include_router(monero_user_wallet.router, prefix='/api/wallet/xmr/me')

        def who(request: Request):
            tok = (request.headers.get('authorization') or '').removeprefix('Bearer ').strip()
            if not tok.startswith('tok-') or tok[4:] not in self.users:
                raise HTTPException(401, 'not signed in')
            return self.users[tok[4:]]

        def admin_only(request: Request):
            user = who(request)
            if not user.is_admin:
                raise HTTPException(403, 'Admin access required')
            return user
        app.dependency_overrides[app_auth.get_current_user] = who
        app.dependency_overrides[router_auth.get_current_user] = who
        app.dependency_overrides[app_auth.get_admin_user] = admin_only
        app.dependency_overrides[get_db] = lambda: None
        ready = threading.Event()

        def run():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://node',
                                            timeout=60)
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
        if self.loop:
            self.loop.call_soon_threadsafe(self.loop.stop)

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
            # The member check the client asks before opening an app (instance-access.js): these accounts
            # are members -- the same answer allow_keys gives the routers. Membership is not under test here.
            tok = (h.headers.get('Authorization') or '').removeprefix('Bearer ').strip()
            if tok[4:] not in self.users:
                h._json({'detail': 'not signed in'}, 403)
            else:
                h._json({'pubkey': tok[4:], 'qualified': True})
            return True
        if not (p == '/api/markets' or p.startswith('/api/wallet/xmr/')):
            return False
        if p == '/api/markets' and MARKETS['mode'] == 'error':
            # What app/main.py's own handler answers when the node could not ask (`Unavailable`).
            h._json({'detail': 'Temporarily unavailable; try again shortly'}, 503)
            return True
        if p == '/api/markets' and MARKETS['mode'] == 'drop':
            h.close_connection = True                   # the connection dies with no answer at all
            return True
        headers = {k: v for k, v in h.headers.items() if k.lower() in ('authorization', 'content-type', 'accept')}
        tok = (h.headers.get('Authorization') or '').removeprefix('Bearer ').strip()

        async def call():
            CUR.set(tok[4:])
            return await self.client.request(method, path, headers=headers, content=body)
        r = asyncio.run_coroutine_threadsafe(call(), self.loop).result(90)
        h.send_response(r.status_code)
        h.send_header('Content-Type', r.headers.get('content-type', 'application/json'))
        h.send_header('Content-Length', str(len(r.content)))
        h.end_headers()
        h.wfile.write(r.content)
        return True


NODE = None


@pytest.fixture(scope='module')
def ran(request):
    global NODE
    H.start()
    NODE = Node([_pubkey(FIRST_FILL + i) for i in range(len(SCENARIOS))])
    held = H._held_doc_req

    def held_or_xdc(filters):
        """`hold_xdc`: this relay never answers a gallery read (webxdc file events / #webxdc notes)."""
        if H.STATE.get('hold_xdc') and any(
                any('webxdc' in str(m) for m in (f.get('#m') or [])) or 'webxdc' in (f.get('#t') or [])
                for f in filters):
            return True
        return held(filters)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(H, '_held_doc_req', held_or_xdc)
        NODE.start(mp)
        try:
            check = run_scenarios(request, SCENARIOS, COST, FIRST_FILL)
        finally:
            NODE.stop()
    return check


async def until(b, expr, seconds=30, what=None):
    await H.wait(b, expr, seconds, state="({errors:__errors,toasts:__toasts,"
                 "feed:(document.querySelector('#feed')||{}).innerText,"
                 "modal:[...document.querySelectorAll('.mw-modal')].map(m=>m.innerText)})")


FEED = "((document.querySelector('#feed')||{}).innerText||'')"
SET = """((sel,v)=>{const x=document.querySelector(sel);x.value=v;x.dispatchEvent(new Event('input',{bubbles:true}));
  return true})"""


async def click(b, sel):
    await until(b, "!!document.querySelector(%s)" % json.dumps(sel))
    await b.js("document.querySelector(%s).click();true" % json.dumps(sel))


# ------------------------------------------------------------------------------------------------ wallet

async def open_wallet(b):
    await b.js("__PC.switchView('wallet');true")
    await until(b, "window.__PC.VIEW==='wallet'")


def _acct(pk):
    w = NODE.rpc.wallet(pk)
    label = 'pc:' + pk
    return next((a for a in w['accounts'] if a['label'] == label), None)


@scenario
async def a_member_wallet_renders_validates_and_sends(S):
    rpc = NODE.rpc
    rpc.mode[S.pk] = 'ok'
    w = rpc.wallet(S.pk)
    w.update(balance=int(1.25 * XMR), unlocked=1 * XMR, blocks=3)
    # The user's account is index 1 (created on first sight); its history, plus a row of ANOTHER account
    # the node must never forward.
    w['transfers'] = {'in': [{'txid': 'a' * 64, 'amount': XMR // 2, 'timestamp': 1700000000, 'height': 9,
                              'subaddr_index': {'major': 1, 'minor': 0}},
                             {'txid': 'c' * 64, 'amount': 7 * XMR, 'timestamp': 1700000100, 'height': 10,
                              'subaddr_index': {'major': 0, 'minor': 0}}]}
    b = await S.device()
    await open_wallet(b)
    await until(b, "!!document.querySelector('.mw-balance') && !!document.querySelector('#mw-me-send')")
    acct = _acct(S.pk)
    assert acct and acct['account_index'] == 1, NODE.rpc.wallet(S.pk)['accounts']
    feed = await b.js(FEED)
    bal = await b.js("document.querySelector('.mw-balance').innerText")
    assert 'Available balance' in bal, bal
    assert await b.js("document.querySelector('.mw-balance strong').innerText.trim()") == '1 XMR', bal
    assert '1.25 XMR total' in bal and '0.25 XMR confirming' in bal, bal
    assert acct['base_address'] in feed, 'the receiving address is not the one the wallet holds for this user'
    assert '+0.5 XMR' in feed, feed
    assert '+7 XMR' not in feed, "another account's payment reached this user's screen"
    assert 'node operator' not in feed and 'admin-only' not in feed

    # ---- the send sheet refuses what it must, without asking the wallet to move anything
    await click(b, '#mw-me-send')
    await until(b, "!!document.querySelector('#mw-ms-to')")

    async def attempt(to, amount, toast):
        await b.js("window.__toasts=[];true")
        await b.js(SET + "('#mw-ms-to',%s)" % json.dumps(to))
        await b.js(SET + "('#mw-ms-amount',%s)" % json.dumps(amount))
        await b.js("document.querySelector('#mw-ms-review').click();true")
        await until(b, "__toasts.some(t=>t.includes(%s))" % json.dumps(toast), 10)
        assert await b.js("!document.querySelector('#mw-ms-go')"), 'the confirm sheet opened for ' + repr((to, amount))
    await attempt('4' + 'D' * 94, '0.1', 'check the Monero address')          # a MAINNET address on stagenet
    await attempt(DEST[:-1], '0.1', 'check the Monero address')               # one character short
    await attempt(DEST, '0', 'enter a positive XMR amount')
    await attempt(DEST, '0.0000000000001', 'enter a positive XMR amount')     # 13 decimal places
    await attempt(DEST, '1.5', 'only 1 XMR is available')                     # more than is UNLOCKED
    assert rpc.spends(S.pk) == [], ('the wallet was asked to spend on an invalid form', rpc.spends(S.pk))

    # ---- a valid one: review, the irreversible-tick gate, then the wallet is asked for exactly that
    await b.js(SET + "('#mw-ms-to',%s)" % json.dumps(DEST))
    await b.js(SET + "('#mw-ms-amount','0.1')")
    await b.js("document.querySelector('#mw-ms-review').click();true")
    await until(b, "!!document.querySelector('#mw-ms-go')")
    assert await b.js("document.querySelector('#mw-ms-go').disabled"), 'Send now is live before the tick'
    confirm = await b.js("document.querySelector('.mw-confirm').innerText")
    assert '0.1 XMR' in confirm and DEST in confirm, confirm
    await b.js("document.querySelector('#mw-ms-go').click();true")             # disabled: nothing happens
    await b.js("(()=>{const x=document.querySelector('#mw-ms-understand');x.checked=true;"
               "x.dispatchEvent(new Event('change',{bubbles:true}));return true})()")
    await until(b, "!document.querySelector('#mw-ms-go').disabled")
    await b.js("document.querySelector('#mw-ms-go').click();true")
    await until(b, "__toasts.some(t=>t==='payment sent')", 20)
    sent = rpc.spends(S.pk)
    assert len(sent) == 1, sent
    assert sent[0]['account_index'] == 1, 'paid from somebody else\'s account'
    assert sent[0]['destinations'] == [{'address': DEST, 'amount': XMR // 10}], sent[0]['destinations']
    assert no_errors(await b.js('__errors')) == []


WATCH_ADMIN = r'''(()=>{window.__sawAdmin=[];const f=document.querySelector('#feed');
  const look=()=>{const t=f.innerText||'';if(/node operator|admin-only|safe external-wallet mode/.test(t))__sawAdmin.push(t.slice(0,160));};
  new MutationObserver(look).observe(f,{childList:true,subtree:true,characterData:true});return true})()'''


@scenario
async def refresh_on_a_busy_wallet_never_shows_the_operators_refusal(S):
    rpc = NODE.rpc
    rpc.mode[S.pk] = 'ok'
    rpc.wallet(S.pk).update(balance=2 * XMR, unlocked=2 * XMR)
    b = await S.device()
    await open_wallet(b)
    await until(b, "!!document.querySelector('#mw-me-send')")
    await b.js(WATCH_ADMIN)
    rpc.mode[S.pk] = 'slow'                    # monero-wallet-rpc scanning: every read outlives the node's budget
    n0 = len(rpc.calls.get(S.pk, []))
    await click(b, '#mw-refresh')
    # Until the busy wallet has been asked and has failed to answer in time, then until the screen settles: either
    # what this device last read of THEIR wallet, or a sentence about their wallet being busy -- never a spinner.
    await S.until(b, lambda: len(rpc.calls.get(S.pk, [])) > n0, 30, 'the busy wallet to be asked')
    await until(b, "!document.querySelector('#feed .spinner') && (!!document.querySelector('#mw-me-send') || "
                   "/busy|catching up|could not be reached/i.test(%s))" % FEED, 30)
    assert await b.js('__sawAdmin') == [], 'a member was shown the operator wallet\'s refusal'
    assert rpc.spends(S.pk) == []
    # Back to answering: the next Refresh (or Try again) shows the new balance, still without the refusal.
    rpc.mode[S.pk] = 'ok'
    rpc.wallet(S.pk).update(balance=5 * XMR, unlocked=5 * XMR)
    await click(b, '#mw-refresh, #mw-retry')
    await until(b, "!!document.querySelector('#mw-me-send') && /5 XMR/.test(document.querySelector('.mw-balance').innerText)", 40)
    assert await b.js('__sawAdmin') == []


@scenario
async def a_member_wallet_that_is_down_says_so_and_retry_recovers(S):
    rpc = NODE.rpc
    rpc.mode[S.pk] = 'down'                    # nothing listening where the node expects its wallet
    rpc.wallet(S.pk).update(balance=3 * XMR, unlocked=3 * XMR)
    b = await S.device()
    await open_wallet(b)
    await b.js(WATCH_ADMIN)
    t0 = time.monotonic()
    await until(b, "!document.querySelector('#feed .spinner') && !!document.querySelector('#mw-retry')", 30)
    took = time.monotonic() - t0
    feed = await b.js(FEED)
    assert took < 15, ('the wallet screen spun for', took)
    assert 'node operator' not in feed and 'admin-only' not in feed, feed
    assert await b.js('__sawAdmin') == [], 'a member was shown the operator wallet\'s refusal'
    assert 'unavailable' in feed.lower() or 'could not' in feed.lower(), feed
    assert rpc.spends(S.pk) == []
    rpc.mode[S.pk] = 'ok'
    await click(b, '#mw-retry')
    await until(b, "!!document.querySelector('#mw-me-send') && /3 XMR/.test(document.querySelector('.mw-balance').innerText)")
    assert no_errors(await b.js('__errors')) == []


@scenario
async def a_tip_asked_while_the_wallet_loads_does_not_cost_the_screen(S):
    """Two probes of the member's wallet overlap -- the screen's, and a second one (the ɱ button's `meTip`, which is
    also what the module's own sign-in warm-up runs when it fires late). The screen's probe is superseded; it must
    still end on the member's wallet, not the node wallet's admin-only refusal."""
    rpc = NODE.rpc
    rpc.mode[S.pk] = 'lag'                     # a wallet that answers, just not instantly
    rpc.wallet(S.pk).update(balance=4 * XMR, unlocked=4 * XMR)
    b = await S.device()
    await b.js(WATCH_ADMIN)
    await b.js("__PC.switchView('wallet');true")
    await S.until(b, lambda: rpc.started.get(S.pk, 0) >= 1, 20, "the wallet screen's probe to reach the wallet")
    # What tips.js runs when a person presses ɱ on a post (desktop: another window; or the warm-up's late retry).
    await b.js("(window.PCMoneroWallet.meTip({address:%s,name:'someone'}),true)" % json.dumps(DEST))
    await until(b, "!document.querySelector('#feed .spinner') && (!!document.querySelector('#mw-me-send') || "
                   "!!document.querySelector('#mw-retry'))", 30)
    assert await b.js('__sawAdmin') == [], "a member's wallet screen was left on the operator wallet's refusal"
    assert await b.js("document.querySelector('.mw-balance strong').innerText.trim()") == '4 XMR'
    assert rpc.spends(S.pk) == []


# ------------------------------------------------------------------------------------------------ webxdc

def _xdc_bytes(name):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr('index.html', '<!doctype html><title>%s</title><p>%s</p>' % (name, name))
        z.writestr('manifest.toml', 'name = "%s"\n' % name)
    return buf.getvalue()


SIGN_AS = """(async(ev,fill)=>NostrTools.finalizeEvent(ev,new Uint8Array(32).fill(fill)))"""


@scenario
async def the_gallery_survives_a_silent_relay_then_lists_and_plays(S):
    H.STATE['hold_xdc'] = True                 # this node's relay never answers a gallery read
    try:
        b = await S.device()
        await b.js("__PC.switchView('xdc');true")
        await until(b, "!!document.querySelector('#xdc-gal-refresh')")
        t0 = time.monotonic()
        await until(b, "!document.querySelector('#xdc-gal-refresh').disabled && !document.querySelector('#feed .spinner')", 30)
        assert time.monotonic() - t0 < 25
        assert 'No mini apps found yet' in await b.js(FEED)
    finally:
        H.STATE['hold_xdc'] = False

    # Another account posts a mini app (its archive on Blossom), and somebody makes a move in it.
    data = _xdc_bytes('Pixel Poll')
    sha = hashlib.sha256(data).hexdigest()
    H.STATE['blobs'][sha] = data
    port = H.HTTP.server_address[1]
    url = f'http://127.0.0.1:{port}/blossom/{sha}'
    now = int(time.time())
    app = await b.js(SIGN_AS + "(%s,%d)" % (json.dumps({
        'kind': 1063, 'created_at': now - 60, 'content': 'try my poll',
        'tags': [['m', 'application/x-webxdc'], ['url', url], ['x', sha], ['alt', 'Webxdc app: Pixel Poll'],
                 ['webxdc', 'pixel-poll-%s' % S.pk[:8]]]}), OTHER_FILL))
    move = await b.js(SIGN_AS + "(%s,%d)" % (json.dumps({
        'kind': 4932, 'created_at': now - 30, 'content': '{"payload":1}',
        'tags': [['i', 'pixel-poll-%s' % S.pk[:8]], ['alt', 'Webxdc update']]}), OTHER_FILL))
    H.RELAY._store(app)
    H.RELAY._store(move)
    fetched = []

    def watch(h, method, path, body):
        if method == 'GET' and path.split('?')[0] == f'/blossom/{sha}':
            fetched.append(path)
        return False
    H.ROUTES.insert(0, watch)
    try:
        await click(b, '#xdc-gal-refresh')
        await until(b, "/Pixel Poll/.test(%s)" % FEED, 30)
        tile = await b.js("document.querySelector('.xdc-tile').innerText")
        assert '1 move' in tile, tile
        assert not await b.js("document.querySelector('#xdc-gal-refresh').disabled")
        await b.js("document.querySelector('.xdc-tile .xdc-tplay').click();true")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 20
        while not fetched and loop.time() < deadline:
            await asyncio.sleep(.2)
        assert fetched, 'Play did not fetch the posted archive'
        await until(b, "!!(window.PCWebxdc && PCWebxdc.sheetOpen && PCWebxdc.sheetOpen())", 20)
    finally:
        H.ROUTES.remove(watch)


# ----------------------------------------------------------------------------------------------- markets

REPORT = {'generated_at': 1761200000, 'coins': [
    {'sym': 'XMR', 'name': 'Monero', 'summary': 'A quiet day for privacy coins.', 'price_str': '$321.00',
     'chg24h': -1.5, 'articles': [{'title': 'Monero holds steady', 'url': 'https://example.invalid/xmr'},
                                  {'title': 'bad link', 'url': 'javascript:alert(1)'}]},
    {'sym': 'BTC', 'name': 'Bitcoin', 'summary': '', 'price_str': '$99,000', 'chg24h': 2.25, 'articles': []}]}


async def open_markets(b):
    await b.js("__PC.switchView('home');true")
    await until(b, "window.__PC.VIEW==='home'")
    await b.js("__PC.switchView('markets');true")


@scenario
async def markets_digest_share_and_every_failure_state(S):
    from app.services import markets_service as ms
    b = await S.device()

    # Building: the server has no report yet; the screen says so, and heals ON ITS OWN when one lands.
    MARKETS['mode'] = 'real'
    ms._memo, ms._gen_task = None, SimpleNamespace(done=lambda: False)
    await open_markets(b)
    await until(b, "/Building today/.test(%s)" % FEED)
    ms._memo = json.loads(json.dumps(REPORT))
    await until(b, "document.querySelectorAll('.mkts-card').length===2", 20)
    xmr = await b.js("document.querySelector('.mkts-card[data-sym=\"XMR\"]').innerText")
    btc = await b.js("document.querySelector('.mkts-card[data-sym=\"BTC\"]').innerText")
    assert '$321.00' in xmr and '▼ 1.50%' in xmr and 'A quiet day' in xmr and 'Monero holds steady' in xmr, xmr
    assert '$99,000' in btc and '▲ 2.25%' in btc and 'No data available right now' in btc, btc
    assert await b.js("!document.querySelector('.mkts-card a[href^=\"javascript\"]')"), 'an unsafe link was drawn'

    # Share: the briefing goes into the composer as a new note.
    await b.js("document.querySelector('.mkts-card[data-sym=\"XMR\"] .mkts-post').click();true")
    await until(b, "[...document.querySelectorAll('textarea')].some(t=>t.value.includes('Monero (XMR)'))", 20)
    text = await b.js("[...document.querySelectorAll('textarea')].find(t=>t.value.includes('Monero (XMR)')).value")
    assert 'A quiet day for privacy coins.' in text and 'https://example.invalid/xmr' in text, text
    assert await b.js("(window.__PC.closeModal&&__PC.closeModal(),true)")

    # The server answers an ERROR (the node's own 503 JSON): never "building", an explicit sentence.
    MARKETS['mode'] = 'error'
    await open_markets(b)
    await until(b, "!!document.querySelector('.mkts-note')")
    note = await b.js("document.querySelector('.mkts-note').innerText")
    assert 'Building' not in note, ('a server error reads as a digest being built', note)
    assert "reach" in note or 'unavailable' in note, note

    # The connection dies with no answer.
    MARKETS['mode'] = 'drop'
    await open_markets(b)
    await until(b, "/Can't reach the server/.test(%s)" % FEED)

    # A node with no AI backend: the feature says it is off, it does not spin.
    MARKETS['mode'] = 'real'
    ms._ai_available = lambda: False
    try:
        await open_markets(b)
        await until(b, "/needs the AI backend/.test(%s)" % FEED)
        assert await b.js("!document.querySelector('#feed .spinner')")
    finally:
        ms._ai_available = lambda: True

    # And back: re-entering shows the digest again.
    await open_markets(b)
    await until(b, "document.querySelectorAll('.mkts-card').length===2")
    assert no_errors(await b.js('__errors')) == []


# --------------------------------------------------------------------------------------------- the tests

@CHROME
def test_a_member_wallet_renders_validates_and_sends(ran):
    ran('a_member_wallet_renders_validates_and_sends')


@CHROME
def test_refresh_on_a_busy_wallet_never_shows_the_operators_refusal(ran):
    ran('refresh_on_a_busy_wallet_never_shows_the_operators_refusal')


@CHROME
def test_a_member_wallet_that_is_down_says_so_and_retry_recovers(ran):
    ran('a_member_wallet_that_is_down_says_so_and_retry_recovers')


@CHROME
def test_a_tip_asked_while_the_wallet_loads_does_not_cost_the_screen(ran):
    ran('a_tip_asked_while_the_wallet_loads_does_not_cost_the_screen')


@CHROME
def test_the_gallery_survives_a_silent_relay_then_lists_and_plays(ran):
    ran('the_gallery_survives_a_silent_relay_then_lists_and_plays')


@CHROME
def test_markets_digest_share_and_every_failure_state(ran):
    ran('markets_digest_share_and_every_failure_state')
