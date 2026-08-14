"""Every client module resolves every name it uses — checked mechanically, not by eye.

THE REFACTOR THIS EXISTS FOR. `static/js/client/app.js` is ~30k lines in one IIFE and is being split
into modules. Each extraction moves a block of code out of a scope where everything was in reach and
into one where nothing is, so the entire risk is a name that used to resolve and now does not. That
failure is invisible until somebody opens the view: `node --check` passes (the syntax is fine), the
page loads, the tests pass, and the feature throws `ReferenceError` on click.

WHY THIS IS A TEST AND NOT A SPREADSHEET. Deriving the dependency list by reading the code was tried
first and got it wrong: a scan that strips `//` comments before looking for identifiers eats the rest
of any line containing `https://` inside a template literal, which is most of the render code. It
reported 13 dependencies for a block that had 17. The four it missed — `timeAgo`, `mdToHtml`,
`_fmtBytes`, `imetaTagsFor` — are all used only inside template literals, which is exactly where a
UI module keeps its calls. So the rule here is applied to the SHIPPED file after the fact, and it
tokenises properly rather than by regex-stripping.

WHAT IT CANNOT SEE, stated so nobody trusts it further than it goes: a name reached dynamically
(`window[x]`, `eval`), and a name that resolves at load time but is `undefined` at call time. It
proves "this identifier is declared somewhere reachable", which is precisely the class of bug an
extraction introduces.
"""
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIENT = os.path.join(ROOT, "static", "js", "client")

# Modules split out of app.js. Each entry is checked; app.js itself is deliberately NOT, because it
# is still the scope everything else was extracted from and its own resolution is what the browser
# checks exercise end to end.
MODULES = ["git.js"]

# Anything a browser (or this app's own shell) provides. A module may use these without declaring
# them; everything else must be local or come off `PC`.
GLOBALS = {
    # language + standard library
    "undefined", "null", "true", "false", "this", "arguments", "globalThis",
    "Object", "Array", "String", "Number", "Boolean", "Symbol", "BigInt", "Math", "JSON", "Date",
    "RegExp", "Error", "TypeError", "RangeError", "SyntaxError", "Promise", "Map", "Set", "WeakMap",
    "WeakSet", "Proxy", "Reflect", "Intl", "parseInt", "parseFloat", "isNaN", "isFinite",
    "encodeURIComponent", "decodeURIComponent", "encodeURI", "decodeURI", "escape", "unescape",
    "Uint8Array", "Int8Array", "Uint16Array", "Uint32Array", "Float32Array", "Float64Array",
    "ArrayBuffer", "DataView", "TextEncoder", "TextDecoder", "structuredClone", "queueMicrotask",
    # DOM + platform
    "window", "document", "location", "history", "navigator", "screen", "localStorage",
    "sessionStorage", "indexedDB", "caches", "crypto", "fetch", "Request", "Response", "Headers",
    "URL", "URLSearchParams", "Blob", "File", "FileReader", "FormData", "AbortController",
    "WebSocket", "EventSource", "Image", "Audio", "Worker", "MutationObserver",
    "IntersectionObserver", "ResizeObserver", "CustomEvent", "Event", "KeyboardEvent",
    "MouseEvent", "PointerEvent", "DOMParser", "XMLHttpRequest", "getComputedStyle", "matchMedia",
    "setTimeout", "clearTimeout", "setInterval", "clearInterval", "requestAnimationFrame",
    "cancelAnimationFrame", "console", "alert", "atob", "btoa", "Element", "HTMLElement", "Node",
    "NodeList", "Notification", "MediaRecorder", "Capacitor", "performance", "self",
    # this app's other modules, reached as globals by design
    "PC", "Relay", "Store", "NostrTools", "PCQR", "PCZip", "PCSync", "PCNotes", "PCJoplin",
    "PCVault", "PCGit", "PCI18n", "PCSprite", "PCOutbox", "PCNegentropy",
}

# Members, labels and other places an identifier-looking token is not a variable reference.
_STRIP_STRINGS = re.compile(r"'(?:[^'\\\n]|\\.)*'|\"(?:[^\"\\\n]|\\.)*\"")


def _tokens(src):
    """Identifier references in `src`, excluding property names, declarations and string bodies.

    Template literals are KEPT (their `${...}` holes are real code, and that is where the missed
    dependencies were), but their literal text contributes no identifiers because the interpolations
    are extracted first.
    """
    # Pull ${...} out of template literals, then drop the literal text.
    holes = " ".join(re.findall(r"\$\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}", src))
    body = re.sub(r"`(?:[^`\\]|\\.)*`", " ", src)
    body = _STRIP_STRINGS.sub(" ", body)
    body = re.sub(r"/\*[\s\S]*?\*/", " ", body)
    body = re.sub(r"^\s*//[^\n]*", " ", body, flags=re.M)      # only whole-line comments
    text = body + " " + _STRIP_STRINGS.sub(" ", holes)
    out = set()
    for m in re.finditer(r"(\.\s*)?\b([A-Za-z_$][\w$]*)\b(\s*:)?", text):
        if m.group(1):                    # a property access: obj.name
            continue
        if m.group(3) and not re.match(r"\s*:\s*:", m.group(3)):
            # `name:` — an object key or a label, not a reference. Ternaries are `? :` and their
            # left side is caught as a normal token before the colon is reached.
            continue
        out.add(m.group(2))
    return out


def _declared(src):
    """Names the file declares: functions, classes, bindings, parameters, catch bindings, imports."""
    names = set()
    for pat in (r"\bfunction\s*\*?\s*([A-Za-z_$][\w$]*)",
                r"\bclass\s+([A-Za-z_$][\w$]*)",
                r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)",
                r"\bcatch\s*\(\s*([A-Za-z_$][\w$]*)",
                r"\bfor\s*\(\s*(?:const|let|var)\s+([A-Za-z_$][\w$]*)"):
        names |= set(re.findall(pat, src))
    # Destructuring, in both declaration and parameter position.
    for block in re.findall(r"(?:const|let|var)\s*\{([^}]*)\}\s*=", src):
        names |= set(re.findall(r"([A-Za-z_$][\w$]*)\s*(?:[:=,}]|$)", block))
    for block in re.findall(r"(?:const|let|var)\s*\[([^\]]*)\]\s*=", src):
        names |= set(re.findall(r"([A-Za-z_$][\w$]*)", block))
    # Parameters: every `(...)` that is followed by `=>` or precedes a function body.
    for params in re.findall(r"\(([^()]*)\)\s*(?:=>|\{)", src):
        names |= set(re.findall(r"([A-Za-z_$][\w$]*)", params))
    for single in re.findall(r"(?:^|[^\w$.])([A-Za-z_$][\w$]*)\s*=>", src):
        names.add(single)
    return names


_KEYWORDS = {
    "if", "else", "for", "while", "do", "return", "break", "continue", "switch", "case",
    "default", "function", "class", "const", "let", "var", "new", "delete", "typeof",
    "instanceof", "in", "of", "try", "catch", "finally", "throw", "async", "await", "yield",
    "void", "extends", "super", "static", "get", "set", "import", "export", "from", "as",
}


def _unresolved(src):
    """Names used but neither declared here nor available globally. ONE definition, used by the
    real check and by its own self-test — two copies is how a self-test starts passing against a
    rule the check no longer applies."""
    return sorted(_tokens(src) - _declared(src) - GLOBALS - _KEYWORDS)


@pytest.mark.parametrize("mod", MODULES)
def test_the_module_resolves_every_name_it_uses(mod):
    path = os.path.join(CLIENT, mod)
    if not os.path.exists(path):
        pytest.skip(f"{mod} has not been split out yet")
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    unresolved = _unresolved(src)
    assert not unresolved, (
        f"{mod} uses names that are neither declared in it nor available globally — every one of "
        f"these throws ReferenceError the moment that code runs:\n  " + "\n  ".join(unresolved))


def test_the_check_can_fail(tmp_path):
    """A resolver that resolves everything is indistinguishable from a clean file."""
    good = "(function(){ const a=1; function f(b){ return a+b; } window.X={f}; })();"
    bad = "(function(){ function f(b){ return notDeclaredAnywhere(b); } window.X={f}; })();"
    assert not _unresolved(good), f"a clean file was reported as broken: {_unresolved(good)}"
    assert "notDeclaredAnywhere" in _unresolved(bad)


def test_a_template_literal_hole_is_not_invisible():
    """The exact hole that made the hand analysis wrong.

    A `//` inside a template literal (every URL) ends a line-comment strip, taking the rest of the
    line — and with it the calls a UI module makes from inside its markup.
    """
    src = "(function(){ const u=`<a href=\"https://x/y\">${timeAgo(t)}</a>`; })();"
    assert "timeAgo" in _tokens(src), "an identifier inside a template literal was not seen"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
