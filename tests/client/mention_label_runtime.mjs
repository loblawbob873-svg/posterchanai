/* A mention of an account whose profile is not known yet shows WHICH account (a short npub), never
 * the word "profile" -- "@bulletbill22@poster.place muted @profile" in the block bot's posts.
 * Runs the SHIPPED linkify() with the client's own bundled nostr-tools. */
import vm from 'node:vm';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { clientSource } from './client_source.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.join(here, '..', '..');
const src = clientSource();
const start = src.indexOf('function linkify(txt, ev){');
let depth = 0, end = -1;
for (let i = src.indexOf('{', start); i < src.length; i++) {
  if (src[i] === '{') depth++; else if (src[i] === '}' && --depth === 0) { end = i + 1; break; }
}
const win = {};
vm.runInNewContext(fs.readFileSync(path.join(root, 'static/vendor/nostr/nostr.bundle.js'), 'utf8'),
                   Object.assign(win, { window: win, self: win, globalThis: win, crypto: globalThis.crypto,
                                        TextEncoder, TextDecoder, console }));
const NT = () => win.NostrTools;
const known = {};
const ctx = {
  NT, console,
  enc: s => String(s).replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c])),
  Store: { profile: pk => known[pk] || null, get: () => null },
  needProfile: () => {}, emojiName: (pk, nm) => nm, niceNip05: n => n || '',
  quotedDiv: () => '', isMediaUrl: () => false,
};
vm.runInNewContext(src.slice(start, end) + '\nthis.linkify = linkify;', ctx, { filename: 'app-linkify.js' });
const pk = '5510cfa39c229b6553a70bf023a1b3fbfbf80b2a71d43fe6f4a678038058b127';
const npub = NT().nip19.npubEncode(pk);
const failures = [];
let html = '';
try { html = ctx.linkify(`@bulletbill22@poster.place muted nostr:${npub} (on Nostr)`); }
catch (e) { failures.push('linkify threw: ' + e.message); }
if (/@profile</.test(html)) failures.push('an unknown account still renders as "@profile": ' + html);
if (!html.includes(`@${npub.slice(0, 10)}…${npub.slice(-5)}<`)) failures.push('no short npub label: ' + html);
if (!html.includes(`data-mpk="${pk}"`)) failures.push('the label must still be replaced when the profile lands');
known[pk] = { name: 'Bill' };
const named = ctx.linkify(`hi nostr:${npub}`);
if (!named.includes('>@Bill<') || named.includes('data-mpk')) failures.push('a known name must be used: ' + named);
if (failures.length) { console.error(failures.map(f => '✗ ' + f).join('\n')); process.exit(1); }
console.log('mention label: short npub until the name is known');
