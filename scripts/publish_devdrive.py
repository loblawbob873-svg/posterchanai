#!/usr/bin/env python3
"""Publish a verified PosterChanOS ISO to devdrive.cloud (a YetiShare API, https://devdrive.cloud/api).

    publish_devdrive.py /absolute/path/to/posterchan-live-YYYYMMDD.iso

Run by scripts/publish_iso.sh AFTER the router.lan publish succeeded -- i.e. only for an image that
passed the install/boot gates. Never on its own initiative.

THE KEYS NEVER TOUCH THE REPO, A COMMAND LINE OR A LOG. They are read from two files, mode 600:
    ~/.config/posterchan/devdrive.key1   ~/.config/posterchan/devdrive.key2
(or the paths in PC_DEVDRIVE_KEY1_FILE / PC_DEVDRIVE_KEY2_FILE). A command line is visible to every
user on the box in `ps`; a traceback prints its locals -- so the keys go straight into the form body.

Order is the same discipline publish_iso.sh keeps: nothing already published is touched until the
new copy is uploaded AND verified (size, and the content hash when the server reports one). Only
then are the older posterchan-live-*.iso files in the folder removed, so a failed upload leaves the
previous image downloadable rather than none.

Exit 0 = published and verified; 1 = failed (the previous upload is untouched); 2 = not configured
or bad arguments (nothing was sent).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

import httpx

API = os.environ.get("PC_DEVDRIVE_API", "https://devdrive.cloud/api/v2")
FOLDER = os.environ.get("PC_DEVDRIVE_FOLDER", "PosterChanOS")
CONF = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "posterchan"
ISO_NAME = re.compile(r"^posterchan-live-\d{8}\.iso$")


class Refused(Exception):
    """The server answered, and the answer was no."""


def _keys() -> tuple[str, str]:
    out = []
    for n in (1, 2):
        p = Path(os.environ.get(f"PC_DEVDRIVE_KEY{n}_FILE", CONF / f"devdrive.key{n}"))
        try:
            k = p.read_text().strip()
        except OSError:
            raise SystemExit(f"devdrive: no key{n} at {p} -- not configured, nothing sent") from None
        if len(k) != 64:
            raise SystemExit(f"devdrive: key{n} in {p} is {len(k)} characters, the API wants 64")
        out.append(k)
    return out[0], out[1]


def _call(client: httpx.Client, path: str, data: dict, files=None, timeout=60.0) -> dict:
    r = client.post(f"{API}/{path}", data=data, files=files, timeout=timeout)
    try:
        body = r.json()
    except ValueError:
        raise Refused(f"{path}: HTTP {r.status_code}, not JSON") from None
    if r.status_code >= 400 or body.get("_status") != "success":
        raise Refused(f"{path}: {body.get('response') or ('HTTP %d' % r.status_code)}")
    return body.get("data") or {}


def _digests(path: Path) -> tuple[str, str]:
    md5, sha = hashlib.md5(), hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8 << 20), b""):
            md5.update(chunk)
            sha.update(chunk)
    return md5.hexdigest(), sha.hexdigest()


def _folder_id(client, auth) -> str:
    listing = _call(client, "folder/listing", auth)
    for f in listing.get("folders") or []:
        if str(f.get("folderName") or f.get("folder_name") or "") == FOLDER:
            return str(f.get("id"))
    made = _call(client, "folder/create", {**auth, "folder_name": FOLDER, "is_public": 2})
    return str(made.get("id"))


def publish(client: httpx.Client, iso: Path, key1: str, key2: str, log=print) -> dict:
    size = iso.stat().st_size
    md5, sha = _digests(iso)
    auth = _call(client, "authorize", {"key1": key1, "key2": key2})
    auth = {"access_token": auth["access_token"], "account_id": str(auth["account_id"])}
    try:
        pkg = _call(client, "account/package", auth)
        limit = int(pkg.get("max_upload_size") or 0)
        if str(pkg.get("can_upload", "1")) in ("0", "false", "False"):
            raise Refused("account/package: this account may not upload")
        if limit and size > limit:
            raise Refused(f"the ISO is {size} bytes and this plan's max_upload_size is {limit} -- nothing sent")
        folder = _folder_id(client, auth)
        log(f"devdrive: uploading {iso.name} ({size} bytes) to folder {FOLDER}")
        with iso.open("rb") as fh:
            up = _call(client, "file/upload", {**auth, "folder_id": folder},
                       files={"upload_file": (iso.name, fh, "application/octet-stream")},
                       timeout=httpx.Timeout(60.0, read=6 * 3600.0, write=6 * 3600.0))
        new_id = str(up.get("file_id") or "")
        if not new_id:
            raise Refused("file/upload: no file_id in the answer")
        # VERIFY BEFORE REPLACING ANYTHING. The upload's own answer first; then what the server says
        # it stored, so a truncated body the server accepted is caught.
        got = int(up.get("size") or 0)
        if got != size:
            raise Refused(f"uploaded {got} bytes, the ISO is {size}")
        h = str(up.get("hash") or "").lower()
        if re.fullmatch(r"[0-9a-f]{32}", h) and h != md5:
            raise Refused(f"server md5 {h} != local {md5}")
        info = _call(client, "file/info", {**auth, "file_id": new_id})
        if int(info.get("fileSize") or info.get("size") or size) != size:
            raise Refused("file/info reports a different size than was uploaded")
        # Only now: retire the older images in the same folder (never the one just made, never a
        # file that is not ours by name).
        listing = _call(client, "folder/listing", {**auth, "parent_folder_id": folder})
        for f in listing.get("files") or []:
            fid = str(f.get("id") or f.get("file_id") or "")
            name = str(f.get("filename") or f.get("originalFilename") or f.get("name") or "")
            if fid and fid != new_id and ISO_NAME.match(name):
                _call(client, "file/delete", {**auth, "file_id": fid})
                log(f"devdrive: retired {name}")
        url = up.get("url") or up.get("short_url") or ""
        rec = {"file": iso.name, "size": size, "sha256": sha, "md5": md5, "file_id": new_id, "url": url}
        return rec
    finally:
        try:
            _call(client, "disable_access_token", auth)
        except Exception:
            pass


def main(argv: list[str]) -> int:
    if len(argv) != 2 or not argv[1].startswith("/") or not Path(argv[1]).is_file():
        print("usage: publish_devdrive.py /absolute/path/to/posterchan-live-YYYYMMDD.iso", file=sys.stderr)
        return 2
    iso = Path(argv[1])
    try:
        key1, key2 = _keys()
    except SystemExit as e:
        print(e, file=sys.stderr)
        return 2
    try:
        with httpx.Client(follow_redirects=False) as client:
            rec = publish(client, iso, key1, key2)
    except (Refused, httpx.HTTPError, OSError) as e:
        print(f"devdrive: NOT published: {e}", file=sys.stderr)
        return 1
    try:
        CONF.mkdir(parents=True, exist_ok=True)
        (CONF / "devdrive-last.json").write_text(json.dumps(rec, indent=1))
    except OSError:
        pass
    print(f"devdrive: published {rec['file']} -> {rec['url']} (sha256 {rec['sha256']})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
