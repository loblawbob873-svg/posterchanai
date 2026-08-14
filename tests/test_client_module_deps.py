"""Every client module resolves every name it uses — checked with a real parser.

THE REFACTOR THIS EXISTS FOR. `static/js/client/app.js` is ~29k lines in one IIFE and is being split
into modules. Each extraction moves code out of a scope where everything was in reach into one where
nothing is, so the entire risk is a name that used to resolve and now does not. That failure is
invisible until somebody opens the view: `node --check` passes (the syntax is fine), the page loads,
the tests pass, and the feature throws `ReferenceError` on click.

WHY A PARSER AND NOT A REGEX — twice learned, both times the hard way:

  * Deriving the dependency list by hand reported 13 names for a block that needed 40. A scan that
    strips `//` comments before looking for identifiers eats the rest of any line containing
    `https://` inside a template literal, which is most of the render code.
  * The first version of THIS FILE was regex-based and failed the `git.js` extraction with a list of
    "unresolved" names that included All, Binary, Close and `also` — words lifted out of comments and
    markup — while the one real-looking entry, `_gitKbBound`, was declared on the shipped line
    `let _kbSel=-1, _gitKbBound=false;` and missed because the pattern only caught the first name in
    a multi-declarator `let`. A checker that cries wolf about a correct extraction is worse than none:
    it trains you to ignore it on the day it is right.

So scope resolution is done by acorn. It is not a dependency of this repo, so the test SKIPS when no
parser can be found rather than pretending — an honest skip beats a regex approximation.

WHAT IT CANNOT SEE, so nobody trusts it further than it goes: a name reached dynamically
(`window[x]`, `eval`), and a name that resolves at load time but is undefined at call time. It proves
"this identifier is declared somewhere reachable", which is exactly the class of bug an extraction
introduces.
"""
import json
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIENT = os.path.join(ROOT, "static", "js", "client")

# Modules split out of app.js.
MODULES = ["git.js"]

# Where acorn might live. The repo does not depend on it; these are the places a machine that has
# built anything JS tends to have one.
_ACORN_CANDIDATES = [
    os.path.join(ROOT, "node_modules", "acorn"),
    os.path.join(ROOT, "desktop", "node_modules", "acorn"),
    os.path.join(ROOT, "mobile", "node_modules", "acorn"),
    "/opt/flood/node_modules/acorn",
    "/usr/lib/node_modules/acorn",
]


def _acorn():
    for p in _ACORN_CANDIDATES:
        if os.path.isdir(p):
            return p
    return None


ACORN = _acorn()
pytestmark = pytest.mark.skipif(
    shutil.which("node") is None or ACORN is None,
    reason="needs node + acorn (this repo does not depend on a JS parser)")


_SCRIPT = r"""
/* Paths come through the ENVIRONMENT, not argv. `node -e code -- a b` consumes the `--`, so the
 * indices shift by one and `require()` was handed the TARGET FILE — which node then EXECUTED,
 * failing with "window is not defined" from the very file it was supposed to be parsing. */
const fs=require('fs'), acorn=require(process.env.PC_ACORN);
const ast=acorn.parse(fs.readFileSync(process.env.PC_TARGET,'utf8'),{ecmaVersion:'latest',locations:true});
function walk(n,fn,p){ if(!n||typeof n.type!=='string')return; fn(n,p);
  for(const k of Object.keys(n)){ if(['loc','range','start','end'].includes(k))continue;
    const v=n[k]; if(Array.isArray(v))v.forEach(c=>c&&typeof c.type==='string'&&walk(c,fn,n));
    else if(v&&typeof v.type==='string')walk(v,fn,n); } }
function names(pat,out){ if(!pat)return out;
  switch(pat.type){ case 'Identifier': out.add(pat.name);break;
    case 'ObjectPattern': pat.properties.forEach(p=>names(p.value||p.argument,out));break;
    case 'ArrayPattern': pat.elements.forEach(e=>names(e,out));break;
    case 'AssignmentPattern': names(pat.left,out);break;
    case 'RestElement': names(pat.argument,out);break; } return out; }
const declared=new Set(), used=new Map();
walk(ast,(n,p)=>{
  if(n.type==='FunctionDeclaration'&&n.id) declared.add(n.id.name);
  if(n.type==='ClassDeclaration'&&n.id) declared.add(n.id.name);
  if(n.type==='VariableDeclarator') names(n.id,declared);
  if(n.type==='FunctionDeclaration'||n.type==='FunctionExpression'||n.type==='ArrowFunctionExpression')
    n.params.forEach(x=>names(x,declared));
  if(n.type==='CatchClause'&&n.param) names(n.param,declared);
  if(n.type!=='Identifier'||!p) return;
  if(p.type==='MemberExpression'&&p.property===n&&!p.computed) return;   // obj.name
  if(p.type==='Property'&&p.key===n&&!p.computed) return;                // {name: …}
  if(p.type==='VariableDeclarator'&&p.id===n) return;
  if((p.type==='FunctionDeclaration'||p.type==='FunctionExpression'||
      p.type==='ArrowFunctionExpression'||p.type==='ClassDeclaration')&&
     (p.id===n||(p.params||[]).includes(n))) return;
  if(p.type==='LabeledStatement'||p.type==='BreakStatement'||p.type==='ContinueStatement') return;
  if(!used.has(n.name)) used.set(n.name,n.loc.start.line);
});
const out=[...used].filter(([n])=>!declared.has(n)).map(([n,l])=>({name:n,line:l}));
console.log(JSON.stringify(out));
"""

# Anything the browser (or this app's own shell) provides.
GLOBALS = {
    "undefined", "arguments", "globalThis", "Object", "Array", "String", "Number", "Boolean",
    "Symbol", "BigInt", "Math", "JSON", "Date", "RegExp", "Error", "TypeError", "RangeError",
    "SyntaxError", "Promise", "Map", "Set", "WeakMap", "WeakSet", "Proxy", "Reflect", "Intl",
    "parseInt", "parseFloat", "isNaN", "isFinite", "Infinity", "NaN", "encodeURIComponent",
    "decodeURIComponent", "encodeURI", "decodeURI", "escape", "unescape", "Uint8Array", "Int8Array",
    "Uint16Array", "Uint32Array", "Float32Array", "Float64Array", "ArrayBuffer", "DataView",
    "TextEncoder", "TextDecoder", "structuredClone", "queueMicrotask", "window", "document",
    "location", "history", "navigator", "screen", "localStorage", "sessionStorage", "indexedDB",
    "caches", "crypto", "fetch", "Request", "Response", "Headers", "URL", "URLSearchParams", "Blob",
    "File", "FileReader", "FormData", "AbortController", "WebSocket", "EventSource", "Image",
    "Audio", "Worker", "MutationObserver", "IntersectionObserver", "ResizeObserver", "CustomEvent",
    "Event", "KeyboardEvent", "MouseEvent", "PointerEvent", "DOMParser", "XMLHttpRequest",
    "getComputedStyle", "matchMedia", "setTimeout", "clearTimeout", "setInterval", "clearInterval",
    "requestAnimationFrame", "cancelAnimationFrame", "console", "alert", "atob", "btoa", "Element",
    "HTMLElement", "Node", "NodeList", "Notification", "MediaRecorder", "MediaStream",
    "MediaMetadata", "RTCPeerConnection", "DOMException", "ClipboardItem", "createImageBitmap",
    "CSS", "innerWidth", "innerHeight", "addEventListener", "removeEventListener", "performance",
    "self", "Capacitor", "BarcodeDetector", "jsQR", "katex",
    # this app's other modules, reached as globals by design
    "PC", "Relay", "Store", "NostrTools", "PCQR", "PCZip", "PCSync", "PCNotes", "PCJoplin",
    "PCVault", "PCGit", "PCGitFactory", "PCI18n", "PCI18N", "PCSprite", "PCOutbox", "PCNegentropy",
    "PCOS", "PCTerm", "PCCalendar", "PCContacts", "PCWebxdc", "PCWebSearch", "PCPlaylists",
    "ClientSettings", "Session", "Outbox", "ICO",
}


def _unresolved(path):
    env = dict(os.environ, PC_ACORN=ACORN, PC_TARGET=path)
    r = subprocess.run(["node", "-e", _SCRIPT], capture_output=True, text=True,
                       timeout=300, env=env)
    if r.returncode != 0:
        raise AssertionError(f"could not parse {path}:\n{r.stderr[:800]}")
    return [x for x in json.loads(r.stdout) if x["name"] not in GLOBALS]


@pytest.mark.parametrize("mod", MODULES)
def test_the_module_resolves_every_name_it_uses(mod):
    path = os.path.join(CLIENT, mod)
    if not os.path.exists(path):
        pytest.skip(f"{mod} has not been split out yet")
    bad = _unresolved(path)
    assert not bad, (
        f"{mod} uses names that are neither declared in it nor available globally — each throws "
        f"ReferenceError the moment that code runs:\n  "
        + "\n  ".join(f"{mod}:{x['line']}  {x['name']}" for x in bad))


def test_the_check_can_fail(tmp_path):
    """A resolver that resolves everything is indistinguishable from a clean file."""
    good = tmp_path / "good.js"
    good.write_text("(function(){ let a=1, b=2; function f(c){ return a+b+c; } window.X={f}; })();")
    bad = tmp_path / "bad.js"
    bad.write_text("(function(){ function f(c){ return notDeclaredAnywhere(c); } window.X={f}; })();")
    assert not _unresolved(str(good)), "a clean file was reported as broken"
    assert [x["name"] for x in _unresolved(str(bad))] == ["notDeclaredAnywhere"]


def test_a_multi_declarator_let_counts_as_declared(tmp_path):
    """The exact miss that made the regex version fail a correct extraction.

    `let _kbSel=-1, _gitKbBound=false;` declares TWO names, and a pattern that stops at the first
    reports the second as unresolved — a false alarm on shipped, working code.
    """
    f = tmp_path / "multi.js"
    f.write_text("(function(){ let a=-1, b=false; function g(){ return a||b; } window.Y={g}; })();")
    assert not _unresolved(str(f))


def test_names_inside_template_literals_are_seen(tmp_path):
    """Where a UI module keeps its calls, and where a `//` in a URL used to hide them."""
    f = tmp_path / "tpl.js"
    f.write_text("(function(){ const u=`<a href=\"https://x/y\">${missingHelper(t)}</a>`; })();")
    assert "missingHelper" in [x["name"] for x in _unresolved(str(f))]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
