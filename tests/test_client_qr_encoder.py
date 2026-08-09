"""The in-browser QR encoder (static/js/client/qr.js).

This file exists because a QR code is the one thing in the app that CANNOT be checked by looking at
it. A wrong error-correction table, a mis-drawn alignment pattern or a mask scored the wrong way all
produce a picture that is indistinguishable from a working one to a human, and unreadable to a phone
— on the sign-in screen, where the QR *is* the instruction and the user has no way to tell whose
fault it is.

So nothing here asserts what the encoder produced. It DECODES it, with jsQR — the scanner this app
already vendors for its own camera, i.e. a completely independent implementation — and checks the
text comes back byte for byte, at every version from 1 to 40. Version selection is cross-checked
against segno, which is what the server used to render these before the client could.
"""
import json
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QR_JS = os.path.join(ROOT, "static", "js", "client", "qr.js")
JSQR = os.path.join(ROOT, "static", "vendor", "qr", "jsqr.js")

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

# The payloads the feature actually carries, alongside the size sweep.
REAL = [
    # A nostrconnect:// URI of the shape beginNostrConnect builds: client pubkey, relay, secret, name.
    "nostrconnect://" + "a3" * 32 + "?relay=wss%3A%2F%2Frelay.nsec.app&secret=" + "9f" * 16
    + "&name=PosterChan&perms=sign_event%3A1%2Csign_event%3A4%2Cnip44_encrypt%2Cnip44_decrypt",
    # ...and the one it ACTUALLY builds, with the full 19-entry `perms` list. Kept separate from the
    # short form above because size is the whole point: the real list pushes this to ~547 bytes and QR
    # version 18 (89x89 modules), where the short sample sits around version 9. A sample that is a
    # third of production's size tests a symbol the app never shows — and it was the growth of `perms`
    # that made the signer QR dense enough to be hard to scan off a screen in the first place.
    "nostrconnect://" + "a3" * 32 + "?relay=wss%3A%2F%2Frelay.nsec.app&secret=k9x2m4p7qz&perms="
    + "get_public_key%2Cnip04_encrypt%2Cnip04_decrypt%2Cnip44_encrypt%2Cnip44_decrypt"
    + "".join("%2Csign_event%3A" + str(k) for k in
              (0, 1, 3, 4, 5, 6, 7, 1059, 9734, 10000, 10002, 10003, 10050, 27235, 30078))
    + "&name=PosterChan&url=https%3A%2F%2Fposter.place",
    # A Bitcoin Cash tip URI (the two tip QRs).
    "bitcoincash:qzm47qz5ue99y9yl4aca7jnz7dwgdenl85jkfx3znl?amount=0.005",
    # An npub, the shortest thing anyone points a camera at here.
    "nostr:npub1fdtthaqmqjvmnwsxqrq2r0y0e4x2n5qjqx0v8y7yxq0m3v9yq8jqvhxvzr",
    # Non-ASCII must go through TextEncoder as UTF-8, not be truncated to bytes.
    "hello — ünïcode ✓ 日本語 🎉",
    "a",
]


def _run(payloads):
    """Encode each payload with qr.js, then decode the result with jsQR. Returns one dict per payload."""
    script = f"""
      const fs = require('fs');
      global.window = {{}};
      require({json.dumps(QR_JS)});
      const PCQR = global.window.PCQR;
      const jsQR = require({json.dumps(JSQR)});
      const SCALE = 4, BORDER = 4;          // a quiet zone is part of the spec; jsQR wants one too
      const out = [];
      for(const text of {json.dumps(payloads)}){{
        const q = PCQR.modules(text);
        const dim = (q.size + BORDER * 2) * SCALE;
        const px = new Uint8ClampedArray(dim * dim * 4).fill(255);
        for(let y = 0; y < q.size; y++) for(let x = 0; x < q.size; x++){{
          if(!q.mod[y][x]) continue;
          for(let dy = 0; dy < SCALE; dy++) for(let dx = 0; dx < SCALE; dx++){{
            const px0 = ((y + BORDER) * SCALE + dy) * dim + (x + BORDER) * SCALE + dx;
            px[px0 * 4] = px[px0 * 4 + 1] = px[px0 * 4 + 2] = 0;
          }}
        }}
        const got = jsQR(px, dim, dim);
        out.push({{ version: q.version, size: q.size, decoded: got ? got.data : null }});
      }}
      console.log('OUT<<<' + JSON.stringify(out) + '>>>');
    """
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=600, cwd=ROOT)
    assert r.returncode == 0, f"node failed:\n{r.stdout}\n{r.stderr}"
    return json.loads(r.stdout.split("OUT<<<")[1].split(">>>")[0])


def _payload(n, seed):
    """n printable ASCII bytes, varied so the mask choice and the run penalties are exercised rather
    than a single repeating character (which is the easiest possible case for every one of them)."""
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789:/?&=._-"
    return "".join(alphabet[(i * 7 + seed * 13) % len(alphabet)] for i in range(n))


def test_every_version_encodes_and_decodes_back():
    """Versions 1 through 40, each filled to its exact byte capacity so that version is the one used.

    Filling to capacity is the point: an off-by-one in the capacity table, or the 8-vs-16-bit character
    count that changes at version 10, only shows up on the payload that just fits.
    """
    # Byte capacity per version at level M, taken from segno's own tables rather than from the code
    # under test — a capacity bug that agreed with itself would otherwise pass.
    from segno import consts

    payloads, want_versions = [], []
    for v in range(1, 41):
        cap = consts.SYMBOL_CAPACITY[v][consts.ERROR_LEVEL_M] // 8
        cap -= 2 if v < 10 else 3          # the mode indicator + character count
        payloads.append(_payload(cap, v))
        want_versions.append(v)

    results = _run(payloads)
    for want, text, got in zip(want_versions, payloads, results):
        assert got["version"] == want, (
            f"a {len(text)}-byte payload picked version {got['version']}, want {want} — the capacity "
            "table is wrong, which means every code at this size is a different symbol than intended")
        assert got["decoded"] == text, (
            f"version {want} ({got['size']}x{got['size']}) did not decode back: "
            f"{'nothing was readable' if got['decoded'] is None else 'got ' + repr(got['decoded'][:60])}")


def test_the_payloads_this_app_actually_shows():
    """The signer URI, a tip URI, an npub and a unicode string. The unicode one is not decoration: a
    naive charCodeAt loop encodes 'é' as one byte and produces a QR that decodes to mojibake."""
    for text, got in zip(REAL, _run(REAL)):
        assert got["decoded"] == text, (
            f"{text[:40]!r}… did not survive the round trip: {got['decoded']!r}")


def test_version_agrees_with_what_the_server_used_to_send():
    """segno rendered these before the client could. Same mode, same error level, so the same symbol
    version — a client QR that is suddenly two versions denser would still scan, but it would mean the
    two encoders disagree about the payload, which is worth knowing."""
    import segno

    results = _run(REAL)
    for text, got in zip(REAL, results):
        # micro=False: segno will otherwise answer a Micro QR ("M3") for a short payload, which is a
        # different symbology — no phone signer reads it, and this encoder does not produce one.
        want = segno.make(text, error="m", mode="byte", micro=False).version
        assert got["version"] == want, f"{text[:30]!r}: client version {got['version']}, segno {want}"


def test_too_long_is_an_error_not_an_empty_box():
    """Callers fall back to showing the link itself, which only works if they are told."""
    script = f"""
      global.window = {{}};
      require({json.dumps(QR_JS)});
      let msg = '';
      try {{ global.window.PCQR.modules('x'.repeat(3000)); }} catch(e) {{ msg = e.message; }}
      console.log('OUT<<<' + JSON.stringify(msg) + '>>>');
    """
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=120, cwd=ROOT)
    assert r.returncode == 0, r.stderr
    msg = json.loads(r.stdout.split("OUT<<<")[1].split(">>>")[0])
    assert "too long" in msg, f"an over-long payload must throw, got {msg!r}"
