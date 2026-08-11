"""A terminal session must outlive the thing you were looking at it through.

Run: venv-unified/bin/python -m pytest tests/test_ssh_resume.py

Reported as: "i just got disconnected and it breaks the entire experiecne", and then, precisely:
"basically it should be like running tmux/screen to resume work where you left off … until you kill
the session … it should somehow survive posterchanai service restarts … i want to be able to resume
session on other devices".

Four different lifetimes are being asserted here and they are NOT the same thing:

  1. the SOCKET dying (a Tor circuit, a phone locking) must not touch the shell;
  2. the CLIENT going away entirely (close the tab, close the app) must not either — nothing here
     expires, so a session ends when it is killed and at no other time;
  3. the WEB APP restarting (`./sync.sh`, several times a day) must not, which is why the PTY lives
     in a separate keeper process talking over a unix socket;
  4. a DIFFERENT DEVICE must be able to pick the session up, which is why the session list is scoped
     to the account and not to whoever holds the id.

The keeper is driven here over a REAL unix socket with a REAL asyncio server and a fake paramiko
channel, because the failure this is guarding against is a protocol one — an app that thinks it
reattached to a shell that is not there is worse than one that refuses.
"""
import asyncio
import contextlib
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services import ssh_keeper, ssh_service            # noqa: E402


class FakeChan:
    """The bits of a paramiko Channel the session touches, and nothing else."""

    def __init__(self):
        self.out = bytearray()
        self.sent = bytearray()
        self.size = None
        self.closed = False

    def feed(self, data: bytes):
        self.out.extend(data)

    def recv_ready(self):
        return bool(self.out)

    def recv(self, n):
        take, self.out = bytes(self.out[:n]), bytearray(self.out[n:])
        return take

    def exit_status_ready(self):
        return False

    def sendall(self, d):
        self.sent.extend(d.encode("utf-8") if isinstance(d, str) else d)

    def resize_pty(self, cols, rows):
        self.size = (cols, rows)

    def close(self):
        self.closed = True


def _fake_session(user_id=1, host="build"):
    """A live session with a fake channel and the real reader running."""
    s = ssh_service.SshSession(user_id=user_id, host_name=host)
    s.chan = FakeChan()
    s.client = None
    s._idle = s._max = s._grace = 0            # the defaults; made explicit for the test
    ssh_service._sessions[s.sid] = s
    s._reader = asyncio.get_event_loop().create_task(s._drain())
    return s


async def _settle(sess, n=40):
    """Let the reader drain — it polls, so this is however long that takes."""
    for _ in range(n):
        await asyncio.sleep(0.01)
        if sess.seq:
            return


def _run(coro_fn, *a, **kw):
    """Drive one coroutine to completion on a fresh loop.

    Deliberately not pytest-asyncio: this venv does not have it, and a test dependency that has to be
    installed on every node before the suite runs is a test dependency that stops being run."""
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro_fn(*a, **kw))
    finally:
        for t in asyncio.all_tasks(loop):
            t.cancel()
        loop.run_until_complete(asyncio.sleep(0))
        loop.close()
        asyncio.set_event_loop(None)


@contextlib.asynccontextmanager
async def keeper(monkeypatch=None):
    """A real keeper on a real unix socket, with paramiko stubbed out."""
    tmp = tempfile.mkdtemp()
    sock = os.path.join(tmp, "k.sock")
    os.environ["POSTERCHANAI_SSH_SOCK"] = sock

    async def fake_connect(self, h, password="", cols=80, rows=24, label="main"):
        if h.host == "nope":
            raise OSError("no route to host")
        self.chan = FakeChan()
        ssh_service._sessions[self.sid] = self
        self._reader = asyncio.get_event_loop().create_task(self._drain())

    real = ssh_service.SshSession.connect
    ssh_service.SshSession.connect = fake_connect
    server = await asyncio.start_unix_server(ssh_keeper._client, path=sock)
    try:
        yield sock
    finally:
        ssh_service.SshSession.connect = real
        server.close()
        await server.wait_closed()
        os.environ.pop("POSTERCHANAI_SSH_SOCK", None)


@pytest.fixture(autouse=True)
def _clean():
    ssh_service._sessions.clear()
    yield
    for s in list(ssh_service._sessions.values()):
        try:
            s.close()
        except Exception:
            pass
    ssh_service._sessions.clear()


# ---------------------------------------------------------------------------------------------
# 1 + 2: the session, on its own


def test_the_shell_keeps_running_and_keeps_reading_while_nobody_is_attached():
    async def _bodytest_the_shell_keeps_running_and_keeps_reading_while_nobody_is_attached():
        """THE ONE THAT MATTERS MOST. If the session stopped draining the channel while detached,
        paramiko's receive window would fill and the REMOTE command would block — so "my connection
        dropped" would silently become "my build froze", which is worse than losing the session."""
        s = _fake_session()
        s.chan.feed(b"hello")
        await _settle(s)
        s.detach()                                  # the socket went away
        s.chan.feed(b" world")
        for _ in range(40):
            await asyncio.sleep(0.01)
            if s.seq >= 11:
                break
        assert not s.closed(), "the PTY was closed when the socket went away"
        assert bytes(s.buf) == b"hello world", "output produced while detached was not collected"

    _run(_bodytest_the_shell_keeps_running_and_keeps_reading_while_nobody_is_attached)


def test_nothing_expires_by_default():
    async def _bodytest_nothing_expires_by_default():
        """`until you kill the session`. Every bound is the operator's to set and off unless they do."""
        assert ssh_service.limits() == (0, 0, 0), "a default install imposes a timeout on sessions"
        s = _fake_session()
        s.detached_at = 1                           # detached since 1970
        s.last_in = 1
        s.started = 1
        await asyncio.sleep(0.08)
        assert not s.closed(), "a session was reaped with every limit switched off"

    _run(_bodytest_nothing_expires_by_default)


def test_an_operator_set_limit_is_honoured():
    async def _bodytest_an_operator_set_limit_is_honoured():
        """The bounds are off, not gone — a shared node has to be able to re-impose them."""
        s = _fake_session()
        s._grace = 0.02
        s.detach()
        for _ in range(60):
            await asyncio.sleep(0.01)
            if s.closed():
                break
        assert s.closed(), "a detach grace was configured and never applied"
        assert "detached" in s.closed_reason

    _run(_bodytest_an_operator_set_limit_is_honoured)


def test_the_idle_clock_does_not_run_while_detached():
    async def _bodytest_the_idle_clock_does_not_run_while_detached():
        """Otherwise "left it running overnight" and "walked away from a prompt" are the same thing, and
        the session you deliberately left running is the one that gets reaped."""
        s = _fake_session()
        s._idle = 0.02
        s.last_in = 0
        s.detach()
        await asyncio.sleep(0.12)
        assert not s.closed(), "the idle timer reaped a DETACHED session"

    _run(_bodytest_the_idle_clock_does_not_run_while_detached)


def test_kill_is_what_ends_a_session():
    async def _bodytest_kill_is_what_ends_a_session():
        s = _fake_session(user_id=7)
        assert ssh_service.kill(s.sid, 7) is True
        assert s.closed()
        assert ssh_service.get_session(s.sid, 7) is None

    _run(_bodytest_kill_is_what_ends_a_session)


def test_a_session_id_is_not_enough_on_its_own():
    async def _bodytest_a_session_id_is_not_enough_on_its_own():
        """The id is the whole authorisation for typing into a running shell, so it is checked against
        the account rather than trusted for being unguessable — a leaked id in a log would otherwise be a
        shell on somebody else's servers."""
        s = _fake_session(user_id=1)
        assert ssh_service.get_session(s.sid, 2) is None
        assert ssh_service.kill(s.sid, 2) is False
        assert ssh_service.get_session(s.sid, 1) is s

    _run(_bodytest_a_session_id_is_not_enough_on_its_own)


# ---------------------------------------------------------------------------------------------
# the replay cursor


def test_you_are_shown_exactly_what_you_missed():
    async def _bodytest_you_are_shown_exactly_what_you_missed():
        s = _fake_session()
        s.chan.feed(b"aaaa")
        await _settle(s)
        mark = s.seq
        s.chan.feed(b"bbbb")
        for _ in range(40):
            await asyncio.sleep(0.01)
            if s.seq > mark:
                break
        assert s.since(mark) == b"bbbb"
        assert s.since(0) == b"aaaabbbb"

    _run(_bodytest_you_are_shown_exactly_what_you_missed)


def test_a_cursor_older_than_the_buffer_gets_what_is_left():
    """A shell, not a transcript. Returning nothing would look like "you missed nothing"."""
    s = ssh_service.SshSession(user_id=1)
    s._push(b"x" * (ssh_service.REPLAY_MAX + 500))
    assert len(s.buf) == ssh_service.REPLAY_MAX, "the replay buffer is unbounded"
    assert s.seq == ssh_service.REPLAY_MAX + 500, "seq must keep counting past what is retained"
    assert len(s.since(0)) == ssh_service.REPLAY_MAX


@pytest.mark.parametrize("text", ["┌─┐│└┘", "héllo wörld", "日本語のテキスト", "🙂🙃"])
def test_a_character_is_never_split_across_two_frames(text):
    """The buffer is bytes and the wire carries text. Decoding a chunk that ends mid-character with
    'replace' burns in a U+FFFD permanently, because the cursor has already moved past it — and
    anything that draws a box is nothing BUT multi-byte characters."""
    raw = text.encode("utf-8")
    for cut in range(1, len(raw) + 1):
        head = raw[:cut]
        take = ssh_service.utf8_take(head)
        assert "�" not in head[:take].decode("utf-8", "replace"), (
            f"splitting {text!r} at byte {cut} produced a replacement character")
    # ...and nothing is dropped: the held-back tail arrives with the next frame.
    # ...and nothing is DROPPED: a held-back tail arrives with the next frame. The window grows
    # rather than sliding, because a fixed 3-byte window can never contain a 4-byte character and a
    # test that skipped it would be testing its own harness.
    out, i, end = "", 0, 0
    while i < len(raw):
        end = min(len(raw), max(end + 1, i + 1))
        take = ssh_service.utf8_take(raw[i:end])
        if not take:
            continue
        out += raw[i:i + take].decode("utf-8")
        i += take
    assert out == text


# ---------------------------------------------------------------------------------------------
# 3: the keeper — a session that outlives the web app


async def _op(sock, req):
    r, w = await asyncio.open_unix_connection(path=sock)
    w.write((json.dumps(req) + "\n").encode())
    await w.drain()
    return r, w


async def _line(r, timeout=3):
    line = await asyncio.wait_for(r.readline(), timeout=timeout)
    return json.loads(line.decode()) if line else None


def test_a_shell_opened_through_the_keeper_survives_the_connection():
    async def _bodytest_a_shell_opened_through_the_keeper_survives_the_connection():
        async with keeper() as sock:
            """This IS "survives a posterchanai restart": from the keeper's side, the app going away and a
            client hanging up are the same event, and neither may end the session."""
            r, w = await _op(sock, {"op": "open", "user_id": 1, "cols": 80, "rows": 24,
                                      "host": {"name": "build", "user": "u", "host": "h", "port": 22}})
            ready = await _line(r)
            assert ready["t"] == "ready" and ready["sid"]
            sid = ready["sid"]
            sess = ssh_service._sessions[sid]
            sess.chan.feed(b"prompt$ ")
            out = await _line(r)
            assert out["t"] == "out" and out["d"] == "prompt$ "

            w.close()                                            # the app "restarts"
            await asyncio.sleep(0.1)
            assert not sess.closed(), "the keeper ended the session when the app disconnected"

            # Something keeps happening while nothing is attached — the point of the whole design.
            sess.chan.feed(b"done.\n")
            await asyncio.sleep(0.1)

            r2, w2 = await _op(sock, {"op": "attach", "user_id": 1, "sid": sid,
                                        "cursor": out["seq"], "cols": 80, "rows": 24})
            again = await _line(r2)
            assert again["t"] == "ready" and again.get("resumed") is True
            missed = await _line(r2)
            assert missed["d"] == "done.\n", "reattaching did not replay what happened while away"
            w2.close()

    _run(_bodytest_a_shell_opened_through_the_keeper_survives_the_connection)


def test_typing_reaches_the_shell_through_the_keeper():
    async def _bodytest_typing_reaches_the_shell_through_the_keeper():
        async with keeper() as sock:
            r, w = await _op(sock, {"op": "open", "user_id": 1,
                                      "host": {"name": "build", "user": "u", "host": "h", "port": 22}})
            sid = (await _line(r))["sid"]
            w.write((json.dumps({"t": "in", "d": "ls\r"}) + "\n").encode())
            await w.drain()
            for _ in range(50):
                await asyncio.sleep(0.01)
                if ssh_service._sessions[sid].chan.sent:
                    break
            assert bytes(ssh_service._sessions[sid].chan.sent) == b"ls\r"
            w.close()

    _run(_bodytest_typing_reaches_the_shell_through_the_keeper)


def test_attaching_to_a_session_that_is_gone_says_so():
    async def _bodytest_attaching_to_a_session_that_is_gone_says_so():
        async with keeper() as sock:
            """A silent new shell in the same window looks like your work vanished, so the app is told which
            it was and re-opens deliberately."""
            r, w = await _op(sock, {"op": "attach", "user_id": 1, "sid": "nope-nope", "cursor": 0})
            msg = await _line(r)
            assert msg["t"] == "gone"
            w.close()

    _run(_bodytest_attaching_to_a_session_that_is_gone_says_so)


def test_the_keeper_will_not_hand_over_another_accounts_shell():
    async def _bodytest_the_keeper_will_not_hand_over_another_accounts_shell():
        async with keeper() as sock:
            r, w = await _op(sock, {"op": "open", "user_id": 1,
                                      "host": {"name": "build", "user": "u", "host": "h", "port": 22}})
            sid = (await _line(r))["sid"]
            r2, w2 = await _op(sock, {"op": "attach", "user_id": 2, "sid": sid, "cursor": 0})
            assert (await _line(r2))["t"] == "gone"
            w.close(); w2.close()

    _run(_bodytest_the_keeper_will_not_hand_over_another_accounts_shell)


def test_the_session_list_is_the_ACCOUNTS_not_the_devices():
    async def _bodytest_the_session_list_is_the_ACCOUNTS_not_the_devices():
        async with keeper() as sock:
            """"i want to be able to resume session on other devices" — the id lives in one browser's
            localStorage, so without an account-scoped list a session started on the laptop is alive and
            unreachable from the phone."""
            r, w = await _op(sock, {"op": "open", "user_id": 5,
                                      "host": {"name": "build", "user": "u", "host": "h", "port": 22}})
            sid = (await _line(r))["sid"]
            out = await _op(sock, {"op": "list", "user_id": 5})
            listed = await _line(out[0])
            assert [x["sid"] for x in listed["sessions"]] == [sid]
            assert listed["sessions"][0]["host"] == "build"
            # ...and only that account's.
            other = await _op(sock, {"op": "list", "user_id": 6})
            assert (await _line(other[0]))["sessions"] == []
            w.close()

    _run(_bodytest_the_session_list_is_the_ACCOUNTS_not_the_devices)


def test_a_failed_connect_reports_the_kind_not_the_text():
    async def _bodytest_a_failed_connect_reports_the_kind_not_the_text():
        async with keeper() as sock:
            """paramiko's message carries the server-side key path, which /api/ssh/hosts deliberately
            withholds. The keeper hands back the exception's TYPE and the router turns it into words, so
            there is exactly one such table."""
            r, w = await _op(sock, {"op": "open", "user_id": 1,
                                      "host": {"name": "x", "user": "u", "host": "nope", "port": 22}})
            msg = await _line(r)
            assert msg["t"] == "err" and msg["kind"] == "OSError"
            from app.routers.ssh_term import _why_kind
            assert _why_kind(msg["kind"], msg["m"]) == "the host could not be reached"
            w.close()

    _run(_bodytest_a_failed_connect_reports_the_kind_not_the_text)


def test_is_up_connects_rather_than_statting():
    """A socket file left behind by a killed keeper is still a file. `os.path.exists` on it is the
    difference between "sessions survive a deploy" and every terminal failing to open with a
    connection-refused nobody can see."""
    import socket as _s
    dead = os.path.join(tempfile.mkdtemp(), "stale.sock")
    srv = _s.socket(_s.AF_UNIX, _s.SOCK_STREAM)
    srv.bind(dead)
    srv.close()                                  # the file remains; nothing is listening
    os.environ["POSTERCHANAI_SSH_SOCK"] = dead
    try:
        assert os.path.exists(dead)
        assert ssh_keeper.is_up() is False
    finally:
        os.environ.pop("POSTERCHANAI_SSH_SOCK", None)


# ---------------------------------------------------------------------------------------------
# the wiring around it


def test_a_deploy_does_not_restart_the_keeper():
    """The unit exists to outlive a deploy, so it is the one unit deliberately left out of
    deploy_targets' conservative "restart everything" — otherwise `./sync.sh` would quietly undo the
    feature several times a day."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("dt", ROOT / "scripts" / "deploy_targets.py")
    dt = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dt)
    assert dt.SHELL not in dt.ALL, "a shared-file change would restart the keeper and kill every shell"
    assert dt.SHELL in dt.units_for(["app/services/ssh_keeper.py"]), (
        "a change to the keeper's own code must restart it, or the fix runs nowhere")
    assert dt.SHELL not in dt.units_for(["app/main.py"])


def test_the_shell_role_is_a_real_role():
    from app import role, role_runner
    assert "shell" in role.ROLES
    assert "shell" in role_runner._ROLE_SERVICES
    body = (ROOT / "scripts" / "install_services.sh").read_text()
    assert "shell" in body.split("UNITS=(")[1].split(")")[0], (
        "install_services.sh does not write posterchanai-shell.service")


def test_the_client_detaches_on_leaving_and_never_kills():
    """Leaving the Terminal screen must not end the session — that is the whole feature, and the
    screen is torn down by renderView without telling anyone."""
    js = (ROOT / "static" / "js" / "client" / "term.js").read_text()
    i = js.index("function unmount()")
    body = js[i:i + 600]
    assert "_bye()" in body and "t: 'close'" not in body, (
        "unmount kills the session instead of detaching from it")
    assert "localStorage" in js and "pc_tty_sid" in js, "the session id is not kept across a reload"
    assert "visibilitychange" in js, "a phone waking up waits out the backoff"
    assert "resume: sid" in js, "the reconnect does not reattach"


def test_the_terminal_settings_are_declared_so_they_hydrate():
    """A key missing from SettingsResponse loads blank on every visit, and a CHECKBOX then posts
    false over the stored value on the next Save."""
    from app.schemas import SettingsResponse
    tab = (ROOT / "templates" / "admin" / "tabs" / "nodes.html").read_text()
    for k in ("ssh_terminal_multiplex", "ssh_terminal_idle_min", "ssh_terminal_max_hours",
              "ssh_terminal_detach_min"):
        assert k in SettingsResponse.model_fields, f"{k} is read at runtime but never declared"
        assert f'id="{k}" name="{k}"' in tab, f"{k} has no input in Admin → Nodes"


def test_the_remote_multiplexer_degrades_instead_of_failing():
    """tmux is not installed here, and it is not this app's business to install it on someone else's
    server. The command must therefore decide ON THE HOST and always end at a working shell."""
    cmd = ssh_service._mux_command("pcai-1-main")
    assert "command -v tmux" in cmd and "command -v screen" in cmd
    assert cmd.rstrip().endswith("fi")
    assert '"${SHELL:-/bin/sh}" -l' in cmd, "a host with neither gets no shell at all"
    # DETERMINISTIC, because it is the only thing that reconnects a person to their shell once every
    # id this process ever issued has been forgotten.
    assert ssh_service._mux_name(1, "main") == ssh_service._mux_name(1, "main")
    assert ssh_service._mux_name(1, "main") != ssh_service._mux_name(2, "main")
    assert ssh_service._mux_name(1, "../../etc; rm -rf /") == "pcai-1-etcrm-rf"
