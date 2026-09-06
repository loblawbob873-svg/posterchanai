"""`pcHost.fileUrl` MUST RETURN A URL, AND IT WAS THROWING ON EVERY CALL.

Reported twice as "0 thumbnails loaded in File Manager" — the second time because the first fix
shipped with this in it:

    fileUrl: (target) => 'app://posterchan/__hostfile/'
      + String(target || '').split('/').map(encodeURIComponent).join('/')
      .then((b) => new Uint8Array(b)),

The `.then(...)` is a fragment of `read` pasted onto a STRING, so every call threw
`String(...).split(...).map(...).join(...).then is not a function`. `thumbAttr` in hostfiles.js
wraps the call in a try/catch and answers '' on failure — which is the RIGHT shape for a host with
no such bridge (the web and the APK) and is exactly why nothing was ever logged. The file list
looked precisely as it had before the feature existed.

Measured on the laptop in ~/Pictures/2021: 85 tiles, 85 `.file-icon` elements, and ZERO carrying
`data-thumb-host`.

The function is EXTRACTED FROM THE SHIPPED FILE and RUN. A test that read the source for
`__hostfile` would have passed against the broken version — the marker was there; it just threw
before it could be used.
"""
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run(paths):
    src = (ROOT / "desktop/preload.js").read_text(encoding="utf-8")
    m = re.search(r"fileUrl: \(target\) =>(.*?),\n\s*open:", src, re.S)
    assert m, "fileUrl moved or changed shape"
    body = m.group(1).strip()
    js = ("const fileUrl = (target) => " + body + ";\n"
          "console.log(JSON.stringify(" + json.dumps(paths) + ".map(p => {"
          "  try { return fileUrl(p); } catch (e) { return 'THREW: ' + e.message; } })));")
    out = subprocess.run(["node", "-e", js], cwd=ROOT, text=True, capture_output=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_it_returns_a_url_and_does_not_throw():
    got = _run(["/home/u/Pictures/2021/a.jpg"])
    assert not got[0].startswith("THREW"), got[0]
    assert got[0].startswith("app://posterchan/__hostfile/"), got[0]


def test_every_segment_is_escaped_but_the_separators_survive():
    """A path is not one component: encoding it whole would turn every `/` into %2F and the
    protocol handler would see a single meaningless segment."""
    got = _run(["/home/u/My Pictures/a b&c.jpg"])[0]
    assert "%20" in got, got
    assert got.count("/") >= 5, f"path separators were escaped away: {got}"
    assert "%2F" not in got.upper().replace("%2F", "%2F"), got


def test_an_empty_or_missing_target_is_not_a_crash():
    for value in ("", None):
        got = _run([value])[0]
        assert not got.startswith("THREW"), got


def test_it_is_not_a_promise():
    """The whole bug: a `.then` chained onto a string. The caller uses the result in a CSS url()."""
    src = (ROOT / "desktop/preload.js").read_text(encoding="utf-8")
    m = re.search(r"fileUrl: \(target\) =>(.*?),\n\s*open:", src, re.S)
    body = re.sub(r"/\*.*?\*/", "", m.group(1), flags=re.S)
    assert ".then(" not in body, f"fileUrl still chains a promise onto a string: {body.strip()}"


def test_the_caller_still_fails_soft_for_a_host_without_the_bridge():
    """The try/catch is correct and must stay: the web and the APK have no `pcHost` at all."""
    hf = (ROOT / "static/js/client/hostfiles.js").read_text(encoding="utf-8")
    at = hf.index("const thumbAttr =")
    body = hf[at: hf.index("};", at)]
    assert "catch(_)" in body and "return ''" in body
    assert "window.pcHost && pcHost.fileUrl" in body
