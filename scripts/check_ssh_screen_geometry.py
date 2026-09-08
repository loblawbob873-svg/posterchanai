#!/usr/bin/env python3
"""Exercise the SSH Screen sizing code with real disposable PTYs; never attach user sessions."""
import ast
import asyncio
import fcntl
import json
import os
from pathlib import Path
import pty
import re
import select
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
import uuid


def main():
    if not shutil.which('screen'):
        print('SKIP GNU Screen is not installed')
        return 2
    source = Path(sys.argv[1]) if len(sys.argv)>1 else Path(__file__).resolve().parents[1]/'app/services/ssh_service.py'
    tree=ast.parse(source.read_text())
    mux=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_mux_command')
    session=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='SshSession')
    methods=[n for n in session.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name in
             ('_push','_fit_screen_display','_schedule_screen_fit','resize','closed')]
    session.body=methods
    namespace=dict(globals(),REPLAY_MAX=262144)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[mux,session],type_ignores=[])),str(source),'exec'),namespace)
    name='pc-area-test-'+uuid.uuid4().hex[:12];clients=[];ttys=[];padding=[]
    def info():
        result=subprocess.run(['screen','-S',name,'-p','0','-Q','info'],capture_output=True,text=True,timeout=3)
        return result.stdout.strip()
    def dimensions():
        match=re.search(r'/\((\d+),(\d+)\)',info())
        return tuple(map(int,match.groups())) if match else None
    def attach(cols,rows,marker='',pair=None):
        master,slave=pair or pty.openpty();tty=os.ttyname(slave);ttys.append(tty)
        fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',rows,cols,0,0))
        command=namespace['_mux_command'](name,marker)
        if marker:command='printf \"startup banner\\n\"; sleep 1.2; '+command.replace('exec screen -xRR','sleep 0.6; exec screen -xRR')
        # Force the Screen branch even when tmux is also installed: the real command still runs.
        command='command(){ if [ "$1" = -v ] && [ "$2" = tmux ]; then return 1; fi; builtin command "$@"; }; '+command
        proc=subprocess.Popen(['bash','-c',command],stdin=slave,stdout=slave,stderr=slave,
                              env={**os.environ,'TERM':'xterm-256color'},start_new_session=True)
        os.close(slave);clients.append((proc,master));return proc,master,tty
    class CommandChannel:
        def settimeout(self,n):pass
        def exec_command(self,cmd):self.proc=subprocess.Popen(['sh','-c',cmd])
        def exit_status_ready(self):return self.proc.poll() is not None
        def recv_exit_status(self):return self.proc.wait()
        def close(self):
            if hasattr(self,'proc') and self.proc.poll() is None:self.proc.terminate();self.proc.wait(timeout=2)
    class Client:
        def get_transport(self):return self
        def open_session(self,timeout):return CommandChannel()
    async def run():
        attach(80,24);await asyncio.sleep(.3)
        assert dimensions()==(80,24),info()
        marker='PCSSH-'+uuid.uuid4().hex
        proc,master,tty=attach(180,60,marker)
        session=namespace['SshSession'].__new__(namespace['SshSession'])
        session.client=Client();session.mux_name=name;session._screen_marker=('\x1e'+marker+':').encode()
        session._screen_pending_at=0;session._screen_probe_at=time.monotonic();session._screen_scanned=0;session._screen_pending=bytearray();session._screen_tty=''
        session._screen_wait_output=False;session._screen_fit_task=None;session._screen_revision=0;session._last_pty_size=(180,60)
        session.buf=bytearray();session.seq=0;session.wake=asyncio.Event()
        class PtyChannel:
            closed=False
            def exit_status_ready(self):return proc.poll() is not None
            def resize_pty(self,c,r):
                fcntl.ioctl(master,termios.TIOCSWINSZ,struct.pack('HHHH',r,c,0,0));proc.send_signal(signal.SIGWINCH)
        session.chan=PtyChannel()
        # Read the actual command's metadata before its client has necessarily finished attaching.
        deadline=time.monotonic()+3
        while session._screen_fit_task is None and time.monotonic()<deadline:
            if select.select([master],[],[],.05)[0]:session._push(os.read(master,4096))
        assert session._screen_tty==tty,'metadata did not identify the actual PTY'
        await session._screen_fit_task
        assert dimensions()==(180,60),('initial fit',info())
        # Real prefix collision: allocate an otherwise unused PTY whose name extends our tty.
        # Its separate Screen window must never be resized by the target display's fit.
        collision=None
        for _ in range(512):
            pair=pty.openpty();candidate=os.ttyname(pair[1])
            if candidate.startswith(tty) and candidate!=tty:
                collision=pair;break
            padding.append(pair)
        assert collision is not None, 'could not allocate a bounded prefix-collision fixture'
        other,other_master,other_tty=attach(90,35,pair=collision)
        await asyncio.sleep(.2);os.write(other_master,b'\x01c');await asyncio.sleep(.2)
        subprocess.run(['sh','-c','screen -S '+shlex.quote(name)+' -X fit < '+shlex.quote(other_tty)],check=True)
        def other_dimensions():
            text=subprocess.run(['screen','-S',name,'-p','1','-Q','info'],capture_output=True,text=True,timeout=3).stdout
            match=re.search(r'/\((\d+),(\d+)\)',text)
            return tuple(map(int,match.groups())) if match else None
        assert other_dimensions()==(90,35),other_dimensions()
        await session.resize(200,70);await session._screen_fit_task
        assert dimensions()==(200,70),('resize',info())
        assert other_dimensions()==(90,35),('prefix collision resized another window',other_dimensions())
        # Another display changes the shared pane; reattaching unchanged PTY size must repair it.
        subprocess.run(['sh','-c','screen -S '+shlex.quote(name)+' -X fit < '+shlex.quote(ttys[0])],check=True)
        assert dimensions()==(80,24),('small client setup',info())
        await session.resize(200,70);await session._screen_fit_task
        assert dimensions()==(200,70),('same-size reattach',info())
        # A sizing command opens the tty only to identify it, never to consume pending input.
        os.write(master,b"printf '\\120\\103\\137\\120\\105\\116\\104\\111\\116\\107\\137\\117\\113\\n'")
        await session.resize(210,75);await session._screen_fit_task
        os.write(master,b'\r');transcript=b'';deadline=time.monotonic()+3
        while b'PC_PENDING_OK' not in transcript and time.monotonic()<deadline:
            if select.select([master],[],[],.05)[0]:transcript+=os.read(master,8192)
        assert b'PC_PENDING_OK' in transcript,'fit consumed pending terminal input'
        assert all(p.poll() is None for p,_ in clients),'a display was detached'
        print(json.dumps({'initial':[180,60],'resized':[200,70],'reattach':[200,70],'bothClientsAttached':True,'prefixCollisionIsolated':True,'pendingInputPreserved':True}))
    try:asyncio.run(run())
    finally:
        subprocess.run(['screen','-S',name,'-X','quit'],capture_output=True,timeout=3)
        for master,slave in padding:os.close(master);os.close(slave)
        for proc,master in clients:
            os.close(master)
            try:proc.wait(timeout=2)
            except subprocess.TimeoutExpired:proc.terminate();proc.wait(timeout=2)
    return 0

if __name__=='__main__':raise SystemExit(main())
