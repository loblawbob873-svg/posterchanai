"""The Chrome/Brave MV3 background must BOOT, and `pair` must answer.

Firefox runs the background as an event page, which has a `window`, a DOM and a forgiving startup.
Chrome runs the same three files inside a SERVICE WORKER via importScripts, and a service worker has
none of that. If any of them throws while loading — a stray `window.`, a `browser ?? chrome` that
should have been a `typeof` test, a top-level API that does not exist in a worker — the worker dies
before `onMessage.addListener` is reached. Nothing is registered, so the popup's `sendMessage`
REJECTS, and every caller reports its own generic sentence. The one people send in is "pairing
failed", which describes neither the cause nor even the right layer: the pairing code was never
looked at.

That failure is invisible to every other check here. `tests/test_vault_extension.py` compares the
copied vaultcore against the app's. The mobile/DOM harnesses never load an extension at all. And
headless Chrome on this box will not load an unpacked extension (`--load-extension` leaves no
extension in the profile), so the browser cannot be asked directly — which is exactly why the boot
is simulated instead.

The stub deliberately provides ONLY what MV3 gives a worker. Adding `window` or `document` to it
would defeat the entire point.
"""
import json
import re
import os
import shutil
import subprocess
import tempfile
import textwrap

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXT = os.path.join(ROOT, "extension")

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

# The exact files Chrome's worker pulls in, in the order background-chrome.js lists them.
def _worker_files():
    """The real importScripts CALL, not the comment above it that explains why it is one.

    background-chrome.js documents that importScripts is unavailable in a module worker, so a naive
    "first line containing importScripts(" reads the prose and tries to load it as a filename.
    """
    src = open(os.path.join(EXT, "background-chrome.js"), encoding="utf-8").read()
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)      # drop the header comment
    src = re.sub(r"(?m)^\s*//.*$", "", src)
    m = re.search(r"importScripts\(([^)]*)\)", src)
    assert m, "background-chrome.js no longer calls importScripts — how does Chrome load it now?"
    files = [p.strip().strip("'\"") for p in m.group(1).split(",") if p.strip()]
    assert files, "importScripts() lists nothing"
    return files


_HARNESS = r"""
const fs=require('fs'), vm=require('vm'), path=require('path');
const EXT=process.argv[2], FILES=JSON.parse(process.argv[3]);
const listeners=[]; const noop=()=>{}; const store={};
const chrome={
  runtime:{ onMessage:{addListener:f=>listeners.push(f)}, onInstalled:{addListener:noop},
            id:'test', getURL:p=>'chrome-extension://test/'+p, getPlatformInfo:async()=>({os:'linux'}),
            onConnect:{addListener:noop}, lastError:null, sendMessage:async()=>({}) },
  storage:{ local:{ get:async()=>({}), set:async(o)=>{Object.assign(store,o);}, remove:async()=>{} } },
  alarms:{ create:noop, onAlarm:{addListener:noop} },
  tabs:{ query:async()=>[], onUpdated:{addListener:noop}, sendMessage:async()=>({}) },
  windows:{ create:async()=>({}) },
  action:{ setBadgeText:noop, setBadgeBackgroundColor:noop, onClicked:{addListener:noop} },
  notifications:{ create:noop, onClicked:{addListener:noop} },
  bookmarks:undefined, idle:{onStateChanged:{addListener:noop}},
  contextMenus:{create:noop,onClicked:{addListener:noop}},
  scripting:{ registerContentScripts:async()=>{}, getRegisteredContentScripts:async()=>[] },
};
// A service worker global: no window, no document, no localStorage. That is the whole point.
const ctx={ chrome, self:null, globalThis:null, console,
  WebSocket:function(){ this.close=noop; this.addEventListener=noop; this.send=noop; this.readyState=0; },
  crypto:require('crypto').webcrypto, TextEncoder, TextDecoder,
  atob:s=>Buffer.from(s,'base64').toString('binary'), btoa:s=>Buffer.from(s,'binary').toString('base64'),
  setTimeout, clearTimeout, setInterval:()=>0, clearInterval,
  fetch:async()=>({ok:true,json:async()=>({}),text:async()=>''}), URL, URLSearchParams, performance,
  importScripts:()=>{}, location:{href:'chrome-extension://test/'},
};
ctx.self=ctx; ctx.globalThis=ctx;
vm.createContext(ctx);
const out={loaded:[], listeners:0, replies:{}};
for(const f of FILES){
  try{ vm.runInContext(fs.readFileSync(path.join(EXT,f),'utf8'), ctx, {filename:f}); out.loaded.push(f); }
  catch(e){ out.threw={file:f, name:e.constructor.name, message:e.message};
            process.stdout.write(JSON.stringify(out)); process.exit(0); }
}
out.listeners=listeners.length;
if(!listeners.length){ process.stdout.write(JSON.stringify(out)); process.exit(0); }
const b64=o=>Buffer.from(JSON.stringify(o)).toString('base64');
const PK='e4e847f9b3e29e930b3f05218767828f8ddd5ac177f5ad46a6616f52e5c37d6d';
const KEY=Buffer.alloc(32,7).toString('base64');
const cases={
  ok:      {v:1,t:'pcvault',pubkey:PK,key:KEY,relay:'wss://r.example',relays:['wss://r.example'],mode:'ro'},
  wrapped: {v:1,t:'pcvault',pubkey:PK,key:KEY,relay:'wss://r.example',relays:['wss://r.example'],mode:'ro'},
  norelay: {v:1,t:'pcvault',pubkey:PK,key:KEY,relay:'',relays:[],mode:'ro'},
  fullnosk:{v:1,t:'pcvault',pubkey:PK,key:KEY,relay:'wss://r.example',relays:['wss://r.example'],mode:'full'},
  notours: {v:1,t:'somethingelse',pubkey:PK,key:KEY,relay:'wss://r.example',relays:['wss://r.example']},
};
(async()=>{
  for(const [name,payload] of Object.entries(cases)){
    let code=b64(payload);
    if(name==='wrapped') code=code.replace(/(.{40})/g,'$1\n');   // a textarea copy carries newlines
    out.replies[name]=await new Promise(res=>{ listeners[0]({type:'pair',code}, {}, res); });
  }
  out.replies.garbage=await new Promise(res=>{ listeners[0]({type:'pair',code:'not base64 at all!!'}, {}, res); });
  process.stdout.write(JSON.stringify(out));
})();
"""


def _run():
    # A TEMP dir, not the repo. Written under tests/ it survived any crashed or interrupted run as an
    # untracked file sitting in the working tree, which a later `git add -A` can sweep into a commit.
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "ext_worker_harness.js")
        with open(src, "w", encoding="utf-8") as fh:
            fh.write(textwrap.dedent(_HARNESS))
        r = subprocess.run(["node", src, EXT, json.dumps(_worker_files())],
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        return json.loads(r.stdout)


def test_the_service_worker_loads_every_file_without_throwing():
    out = _run()
    assert "threw" not in out, (
        "the Chrome background died while loading %s (%s: %s). Nothing registers onMessage after "
        "that, so the popup's sendMessage rejects and every action reports its own generic failure "
        "— 'pairing failed' being the one users send in."
        % (out["threw"]["file"], out["threw"]["name"], out["threw"]["message"]))


def test_a_message_listener_is_actually_registered():
    """Loading without throwing is not enough — the listener has to exist, and it has to be
    registered synchronously at load, or Chrome will not deliver the message that woke the worker."""
    out = _run()
    assert out["listeners"] == 1, f"expected exactly one onMessage listener, got {out['listeners']}"


def test_pairing_a_normal_code_succeeds():
    out = _run()
    assert out["replies"]["ok"] == {"ok": True, "mode": "ro"}, out["replies"]["ok"]


def test_a_code_copied_with_line_breaks_still_pairs():
    """It is shown in a <textarea> and copied by hand, so it arrives wrapped more often than not."""
    assert _run()["replies"]["wrapped"].get("ok") is True


def test_every_refusal_says_which_one_it_is():
    """The popup prints `r.error`, so a refusal with no message is what becomes a bare
    'pairing failed' — unactionable for whoever reports it."""
    out = _run()
    for name in ("norelay", "fullnosk", "notours", "garbage"):
        r = out["replies"][name]
        assert r.get("ok") is not True, f"{name} should not have paired"
        assert (r.get("error") or "").strip(), f"{name} refused with no reason given"


def test_the_popup_keeps_the_reason_without_making_the_reply_truthy():
    """A rejected sendMessage means nothing answered — the worker failed to boot — not that the
    operation failed. Chrome's own explanation is the only clue there is, so it must be kept.

    But it must be kept BESIDE the null, never in place of it. Every caller here separates "the
    background said no" from "nothing answered" with a plain `if(!r)`, and several then read the
    reply's fields: returning a truthy error object made the Relays pane paint `(r.relays||[])` — an
    empty textarea — which one Save wrote back as the real relay list, stopping the vault syncing.
    A diagnostic must not be able to delete anything.
    """
    src = open(os.path.join(EXT, "popup.js"), encoding="utf-8").read()
    assert "B.runtime.sendMessage(msg).catch(() => null)" not in src, \
        "the rejection reason is the only clue there is when the worker does not start"
    assert "function lastNoAnswer(" in src and "_noAnswerWhy" in src
    # send() must still be able to resolve null, or every `if(!r)` guard in the file is dead.
    body = src[src.index("const send = async (msg)"):]
    body = body[:body.index("\n};")]
    assert "return null;" in body, \
        "send() must still resolve null when nothing answered — the callers' guards depend on it"
    assert "__noAnswer" not in src, "the truthy error object is what wiped the relay list"


def test_the_no_answer_advice_names_the_right_browser():
    """`chrome://extensions` does not exist in Firefox, and Firefox for Android has no such page at
    all — sending someone there is worse than saying nothing."""
    src = open(os.path.join(EXT, "popup.js"), encoding="utf-8").read()
    fn = src[src.index("function lastNoAnswer("):]
    fn = fn[:fn.index("\n}")]
    assert "typeof browser !== 'undefined'" in fn, "the advice must branch on the actual browser"
    assert "about:addons" in fn and "chrome://extensions" in fn
