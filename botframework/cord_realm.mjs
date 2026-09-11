/* ONE DEFINITION OF THE REALM THE CORD MODULES RUN IN.
 *
 * `cord-protocol.js` and `cord-reader.js` are browser modules. Running them under node means
 * building a vm context that looks enough like a browser — and "enough" has been wrong four times,
 * each time silently:
 *
 *   Object / Array   injected from the host, so every `instanceof` inside the module failed
 *   TextEncoder      ditto, reported as "expected Uint8Array, got object"
 *   crypto           ditto
 *   performance      absent, so only the READ op failed, which read as an unreadable message
 *   URL              absent, and the invite parser does `try { new URL(t) } catch { return }`
 *                    — so the throw was swallowed and EVERY REAL INVITE LINK came back "invalid
 *                    CORD invite", while the bare naddr1…#secret form worked perfectly
 *
 * The last one is why this file exists rather than a second copy of the setup. The bridge had a
 * realm and the test fixture had its own, and a fixture with the same gap as the code cannot see
 * the gap: it agrees with the bug. Both import this now, so an incomplete realm is incomplete in
 * the test too, and the test is the thing that says so.
 *
 * THE RULE FOR ADDING ANYTHING HERE: define it INSIDE the realm in terms of a `__`-prefixed host
 * helper that takes and returns only JSON-shaped values. Never hand a host object across — a value
 * built in the host realm fails every prototype and `instanceof` check on the other side.
 */
import fs from 'node:fs';
import vm from 'node:vm';
import { webcrypto } from 'node:crypto';

export function makeRealm() {
  const ctx = vm.createContext({
    __enc: (s) => Array.from(new TextEncoder().encode(String(s))),
    __dec: (a) => new TextDecoder().decode(Uint8Array.from(a)),
    __rand: (n) => Array.from(webcrypto.getRandomValues(new Uint8Array(n))),
    __uuid: () => webcrypto.randomUUID(),
    __now: () => Date.now(),
    __btoa: (s) => Buffer.from(s, 'binary').toString('base64'),
    __atob: (s) => Buffer.from(s, 'base64').toString('binary'),
    // Parsed on the host, returned as a PLAIN OBJECT of strings. null means "not a URL", so the
    // realm-side shim can throw its own TypeError — which is what the parser's catch expects.
    __url: (s, base) => {
      try {
        const u = base ? new URL(String(s), String(base)) : new URL(String(s));
        return { href: u.href, protocol: u.protocol, origin: u.origin, host: u.host,
                 hostname: u.hostname, port: u.port, pathname: u.pathname,
                 search: u.search, hash: u.hash };
      } catch (_) { return null; }
    },
    setTimeout, clearTimeout,
  });
  vm.runInContext(`
    globalThis.TextEncoder = class TextEncoder { encode(s){ return Uint8Array.from(__enc(s)); } };
    globalThis.TextDecoder = class TextDecoder { decode(b){ return __dec(Array.from(b ?? [])); } };
    globalThis.crypto = { getRandomValues(a){ a.set(__rand(a.length)); return a; },
                          randomUUID: () => __uuid() };
    globalThis.btoa = (s) => __btoa(String(s));
    globalThis.atob = (s) => __atob(String(s));
    globalThis.window = globalThis; globalThis.self = globalThis;
    globalThis.document = { createElement: () => ({}), querySelector: () => null };
    globalThis.location = { origin: 'https://poster.place' };
    globalThis.performance = { now: () => __now() };
    globalThis.URL = class URL {
      constructor(input, base){
        const u = __url(input, base);
        if (!u) throw new TypeError('Invalid URL: ' + String(input));
        Object.assign(this, u);
      }
      toString(){ return this.href; }
    };
    globalThis.console = { log(){}, warn(){}, error(){} };
  `, ctx);
  return ctx;
}

export const loadInto = (ctx, file) =>
  vm.runInContext(fs.readFileSync(file, 'utf8'), ctx, { filename: String(file) });

/** Copy a value ACROSS the boundary by rebuilding it inside the target realm. */
export const into = (ctx) => (v) => vm.runInContext('(' + JSON.stringify(v) + ')', ctx);
