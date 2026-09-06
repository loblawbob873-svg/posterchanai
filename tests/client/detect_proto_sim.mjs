/* The SHIPPED detectProto against a stubbed fetch. The question this answers is not "is the code
   there" but "does it still make the request", which is the whole complaint. */
import fs from 'node:fs';
const src = fs.readFileSync(new URL('../../static/js/client/app.js', import.meta.url), 'utf8');
const a = src.indexOf('  async function detectProto(url){');
const b = src.indexOf('\n  }', a) + 4;
if (a < 0) throw new Error('detectProto moved');
const shipped = src.slice(a, b);

let asked = [];
globalThis.fetch = async (u) => { asked.push(String(u)); return { ok: false, json: async () => null }; };
globalThis.CFG = { blossom_url: 'https://media.poster.place' };
globalThis._serverOrigin = () => 'https://poster.place';
globalThis._blossomBuiltin = () => ({ url: 'https://poster.place/blossom', proto: 'blossom' });

const detectProto = new Function(`${shipped}; return detectProto;`)();
const out = {};

asked = []; out.ownMediaHost = await detectProto('https://media.poster.place');
out.ownMediaAsked = asked.length;

asked = []; out.ownMediaTrailingSlash = await detectProto('https://media.poster.place/');
out.ownMediaSlashAsked = asked.length;

asked = []; out.ownMediaSubPath = await detectProto('https://media.poster.place/blossom');
out.ownMediaSubPathAsked = asked.length;

asked = []; out.builtin = await detectProto('https://poster.place/blossom');
out.builtinAsked = asked.length;

// Every OTHER host is still probed — that is what the function is for.
asked = []; out.stranger = await detectProto('https://someone.else');
out.strangerAsked = asked.length;
out.strangerUrl = asked[0] || '';

// nostr.build keeps its hostname answer when the probe says nothing.
asked = []; out.nostrBuild = await detectProto('https://nostr.build');

// No config (a bundle with no instance) must not throw.
globalThis.CFG = undefined;
asked = []; out.noCfg = await detectProto('https://someone.else');
out.noCfgAsked = asked.length;

process.stdout.write(JSON.stringify(out));
