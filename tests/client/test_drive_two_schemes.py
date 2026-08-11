"""An encrypted drive file must decrypt whichever of the TWO schemes it was written with.

Run: venv-unified/bin/python -m pytest tests/client/test_drive_two_schemes.py

    v2 (`meta.mk`)     — the master key, IV prepended to the blob.
    v1 (`meta.keyenc`) — a key per FILE, itself NIP-44-encrypted to the owner.

`trackUrl` has always branched on that. `_encFileUrl` — the generic path behind Notes attachments,
the desktop wallpaper and its picker — did not: it ran every blob through `_masterDecrypt`, which on
a v1 file throws AES-GCM's `OperationError`, "The operation failed for an operation-specific reason".
That is character-for-character the message a WRONG KEY produces, so the failure read as a key
problem and the retry re-fetched a key that was never at fault. Reported as a desktop background that
would neither preview nor apply, on files whose identical bytes played fine in the music player.

This does not assert that the branch exists — a branch can be present and wrong. It builds a real v1
blob (random AES-GCM key + iv, the key handed back through a stubbed signer exactly as `keyenc` does)
and a real v2 blob, runs the SHIPPED `_driveDecrypt` over both under node's WebCrypto, and checks the
plaintext comes back. The helpers are extracted from app.js rather than copied, so this cannot drift
from what ships.
"""
import json
import os
import re
import shutil
import subprocess
import textwrap

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP = os.path.join(ROOT, "static", "js", "client", "app.js")

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _fn(src, name):
    """The function declaration `name`, from its opening line to the line that closes it.

    Matched on the declaration and closed by brace counting over the DECLARATION ONLY (these three are
    short and contain no prose comments, unlike the modules test_video_mount_browser extracts).
    """
    m = re.search(r"^(\s*)(?:async )?function " + re.escape(name) + r"\(", src, re.M)
    assert m, f"{name} is gone from app.js — the drive decryptor moved, re-point this test"
    i = m.start()
    depth, j, started = 0, i, False
    while j < len(src):
        c = src[j]
        if c == "{":
            depth += 1
            started = True
        elif c == "}":
            depth -= 1
            if started and depth == 0:
                return src[i : j + 1]
        j += 1
    raise AssertionError(f"could not find the end of {name}")


def _run(body):
    src = open(APP, encoding="utf-8").read()
    helpers = "\n".join(_fn(src, n) for n in ("_b64u8", "_aesDecrypt", "_masterDecrypt", "_driveDecrypt"))
    prog = textwrap.dedent(
        """
        const crypto = require('crypto').webcrypto;
        const atob = (b) => Buffer.from(b, 'base64').toString('binary');
        const btoa = (s) => Buffer.from(s, 'binary').toString('base64');
        // The two things _driveDecrypt reaches for besides the crypto helpers. The signer stub returns
        // the per-file key exactly as nip44dec would after unwrapping `keyenc`; the master key is fixed.
        const MK = new Uint8Array(32).fill(7);
        const ME = { pubkey: 'deadbeef' };
        let KEYENC = null;
        const signer = { nip44dec: async () => KEYENC };
        const FilesIdx = { _ensureMK: async () => MK };
        %(helpers)s
        const u8b64 = (u) => Buffer.from(u).toString('base64');
        async function encrypt(key, iv, text, prependIv){
          const ck = await crypto.subtle.importKey('raw', key, 'AES-GCM', false, ['encrypt']);
          const ct = new Uint8Array(await crypto.subtle.encrypt({name:'AES-GCM', iv}, ck,
                                     new TextEncoder().encode(text)));
          if(!prependIv) return ct;
          const out = new Uint8Array(iv.length + ct.length); out.set(iv, 0); out.set(ct, iv.length);
          return out;
        }
        const out = (o) => process.stdout.write(JSON.stringify(o));
        (async () => {
        %(body)s
        })().catch(e => { console.error(e && e.stack || e); process.exit(1); });
        """
        % {"helpers": textwrap.indent(helpers, "        "), "body": textwrap.indent(textwrap.dedent(body), "        ")}
    )
    path = "/tmp/pcai-drive-schemes.js"
    with open(path, "w") as f:
        f.write(prog)
    proc = subprocess.run(["node", path], capture_output=True, timeout=60)
    assert proc.returncode == 0, proc.stderr.decode()[:3000]
    return json.loads(proc.stdout.decode())


def test_a_v1_per_file_key_blob_decrypts():
    """The one that was broken: no `mk`, a `keyenc` carrying the file's own key and iv."""
    r = _run("""
        const key = crypto.getRandomValues(new Uint8Array(32));
        const iv  = crypto.getRandomValues(new Uint8Array(12));
        const blob = await encrypt(key, iv, 'the wallpaper bytes', false);   // v1: iv is NOT prepended
        KEYENC = JSON.stringify({ k: u8b64(key), iv: u8b64(iv) });
        const plain = await _driveDecrypt({ keyenc: 'nip44-ciphertext', mime: 'image/png' }, blob, true);
        out({ text: new TextDecoder().decode(plain) });
    """)
    assert r["text"] == "the wallpaper bytes"


def test_a_v2_master_key_blob_still_decrypts():
    r = _run("""
        const iv = crypto.getRandomValues(new Uint8Array(12));
        const blob = await encrypt(MK, iv, 'the newer bytes', true);        // v2: iv prepended
        const plain = await _driveDecrypt({ mk: 1, mime: 'image/png' }, blob, true);
        out({ text: new TextDecoder().decode(plain) });
    """)
    assert r["text"] == "the newer bytes"


def test_mk_wins_when_a_record_somehow_carries_both():
    """`mk` is the scheme the bytes were actually written with; `keyenc` can linger on a re-encrypted
    record, and preferring it there would decrypt with a key the blob was never sealed under."""
    r = _run("""
        const iv = crypto.getRandomValues(new Uint8Array(12));
        const blob = await encrypt(MK, iv, 'master bytes', true);
        KEYENC = JSON.stringify({ k: u8b64(new Uint8Array(32).fill(9)), iv: u8b64(iv) });
        const plain = await _driveDecrypt({ mk: 1, keyenc: 'stale' }, blob, true);
        out({ text: new TextDecoder().decode(plain) });
    """)
    assert r["text"] == "master bytes"


def test_no_meta_at_all_falls_back_to_the_master_key():
    """An index entry that was never flushed has neither flag — the master key is what such a blob
    was written with, and throwing instead is what 'no key' used to do to a track."""
    r = _run("""
        const iv = crypto.getRandomValues(new Uint8Array(12));
        const blob = await encrypt(MK, iv, 'unflagged', true);
        const plain = await _driveDecrypt(null, blob, false);
        out({ text: new TextDecoder().decode(plain) });
    """)
    assert r["text"] == "unflagged"


def test_a_file_that_was_never_encrypted_is_returned_as_is():
    """The drive holds plain files too — the index carries `enc` and the Files explorer branches on it
    everywhere (public URL and a normal icon, versus the lock card). Decrypting one fails with the very
    same OperationError a wrong key gives, which is what a desktop background that would neither
    preview nor apply actually was: a picture that needed no decrypting, being decrypted."""
    r = _run("""
        const bytes = new TextEncoder().encode('plain PNG bytes');
        const plain = await _driveDecrypt({ name:'wall.png', mime:'image/png', enc:false }, bytes, true);
        out({ text: new TextDecoder().decode(plain) });
    """)
    assert r["text"] == "plain PNG bytes"


def test_an_unencrypted_record_with_no_name_is_still_unencrypted():
    """The first version of this guard inferred "we have an index record" from `m.name !== undefined`,
    and the index does not promise a name — os.js's own backgrounds() falls back to the sha for exactly
    that reason. So a real, unencrypted record that happened to carry no name skipped the passthrough
    and was decrypted anyway: the same failure, on the same screen, one fix later."""
    r = _run("""
        const bytes = new TextEncoder().encode('nameless but plain');
        const plain = await _driveDecrypt({ mime:'image/png', enc:false }, bytes, true);
        out({ text: new TextDecoder().decode(plain) });
    """)
    assert r["text"] == "nameless but plain"


def test_a_synthesised_meta_is_not_an_index_record():
    """A Notes attachment arrives with a `{mime}` built from the NOTE, not from the drive index — it is
    encrypted, and reading its absent `enc` as "plaintext" would hand ciphertext to an <img>."""
    r = _run("""
        const iv = crypto.getRandomValues(new Uint8Array(12));
        const blob = await encrypt(MK, iv, 'attachment bytes', true);
        const plain = await _driveDecrypt({ mime:'image/png' }, blob, false);   // indexed = false
        out({ text: new TextDecoder().decode(plain) });
    """)
    assert r["text"] == "attachment bytes"


def test_a_missing_flag_is_not_a_false_one():
    """With no record at all `enc` is absent, which is not the same as known-plaintext — an index entry
    that was never flushed still needs the master key, and handing back ciphertext would be silent."""
    r = _run("""
        const iv = crypto.getRandomValues(new Uint8Array(12));
        const blob = await encrypt(MK, iv, 'still sealed', true);
        const plain = await _driveDecrypt(null, blob, false);      // no record → master key, not passthrough
        out({ text: new TextDecoder().decode(plain) });
    """)
    assert r["text"] == "still sealed"


def test_both_readers_go_through_the_one_decryptor():
    """Two of these is how they drifted the first time: trackUrl branched, _encFileUrl did not."""
    src = open(APP, encoding="utf-8").read()
    assert src.count("_driveDecrypt(") >= 3, "expected the definition plus both call sites"
    body = _fn(src, "_encFileUrl")
    assert "_driveDecrypt(" in body, "_encFileUrl no longer uses the shared decryptor"
    assert "_masterDecrypt(" not in body, (
        "_encFileUrl decrypts with the master key directly again — that is exactly the bug: a v1 "
        "per-file blob fails there with OperationError, which is indistinguishable from a wrong key")
