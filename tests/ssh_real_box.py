"""A real SSH server on loopback for the terminal tests: paramiko's server side, a real bash per channel.

What a real sshd does that the terminal depends on, and nothing more:
  * password AND public-key auth (`authorized` holds the keys it accepts);
  * `shell` (a login shell in a PTY) and `exec` (a command, in a PTY when one was requested -- which is how
    ssh_service runs tmux/screen -- and on plain pipes otherwise, which is how it ends one and fits screen);
  * several channels on ONE connection (the multiplexer's `kill-session`/`fit` ride the shell's transport);
  * a channel closing HANGS UP its process group (SIGHUP, as sshd does) -- which is what DETACHES a tmux or
    screen client while the multiplexer's own server, which daemonised into a session of its own, lives on.

EVERYTHING THE REMOTE SIDE WRITES STAYS IN ONE TEMP DIRECTORY. HOME, SCREENDIR (screen's sockets), TMUX_TMPDIR
(tmux's), the screenrc, and PATH -- a directory of symlinks naming only the tools a test allows, so "is tmux
installed on the far end" is the test's decision and not this machine's. `stop()` ends every process that
carries this box's marker in its environment, multiplexer servers included, and `leftovers()` says whether
anything survived. Nothing it starts outlives the test, and no session of anybody else's is ever touched:
sockets live under this box's own directory and are only ever addressed there.
"""
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time

BASE_TOOLS = ('bash', 'sh', 'tty', 'cat', 'sleep', 'env', 'printf')


class SshBox:
    def __init__(self, password='', user='tester', tools=BASE_TOOLS, authorized=()):
        import paramiko
        self.paramiko = paramiko
        self.user, self.password = user, password
        self.authorized = list(authorized)            # PKey objects accepted for publickey auth
        self.key = paramiko.RSAKey.generate(2048)     # the HOST key
        self.home = tempfile.mkdtemp(prefix='pc-ssh-home-')
        os.chmod(self.home, 0o700)
        self.marker = 'pcsshbox-%d-%s' % (os.getpid(), os.urandom(4).hex())
        self.screendir = os.path.join(self.home, '.screens')
        self.tmuxdir = os.path.join(self.home, '.tmux')
        for d in (self.screendir, self.tmuxdir):
            os.makedirs(d, mode=0o700)
        self.screenrc = os.path.join(self.home, '.screenrc')
        with open(self.screenrc, 'w') as f:
            f.write('startup_message off\nvbell off\n')
        self.bin = os.path.join(self.home, '.bin')
        os.makedirs(self.bin)
        for t in tools:
            p = shutil.which(t, path='/usr/bin:/bin:/usr/local/bin')
            if p:
                os.symlink(p, os.path.join(self.bin, t))
        # The shell the multiplexer starts ($SHELL): bash with no rc files, so this machine's /etc/bash/bashrc and
        # the account's own dotfiles never reach the far end.
        self.shell = os.path.join(self.bin, 'pcshell')
        with open(self.shell, 'w') as f:
            f.write('#!/bin/bash\nexec /bin/bash --noprofile --norc -i\n')
        os.chmod(self.shell, 0o755)
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(('127.0.0.1', 0))
        self.sock.listen(32)
        self.port = self.sock.getsockname()[1]
        self.known_hosts = os.path.join(self.home, 'known_hosts')
        with open(self.known_hosts, 'w') as f:
            f.write('[127.0.0.1]:%d %s %s\n' % (self.port, self.key.get_name(), self.key.get_base64()))
        self.procs, self.transports, self.execs, self.stopped = [], [], [], False
        self.auth_log = []                            # (method, ok) per attempt the server saw
        threading.Thread(target=self._accept, daemon=True).start()

    # ---- the environment of everything the far end runs
    def env(self):
        return {'HOME': self.home, 'PATH': self.bin, 'TERM': 'xterm-256color', 'PS1': 'pc$ ', 'LANG': 'C.UTF-8',
                'SHELL': self.shell, 'SCREENDIR': self.screendir, 'SCREENRC': self.screenrc,
                'SYSSCREENRC': '/dev/null', 'TMUX_TMPDIR': self.tmuxdir, 'PCSSHBOX': self.marker}

    def run(self, command, timeout=10):
        """Run a command AS THE FAR END would (same env, same PATH) -- for asserting what is left there."""
        r = subprocess.run(['/bin/bash', '-c', command], env=self.env(), cwd=self.home, capture_output=True,
                           text=True, timeout=timeout)
        return r.returncode, r.stdout + r.stderr

    def live_transports(self):
        return [t for t in self.transports if t.is_active()]

    def _accept(self):
        while not self.stopped:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        paramiko, box = self.paramiko, self
        chans = {}                                    # chanid -> {'pty': (cols, rows) | None, 'want': ..., 'ev': Event}

        def st(cid):
            return chans.setdefault(cid, {'pty': None, 'want': None, 'ev': threading.Event(), 'fd': None})

        class Iface(paramiko.ServerInterface):
            def get_allowed_auths(self, username):
                return ','.join((['password'] if box.password else []) + (['publickey'] if box.authorized else []))

            def check_auth_password(self, username, password):
                ok = bool(box.password) and username == box.user and password == box.password
                box.auth_log.append(('password', ok))
                return paramiko.AUTH_SUCCESSFUL if ok else paramiko.AUTH_FAILED

            def check_auth_publickey(self, username, key):
                ok = username == box.user and any(k.get_base64() == key.get_base64() for k in box.authorized)
                box.auth_log.append(('publickey', ok))
                return paramiko.AUTH_SUCCESSFUL if ok else paramiko.AUTH_FAILED

            def check_channel_request(self, kind, chanid):
                if kind != 'session':
                    return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
                st(chanid)
                return paramiko.OPEN_SUCCEEDED

            def check_channel_pty_request(self, channel, term, width, height, pw, ph, modes):
                st(channel.get_id())['pty'] = (width or 80, height or 24)
                return True

            def check_channel_window_change_request(self, channel, width, height, pw, ph):
                s = st(channel.get_id())
                s['pty'] = (width, height)
                if s['fd'] is not None:
                    box._winsize(s['fd'], height, width)
                return True

            def check_channel_shell_request(self, channel):
                s = st(channel.get_id())
                s['want'] = None
                s['ev'].set()
                return True

            def check_channel_exec_request(self, channel, command):
                s = st(channel.get_id())
                s['want'] = command.decode('utf-8', 'replace') if isinstance(command, bytes) else str(command)
                box.execs.append(s['want'])
                s['ev'].set()
                return True

        t = paramiko.Transport(conn)
        self.transports.append(t)
        t.add_server_key(self.key)
        try:
            t.start_server(server=Iface())
        except Exception:
            return
        while t.is_active() and not self.stopped:
            chan = t.accept(1)
            if chan is None:
                continue
            threading.Thread(target=self._channel, args=(chan, st(chan.get_id())), daemon=True).start()

    def _channel(self, chan, s):
        if not s['ev'].wait(30):
            chan.close()
            return
        argv = ['/bin/bash', '--noprofile', '--norc', '-i'] if s['want'] is None else ['/bin/bash', '-c', s['want']]
        if s['pty'] is None:
            self._pipes(chan, argv)
        else:
            self._pty(chan, argv, s)

    def _spawn(self, argv, **kw):
        p = subprocess.Popen(argv, env=self.env(), cwd=self.home, start_new_session=True, close_fds=True, **kw)
        self.procs.append(p)
        return p

    def _pty(self, chan, argv, s):
        master, slave = os.openpty()
        s['fd'] = master
        self._winsize(master, s['pty'][1], s['pty'][0])
        p = self._spawn(argv, stdin=slave, stdout=slave, stderr=slave)
        os.close(slave)

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
                    chan.send_exit_status(p.poll() if p.poll() is not None else 0)
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
            self._hangup(p)
            try:
                os.close(master)
            except OSError:
                pass

    def _pipes(self, chan, argv):
        p = self._spawn(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            out, _ = p.communicate(timeout=20)
            chan.sendall(out or b'')
            chan.send_exit_status(p.returncode)
        except Exception:
            self._hangup(p)
        finally:
            try:
                chan.close()
            except Exception:
                pass

    @staticmethod
    def _hangup(p):
        """What sshd does when a channel goes away: HUP the session's process group, then make sure."""
        for sig in (signal.SIGHUP, signal.SIGKILL):
            if p.poll() is not None:
                return
            try:
                os.killpg(p.pid, sig)
            except OSError:
                pass
            try:
                p.wait(2)
            except subprocess.TimeoutExpired:
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

    # ---- what is left behind
    def marked_pids(self):
        """Every live process carrying this box's marker -- multiplexer servers included, whatever they forked."""
        want = ('PCSSHBOX=' + self.marker).encode()
        out = []
        for d in os.listdir('/proc'):
            if not d.isdigit():
                continue
            try:
                with open('/proc/%s/environ' % d, 'rb') as f:
                    if want in f.read().split(b'\0'):
                        out.append(int(d))
            except OSError:
                pass
        return out

    def leftovers(self, settle=5.0):
        """The marked processes still alive after giving them `settle` seconds to go."""
        end = time.time() + settle
        while True:
            pids = [p for p in self.marked_pids() if os.path.exists('/proc/%d' % p) and not _zombie(p)]
            if not pids or time.time() > end:
                return pids
            time.sleep(0.1)

    def stop(self):
        self.stopped = True
        try:
            self.sock.close()
        except OSError:
            pass
        for t in self.transports:
            try:
                t.close()
            except Exception:
                pass
        for p in self.procs:
            self._hangup(p)
        for pid in self.marked_pids():               # a multiplexer server daemonised out of every group above
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        shutil.rmtree(self.home, ignore_errors=True)


class Keys:
    """Every shape of key file an operator might name, written the way the tools that make them write them."""

    def __init__(self, d):
        from cryptography.hazmat.primitives import serialization as ser
        from cryptography.hazmat.primitives.asymmetric import ed25519, rsa
        import paramiko
        self.dir = d
        rsa_k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        ed_k = ed25519.Ed25519PrivateKey.generate()

        def put(name, data, mode=0o600):
            p = os.path.join(d, name)
            with open(p, 'wb') as f:
                f.write(data)
            os.chmod(p, mode)
            return p
        # `ssh-keygen -m PEM` / openssl: the classic `BEGIN RSA PRIVATE KEY`, unencrypted and encrypted
        self.rsa_pem = put('id_rsa', rsa_k.private_bytes(ser.Encoding.PEM, ser.PrivateFormat.TraditionalOpenSSL,
                                                         ser.NoEncryption()))
        self.rsa_pem_locked = put('id_rsa_locked', rsa_k.private_bytes(
            ser.Encoding.PEM, ser.PrivateFormat.TraditionalOpenSSL, ser.BestAvailableEncryption(b'hunter2')))
        # today's `ssh-keygen` default: the OpenSSH container
        self.ed = put('id_ed25519', ed_k.private_bytes(ser.Encoding.PEM, ser.PrivateFormat.OpenSSH, ser.NoEncryption()))
        self.ed_locked = put('id_ed25519_locked', ed_k.private_bytes(
            ser.Encoding.PEM, ser.PrivateFormat.OpenSSH, ser.BestAvailableEncryption(b'hunter2')))
        self.pub = put('id_ed25519.pub', ed_k.public_key().public_bytes(ser.Encoding.OpenSSH, ser.PublicFormat.OpenSSH)
                       + b' tester@laptop\n', 0o644)
        self.missing = os.path.join(d, 'id_gone')
        self.accepted = [paramiko.RSAKey.from_private_key_file(self.rsa_pem),
                         paramiko.Ed25519Key.from_private_key_file(self.ed)]


def _zombie(pid):
    try:
        with open('/proc/%d/stat' % pid) as f:
            return f.read().rsplit(')', 1)[1].split()[0] == 'Z'
    except OSError:
        return True
