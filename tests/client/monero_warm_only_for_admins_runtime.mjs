/* THE NODE'S WALLET IS WARMED ONLY FOR SOMEBODY WHO CAN OPEN IT (2026-10-22). The live sign-in check found every
 * ordinary member paying three 403s on every page load (/api/wallet/xmr/status, balance, history) for a wallet that
 * is admin-only. Loads the SHIPPED monero-wallet.js in a vm and records what its warm-up asks. */
import fs from 'node:fs';
import vm from 'node:vm';
const src = fs.readFileSync(new URL('../../static/js/client/monero-wallet.js', import.meta.url), 'utf8');

async function warmUp(isAdmin) {
  const asked = [];
  const PC = { viewer: () => ({ pubkey: 'a'.repeat(64) }), ensureAiSession: async () => ({}), apiBase: () => '',
               authFetch: async (path) => { asked.push(String(path)); return { ok: true, status: 200, json: async () => ({}) }; } };
  if (isAdmin !== undefined) PC.isAdmin = () => isAdmin;
  const fetchFn = async (path) => { asked.push(String(path)); return { ok: true, status: 200, json: async () => ({}) }; };
  const ctx = { console, setTimeout, clearTimeout, AbortController, URL, document: { addEventListener() {}, querySelector: () => null },
                localStorage: { getItem: () => null, setItem() {} }, fetch: fetchFn, navigator: {}, location: { href: 'https://poster.place/' } };
  ctx.window = ctx; ctx.__PC = PC; ctx.globalThis = ctx;
  vm.runInNewContext(src, ctx);
  await new Promise(r => setTimeout(r, 150));
  return asked.map(u => u.replace(/^https?:\/\/[^/]+/, '').split('?')[0]);
}
const fail = (m, a) => { console.error('FAIL: ' + m + '\n' + JSON.stringify(a)); process.exit(1); };
const node = u => u.startsWith('/api/wallet/xmr/') && !u.startsWith('/api/wallet/xmr/me');
const member = await warmUp(false);
if (member.some(node)) fail('a non-admin member was probed for the admin-only node wallet', member);
if (!member.some(u => u.startsWith('/api/wallet/xmr/me'))) fail('a member\'s OWN wallet was not warmed', member);
const admin = await warmUp(true);
if (!admin.some(u => u === '/api/wallet/xmr/status')) fail('the operator\'s node wallet was not warmed', admin);
const old = await warmUp(undefined);
if (!old.some(u => u === '/api/wallet/xmr/status')) fail('an embedder without isAdmin lost the warm-up', old);
console.log('monero warm only for admins: ok');
