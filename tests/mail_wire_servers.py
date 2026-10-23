"""A real SMTP server and a real IMAP server on loopback, for driving the shipped mail code over the wire.

Nothing here is a stub of `mail_service`: the app's own `smtplib`/`imaplib` calls connect to these sockets and
speak the protocol (EHLO, STARTTLS with a real TLS handshake, AUTH, MAIL/RCPT/DATA; CAPABILITY, LOGIN, LIST,
SELECT/EXAMINE, UID SEARCH, UID FETCH, APPEND with a literal). What leaves the app is recorded as the exact bytes
that crossed the socket, and every refusal a real server makes is available:

  SmtpSink.rcpt_reply / data_reply / auth_reply   a 5xx answer at that step (the mail is not accepted)
  SmtpSink.data_delay                             the server takes this long to accept the message
  SmtpSink.tls = 'broken'                         STARTTLS is offered and the handshake then fails
  closed_port()                                   a port nothing listens on (connection refused)

Stdlib only (plus `cryptography`, already a dependency, for the throwaway TLS certificate). No internet.
"""
from __future__ import annotations

import base64
import datetime
import os
import re
import socket
import socketserver
import ssl
import tempfile
import threading
import time
from email import message_from_bytes, policy


# ------------------------------------------------------------------------------------------------ TLS
def self_signed(tmpdir: str) -> tuple[str, str]:
    """A throwaway certificate for 127.0.0.1, generated here (nothing committed, nothing fetched)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    import ipaddress
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'mail.test')])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=2))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName('mail.test'),
                                                        x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]),
                           critical=False)
            .sign(key, hashes.SHA256()))
    cp, kp = os.path.join(tmpdir, 'cert.pem'), os.path.join(tmpdir, 'key.pem')
    with open(cp, 'wb') as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(kp, 'wb') as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()))
    return cp, kp


def closed_port() -> int:
    """A loopback port with nothing listening: connecting to it is refused."""
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


# ------------------------------------------------------------------------------------------------ SMTP
class Delivered:
    """One message the sink accepted: the envelope and the exact DATA bytes (dot-unstuffed)."""

    def __init__(self, mail_from, rcpts, data, auth, tls):
        self.mail_from, self.rcpts, self.data, self.auth, self.tls = mail_from, list(rcpts), data, auth, tls

    @property
    def msg(self):
        return message_from_bytes(self.data, policy=policy.default)


class SmtpSink:
    """An ESMTP server with STARTTLS and AUTH PLAIN/LOGIN. `delivered` is what it accepted; `sessions` every
    command it was sent, per connection (for 'what did the client try')."""

    def __init__(self, tmpdir: str | None = None, user='me@home.test', password='app-password'):
        self._tmp = tmpdir or tempfile.mkdtemp(prefix='pc-smtp-')
        self.cert, self.key = self_signed(self._tmp)
        self.user, self.password = user, password
        self.delivered: list[Delivered] = []
        self.sessions: list[list[str]] = []
        self.tls = 'ok'               # 'ok' | 'broken' | 'none' (not offered)
        self.rcpt_reply = None        # e.g. '550 5.1.1 <x>: no such user' (every recipient), or {addr: reply}
        self.data_reply = None        # e.g. '554 5.7.1 rejected as spam'
        self.auth_reply = None        # e.g. '535 5.7.8 bad credentials'
        self.data_delay = 0.0
        self.lock = threading.Lock()
        sink = self

        class H(socketserver.StreamRequestHandler):
            def handle(self):
                sink._session(self)

        self.server = _Server(('127.0.0.1', 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def wait(self, n=1, seconds=20):
        end = time.time() + seconds
        while time.time() < end:
            if len(self.delivered) >= n:
                return self.delivered
            time.sleep(.05)
        raise AssertionError(('the sink got', len(self.delivered), 'messages, wanted', n, self.sessions[-3:]))

    # -- the protocol ------------------------------------------------------------------------------
    def _session(self, h):
        log = []
        with self.lock:
            self.sessions.append(log)
        rfile, wfile, sock = h.rfile, h.wfile, h.connection
        state = {'tls': False, 'auth': None, 'from': None, 'rcpt': []}

        def say(line):
            wfile.write((line + '\r\n').encode())
            wfile.flush()

        say('220 mail.test ESMTP sink')
        while True:
            raw = rfile.readline(65536)
            if not raw:
                return
            line = raw.decode('utf-8', 'replace').rstrip('\r\n')
            log.append(line if not line.upper().startswith('AUTH') else line.split(' ')[0] + ' ' + line.split(' ')[1] + ' ***')
            verb = line.split(' ', 1)[0].upper()
            arg = line[len(verb):].strip()
            if verb in ('EHLO', 'HELO'):
                feats = ['mail.test', '8BITMIME', 'SIZE 52428800']
                if self.tls != 'none' and not state['tls']:
                    feats.append('STARTTLS')
                feats.append('AUTH PLAIN LOGIN')
                for i, f in enumerate(feats):
                    say(('250 ' if i == len(feats) - 1 else '250-') + f)
            elif verb == 'STARTTLS':
                say('220 2.0.0 go ahead')
                if self.tls == 'broken':
                    # A server whose TLS is broken: the client's ClientHello is answered with garbage.
                    try:
                        sock.recv(4096)
                        sock.sendall(b'\x15\x03\x03\x00\x02\x02\x28')     # a fatal TLS alert (handshake_failure)
                    except OSError:
                        pass
                    return
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                ctx.load_cert_chain(self.cert, self.key)
                try:
                    tls = ctx.wrap_socket(sock, server_side=True)
                except (ssl.SSLError, OSError):
                    return
                state['tls'] = True
                rfile, wfile = tls.makefile('rb'), tls.makefile('wb')
                sock = tls
            elif verb == 'AUTH':
                mech, _, rest = arg.partition(' ')
                creds = None
                if mech.upper() == 'PLAIN':
                    if not rest:
                        say('334 ')
                        rest = rfile.readline().decode().strip()
                    try:
                        parts = base64.b64decode(rest).split(b'\0')
                        creds = (parts[1].decode(), parts[2].decode())
                    except Exception:
                        creds = None
                elif mech.upper() == 'LOGIN':
                    say('334 ' + base64.b64encode(b'Username:').decode())
                    u = base64.b64decode(rfile.readline().strip()).decode()
                    say('334 ' + base64.b64encode(b'Password:').decode())
                    p = base64.b64decode(rfile.readline().strip()).decode()
                    creds = (u, p)
                if self.auth_reply:
                    say(self.auth_reply)
                elif creds == (self.user, self.password):
                    state['auth'] = creds[0]
                    say('235 2.7.0 authenticated')
                else:
                    say('535 5.7.8 authentication failed')
            elif verb == 'MAIL':
                m = re.search(r'<([^>]*)>', arg)
                state['from'], state['rcpt'] = (m.group(1) if m else arg), []
                say('250 2.1.0 ok')
            elif verb == 'RCPT':
                m = re.search(r'<([^>]*)>', arg)
                addr = m.group(1) if m else arg
                refuse = self.rcpt_reply.get(addr) if isinstance(self.rcpt_reply, dict) else self.rcpt_reply
                if refuse:
                    say(refuse)
                elif not re.fullmatch(r'[^@\s<>,]+@[^@\s<>,]+', addr):
                    say('501 5.1.3 bad recipient address syntax')       # what postfix says to <a@x, b@y>
                else:
                    state['rcpt'].append(addr)
                    say('250 2.1.5 ok')
            elif verb == 'DATA':
                if not state['rcpt']:
                    say('554 5.5.1 no valid recipients')
                    continue
                say('354 end data with <CR><LF>.<CR><LF>')
                buf = []
                while True:
                    d = rfile.readline(1 << 20)
                    if not d or d in (b'.\r\n', b'.\n'):
                        break
                    buf.append(d[1:] if d.startswith(b'..') else d)
                if self.data_delay:
                    time.sleep(self.data_delay)
                if self.data_reply:
                    say(self.data_reply)
                else:
                    with self.lock:
                        self.delivered.append(Delivered(state['from'], state['rcpt'], b''.join(buf),
                                                        state['auth'], state['tls']))
                    say('250 2.0.0 queued as SINK%d' % len(self.delivered))
            elif verb == 'RSET':
                state['from'], state['rcpt'] = None, []
                say('250 2.0.0 ok')
            elif verb == 'NOOP':
                say('250 2.0.0 ok')
            elif verb == 'QUIT':
                say('221 2.0.0 bye')
                return
            else:
                say('502 5.5.2 command not recognized')


# ------------------------------------------------------------------------------------------------ IMAP
class Mailbox:
    def __init__(self, name, flags=()):
        self.name, self.flags, self.msgs, self.next_uid = name, list(flags), [], 1   # msgs: [uid, flags, bytes]

    def add(self, raw: bytes, seen=False) -> int:
        uid = self.next_uid
        self.next_uid += 1
        self.msgs.append([uid, ['\\Seen'] if seen else [], raw])
        return uid


def _tokens(s: str) -> list:
    """IMAP arguments: atoms, "quoted strings" (with \\ escapes) and parenthesised lists kept whole."""
    out, i = [], 0
    while i < len(s):
        c = s[i]
        if c == ' ':
            i += 1
        elif c == '"':
            j, buf = i + 1, []
            while j < len(s) and s[j] != '"':
                if s[j] == '\\' and j + 1 < len(s):
                    j += 1
                buf.append(s[j])
                j += 1
            out.append(''.join(buf))
            i = j + 1
        elif c == '(':
            depth, j = 0, i
            while j < len(s):
                depth += {'(': 1, ')': -1}.get(s[j], 0)
                j += 1
                if depth == 0:
                    break
            out.append(s[i:j])
            i = j
        else:
            j = s.find(' ', i)
            j = len(s) if j < 0 else j
            out.append(s[i:j])
            i = j
    return out


def _uidset(spec: str, uids: list) -> list:
    out = []
    top = max(uids) if uids else 0
    for part in spec.split(','):
        if ':' in part:
            a, b = part.split(':', 1)
            a = top if a == '*' else int(a)
            b = top if b == '*' else int(b)
            lo, hi = min(a, b), max(a, b)
            out += [u for u in uids if lo <= u <= hi]
        elif part:
            u = top if part == '*' else int(part)
            if u in uids:
                out.append(u)
    return out


class ImapServer:
    """IMAP4rev1 over plain TCP, enough of it for imaplib and the app: one account, named mailboxes with RFC 6154
    flags. `boxes['INBOX'].add(raw)` delivers a message; `appended` records every APPEND (the Sent copies)."""

    def __init__(self, user='me@home.test', password='app-password'):
        self.user, self.password = user, password
        self.boxes = {'INBOX': Mailbox('INBOX'), 'Sent': Mailbox('Sent', ['\\Sent']),
                      'Drafts': Mailbox('Drafts', ['\\Drafts']), 'Trash': Mailbox('Trash', ['\\Trash'])}
        self.commands: list[str] = []
        self.appended: list[tuple[str, bytes]] = []
        self.lock = threading.Lock()
        srv = self

        class H(socketserver.StreamRequestHandler):
            def handle(self):
                srv._session(self.rfile, self.wfile)

        self.server = _Server(('127.0.0.1', 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def deliver(self, raw: bytes, box='INBOX', seen=False) -> int:
        with self.lock:
            return self.boxes[box].add(raw, seen)

    def _session(self, rfile, wfile):
        sel = {'box': None, 'authed': False}

        def say(b):
            wfile.write(b if isinstance(b, bytes) else (b + '\r\n').encode())
            wfile.flush()

        say('* OK [CAPABILITY IMAP4rev1 UIDPLUS SPECIAL-USE AUTH=PLAIN] test IMAP ready')
        while True:
            raw = rfile.readline(1 << 20)
            if not raw:
                return
            line = raw.decode('utf-8', 'replace').rstrip('\r\n')
            # A literal at the end of the line ({N}): ask for it, read exactly N bytes, then the rest of the line.
            literal = None
            m = re.search(r'\{(\d+)\}$', line)
            if m:
                say('+ ready for literal')
                literal = rfile.read(int(m.group(1)))
                rest = rfile.readline().decode('utf-8', 'replace').rstrip('\r\n')
                line = line[:m.start()].rstrip() + (' ' + rest if rest else '')
            tag, _, cmd = line.partition(' ')
            verb, _, arg = cmd.partition(' ')
            verb = verb.upper()
            with self.lock:
                self.commands.append(verb if verb != 'LOGIN' else 'LOGIN ***')
            if verb == 'UID':
                sub, _, arg = arg.partition(' ')
                verb = 'UID ' + sub.upper()
            try:
                if not self._command(tag, verb, arg, literal, sel, say):
                    return
            except Exception as e:          # a bug here must answer, not hang the client
                say('%s BAD server error %s' % (tag, type(e).__name__))

    def _command(self, tag, verb, arg, literal, sel, say) -> bool:
        if verb == 'CAPABILITY':
            say('* CAPABILITY IMAP4rev1 UIDPLUS SPECIAL-USE AUTH=PLAIN')
            say(tag + ' OK CAPABILITY completed')
        elif verb == 'LOGIN':
            t = _tokens(arg)
            if len(t) >= 2 and (t[0], t[1]) == (self.user, self.password):
                sel['authed'] = True
                say(tag + ' OK [CAPABILITY IMAP4rev1 UIDPLUS SPECIAL-USE] logged in')
            else:
                say(tag + ' NO [AUTHENTICATIONFAILED] invalid credentials')
        elif verb == 'LOGOUT':
            say('* BYE logging out')
            say(tag + ' OK LOGOUT completed')
            return False
        elif verb == 'NOOP':
            say(tag + ' OK NOOP completed')
        elif not sel['authed']:
            say(tag + ' NO not authenticated')
        elif verb == 'LIST':
            for b in self.boxes.values():
                say('* LIST (%s) "/" "%s"' % (' '.join(['\\HasNoChildren'] + b.flags), b.name))
            say(tag + ' OK LIST completed')
        elif verb in ('SELECT', 'EXAMINE'):
            name = (_tokens(arg) or [''])[0]
            box = self.boxes.get(name)
            if not box:
                sel['box'] = None
                say(tag + ' NO [NONEXISTENT] no such mailbox')
            else:
                sel['box'] = box
                say('* %d EXISTS' % len(box.msgs))
                say('* 0 RECENT')
                say('* OK [UIDVALIDITY 7] UIDs valid')
                say('* OK [UIDNEXT %d] next' % box.next_uid)
                say('* FLAGS (\\Seen \\Answered \\Flagged \\Deleted \\Draft)')
                say('%s OK [%s] %s completed' % (tag, 'READ-ONLY' if verb == 'EXAMINE' else 'READ-WRITE', verb))
        elif verb == 'UID SEARCH':
            box = sel['box']
            if not box:
                say(tag + ' BAD no mailbox selected')
            else:
                say('* SEARCH' + ''.join(' %d' % m[0] for m in box.msgs))
                say(tag + ' OK SEARCH completed')
        elif verb == 'UID FETCH':
            box = sel['box']
            if not box:
                say(tag + ' BAD no mailbox selected')
                return True
            spec, _, items = arg.partition(' ')
            items = items.upper()
            uids = [m[0] for m in box.msgs]
            for seq, m in enumerate(box.msgs, 1):
                if m[0] not in _uidset(spec, uids):
                    continue
                head = '* %d FETCH (UID %d FLAGS (%s)' % (seq, m[0], ' '.join(m[1]))
                if 'RFC822' in items or 'BODY' in items:
                    key = 'RFC822' if 'RFC822' in items else 'BODY[]'
                    say((head + ' %s {%d}\r\n' % (key, len(m[2]))).encode() + m[2] + b')\r\n')
                    if 'PEEK' not in items and '\\Seen' not in m[1]:
                        m[1].append('\\Seen')
                else:
                    say(head + ')')
            say(tag + ' OK FETCH completed')
        elif verb == 'APPEND':
            t = _tokens(arg)
            name = t[0] if t else ''
            box = self.boxes.get(name)
            if not box or literal is None:
                say(tag + ' NO [TRYCREATE] no such mailbox')
            else:
                uid = box.add(literal, seen=True)
                self.appended.append((name, literal))
                say('%s OK [APPENDUID 7 %d] APPEND completed' % (tag, uid))
        elif verb in ('CLOSE', 'EXPUNGE', 'CHECK'):
            say(tag + ' OK completed')
        else:
            say(tag + ' BAD unknown command')
        return True
