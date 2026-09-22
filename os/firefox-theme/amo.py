#!/usr/bin/env python3
"""Publish the theme on addons.mozilla.org as a LISTED (public) version and fetch the signed file.

Standard library only. `web-ext sign --channel listed` could submit, but a listed version is only
signed once Mozilla's review approves it, and web-ext cannot be re-run for a version it already
submitted ("version already exists"), so a review slower than one CI job would strand it. This is
RESUMABLE instead: every run first asks AMO whether this version exists, and only uploads when it
does not — so a scheduled re-run simply picks up the signed file once review is done.

  amo.py --xpi FILE --out SIGNED.xpi [--timeout SECONDS]

Exit 0: SIGNED.xpi written (Mozilla-signed, public). 3: submitted/pending review, nothing written.
1: AMO refused it (validation errors are printed). Credentials: AMO_JWT_ISSUER / AMO_JWT_SECRET.
"""
import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
import zipfile

API = os.environ.get("AMO_API", "https://addons.mozilla.org/api/v5")
SUMMARY = ("PosterChanOS's cyberpunk look for Firefox: a neon synthwave skyline across the tab strip, "
           "violet toolbars, cyan and magenta accents and the PosterChan mascot, with every text "
           "colour pair WCAG AA readable.")
LICENSE = "cc-all-rights-reserved"
CATEGORIES = ["abstract"]
PENDING = 3


def _b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def token(issuer, secret, now=None):
    now = int(now if now is not None else time.time())
    head = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = _b64(json.dumps({"iss": issuer, "jti": str(uuid.uuid4()), "iat": now, "exp": now + 60}).encode())
    sig = hmac.new(secret.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest()
    return f"{head}.{body}.{_b64(sig)}"


class AMO:
    def __init__(self, issuer, secret, api=API):
        self.issuer, self.secret, self.api = issuer, secret, api.rstrip("/")

    def call(self, method, path, data=None, files=None, raw=False):
        url = path if path.startswith("http") else self.api + path
        headers = {"Authorization": "JWT " + token(self.issuer, self.secret)}
        body = None
        if files is not None:
            boundary = uuid.uuid4().hex
            parts = []
            for k, v in (data or {}).items():
                parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
            for k, (name, blob) in files.items():
                parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; filename="{name}"\r\n'
                             f'Content-Type: application/x-xpinstall\r\n\r\n'.encode() + blob + b"\r\n")
            parts.append(f"--{boundary}--\r\n".encode())
            body = b"".join(parts)
            headers["Content-Type"] = "multipart/form-data; boundary=" + boundary
        elif data is not None:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                out = r.read()
                return r.status, (out if raw else (json.loads(out) if out else {}))
        except urllib.error.HTTPError as e:
            out = e.read()
            try:
                return e.code, json.loads(out)
            except ValueError:
                return e.code, {"detail": out.decode("utf-8", "replace")[:500]}


def manifest_of(xpi):
    with zipfile.ZipFile(xpi) as z:
        return json.loads(z.read("manifest.json"))


def is_signed(path):
    with zipfile.ZipFile(path) as z:
        return any(n.startswith("META-INF/") and n.endswith((".rsa", ".p7s")) for n in z.namelist())


def publish(amo, xpi, out, timeout=2400, poll=20, sleep=time.sleep, clock=time.monotonic):
    m = manifest_of(xpi)
    guid, ver = m["browser_specific_settings"]["gecko"]["id"], m["version"]
    vpath = f"/addons/addon/{guid}/versions/v{ver}/"
    status, v = amo.call("GET", vpath)
    if status == 404:
        with open(xpi, "rb") as f:
            blob = f.read()
        status, up = amo.call("POST", "/addons/upload/", data={"channel": "listed"},
                              files={"upload": (os.path.basename(xpi), blob)})
        if status >= 400:
            print("upload refused:", json.dumps(up)[:1500], file=sys.stderr)
            return 1
        deadline = clock() + 900
        while not up.get("processed"):
            if clock() > deadline:
                print("AMO never finished validating the upload", file=sys.stderr)
                return 1
            sleep(5)
            status, up = amo.call("GET", f"/addons/upload/{up['uuid']}/")
        if not up.get("valid"):
            print("AMO validation failed:", json.dumps(up.get("validation"))[:3000], file=sys.stderr)
            return 1
        # An add-on that has only ever had UNLISTED versions has no listing yet; a listed version
        # needs one (summary + category). Idempotent, so it is simply sent every first submission.
        status, r = amo.call("PATCH", f"/addons/addon/{guid}/",
                             data={"categories": CATEGORIES, "summary": {"en-US": SUMMARY},
                                   "homepage": {"en-US": m.get("homepage_url", "https://poster.place")}})
        if status >= 400:
            print("note: listing metadata not accepted:", json.dumps(r)[:800], file=sys.stderr)
        status, v = amo.call("POST", f"/addons/addon/{guid}/versions/",
                             data={"upload": up["uuid"], "license": LICENSE})
        if status >= 400:
            print("version refused:", json.dumps(v)[:1500], file=sys.stderr)
            return 1
        print(f"submitted {guid} {ver} to the public listing")
    elif status >= 400:
        print("could not read the version:", status, json.dumps(v)[:800], file=sys.stderr)
        return 1
    deadline = clock() + timeout
    while True:
        f = (v or {}).get("file") or {}
        if f.get("status") == "public" and f.get("url"):
            st, blob = amo.call("GET", f["url"], raw=True)
            if st >= 400:
                print("download failed:", st, file=sys.stderr)
                return 1
            with open(out, "wb") as fh:
                fh.write(blob)
            if not is_signed(out):
                os.unlink(out)
                print("AMO returned a file with no signature", file=sys.stderr)
                return 1
            print(f"signed and public: {out}")
            return 0
        if f.get("status") in ("disabled",):
            print("Mozilla's review REJECTED this version:", json.dumps(v)[:1500], file=sys.stderr)
            return 1
        if clock() > deadline:
            print(f"{guid} {ver} is awaiting Mozilla's review (file status {f.get('status')!r}); "
                  "a later run will fetch it once approved")
            return PENDING
        sleep(poll)
        status, v = amo.call("GET", vpath)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--xpi", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--timeout", type=int, default=2400)
    a = ap.parse_args(argv)
    iss, sec = os.environ.get("AMO_JWT_ISSUER"), os.environ.get("AMO_JWT_SECRET")
    if not iss or not sec:
        print("AMO_JWT_ISSUER / AMO_JWT_SECRET are not set", file=sys.stderr)
        return 2
    return publish(AMO(iss, sec), a.xpi, a.out, timeout=a.timeout)


if __name__ == "__main__":
    sys.exit(main())
