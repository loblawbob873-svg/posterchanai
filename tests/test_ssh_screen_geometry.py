import asyncio
import time
import shlex

import pytest
from app.services.ssh_service import SshSession, _mux_command


def probe():
    s = SshSession(1, 'fixture')
    s._screen_marker = b'\x1ePCSSH-nonce:'
    s._screen_probe_at = time.monotonic()
    s.chan=type('LiveChannel',(),{'closed':False,'exit_status_ready':lambda self:False,'close':lambda self:None})()
    s.fits = []
    s._schedule_screen_fit = lambda: s.fits.append(s._screen_tty)
    return s


@pytest.mark.parametrize('split', range(35))
def test_metadata_is_removed_at_every_chunk_boundary_without_losing_output(split):
    s = probe()
    data = b'\x1ePCSSH-nonce:/dev/pts/24\x1fprompt\r\n'
    s._push(data[:split]);s._push(data[split:])
    assert bytes(s.buf) == b'prompt\r\n'
    assert s.seq == len(b'prompt\r\n')
    assert s.fits == ['/dev/pts/24']


@pytest.mark.parametrize('data', [b'ordinary prompt', b'\x1eother:123\x1f',
    b'\x1ePCSSH-nonce:' + b'x'*300])
def test_nonmetadata_output_is_never_swallowed(data):
    s = probe();s._push(data)
    assert bytes(s.buf) == data
    assert not s.fits


def test_partial_metadata_timeout_or_close_preserves_bytes():
    for close in (False, True):
        s = probe();data=b'\x1ePCSSH-nonce:/dev/'
        s._push(data);assert not s.buf
        if close:s.close()
        else:s._screen_pending_at-=2;s._push(b'')
        assert bytes(s.buf)==data
        assert not s.fits


def test_fit_targets_only_validated_display_and_closes_command_channel():
    s=probe();s.mux_name='pcai-1-main';s._screen_tty='/dev/pts/24'
    calls=[]
    class Channel:
        def settimeout(self,n):calls.append(('timeout',n))
        def exec_command(self,cmd):calls.append(('command',cmd))
        def exit_status_ready(self):return True
        def recv_exit_status(self):return 0
        def close(self):calls.append(('close',))
    class Client:
        def get_transport(self):return self
        def open_session(self,timeout):assert timeout==2;return Channel()
    s.client=Client()
    assert s._fit_screen_display()
    cmd=next(x[1] for x in calls if x[0]=='command')
    assert shlex.split(cmd)[:7]==['screen','-S','pcai-1-main','-X','fit','<','/dev/pts/24']
    assert calls[-1]==('close',)
    s._screen_tty='/dev/pts/24;echo BAD';calls.clear()
    assert not s._fit_screen_display() and not calls


def test_resize_burst_coalesces_fit_and_deduplicates_unchanged_dimensions():
    async def run():
        s=SshSession(1,'fixture');s._screen_tty='/dev/pts/24'
        sizes=[];fits=[]
        class Channel:
            closed=False
            def exit_status_ready(self):return False
            def resize_pty(self,c,r):sizes.append((c,r))
            def close(self):self.closed=True
        s.chan=Channel();s._fit_screen_display=lambda:fits.append(s._last_pty_size) or True
        for size in [(80,24),(100,30),(180,60),(180,60)]:await s.resize(*size)
        await s._screen_fit_task
        assert sizes==[(80,24),(100,30),(180,60)]
        assert fits==[(180,60)]
        s.close()
    asyncio.run(run())


def test_screen_metadata_does_not_change_tmux_or_plain_shell_fallback():
    command=_mux_command('pcai-1-main','PCSSH-fixture')
    screen=command.split('elif command -v screen')[1]
    assert 'printf' in screen and '$(tty)' in screen and 'exec screen -xRR' in screen
    assert 'exec tmux -u new-session -A' in command
    assert 'exec "${SHELL:-/bin/sh}" -l' in command
    assert ' -D' not in command and ' -d' not in command


def test_exec_ack_watchdog_closes_blocked_channel_and_worker_returns():
    import threading
    s=probe();s.mux_name='pcai-1-test';s._screen_tty='/dev/pts/24'
    closed=threading.Event()
    class Channel:
        def settimeout(self,n):pass
        def exec_command(self,cmd):
            assert closed.wait(3), 'exec acknowledgement worker was stranded'
            raise EOFError('watchdog closed channel')
        def close(self):closed.set()
    class Client:
        def get_transport(self):return self
        def open_session(self,timeout):return Channel()
    s.client=Client();start=time.monotonic()
    assert not s._fit_screen_display()
    assert closed.is_set() and time.monotonic()-start<2.8


def test_oversized_complete_metadata_is_not_accepted_as_a_tty():
    s=probe();data=b'\x1ePCSSH-nonce:/dev/pts/'+b'9'*300+b'\x1f'
    s._push(data)
    assert bytes(s.buf)==data and not s._screen_tty


@pytest.mark.parametrize('tty,accepted', [('/dev/ttys000',True),('/unsupported/path',False),('/dev/pts/2;BAD',False)])
def test_framed_tty_compatibility_preserves_following_output(tty,accepted):
    s=probe();s._push(b'\x1ePCSSH-nonce:'+tty.encode()+b'\x1fprompt')
    assert bytes(s.buf)==b'prompt'
    assert bool(s._screen_tty)==accepted


def test_first_fit_waits_for_screen_output_not_a_startup_timer():
    s=probe();s._push(b'\x1ePCSSH-nonce:/dev/pts/24\x1f')
    assert not s.fits
    s._screen_probe_at-=5;s._push(b'')
    assert not s.fits and not s.buf
    s._push(b'first Screen redraw')
    assert s.fits==['/dev/pts/24']


def test_startup_banner_is_preserved_and_following_nonce_still_recognized():
    s=probe();s._push(b'Welcome to host\r\n');s._push(b'\x1ePCSSH-no')
    s._push(b'nce:/dev/pts/24\x1f')
    assert bytes(s.buf)==b'Welcome to host\r\n' and not s.fits
    s._push(b'Screen redraw')
    assert bytes(s.buf)==b'Welcome to host\r\nScreen redraw'
    assert s.fits==['/dev/pts/24']


def test_startup_scan_budget_is_bounded_and_preserves_remaining_bytes():
    s=probe();data=b'banner'*1000;s._push(data)
    assert bytes(s.buf)==data and not s._screen_marker and not s._screen_pending
    following=b'\x1ePCSSH-nonce:/dev/pts/24\x1f'
    s._push(following)
    assert bytes(s.buf)==data+following and not s.fits


def test_slow_shell_startup_banner_does_not_disable_later_metadata():
    s=probe();s._push(b'Starting remote environment\r\n')
    s._screen_probe_at-=2
    s._push(b'\x1ePCSSH-nonce:/dev/pts/24\x1fScreen redraw')
    assert bytes(s.buf)==b'Starting remote environment\r\nScreen redraw'
    assert s.fits==['/dev/pts/24']
