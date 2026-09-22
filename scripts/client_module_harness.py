"""Build a module split out of app.js inside a CHECK's own page, from that page's own stubs.

The browser checks used to lift a screen out of app.js by slicing its text between two function
names and pasting it into a harness page whose `const enc = …`, `const toast = …` stand in for the
rest of the client. Screens that moved into their own files (mail.js, …) are factories instead —
`window.PCXFactory = function(dep){ const S = dep.state; const { enc, toast, … } = dep; … }` — so
the harness builds the factory the way app.js does: it hands in an object holding every name the
module takes, each read from the page's global scope (where the harness stubs live), and a
`state` object of getters for the live bindings the module reads as `S.<name>`.

The dependency list is read from the SHIPPED module text, so the harness cannot drift from it: a
name the module starts taking appears here automatically, as `undefined` if the page does not stub
it — exactly what a lifted slice saw for an unstubbed name.
"""
from __future__ import annotations

import re


def module_deps(src: str) -> tuple[list[str], list[str], list[str]]:
    """(plain deps, live-state names read, live-state names written) of a split module."""
    m = re.search(r"\n  const \{\n(.*?)\n  \} = dep;", src, re.S)
    if not m:
        raise ValueError("no `const { … } = dep;` block — not a split module")
    plain = [n.strip() for n in m.group(1).replace("\n", " ").split(",") if n.strip()]
    read = sorted(set(re.findall(r"(?<![\w$.])S\.([A-Za-z_$][\w$]*)", src)))
    written = sorted(set(re.findall(r"(?<![\w$.])S\.([A-Za-z_$][\w$]*)\s*(?:=(?!=)|\+\+|--|[-+*/|&]=)", src)))
    return plain, read, written


def factory_harness_js(src: str, factory: str, expose: list[str] | None = None) -> str:
    """JS that builds `factory` from the page's globals into `window.__mod`, and re-publishes the
    names in `expose` (entry points or `Mail`-style values) as page globals."""
    plain, read, written = module_deps(src)
    g = lambda n: f"(typeof {n} !== 'undefined' ? {n} : undefined)"
    state = [f"get {n}(){{ return {g(n)}; }}" for n in read]
    state += [f"set {n}(v){{ {n} = v; }}" for n in written]
    out = ["window.__mod = window." + factory + "({",
           "  state: { " + ", ".join(state) + " },"]
    out += [f"  {json_key(n)}: {g(n)}," for n in plain]
    out.append("});")
    for n in expose or []:
        out.append(f"var {n} = window.__mod.{n};")
    return "\n".join(out) + "\n"


def json_key(n: str) -> str:
    return n if re.match(r"^[A-Za-z_$][\w$]*$", n) else repr(n)
