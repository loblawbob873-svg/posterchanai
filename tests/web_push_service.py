"""A LOCAL WEB PUSH SERVICE: the receiving side of RFC 8030 (delivery), RFC 8292 (VAPID) and RFC 8291
(message encryption), written independently of the sender.

The server's push path ends in pywebpush, which POSTs an encrypted body to whatever endpoint the
browser's vendor handed out (fcm.googleapis.com, updates.push.services.mozilla.com, ...). Every test
before this one stopped at the call into pywebpush: what crossed the wire -- the headers, the VAPID
signature, the ciphertext -- was never looked at, so a payload that no browser could decrypt, or a
VAPID claim a push service rejects, would have passed them all.

This is what a vendor's push service checks, and then what the browser does with the bytes:

  * the POST carries a TTL and `Content-Encoding: aes128gcm`;
  * `Authorization: vapid t=<JWT>, k=<key>`: the JWT is ES256, VERIFIED here against `k`, and its
    `aud` is the endpoint's origin, `exp` is in the future and at most 24h away, `sub` a contact URI;
  * the body is decrypted with the SUBSCRIBER's private key and auth secret by a decoder written here
    from the RFCs (HKDF + AES-128-GCM over `cryptography`), never with http_ece, the library pywebpush
    encrypts with -- one shared bug in both directions would otherwise look like a working round trip.

An endpoint can be told to answer 404/410 (unsubscribed / expired at the vendor), which is what makes
the server prune the device. Nothing here talks to any real push service.
"""
from __future__ import annotations

import base64
import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


def unb64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + '=' * (-len(s) % 4))


def _hkdf(salt: bytes, ikm: bytes, info: bytes, n: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=n, salt=salt, info=info).derive(ikm)


class Browser:
    """One PushSubscription's keys, as a browser holds them (p256dh keypair + 16-byte auth secret)."""

    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.public = self.key.public_key().public_bytes(serialization.Encoding.X962,
                                                         serialization.PublicFormat.UncompressedPoint)
        self.auth = secrets.token_bytes(16)

    def keys(self) -> dict:
        return {'p256dh': b64u(self.public), 'auth': b64u(self.auth)}

    def decrypt(self, body: bytes) -> bytes:
        """RFC 8188 aes128gcm, keyed per RFC 8291 section 3.4."""
        salt, rs, idlen = body[:16], int.from_bytes(body[16:20], 'big'), body[20]
        as_public = body[21:21 + idlen]
        records = body[21 + idlen:]
        assert idlen == 65, f'keyid must be the sender ECDH key (65 bytes), got {idlen}'
        assert rs >= 18, rs
        peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public)
        secret = self.key.exchange(ec.ECDH(), peer)
        ikm = _hkdf(self.auth, secret, b'WebPush: info\x00' + self.public + as_public, 32)
        cek = _hkdf(salt, ikm, b'Content-Encoding: aes128gcm\x00', 16)
        nonce = _hkdf(salt, ikm, b'Content-Encoding: nonce\x00', 12)
        out = b''
        seq = 0
        for i in range(0, len(records), rs):
            rec = records[i:i + rs]
            n = (int.from_bytes(nonce, 'big') ^ seq).to_bytes(12, 'big')
            plain = AESGCM(cek).decrypt(n, rec, None).rstrip(b'\x00')
            last = i + rs >= len(records)
            assert plain[-1:] == (b'\x02' if last else b'\x01'), 'bad record delimiter'
            out += plain[:-1]
            seq += 1
        return out


def verify_vapid(header: str, endpoint: str) -> dict:
    """Parse and VERIFY `vapid t=..., k=...`; returns the claims plus the key."""
    assert header.startswith('vapid '), f'not a VAPID authorization: {header[:20]!r}'
    parts = dict(p.strip().split('=', 1) for p in header[6:].split(','))
    token, k = parts['t'], parts['k']
    h, p, sig = token.split('.')
    head = json.loads(unb64u(h))
    assert head.get('alg') == 'ES256', head
    raw = unb64u(sig)
    assert len(raw) == 64, 'ES256 signature must be raw r||s'
    pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), unb64u(k))
    pub.verify(encode_dss_signature(int.from_bytes(raw[:32], 'big'), int.from_bytes(raw[32:], 'big')),
               f'{h}.{p}'.encode(), ec.ECDSA(hashes.SHA256()))       # raises InvalidSignature
    claims = json.loads(unb64u(p))
    u = urlparse(endpoint)
    assert claims.get('aud') == f'{u.scheme}://{u.netloc}', claims
    now = time.time()
    assert now < int(claims['exp']) <= now + 86400 + 60, claims
    assert str(claims.get('sub', '')).startswith(('mailto:', 'https:')), claims
    return dict(claims, k=k)


class PushService:
    """A push service on 127.0.0.1: `subscribe()` mints an endpoint + browser keys; every delivery is
    checked and decrypted into `received[token]`; `expire(token, 410)` makes that endpoint gone."""

    def __init__(self):
        self.browsers: dict[str, Browser] = {}
        self.received: dict[str, list[dict]] = {}
        self.attempts: dict[str, int] = {}
        self.gone: dict[str, int] = {}
        self.errors: list[str] = []
        svc = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                tok = self.path.rsplit('/', 1)[-1]
                body = self.rfile.read(int(self.headers.get('Content-Length') or 0))
                svc.attempts[tok] = svc.attempts.get(tok, 0) + 1
                if tok in svc.gone or tok not in svc.browsers:
                    self.send_response(svc.gone.get(tok, 404))
                    self.end_headers()
                    return
                try:
                    endpoint = svc.endpoint(tok)
                    assert self.headers.get('TTL') is not None, 'RFC 8030 requires a TTL header'
                    assert self.headers.get('Content-Encoding') == 'aes128gcm', self.headers.get('Content-Encoding')
                    vapid = verify_vapid(self.headers.get('Authorization') or '', endpoint)
                    data = svc.browsers[tok].decrypt(body)
                    svc.received.setdefault(tok, []).append(
                        {'payload': json.loads(data), 'raw': data.decode(), 'vapid': vapid,
                         'ttl': int(self.headers.get('TTL')), 'urgency': self.headers.get('Urgency')})
                    self.send_response(201)
                except Exception as e:                         # a push service rejects what it cannot read
                    svc.errors.append(f'{type(e).__name__}: {e}')
                    self.send_response(400)
                self.end_headers()

        self.srv = ThreadingHTTPServer(('127.0.0.1', 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def endpoint(self, tok: str) -> str:
        return f'http://127.0.0.1:{self.port}/push/{tok}'

    def subscribe(self) -> tuple[str, dict]:
        tok = secrets.token_hex(8)
        self.browsers[tok] = Browser()
        return tok, {'endpoint': self.endpoint(tok), 'keys': self.browsers[tok].keys()}

    def expire(self, tok: str, status: int = 410):
        self.gone[tok] = status

    def take(self, tok: str) -> list[dict]:
        out = self.received.get(tok, [])
        self.received[tok] = []
        return out

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()
