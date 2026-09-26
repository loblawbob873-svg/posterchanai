#!/usr/bin/env python3
"""Publish a verified PosterChanOS ISO to Cloudflare R2 (S3 API), under ONE stable name.

    publish_r2.py /absolute/path/to/posterchan-live-YYYYMMDD.iso

Run by scripts/publish_iso.sh AFTER the router.lan publish verified -- only for an image that passed
the install/boot gates.

WHY THE STABLE NAME IS SAFE. The ISO is uploaded under a STAGING key first (multipart, 64 MB parts,
each part's MD5 checked by R2 via Content-MD5), then verified -- R2's size, and a full SHA-256 we
send as object metadata and compare with the local file -- and only then COPIED over
`posterchanos.iso`. An S3 object replace is atomic: a downloader gets the old image or the new one,
never half. The `.sha256` sidecar is written after the copy. A failure at any step leaves the
published image untouched.

CREDENTIALS NEVER TOUCH THE REPO, A COMMAND LINE OR A LOG: ~/.config/posterchan/
r2.access_key_id, r2.secret_access_key, cloudflare.account (mode 600). The access key id is the
R2 API token's id; the secret is SHA-256 of the token (how Cloudflare derives S3 credentials).

Signing is AWS Signature V4, stdlib only -- tests/test_publish_r2.py checks it against AWS's own
published test vector, so a subtle canonicalisation mistake is a test failure, not a 403 at 2 a.m.

Exit 0 = published and verified; 1 = failed (published copy untouched); 2 = not configured / usage.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import os
import re
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

import urllib.error
import urllib.request

CONF = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "posterchan"
BUCKET = os.environ.get("PC_R2_BUCKET", "posterchan")
KEY = os.environ.get("PC_R2_KEY", "posterchanos.iso")
PART = 64 * 1024 * 1024
# The staged object is put under the stable name with ONE CopyObject, which S3 (and R2) cap at 5 GiB.
# Past that the copy needs UploadPartCopy; refuse up front rather than after uploading 5 GB for nothing.
COPY_LIMIT = 5 * 1024 ** 3
REGION, SERVICE = "auto", "s3"


class Refused(Exception):
    pass


# ------------------------------------------------------------------ AWS Signature Version 4

def _h(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _hmac(k: bytes, m: str) -> bytes:
    return hmac.new(k, m.encode(), hashlib.sha256).digest()


def _q(s: str, safe: str = "-_.~") -> str:
    return urllib.parse.quote(s, safe=safe)


def sign(method: str, url: str, headers: dict, payload_hash: str, *, key_id: str, secret: str,
         now: dt.datetime, region: str = REGION, service: str = SERVICE) -> dict:
    """Headers (incl. Authorization) for one request. `headers` must already hold `host`."""
    u = urllib.parse.urlsplit(url)
    amz = now.strftime("%Y%m%dT%H%M%SZ")
    day = amz[:8]
    hdrs = {k.lower(): " ".join(str(v).strip().split()) for k, v in headers.items()}
    hdrs["x-amz-date"] = amz
    if service == "s3":
        hdrs["x-amz-content-sha256"] = payload_hash
    names = sorted(hdrs)
    canon_headers = "".join(f"{n}:{hdrs[n]}\n" for n in names)
    signed = ";".join(names)
    path = "/".join(_q(urllib.parse.unquote(p)) for p in (u.path or "/").split("/")) or "/"
    pairs = sorted((_q(urllib.parse.unquote_plus(k)), _q(urllib.parse.unquote_plus(v)))
                   for k, v in urllib.parse.parse_qsl(u.query, keep_blank_values=True))
    query = "&".join(f"{k}={v}" for k, v in pairs)
    creq = "\n".join([method, path, query, canon_headers, signed, payload_hash])
    scope = f"{day}/{region}/{service}/aws4_request"
    sts = "\n".join(["AWS4-HMAC-SHA256", amz, scope, _h(creq.encode())])
    k = _hmac(_hmac(_hmac(_hmac(("AWS4" + secret).encode(), day), region), service), "aws4_request")
    sig = hmac.new(k, sts.encode(), hashlib.sha256).hexdigest()
    out = {n: hdrs[n] for n in names}
    out["authorization"] = f"AWS4-HMAC-SHA256 Credential={key_id}/{scope}, SignedHeaders={signed}, Signature={sig}"
    return out


# ------------------------------------------------------------------ the R2 client

class R2:
    """Signed S3 requests to one bucket. Standard library only: the packaged publisher runs under the
    system python3 of whatever machine built the image, where httpx is not a given."""

    def __init__(self, account: str, key_id: str, secret: str, bucket: str = BUCKET,
                 clock=lambda: dt.datetime.now(dt.timezone.utc)):
        self.key_id, self.secret, self.bucket, self.clock = key_id, secret, bucket, clock
        self.host = f"{account}.r2.cloudflarestorage.com"

    def req(self, method: str, key: str, query: str = "", body: bytes = b"", headers=None, ok=(200,), timeout=120.0):
        url = f"https://{self.host}/{self.bucket}/{_q(key, safe='/-_.~')}" + (f"?{query}" if query else "")
        h = {"host": self.host, **(headers or {})}
        signed = sign(method, url, h, _h(body), key_id=self.key_id, secret=self.secret, now=self.clock())
        request = urllib.request.Request(url, data=body if method in ("PUT", "POST") else None,
                                         headers=signed, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as r:
                status, content, hdrs = r.status, r.read(), {k.lower(): v for k, v in r.headers.items()}
        except urllib.error.HTTPError as e:
            status, content, hdrs = e.code, e.read() or b"", {k.lower(): v for k, v in e.headers.items()}
        except (urllib.error.URLError, OSError) as e:
            raise Refused(f"{method} {key}: {e}") from None
        if status not in ok:
            code = re.search(rb"<Code>([^<]+)</Code>", content)
            raise Refused(f"{method} {key}{'?' + query if query else ''}: HTTP {status}"
                          + (f" {code.group(1).decode()}" if code else ""))
        return _Resp(status, content, hdrs)


class _Resp:
    def __init__(self, status_code, content, headers):
        self.status_code, self.content, self.headers = status_code, content, headers


def _ns(tag: str) -> str:
    return "{http://s3.amazonaws.com/doc/2006-03-01/}" + tag


def _log(m):
    print(m, flush=True)             # progress must reach a redirected log DURING a 4 GB upload


def publish(r2: R2, iso: Path, *, key: str = KEY, log=_log, sleep=time.sleep) -> dict:
    size = iso.stat().st_size
    if size > COPY_LIMIT:
        raise Refused(f"{iso.name} is {size >> 20} MiB; a single CopyObject stops at 5 GiB -- "
                      "publish_r2.py needs UploadPartCopy before it can publish an image this large")
    sha = hashlib.sha256()
    staging = f".uploading/{key}.{int(time.time())}"
    r = r2.req("POST", staging, "uploads=", headers={"content-type": "application/octet-stream",
                                                        "x-amz-meta-sha256": "pending"})
    upload_id = ET.fromstring(r.content).findtext(_ns("UploadId")) or ET.fromstring(r.content).findtext("UploadId")
    if not upload_id:
        raise Refused("CreateMultipartUpload returned no UploadId")
    parts = []
    try:
        with iso.open("rb") as f:
            n = 0
            while True:
                chunk = f.read(PART)
                if not chunk:
                    break
                n += 1
                sha.update(chunk)
                md5 = base64.b64encode(hashlib.md5(chunk).digest()).decode()
                for attempt in range(4):
                    try:
                        pr = r2.req("PUT", staging, f"partNumber={n}&uploadId={_q(upload_id)}", body=chunk,
                                    headers={"content-md5": md5, "content-length": str(len(chunk))}, timeout=900.0)
                        break
                    except Refused as e:
                        if attempt == 3:
                            raise
                        log(f"r2: part {n} retry ({e})")
                        sleep(5 * (attempt + 1))
                parts.append((n, pr.headers.get("etag", "")))
                if n % 8 == 0:
                    log(f"r2: {n * PART // (1 << 20)} MiB of {size >> 20} MiB")
        body = "<CompleteMultipartUpload>" + "".join(
            f"<Part><PartNumber>{n}</PartNumber><ETag>{e}</ETag></Part>" for n, e in parts) + "</CompleteMultipartUpload>"
        r2.req("POST", staging, f"uploadId={_q(upload_id)}", body=body.encode(),
               headers={"content-type": "application/xml"}, timeout=600.0)
    except Exception:
        try:
            r2.req("DELETE", staging, f"uploadId={_q(upload_id)}", ok=(200, 204, 404))
        except Exception:
            pass
        raise
    digest = sha.hexdigest()
    try:
        # VERIFY THE STAGED OBJECT before it can replace anything.
        head = r2.req("HEAD", staging)
        got = int(head.headers.get("content-length") or -1)
        if got != size:
            raise Refused(f"staged object is {got} bytes, the ISO is {size}")
        # Copy over the stable name, carrying the verified SHA-256 as metadata (REPLACE directive so
        # the placeholder from the staging upload does not travel with it). Atomic for readers.
        src = f"/{r2.bucket}/{_q(staging, safe='/-_.~')}"
        r2.req("PUT", key, headers={"x-amz-copy-source": src, "x-amz-metadata-directive": "REPLACE",
                                     "x-amz-meta-sha256": digest, "content-type": "application/octet-stream",
                                     "content-disposition": f'attachment; filename="{key}"',
                                     # the same NAME carries every release: an edge must revalidate, never
                                     # hand out yesterday's image or yesterday's checksum
                                     "cache-control": "no-cache"}, timeout=1800.0)
        final = r2.req("HEAD", key)
        if int(final.headers.get("content-length") or -1) != size or final.headers.get("x-amz-meta-sha256") != digest:
            raise Refused("the published object does not match what was verified")
        side = f"{digest}  {key}\n".encode()
        r2.req("PUT", key + ".sha256", body=side, headers={"content-type": "text/plain; charset=utf-8", "cache-control": "no-cache"})
    finally:
        try:
            r2.req("DELETE", staging, ok=(200, 204, 404))
        except Exception:
            pass
    return {"key": key, "size": size, "sha256": digest, "source": iso.name}


def _creds():
    try:
        return tuple((CONF / n).read_text().strip() for n in ("cloudflare.account", "r2.access_key_id", "r2.secret_access_key"))
    except OSError as e:
        raise SystemExit(f"r2: not configured ({e.filename} missing) -- nothing sent") from None


def main(argv) -> int:
    if len(argv) != 2 or not argv[1].startswith("/") or not Path(argv[1]).is_file():
        print("usage: publish_r2.py /absolute/path/to/posterchan-live-YYYYMMDD.iso", file=sys.stderr)
        return 2
    try:
        account, key_id, secret = _creds()
    except SystemExit as e:
        print(e, file=sys.stderr)
        return 2
    try:
        rec = publish(R2(account, key_id, secret), Path(argv[1]))
    except (Refused, OSError) as e:
        print(f"r2: NOT published: {e}", file=sys.stderr)
        return 1
    print(f"r2: published {rec['source']} as {BUCKET}/{rec['key']} (sha256 {rec['sha256']})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
