"""The Terminal and the Calculator, driven end to end in the shipped web client on a REAL node.

  terminal  the REAL SSH terminal: app/routers/ssh_term.py (`/api/ssh/hosts`, `/api/ssh/sessions`, the `/ws/ssh`
            socket) served by uvicorn in this process, app/services/ssh_service.py (paramiko) dialling a REAL SSH
            server (paramiko's server side, here) that runs a real bash in a real PTY. The browser opens Terminal,
            types the host's password into the app's prompt, and types a command with the keyboard; the output is
            read off the screen. A reload comes back to the SAME shell (a variable set before it is still set), a
            FRESH device of the account finds the shell running and attaches to it from its tab, a wrong password
            says so instead of hanging, and the host list answering 503 or slowly says so / connects late -- never a
            screen that sits there. Only the account lookup (an id -> a member) and the operator's settings (the
            host line, the allowlist) are stand-ins; the session token is a real one the router decodes.
  calculator  the keypad computes and the tape and DEG/RAD survive a reload; a SECOND account signed in on the same
            device does not see the first account's tape (it is a log of what somebody calculated); and it works
            with the relay saying nothing at all (it needs no network).

Multiplexing (tmux/screen on the far end) is OFF here: the far end is this machine, and `screen -xRR` would leave
real screen sessions behind on it. ssh_service's resume (the PTY outliving the socket) is what is exercised.
Harness: tests/client/discover_flows_harness.py. Fail-first: PC_JOURNEY_OVERRIDE="/static/js/client/term.js=/tmp/x.js".
"""
import asyncio
import json
import os
import socket
import subprocess
import tempfile
import threading
import time

import pytest

from tests.client import discover_flows_harness as F
from tests.client import realnode_harness as R
from tests.client import wallpaper_harness as H

CHROME = pytest.mark.skipif(not R.chrome_ok(), reason='Chrome required')
FEATURE_COVERS = ('terminal', 'calculator')
FIRST_FILL = 110
SCENARIOS = {}
COST = {'a_fresh_device_finds_the_running_shell_and_attaches_to_it': 30,
        'a_command_runs_and_a_reload_comes_back_to_the_same_shell': 30}
PASSWORD = 'pc-test-ssh-%d' % os.getpid()
FORCE_DOM = "try{localStorage.setItem('pc_term_gl_lost',String(Date.now()))}catch(_){}\n"   # read rows off the DOM


def scenario(fn):
    SCENARIOS[fn.__name__] = fn
    return fn


# ------------------------------------------------------------------------------------------------ the SSH host
class SshBox:
    """An SSH server (paramiko's server side) on loopback: password auth, a PTY, a real bash per channel."""

    def __init__(self):
        import paramiko
        self.paramiko = paramiko
        self.key = paramiko.RSAKey.generate(2048)
        self.home = tempfile.mkdtemp(prefix='pc-ssh-home-')
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(('127.0.0.1', 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.known_hosts = os.path.join(self.home, 'known_hosts')
        with open(self.known_hosts, 'w') as f:
            f.write('[127.0.0.1]:%d %s %s\n' % (self.port, self.key.get_name(), self.key.get_base64()))
        self.procs, self.transports, self.stopped = [], [], False
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while not self.stopped:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        paramiko, box = self.paramiko, self
        shell = threading.Event()
        size = {'cols': 80, 'rows': 24, 'fd': None}

        class Iface(paramiko.ServerInterface):
            def get_allowed_auths(self, username):
                return 'password'

            def check_auth_password(self, username, password):
                ok = username == 'tester' and password == PASSWORD
                return paramiko.AUTH_SUCCESSFUL if ok else paramiko.AUTH_FAILED

            def check_channel_request(self, kind, chanid):
                return paramiko.OPEN_SUCCEEDED if kind == 'session' else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

            def check_channel_pty_request(self, channel, term, width, height, pw, ph, modes):
                size['cols'], size['rows'] = width or 80, height or 24
                return True

            def check_channel_window_change_request(self, channel, width, height, pw, ph):
                size['cols'], size['rows'] = width, height
                if size['fd'] is not None:
                    box._winsize(size['fd'], height, width)
                return True

            def check_channel_shell_request(self, channel):
                shell.set()
                return True

        t = paramiko.Transport(conn)
        self.transports.append(t)
        t.add_server_key(self.key)
        try:
            t.start_server(server=Iface())
        except Exception:
            return
        chan = t.accept(30)
        if chan is None or not shell.wait(30):
            return
        master, slave = os.openpty()
        size['fd'] = master
        self._winsize(master, size['rows'], size['cols'])
        env = {'HOME': self.home, 'PATH': '/usr/bin:/bin', 'TERM': 'xterm-256color', 'PS1': 'pc$ ', 'LANG': 'C.UTF-8'}
        p = subprocess.Popen(['/bin/bash', '--noprofile', '--norc', '-i'], stdin=slave, stdout=slave, stderr=slave,
                             env=env, cwd=self.home, start_new_session=True, close_fds=True)
        os.close(slave)
        self.procs.append(p)

        def out():
            try:
                while True:
                    d = os.read(master, 65536)
                    if not d:
                        break
                    chan.sendall(d)
            except OSError:
                pass
            finally:
                try:
                    chan.close()
                except Exception:
                    pass
        threading.Thread(target=out, daemon=True).start()
        try:
            while True:
                d = chan.recv(65536)
                if not d:
                    break
                os.write(master, d)
        except Exception:
            pass
        finally:
            p.kill()
            try:
                os.close(master)
            except OSError:
                pass

    @staticmethod
    def _winsize(fd, rows, cols):
        import fcntl
        import struct
        import termios
        try:
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))
        except OSError:
            pass

    def stop(self):
        self.stopped = True
        try:
            self.sock.close()
        except OSError:
            pass
        for p in self.procs:
            try:
                p.kill()
                p.wait(5)
            except Exception:
                pass
        for t in self.transports:
            try:
                t.close()
            except Exception:
                pass


# ------------------------------------------------------------------------------------------------ the node
class Ctx:
    def __init__(self, n):
        import httpx
        import paramiko
        import uvicorn
        from fastapi import FastAPI, HTTPException, Request
        from app import auth as app_auth
        from app.database import get_db
        from app.routers import ssh_term
        from app.services import ssh_service, user_table
        self.mp = mp = pytest.MonkeyPatch()
        self.n = n
        self.box = SshBox()
        self.users = F.members(FIRST_FILL, SCENARIOS)
        by_id = {u.id: u for u in self.users.values()}
        # The operator's settings for the terminal (Admin -> Nodes): on, one host, every member allowed.
        conf = {'ssh_terminal_enabled': 'true', 'ssh_terminal_multiplex': 'false',
                'ssh_hosts': 'box tester@127.0.0.1:%d' % self.box.port,
                'ssh_terminal_users': ' '.join(self.users)}
        mp.setattr(ssh_service, '_get', lambda k, d='': conf.get(k) or d)
        # This test host's key, the way the server account's known_hosts would hold it (RejectPolicy stays real).
        mp.setattr(paramiko.SSHClient, 'load_system_host_keys',
                   lambda cli, filename=None: cli.load_host_keys(self.box.known_hosts))

        async def aget(_db, uid):
            return by_id.get(int(uid))
        mp.setattr(user_table, 'aget', aget)
        mp.setattr(ssh_service, '_sessions', {})
        self.tokens = {pk: app_auth.create_access_token({'sub': str(u.id)}) for pk, u in self.users.items()}

        def who(request: Request):
            tok = (request.headers.get('authorization') or '').removeprefix('Bearer ').strip()
            payload = app_auth.decode_token(tok)
            u = by_id.get(int((payload or {}).get('sub') or 0))
            if not u:
                raise HTTPException(401, 'not signed in')
            return u
        app = FastAPI()
        app.include_router(ssh_term.router)
        app.include_router(ssh_term.ws_router)
        app.dependency_overrides[app_auth.get_current_user] = who
        app.dependency_overrides[get_db] = lambda: None
        s = socket.socket()
        s.bind(('127.0.0.1', 0))
        self.port = s.getsockname()[1]
        s.close()
        self.server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=self.port, log_level='warning',
                                                    ws='websockets', lifespan='off'))
        threading.Thread(target=self.server.run, daemon=True).start()
        for _ in range(100):
            if self.server.started:
                break
            time.sleep(.05)
        self.http = httpx.Client(base_url='http://127.0.0.1:%d' % self.port, timeout=60, trust_env=False)
        self.faults, self.seen = {}, []
        H.ROUTES.insert(0, self.route)
        self.gate = F.Gate(n.home)

    def fail(self, prefix, how, tag):
        self.faults[(prefix, tag)] = how

    def heal(self, prefix, tag):
        self.faults.pop((prefix, tag), None)

    def route(self, h, method, path, body):
        p = path.split('?')[0]
        tag = ''
        for part in (h.headers.get('Cookie') or '').split(';'):
            k, _, v = part.strip().partition('=')
            if k == 'pcdev':
                tag = v
        if p == '/api/auth/nostr-login' and method == 'POST':
            pk = (json.loads(body or b'{}') or {}).get('pubkey') or ''
            if pk not in self.users:
                return False
            u = self.users[pk]
            h._json({'access_token': self.tokens[pk], 'token_type': 'bearer',
                     'user': {'id': u.id, 'username': u.username, 'nostr_npub': pk, 'is_admin': False}})
            return True
        if p == '/api/instance-welcome/access':
            h._json({'pubkey': '', 'qualified': True})
            return True
        if p == '/ws/ssh':
            h._splice_to(self.port)                    # the upgrade, byte for byte, as nginx routes it
            return True
        if not p.startswith('/api/ssh/'):
            return False
        self.seen.append((p, tag))
        f = next((v for (pre, t), v in self.faults.items() if p.startswith(pre) and t == tag), None)
        if f == 503:
            h._json({'detail': 'Temporarily unavailable; try again shortly'}, 503)
            return True
        if isinstance(f, (int, float)) and f:
            time.sleep(f)
        headers = {k: v for k, v in h.headers.items() if k.lower() in ('authorization', 'content-type', 'accept')}
        r = self.http.request(method, path, headers=headers, content=body or None)
        h.send_response(r.status_code)
        h.send_header('Content-Type', r.headers.get('content-type', 'application/json'))
        h.send_header('Content-Length', str(len(r.content)))
        h.end_headers()
        h.wfile.write(r.content)
        return True

    def stop(self):
        try:
            H.ROUTES.remove(self.route)
        except ValueError:
            pass
        from app.services import ssh_service
        for sess in list(ssh_service._sessions.values()):
            try:
                sess.close()
            except Exception:
                pass
        self.server.should_exit = True
        self.box.stop()
        self.http.close()
        self.mp.undo()


@pytest.fixture(scope='module')
def ran(request):
    return F.run(request, SCENARIOS, COST, FIRST_FILL, setup=Ctx)


async def reload(d):
    await d.b.js("window.__beforeReload=1;true")
    await d.b.call('Page.navigate', {'url': d.node.origin + '/client'})
    await R.wait(d.b, "!window.__beforeReload && !!window.__PC && !!__PC.me && !!__PC.me() && Relay.status==='ok'", 60,
                 'reopened and signed in')


# ------------------------------------------------------------------------------------------------ terminal
SCREEN = "[...document.querySelectorAll('#tty-screen .xterm-rows > div')].map(r=>r.textContent).join('\\n')"
STATE = "(document.getElementById('tty-state')||{}).textContent||''"


async def open_terminal(d):
    await d.b.js("PCInstanceAccess.accept({pubkey:__PC.me().pubkey,qualified:true},__PC.me().pubkey);true")
    await d.b.js("__PC.switchView('terminal');true")
    await F.wait(d, "!!document.getElementById('tty-state')", 30, 'the Terminal')


async def connected(d, seconds=40):
    await R.wait(d.b, "!!(window.PCTerm && PCTerm.connected()) && /pc\\$ /.test(%s)" % SCREEN, seconds,
                 'a shell prompt on screen', state="({state:%s,screen:%s,toasts:window.__toastLog})" % (STATE, SCREEN))


async def type_line(d, text):
    """Typed with the keyboard into the terminal, then Enter -- as a person types a command."""
    await d.b.js("(document.querySelector('#tty-screen .xterm-helper-textarea')||{focus(){}}).focus();true")
    await d.b.call('Input.insertText', {'text': text})
    for t in ('keyDown', 'keyUp'):
        await d.b.call('Input.dispatchKeyEvent', {'type': t, 'key': 'Enter', 'code': 'Enter',
                                                  'windowsVirtualKeyCode': 13, **({'text': '\r'} if t == 'keyDown' else {})})


async def shows(d, text, seconds=20, what=None):
    await R.wait(d.b, "%s.includes(%s)" % (SCREEN, json.dumps(text)), seconds, what or ('%r on the screen' % text),
                 state="({state:%s,screen:%s})" % (STATE, SCREEN))


@scenario
async def a_command_runs_and_a_reload_comes_back_to_the_same_shell(S):
    a = await S.device(0, pre_init=FORCE_DOM)
    await open_terminal(a)
    await H.answer_prompt(a.b, PASSWORD, 40)           # the host has no key on the server: the app asks
    await connected(a)
    await type_line(a, 'echo answer-$((6*7)); export PC_MARK=kept-%d' % S.base)
    await shows(a, 'answer-42', what='the command\'s output')
    await reload(a)
    await open_terminal(a)
    await connected(a)
    assert not await a.b.js("!!document.querySelector('.uiconfirm-bg .uiprompt-in')"), 'a reattach asked for the password'
    await type_line(a, 'echo mark=$PC_MARK')
    await shows(a, 'mark=kept-%d' % S.base, what='the variable set before the reload (the same shell)')


@CHROME
def test_a_command_runs_and_a_reload_comes_back_to_the_same_shell(ran):
    ran('a_command_runs_and_a_reload_comes_back_to_the_same_shell')


@scenario
async def a_fresh_device_finds_the_running_shell_and_attaches_to_it(S):
    a = await S.device(0, pre_init=FORCE_DOM)
    await open_terminal(a)
    await H.answer_prompt(a.b, PASSWORD, 40)
    await connected(a)
    await type_line(a, 'export PC_MARK=from-the-laptop-%d' % S.base)
    sid = await a.b.js("PCTerm.sessionId()")
    assert sid
    b = await S.device(0, pre_init=FORCE_DOM)          # the phone: no session id of its own
    await open_terminal(b)
    sel = "#tty-sessions .tty-tab[data-tab=%s]" % json.dumps(sid)
    await R.wait(b.b, "!!document.querySelector(%s)" % json.dumps(sel), 40, 'the running shell offered as a tab',
                 state="({state:%s,tabs:(document.querySelector('#tty-sessions')||{}).innerText})" % STATE)
    await b.b.js("document.querySelector(%s).click();true" % json.dumps(sel))
    await connected(b)
    await type_line(b, 'echo seen=$PC_MARK')
    await shows(b, 'seen=from-the-laptop-%d' % S.base, what='the laptop\'s shell, on the phone')


@CHROME
def test_a_fresh_device_finds_the_running_shell_and_attaches_to_it(ran):
    ran('a_fresh_device_finds_the_running_shell_and_attaches_to_it')


@scenario
async def a_wrong_password_says_so(S):
    a = await S.device(0, pre_init=FORCE_DOM)
    await open_terminal(a)
    await H.answer_prompt(a.b, 'not-the-password', 40)
    await R.wait(a.b, "/could not connect/i.test(%s)" % STATE, 40, 'the failure said on screen',
                 state="({state:%s,title:(document.getElementById('tty-state')||{}).title})" % STATE)
    assert not await a.b.js("PCTerm.connected()")
    await asyncio.sleep(1.5)                           # the server's `end` follows its refusal
    assert 'could not connect' in (await a.b.js(STATE)).lower(), ('the reason was replaced', await a.b.js(STATE))


@CHROME
def test_a_wrong_password_says_so(ran):
    ran('a_wrong_password_says_so')


@scenario
async def the_host_list_answering_503_says_so_and_reopening_recovers(S):
    tag = 'tt%d' % S.base
    S.ctx.fail('/api/ssh/hosts', 503, tag)
    try:
        a = await S.device(0, pre_init=FORCE_DOM, tag=tag)
        await open_terminal(a)
        await R.wait(a.b, "/retrying/.test(%s)" % STATE, 30, 'the Terminal said it could not reach the server',
                     state="({state:%s})" % STATE)
        assert not await a.b.js("!!document.querySelector('.uiconfirm-bg .uiprompt-in')"), 'asked for a password for no host'
        S.ctx.heal('/api/ssh/hosts', tag)
        await a.b.js("__PC.switchView('home');true")
        await open_terminal(a)                         # opening it again, as a person does
        await H.answer_prompt(a.b, PASSWORD, 40)
        await connected(a)
    finally:
        S.ctx.heal('/api/ssh/hosts', tag)


@CHROME
def test_the_host_list_answering_503_says_so_and_reopening_recovers(ran):
    ran('the_host_list_answering_503_says_so_and_reopening_recovers')


@scenario
async def a_slow_host_list_still_connects(S):
    tag = 'ts%d' % S.base
    S.ctx.fail('/api/ssh/hosts', 3, tag)
    try:
        a = await S.device(0, pre_init=FORCE_DOM, tag=tag)
        await open_terminal(a)
        await H.answer_prompt(a.b, PASSWORD, 40)
        await connected(a)
    finally:
        S.ctx.heal('/api/ssh/hosts', tag)


@CHROME
def test_a_slow_host_list_still_connects(ran):
    ran('a_slow_host_list_still_connects')


# ------------------------------------------------------------------------------------------------ calculator
TAPE = "[...document.querySelectorAll('.calc-tape-row')].map(r=>r.textContent.replace(/\\s+/g,' ').trim())"


async def open_calculator(d):
    await d.b.js("__PC.switchView('calculator');true")
    await F.wait(d, "!!document.querySelector('#feed .calc-app')", 30, 'the Calculator')


async def press(d, *keys):
    for k in keys:
        await d.b.js("document.querySelector(%s).click();true" % json.dumps('.calc-key[data-k="%s"]' % k))


@scenario
async def a_calculation_and_its_tape_survive_a_reload(S):
    a = await S.device(0)
    await open_calculator(a)
    await press(a, '1', '2', '+', '3', '4', 'eq')
    assert await a.b.js("document.querySelector('.calc-expr').textContent") == '46'
    await a.b.js("document.querySelector('.calc-deg').click();true")         # DEG -> RAD
    assert await a.b.js("document.querySelector('.calc-deg').textContent") == 'RAD'
    await reload(a)
    await open_calculator(a)
    assert await a.b.js(TAPE) == ['12+34= 46'], await a.b.js(TAPE)
    assert await a.b.js("document.querySelector('.calc-deg').textContent") == 'RAD'
    await a.b.js("document.querySelector('.calc-tape-row').click();true")   # a tape row is a number to use again
    await press(a, '*', '2', 'eq')
    assert await a.b.js("document.querySelector('.calc-expr').textContent") == '92'


@CHROME
def test_a_calculation_and_its_tape_survive_a_reload(ran):
    ran('a_calculation_and_its_tape_survive_a_reload')


async def sign_in_as(d, fill):
    """Log out, then sign in with another account's nsec on the SAME device, through the login screen."""
    await d.b.js("window.__beforeReload=1;__PC.logout?__PC.logout():document.querySelector('#btn-logout').click();true")
    await R.wait(d.b, "!window.__beforeReload && !!document.querySelector('#nsec-input') && "
                      "document.body.classList.contains('guest')", 60, 'the login screen after logging out')
    await d.b.js("(()=>{const key=new Uint8Array(32).fill(%d);document.querySelector('#nsec-input').value="
                 "NostrTools.nip19.nsecEncode(key);document.querySelector('#btn-nsec-login').click();return true})()" % fill)
    await R.wait(d.b, "!!__PC.me() && __PC.me().pubkey===%s && Relay.status==='ok'" % json.dumps(R.pubkey(fill)), 60,
                 'signed in as the second account')


@scenario
async def a_second_account_on_this_device_does_not_see_the_first_ones_tape(S):
    a = await S.device(0)
    await open_calculator(a)
    await press(a, '7', '2', '0', '0', '/', '1', '2', 'eq')                # a salary split, say
    assert await a.b.js(TAPE) == ['7200/12= 600']
    await sign_in_as(a, S.fill(1))
    await open_calculator(a)
    assert await a.b.js(TAPE) == [], ('another account\'s calculations are on this account\'s tape', await a.b.js(TAPE))
    await press(a, '2', '+', '2', 'eq')
    await sign_in_as(a, S.fill(0))                     # and the first account still has its own
    await open_calculator(a)
    assert await a.b.js(TAPE) == ['7200/12= 600'], await a.b.js(TAPE)


@CHROME
def test_a_second_account_on_this_device_does_not_see_the_first_ones_tape(ran):
    ran('a_second_account_on_this_device_does_not_see_the_first_ones_tape')


@scenario
async def the_calculator_works_while_the_relay_says_nothing(S):
    tag = 'tc%d' % S.base
    rule = S.ctx.gate.add(lambda fl, c: 'never' if F.device_of(c) == tag else None)
    try:
        a = await S.device(0, tag=tag)
        await open_calculator(a)
        await press(a, '9', '/', '3', 'eq')
        assert await a.b.js("document.querySelector('.calc-expr').textContent") == '3'
        assert await a.b.js(TAPE) == ['9/3= 3']
    finally:
        S.ctx.gate.drop(rule)


@CHROME
def test_the_calculator_works_while_the_relay_says_nothing(ran):
    ran('the_calculator_works_while_the_relay_says_nothing')
