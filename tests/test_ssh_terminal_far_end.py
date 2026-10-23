"""The SSH terminal against a REAL far end: the shipped /ws/ssh router on uvicorn, a real WebSocket client, a
real SSH server (tests/ssh_real_box.py) running real bash -- and, where the feature needs one, a real tmux or
screen, and a real shell keeper in its own process.

Three things nothing else drives end to end:

  MULTIPLEXER   `ssh_terminal_multiplex` on: ssh_service runs `tmux new-session -A` / `screen -xRR` ON THE HOST.
                A dropped socket reattaches to the same shell; so does an APP RESTART that forgot every session
                (the far end's multiplexer is the only thing still holding the shell); two tab labels are two
                shells; another account's same label is not yours; Kill ends the multiplexer session for real.
                The multiplexer runs inside the test's own SSH server with its own HOME, SCREENDIR, TMUX_TMPDIR
                and PATH, so no session of this machine's is touched and none is left behind (asserted: no
                process carrying the box's marker survives, and the socket directory lists nothing).
                Which multiplexer the host has is the box's PATH, so each one is tested by name -- and one
                not installed on THIS machine skips honestly. This file is deliberately NOT a *_full_app browser
                test and not on the gate's required list: the desktop CI runner has neither tmux nor screen
                (tests/test_ci_required_tests_need_only_runner_tools.py), and the deploy gate's full pass is the
                one that runs it, on a node that has them.
  KEYS          a host configured with a private key FILE connects with it; a passphrase-protected key (both the
                PEM and the OpenSSH container), a .pub given for the private half, a missing file and an
                unknown host key each answer their own sentence -- in-process AND through the keeper -- and no
                answer, and no host list, ever carries the key's path.
  KEEPER        a test-owned keeper (tests/ssh_keeper_runner.py: the shipped ssh_keeper in a subprocess on a temp
                socket): the router restarts, the shell is still running in the keeper, the account reattaches
                to it, and another account can neither list it, attach to it nor kill it.
"""
import asyncio
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.ssh_real_box import BASE_TOOLS, Keys, SshBox  # noqa: E402

PY = sys.executable
PASSWORD = 'pc-far-end-%d' % os.getpid()
ANSI = re.compile(r'\x1b(\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(\x07|\x1b\\)|[()][0-9A-Za-z]|[=>78MDEHc])')


def _hex(n):
    return ('%02x' % n) * 32


ALICE = SimpleNamespace(id=4101, username='alice', nostr_npub=_hex(0xA1), is_admin=False, tokens_valid_after=None)
BOB = SimpleNamespace(id=4102, username='bob', nostr_npub=_hex(0xB2), is_admin=False, tokens_valid_after=None)


def _free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    p = s.getsockname()[1]
    s.close()
    return p


# ------------------------------------------------------------------------------------------------ the node
class Node:
    """The shipped ssh_term routers on uvicorn in this process -- the shell keeper OFF unless `keeper_sock`."""

    def __init__(self, conf, known_hosts, sock):
        import paramiko
        from app import auth as app_auth
        from app.services import ssh_service, user_table
        self.mp = mp = pytest.MonkeyPatch()
        self.conf = conf
        # NEVER the node's own keeper (<checkout>/.run/ssh-keeper.sock): a path of the test's choosing, which is
        # either nobody (in-process sessions) or the test's own keeper.
        mp.setenv('POSTERCHANAI_SSH_SOCK', sock)
        mp.setattr(ssh_service, '_get', lambda k, d='': conf.get(k) or d)
        mp.setattr(paramiko.SSHClient, 'load_system_host_keys',
                   lambda cli, filename=None: cli.load_host_keys(known_hosts))
        by_id = {u.id: u for u in (ALICE, BOB)}

        async def aget(_db, uid):
            return by_id.get(int(uid))
        mp.setattr(user_table, 'aget', aget)
        mp.setattr(ssh_service, '_sessions', {})
        self.tokens = {u.id: app_auth.create_access_token({'sub': str(u.id)}) for u in (ALICE, BOB)}
        self.by_id = by_id
        self.port = _free_port()
        self.server = None
        self.start()

    def app(self):
        from fastapi import FastAPI, HTTPException, Request
        from app import auth as app_auth
        from app.database import get_db
        from app.routers import ssh_term
        by_id = self.by_id

        def who(request: Request):
            payload = app_auth.decode_token((request.headers.get('authorization') or '').removeprefix('Bearer ').strip())
            u = by_id.get(int((payload or {}).get('sub') or 0))
            if not u:
                raise HTTPException(401, 'not signed in')
            return u
        app = FastAPI()
        app.include_router(ssh_term.router)
        app.include_router(ssh_term.ws_router)
        app.dependency_overrides[app_auth.get_current_user] = who
        app.dependency_overrides[get_db] = lambda: None
        return app

    def start(self):
        import uvicorn
        self.server = uvicorn.Server(uvicorn.Config(self.app(), host='127.0.0.1', port=self.port, log_level='warning',
                                                    ws='websockets', lifespan='off'))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        for _ in range(200):
            if self.server.started:
                return
            time.sleep(.05)
        raise AssertionError('uvicorn did not start')

    def restart(self):
        """The app process going away and coming back (`./sync.sh`): every in-process session is forgotten and its
        connection dropped -- WITHOUT the kill a person would send -- and a new server answers on the same port."""
        from app.services import ssh_service
        self.server.should_exit = True
        self.thread.join(15)
        for s in list(ssh_service._sessions.values()):
            s.close()                                 # the TCP connection dies with the process; nothing is ended
        ssh_service._sessions.clear()
        self.start()

    def get(self, user, path):
        import httpx
        r = httpx.get('http://127.0.0.1:%d%s' % (self.port, path), trust_env=False, timeout=30,
                      headers={'Authorization': 'Bearer ' + self.tokens[user.id]})
        return r.status_code, r.json()

    def post(self, user, path, body):
        import httpx
        r = httpx.post('http://127.0.0.1:%d%s' % (self.port, path), json=body, trust_env=False, timeout=30,
                       headers={'Authorization': 'Bearer ' + self.tokens[user.id]})
        return r.status_code, r.json()

    def stop(self):
        from app.services import ssh_service
        if self.server:
            self.server.should_exit = True
            self.thread.join(15)
        for s in list(ssh_service._sessions.values()):
            try:
                s.close()
            except Exception:
                pass
        self.mp.undo()


class Term:
    """One browser tab's socket, spoken exactly as term.js speaks it."""

    def __init__(self, node, user):
        self.node, self.user, self.text, self.frames, self.ws = node, user, '', [], None

    async def open(self, **frame):
        import websockets
        self.ws = await websockets.connect('ws://127.0.0.1:%d/ws/ssh' % self.node.port, max_size=None)
        await self.ws.send(json.dumps(dict({'t': 'open', 'token': self.node.tokens[self.user.id], 'cols': 100,
                                            'rows': 30}, **frame)))
        while True:
            m = await self.frame(60)
            if m.get('t') in ('ready', 'err'):
                return m

    async def frame(self, timeout=30):
        m = json.loads(await asyncio.wait_for(self.ws.recv(), timeout))
        self.frames.append(m)
        if m.get('t') == 'out':
            self.text += m.get('d') or ''
        return m

    def screen(self):
        return ANSI.sub('', self.text).replace('\r', '')

    async def type(self, line):
        await self.ws.send(json.dumps({'t': 'in', 'd': line + '\r'}))

    async def until(self, needle, timeout=30):
        end = time.time() + timeout
        while needle not in self.screen():
            left = end - time.time()
            if left <= 0:
                raise AssertionError('%r never came back; the screen says:\n%s' % (needle, self.screen()[-1500:]))
            try:
                await self.frame(left)
            except asyncio.TimeoutError:
                pass

    async def ask(self, tag, expr, timeout=30):
        """Have the shell print `<tag>=<expr>` and return what it printed. The tag makes the answer unambiguous
        on a screen a multiplexer has just redrawn with every earlier line still on it."""
        await self.type("echo '%s'=%s" % (tag, expr))
        pat = re.compile(re.escape(tag) + r'=([^\n]*)\n')
        end = time.time() + timeout
        while True:
            m = pat.search(self.screen())
            if m:
                return m.group(1).strip()
            left = end - time.time()
            if left <= 0:
                raise AssertionError('no answer to %s; the screen says:\n%s' % (tag, self.screen()[-1500:]))
            try:
                await self.frame(left)
            except asyncio.TimeoutError:
                pass

    async def send(self, obj):
        await self.ws.send(json.dumps(obj))

    async def drop(self):
        """The socket dies -- a Tor circuit, a phone locking. Nothing is said to the server first."""
        try:
            await self.ws.close()
        except Exception:
            pass

    async def ended(self, timeout=20):
        """True once the server says `end` (the session is over) or closes the socket -- within `timeout`."""
        import websockets
        end = time.time() + timeout
        while time.time() < end:
            try:
                m = await self.frame(max(0.01, end - time.time()))
            except asyncio.TimeoutError:
                return False
            except websockets.ConnectionClosed:
                return True
            if m.get('t') == 'end':
                return True
        return False


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 240))


# ================================================================================================ MULTIPLEXER
MUX = {'screen': 'screen', 'tmux': 'tmux'}


@pytest.fixture(params=sorted(MUX))
def mux(request):
    tool = request.param
    if not shutil.which(tool):
        pytest.skip('%s is not installed on this machine (the far end here is this machine)' % tool)
    box = SshBox(password=PASSWORD, tools=BASE_TOOLS + (tool,))
    node = Node({'ssh_terminal_enabled': 'true', 'ssh_terminal_multiplex': 'true',
                 'ssh_hosts': 'box tester@127.0.0.1:%d' % box.port,
                 'ssh_terminal_users': ' '.join(u.nostr_npub for u in (ALICE, BOB))},
                box.known_hosts, os.path.join(box.home, 'no-keeper.sock'))
    box.tool = tool
    try:
        yield box, node
    finally:
        node.stop()
        left = box.leftovers(settle=0.5)
        box.stop()
        assert not box.leftovers(settle=2), 'processes of the test far end outlived it'
        del left


def sessions_on_host(box):
    """The multiplexer's own list, read where the far end keeps it (the box's socket dirs, never this machine's)."""
    if box.tool == 'screen':
        _, out = box.run('screen -ls')
        return sorted(set(re.findall(r'\d+\.(pcai-[\w-]+)', out)))
    _, out = box.run("tmux list-sessions -F '#{session_name}'")
    return sorted(set(re.findall(r'^(pcai-[\w-]+)$', out, re.M)))


def sessions_settle(box, want=(), timeout=10):
    """The host's session list once it stops changing toward `want` (Kill's quit runs as its own exec)."""
    end = time.time() + timeout
    while True:
        got = sessions_on_host(box)
        if got == sorted(want) or time.time() > end:
            return got
        time.sleep(.1)


def test_a_multiplexed_shell_survives_a_dropped_socket_and_an_app_restart(mux):
    box, node = mux

    async def go():
        a = Term(node, ALICE)
        ready = await a.open(host='box', password=PASSWORD, label='main')
        assert ready['t'] == 'ready', ready
        assert any(box.tool in e for e in box.execs), ('the host was not asked to run a multiplexer', box.execs)
        await a.type('cd /tmp; export PC_MARK=$((4000+%d))z' % (os.getpid() % 997))
        want = await a.ask('m0', '$PC_MARK')
        assert re.fullmatch(r'\d+z', want), want
        assert sessions_on_host(box) == ['pcai-%d-main' % ALICE.id]
        sid = ready['sid']
        # 1. the socket dies; the session is resumed by id
        await a.drop()
        b = Term(node, ALICE)
        again = await b.open(resume=sid, host='box', label='main', cursor=0)
        assert again.get('resumed') and again['sid'] == sid, again
        assert await b.ask('m1', '$PC_MARK') == want
        await b.drop()
        # 2. the app restarts: this process forgets the session, the far end's multiplexer still has it
        node.restart()
        from app.services import ssh_service
        assert not ssh_service._sessions
        assert sessions_on_host(box) == ['pcai-%d-main' % ALICE.id], 'the restart ended the far-end session'
        c = Term(node, ALICE)
        back = await c.open(resume=sid, host='box', password=PASSWORD, label='main')
        assert any(f.get('t') == 'gone' for f in c.frames), 'a lost session must be SAID, not silently replaced'
        assert back['t'] == 'ready' and back['sid'] != sid
        assert await c.ask('m2', '$PC_MARK') == want, 'not the same shell after the app restart'
        assert await c.ask('m3', '$PWD') == '/tmp'
        assert sessions_on_host(box) == ['pcai-%d-main' % ALICE.id], 'a second shell was made instead'
        await c.send({'t': 'close'})
        await c.ended()
    run(go())
    assert sessions_settle(box) == [], 'Kill left the multiplexer session running on the host'


def test_two_labels_are_two_shells_and_another_account_never_gets_yours(mux):
    box, node = mux

    async def go():
        one, two, bob = Term(node, ALICE), Term(node, ALICE), Term(node, BOB)
        assert (await one.open(host='box', password=PASSWORD, label='main'))['t'] == 'ready'
        await one.type('export PC_TAB=$((7*6))first')
        assert await one.ask('t1', '$PC_TAB') == '42first'
        assert (await two.open(host='box', password=PASSWORD, label='t2'))['t'] == 'ready'
        assert await two.ask('t2', '"[$PC_TAB]"') == '[]', 'the second tab is the first tab\'s shell'
        assert (await bob.open(host='box', password=PASSWORD, label='main'))['t'] == 'ready'
        assert await bob.ask('t3', '"[$PC_TAB]"') == '[]', 'another account\'s "main" is somebody else\'s shell'
        assert sessions_on_host(box) == sorted(['pcai-%d-main' % ALICE.id, 'pcai-%d-t2' % ALICE.id,
                                                'pcai-%d-main' % BOB.id])
        for t in (one, two, bob):
            await t.send({'t': 'close'})
            await t.ended()
    run(go())
    assert sessions_settle(box) == [], 'Kill left the multiplexer session running on the host'


def test_kill_ends_the_far_end_session_and_the_next_connect_is_a_fresh_shell(mux):
    box, node = mux

    async def go():
        a = Term(node, ALICE)
        ready = await a.open(host='box', password=PASSWORD, label='main')
        await a.type('export PC_KILLED=$((3*3))nine')
        assert await a.ask('k1', '$PC_KILLED') == '9nine'
        # Kill from the session list (the other way a person ends one) while it is attached
        code, body = node.post(ALICE, '/api/ssh/sessions/kill', {'sid': ready['sid']})
        assert code == 200 and body == {'ok': True}, body
        assert await a.ended(), 'the attached socket was not told its session ended'
        assert sessions_settle(box) == [], 'Kill left the multiplexer session running on the host'
        b = Term(node, ALICE)
        assert (await b.open(host='box', password=PASSWORD, label='main'))['t'] == 'ready'
        assert await b.ask('k2', '"[$PC_KILLED]"') == '[]', 'the killed shell came back'
        await b.send({'t': 'close'})
        await b.ended()
    run(go())
    assert sessions_settle(box) == [], 'Kill left the multiplexer session running on the host'


# ================================================================================================ KEYS
SENTENCES = {
    'locked_pem': "that private key is passphrase-protected, which this terminal cannot unlock",
    'locked_ssh': "that private key is passphrase-protected, which this terminal cannot unlock",
    'public': "that key file is not a usable private key",
    'nokey': "the key file configured for this host is not on the server",
    'stranger': "this server does not know that host's key yet",
    'wrongkey': "the host refused those credentials",
}


@pytest.fixture(scope='module')
def keyed():
    d = tempfile.mkdtemp(prefix='pc-ssh-keys-')
    keys = Keys(d)
    box = SshBox(authorized=keys.accepted[:2])
    stranger = SshBox(authorized=keys.accepted[:2])        # a host whose key nobody has recorded
    other = Keys(tempfile.mkdtemp(prefix='pc-ssh-keys-other-', dir=d))
    line = lambda name, k, port=box.port: '%s tester@127.0.0.1:%d key=%s' % (name, port, k)
    conf = {'ssh_terminal_enabled': 'true', 'ssh_terminal_multiplex': 'false',
            'ssh_terminal_users': ' '.join(u.nostr_npub for u in (ALICE, BOB)),
            'ssh_hosts': '\n'.join([line('rsa', keys.rsa_pem), line('ed', keys.ed),
                                    line('locked_pem', keys.rsa_pem_locked), line('locked_ssh', keys.ed_locked),
                                    line('public', keys.pub), line('nokey', keys.missing),
                                    line('stranger', keys.ed, stranger.port), line('wrongkey', other.ed)])}
    yield SimpleNamespace(keys=keys, box=box, stranger=stranger, conf=conf, dir=d)
    box.stop()
    stranger.stop()
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture(params=['in-process', 'keeper'])
def keyed_node(request, keyed):
    sock = os.path.join(keyed.dir, 'k-%s.sock' % os.urandom(3).hex())
    keeper = Keeper(sock, keyed.conf, keyed.box.known_hosts) if request.param == 'keeper' else None
    node = Node(keyed.conf, keyed.box.known_hosts, sock)
    try:
        yield node
    finally:
        node.stop()
        if keeper:
            keeper.stop()


def test_a_host_with_a_private_key_file_connects_with_it(keyed, keyed_node):
    async def go():
        for name in ('rsa', 'ed'):
            t = Term(keyed_node, ALICE)
            ready = await t.open(host=name, label='k-' + name)
            assert ready['t'] == 'ready', (name, ready)
            assert await t.ask('who', '$((20+%d))' % len(name)) == str(20 + len(name))
            await t.send({'t': 'close'})
            await t.ended()
    run(go())
    assert ('publickey', True) in keyed.box.auth_log
    assert ('password', True) not in keyed.box.auth_log


@pytest.mark.parametrize('host', sorted(SENTENCES))
def test_every_key_failure_says_its_own_sentence_and_never_the_path(keyed, keyed_node, host):
    before = len(keyed.box.live_transports()) + len(keyed.stranger.live_transports())

    async def go():
        t = Term(keyed_node, ALICE)
        m = await t.open(host=host, label='x')
        return m
    m = run(go())
    assert m['t'] == 'err', m
    assert m['m'].startswith('could not connect: ' + SENTENCES[host]), (host, m['m'])
    assert keyed.dir not in m['m'] and '/' not in m['m'].split(':', 1)[1], ('a server path reached the browser', m)
    # a refused connect hangs up: nothing is left dialled into the host for every failed attempt
    for _ in range(50):
        if len(keyed.box.live_transports()) + len(keyed.stranger.live_transports()) <= before:
            break
        time.sleep(.1)
    assert len(keyed.box.live_transports()) + len(keyed.stranger.live_transports()) <= before, \
        'a failed connect left its SSH connection open on the host'


def test_the_host_list_names_keyed_hosts_without_their_key_paths(keyed, keyed_node):
    code, body = keyed_node.get(ALICE, '/api/ssh/hosts')
    assert code == 200
    raw = json.dumps(body)
    assert keyed.dir not in raw and 'key=' not in raw and 'id_' not in raw, raw
    assert {h['name']: h['keyed'] for h in body['hosts']}['rsa'] is True


# ================================================================================================ KEEPER
class Keeper:
    def __init__(self, sock, conf, known_hosts):
        self.sock = sock
        env = dict(os.environ, POSTERCHANAI_SSH_SOCK=sock, PC_TEST_KEEPER_CONF=json.dumps(conf),
                   PC_TEST_KNOWN_HOSTS=known_hosts, PYTHONDONTWRITEBYTECODE='1')
        self.log = tempfile.TemporaryFile()
        self.proc = subprocess.Popen([PY, str(ROOT / 'tests' / 'ssh_keeper_runner.py')], cwd=str(ROOT), env=env,
                                     stdout=self.log, stderr=subprocess.STDOUT)
        for _ in range(600):
            if self.up():
                return
            if self.proc.poll() is not None:
                break
            time.sleep(.1)
        self.log.seek(0)
        raise AssertionError('the test keeper did not start: %s' % self.log.read()[-2000:])

    def up(self):
        s = socket.socket(socket.AF_UNIX)
        try:
            s.connect(self.sock)
            return True
        except OSError:
            return False
        finally:
            s.close()

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(5)
        self.log.close()


@pytest.fixture
def kept():
    box = SshBox(password=PASSWORD)
    conf = {'ssh_terminal_enabled': 'true', 'ssh_terminal_multiplex': 'false',
            'ssh_hosts': 'box tester@127.0.0.1:%d' % box.port,
            'ssh_terminal_users': ' '.join(u.nostr_npub for u in (ALICE, BOB))}
    sock = os.path.join(box.home, 'keeper.sock')
    keeper = Keeper(sock, conf, box.known_hosts)
    node = Node(conf, box.known_hosts, sock)
    try:
        yield box, node, keeper
    finally:
        node.stop()
        keeper.stop()
        assert not box.leftovers(settle=10), 'the keeper going away left shells running on the host'
        box.stop()


def test_a_shell_in_the_keeper_survives_an_app_restart_and_only_its_owner_gets_it_back(kept):
    box, node, keeper = kept
    from app.services import ssh_keeper, ssh_service
    assert ssh_keeper.is_up() and ssh_keeper.socket_path() == keeper.sock

    async def go():
        a = Term(node, ALICE)
        ready = await a.open(host='box', password=PASSWORD, label='main')
        assert ready['t'] == 'ready', ready
        sid = ready['sid']
        assert not ssh_service._sessions, 'the shell is in the app process, not the keeper'
        await a.type('cd /tmp; export PC_KEPT=$((900+%d))k' % (os.getpid() % 97))
        want = await a.ask('a0', '$PC_KEPT')
        await a.drop()

        node.restart()                                       # ./sync.sh

        code, mine = node.get(ALICE, '/api/ssh/sessions')
        assert code == 200 and mine['keeper'] is True and [s['sid'] for s in mine['sessions']] == [sid], mine
        code, theirs = node.get(BOB, '/api/ssh/sessions')
        assert code == 200 and theirs['sessions'] == [], ('another account is shown this shell', theirs)
        # Bob holding Alice's id gets NOTHING of hers: not an attach, not a kill
        b = Term(node, BOB)
        bm = await b.open(resume=sid, host='box', password=PASSWORD, label='main')
        assert any(f.get('t') == 'gone' for f in b.frames), b.frames
        assert bm['t'] == 'ready' and bm['sid'] != sid
        assert await b.ask('b0', '"[$PC_KEPT]"') == '[]', 'Bob was handed Alice\'s shell'
        await b.send({'t': 'close'})
        await b.ended()
        code, body = node.post(BOB, '/api/ssh/sessions/kill', {'sid': sid})
        assert code == 200 and body == {'ok': False}, body
        assert [s['sid'] for s in node.get(ALICE, '/api/ssh/sessions')[1]['sessions']] == [sid]

        c = Term(node, ALICE)
        back = await c.open(resume=sid, host='box', label='main', cursor=0)
        assert back['t'] == 'ready' and back.get('resumed') and back['sid'] == sid, back
        assert await c.ask('a1', '$PC_KEPT') == want, 'reattached, but not to the same shell'
        assert await c.ask('a2', '$PWD') == '/tmp'
        await c.send({'t': 'close'})
        await c.ended()
        code, after = node.get(ALICE, '/api/ssh/sessions')
        assert after['sessions'] == [], after
    run(go())


def test_the_keeper_itself_refuses_another_accounts_session_at_its_socket(kept):
    """The router is one guard; the keeper's own `user_id` check is the second, for an app bug that names the
    wrong account. Spoken straight at the keeper's socket."""
    box, node, keeper = kept

    async def go():
        a = Term(node, ALICE)
        sid = (await a.open(host='box', password=PASSWORD, label='main'))['sid']
        await a.drop()
        r, w = await asyncio.open_unix_connection(keeper.sock)
        w.write((json.dumps({'op': 'attach', 'user_id': BOB.id, 'sid': sid, 'cols': 80, 'rows': 24}) + '\n').encode())
        await w.drain()
        answer = json.loads(await asyncio.wait_for(r.readline(), 10))
        w.close()
        assert answer.get('t') == 'gone', answer
        r, w = await asyncio.open_unix_connection(keeper.sock)
        w.write((json.dumps({'op': 'list', 'user_id': BOB.id}) + '\n').encode())
        await w.drain()
        assert json.loads(await asyncio.wait_for(r.readline(), 10)) == {'ok': True, 'sessions': []}
        w.close()
        c = Term(node, ALICE)
        assert (await c.open(resume=sid, host='box', label='main'))['sid'] == sid
        await c.send({'t': 'close'})
        await c.ended()
    run(go())


# ================================================================================================ A SHELL THAT ENDS
@pytest.fixture(params=['in-process', 'keeper'])
def plain(request):
    box = SshBox(password=PASSWORD)
    conf = {'ssh_terminal_enabled': 'true', 'ssh_terminal_multiplex': 'false',
            'ssh_hosts': 'box tester@127.0.0.1:%d' % box.port,
            'ssh_terminal_users': ' '.join(u.nostr_npub for u in (ALICE, BOB))}
    sock = os.path.join(box.home, 'keeper.sock')
    keeper = Keeper(sock, conf, box.known_hosts) if request.param == 'keeper' else None
    node = Node(conf, box.known_hosts, sock)
    try:
        yield box, node
    finally:
        node.stop()
        if keeper:
            keeper.stop()
        assert not box.leftovers(settle=10)
        box.stop()


def test_a_shell_that_exits_ends_the_tab_at_once(plain):
    """`exit` (or the shell dying) is the session ending, and the tab is told -- not after the socket's next
    30-second receive timeout, during which it sat on a dead prompt that swallowed the first keystroke."""
    box, node = plain

    async def go():
        a = Term(node, ALICE)
        assert (await a.open(host='box', password=PASSWORD, label='main'))['t'] == 'ready'
        assert await a.ask('e0', '$((2+2))') == '4'
        await a.type('exit')
        t0 = time.time()
        assert await a.ended(8), 'the shell exited and the tab was not told'
        return time.time() - t0
    assert run(go()) < 8


def test_a_session_killed_from_another_device_ends_the_tab_holding_it(plain):
    box, node = plain

    async def go():
        a = Term(node, ALICE)
        ready = await a.open(host='box', password=PASSWORD, label='main')
        assert await a.ask('f0', '$((3+3))') == '6'
        code, body = node.post(ALICE, '/api/ssh/sessions/kill', {'sid': ready['sid']})
        assert body == {'ok': True}, body
        assert await a.ended(8), 'the tab holding a killed session was not told'
    run(go())
