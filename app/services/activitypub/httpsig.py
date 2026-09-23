"""HTTP Signatures (draft-cavage-12, the form Mastodon, Pleroma, Misskey and GoToSocial all send
and accept) with RSA-SHA256. PURE apart from the key material handed in.

Verification is strict where it matters and the reasons are in the checks:
  * the signed headers must include `(request-target)`, `host` and `date`, and `digest` for a body,
    or a captured signature could be replayed against another path or another body;
  * the Digest must match the body we actually received;
  * the Date must be recent (12h back, 1h ahead -- Mastodon's own window), which bounds replay;
  * the key's OWNER must be the activity's actor. A valid signature from anybody else proves only
    that somebody signed something (see inbox.py, which enforces it).
"""
from __future__ import annotations

import base64
import hashlib
import re
from email.utils import format_datetime, parsedate_to_datetime
from datetime import datetime, timezone

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

MAX_PAST = 12 * 3600
MAX_FUTURE = 3600
_PARAM_RE = re.compile(r'(\w+)="([^"]*)"')


class SignatureError(Exception):
    pass


def new_keypair() -> tuple[str, str]:
    """(private PEM, public PEM) for a fresh 2048-bit RSA key -- what every major server accepts."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()
    pub = key.public_key().public_bytes(serialization.Encoding.PEM,
                                        serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return priv, pub


def digest_of(body: bytes) -> str:
    return "SHA-256=" + base64.b64encode(hashlib.sha256(body or b"").digest()).decode()


def http_date(now: datetime | None = None) -> str:
    return format_datetime(now or datetime.now(timezone.utc), usegmt=True)


def _signing_string(method: str, path: str, headers: dict, names: list) -> str:
    lines = []
    for n in names:
        if n == "(request-target)":
            lines.append(f"(request-target): {method.lower()} {path}")
        else:
            v = headers.get(n)
            if v is None:
                raise SignatureError(f"signed header {n!r} is missing")
            lines.append(f"{n}: {v}")
    return "\n".join(lines)


def sign(method: str, url: str, *, key_id: str, private_pem: str, body: bytes | None = None,
         now: datetime | None = None) -> dict:
    """Headers to send (Host, Date, Digest for a body, Signature) for a request to `url`."""
    from urllib.parse import urlsplit
    u = urlsplit(url)
    path = u.path or "/"
    if u.query:
        path += "?" + u.query
    headers = {"host": u.netloc, "date": http_date(now)}
    names = ["(request-target)", "host", "date"]
    if body is not None:
        headers["digest"] = digest_of(body)
        names.append("digest")
    key = serialization.load_pem_private_key(private_pem.encode(), password=None)
    sig = key.sign(_signing_string(method, path, headers, names).encode(), padding.PKCS1v15(), hashes.SHA256())
    out = {"Host": headers["host"], "Date": headers["date"],
           "Signature": f'keyId="{key_id}",algorithm="rsa-sha256",headers="{" ".join(names)}",'
                        f'signature="{base64.b64encode(sig).decode()}"'}
    if body is not None:
        out["Digest"] = headers["digest"]
    return out


def parse(header: str) -> dict:
    params = dict(_PARAM_RE.findall(header or ""))
    if not params.get("keyId") or not params.get("signature"):
        raise SignatureError("no keyId/signature in the Signature header")
    params["headers"] = (params.get("headers") or "date").lower().split()
    return params


def verify(method: str, path: str, headers: dict, body: bytes | None, public_pem: str,
           params: dict, now: datetime | None = None) -> None:
    """Raise SignatureError unless the request is signed by `public_pem` as `params` claims.

    `headers` are the request's headers with lowercase names; `path` includes the query string.
    The `host` header is the PUBLIC one the sender signed -- behind a proxy the caller passes the
    configured domain, because the upstream request may carry another Host."""
    names = params["headers"]
    for need in ("(request-target)", "host", "date"):
        if need not in names:
            raise SignatureError(f"{need} is not signed")
    if body is not None and len(body) and "digest" not in names:
        raise SignatureError("a request body without a signed Digest")
    if "digest" in names:
        got = headers.get("digest") or ""
        want = digest_of(body or b"")
        # Several algorithms may be listed; ours must be among them and match.
        parts = [p.strip() for p in got.split(",")]
        sha = next((p for p in parts if p.lower().startswith("sha-256=")), "")
        if not sha or sha.split("=", 1)[1] != want.split("=", 1)[1]:
            raise SignatureError("the Digest does not match the body")
    try:
        when = parsedate_to_datetime(headers.get("date") or "")
    except (TypeError, ValueError):
        raise SignatureError("unreadable Date")
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    age = ((now or datetime.now(timezone.utc)) - when).total_seconds()
    if age > MAX_PAST or age < -MAX_FUTURE:
        raise SignatureError("the Date is outside the accepted window")
    algo = (params.get("algorithm") or "rsa-sha256").lower()
    if algo not in ("rsa-sha256", "hs2019"):
        raise SignatureError(f"unsupported algorithm {algo}")
    try:
        key = serialization.load_pem_public_key(public_pem.encode())
    except (ValueError, TypeError):
        raise SignatureError("unreadable public key")
    if not isinstance(key, rsa.RSAPublicKey):
        raise SignatureError("only RSA keys are accepted")
    try:
        key.verify(base64.b64decode(params["signature"]),
                   _signing_string(method, path, headers, names).encode(),
                   padding.PKCS1v15(), hashes.SHA256())
    except (InvalidSignature, ValueError):
        raise SignatureError("the signature does not verify")
