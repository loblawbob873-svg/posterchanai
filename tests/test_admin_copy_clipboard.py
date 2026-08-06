"""The admin panel's Copy buttons (the .onion address, the relay keys).

The bug this guards is silent by construction: `try { navigator.clipboard.writeText(v) } catch (_) {
fallback }` catches NOTHING, because writeText returns a PROMISE and its rejection is asynchronous.
So on the one deployment where it rejects — the panel framed cross-origin by the client, where
`clipboard-write` is not delegated, i.e. the Windows/desktop app — the button said "copied", the
fallback never ran, and the clipboard kept whatever was in it. Nothing on screen is wrong; you only
find out when you paste.

No assertion about the SOURCE could catch that (the broken version reads fine), so this runs the real
copyToClipboard under node with a clipboard that rejects, and checks the value reached the fallback.
"""
import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADMIN_JS = os.path.join(ROOT, "static", "js", "admin.js")
NETWORK_HTML = os.path.join(ROOT, "templates", "admin", "tabs", "network.html")
CLIENT_JS = os.path.join(ROOT, "static", "js", "client", "app.js")

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _extract(src: str, decl: str) -> str:
    """The function's own source, by brace matching — admin.js touches the DOM at module scope, so it
    cannot simply be require()d."""
    i = src.index(decl)
    j = src.index("{", i)
    depth = 0
    for k in range(j, len(src)):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
    raise AssertionError("unbalanced braces after " + decl)


def _run(scenario: str) -> dict:
    fn = _extract(open(ADMIN_JS, encoding="utf-8").read(), "async function copyToClipboard")
    harness = """
const S = %s;
const log = { exec: 0, copied: null, selected: [], focused: [] };
function makeEl(tag){ return { tagName: tag, value: '', dataset: {}, style: {}, textContent: '',
  focus(){ log.focused.push(this); }, select(){ log.selected.push(this); },
  remove(){ log.removed = true; }, appendChild(){}, }; }
globalThis.document = {
  createElement: makeEl,
  body: { appendChild(){}, },
  execCommand(cmd){
    log.exec++;
    if (!S.execWorks) return false;
    const el = log.selected[log.selected.length - 1];
    log.copied = el ? el.value : null;
    return true;
  },
};
// node ships its own read-only `navigator`, so this has to be defineProperty, not assignment.
Object.defineProperty(globalThis, 'navigator', { configurable: true, writable: true,
  value: S.hasApi ? { clipboard: { writeText: (v) => {
    if (S.apiWorks) { log.copied = v; return Promise.resolve(); }
    return Promise.reject(new DOMException('denied', 'NotAllowedError'));
  } } } : {} });
globalThis.DOMException = globalThis.DOMException || class extends Error {};
globalThis.setTimeout = () => 0;   // the label reset never has to fire

%s

(async () => {
  const btn = makeEl('button'); btn.textContent = '\\u{1F4CB} Copy';
  const src = S.withSrcEl ? makeEl('input') : null;
  if (src) src.value = S.text;
  const ok = await copyToClipboard(S.text, btn, src);
  console.log(JSON.stringify({ ok, copied: log.copied, exec: log.exec, label: btn.textContent,
                               srcSelected: src ? log.selected.includes(src) : false }));
})();
""" % (scenario, fn)
    out = subprocess.run(["node", "--input-type=module", "-e", harness],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    import json
    return json.loads(out.stdout.strip().splitlines()[-1])


ONION = "o2c7ssznoqr3xjfjtewxi2gerrbglckdm5y54lvsev4kv3ahjh2bf4qd.onion"


def test_clipboard_api_available_and_permitted():
    r = _run('{text:"%s", hasApi:true, apiWorks:true, execWorks:true, withSrcEl:true}' % ONION)
    assert r["ok"] is True
    assert r["copied"] == ONION
    assert r["exec"] == 0, "the fallback must not run when the real API worked"


def test_framed_panel_denied_clipboard_write_falls_back():
    """THE regression: the API exists but rejects (cross-origin iframe with no clipboard-write).
    The old code awaited nothing, so this path copied nothing and still claimed success."""
    r = _run('{text:"%s", hasApi:true, apiWorks:false, execWorks:true, withSrcEl:true}' % ONION)
    assert r["exec"] == 1, "an async rejection must reach the execCommand fallback"
    assert r["copied"] == ONION
    assert r["ok"] is True


def test_cleartext_instance_has_no_clipboard_api():
    """An .onion / LAN instance is plain HTTP, so navigator.clipboard is absent entirely."""
    r = _run('{text:"%s", hasApi:false, apiWorks:false, execWorks:true, withSrcEl:false}' % ONION)
    assert r["ok"] is True and r["copied"] == ONION


def test_both_paths_refused_reports_failure_and_selects_the_value():
    r = _run('{text:"%s", hasApi:true, apiWorks:false, execWorks:false, withSrcEl:true}' % ONION)
    assert r["ok"] is False, "must not claim success when nothing was copied"
    assert "Ctrl+C" in r["label"]
    assert r["srcSelected"] is True, "the last resort is a selection the admin can copy by hand"


def test_no_sync_try_catch_around_writeText_in_admin_surfaces():
    """The shape of the original bug, in any admin file: writeText inside a `try {` whose `catch` is
    the fallback, with no `await`. Anything here should go through copyToClipboard instead."""
    bad = []
    for path in [NETWORK_HTML, ADMIN_JS,
                 os.path.join(ROOT, "static", "js", "admin-bots.js"),
                 os.path.join(ROOT, "static", "js", "admin-auth.js"),
                 os.path.join(ROOT, "static", "js", "admin-emoji.js")]:
        if not os.path.exists(path):
            continue
        src = open(path, encoding="utf-8").read()
        src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)          # the doc comment here QUOTES the bad shape
        src = re.sub(r"^\s*//.*$", "", src, flags=re.M)
        for m in re.finditer(r"try\s*\{[^}]*navigator\.clipboard\.writeText[^}]*\}", src):
            frag = m.group(0)
            if "await" not in frag and ".then" not in frag and ".catch" not in frag:
                bad.append((os.path.basename(path), frag[:120]))
    assert not bad, "unawaited writeText in a sync try/catch (the rejection escapes): %r" % bad


def test_client_delegates_clipboard_write_to_the_admin_iframe():
    """The other half: a cross-origin frame gets no clipboard-write unless the framer grants it."""
    src = open(CLIENT_JS, encoding="utf-8").read()
    i = src.index("ifr.className='admin-frame'")
    assert "clipboard-write" in src[i:i + 900], "admin iframe must carry allow='clipboard-write'"
