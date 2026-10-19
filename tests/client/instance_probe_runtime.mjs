/* Run the SHIPPED _probeInstance / _confirmInstanceNotPc (app.js) against scripted answers from /client/config. */
import fs from 'node:fs';
import vm from 'node:vm';
const src = fs.readFileSync(new URL('../../static/js/client/app.js', import.meta.url), 'utf8');
function fnSrc(name){
  const i = src.indexOf('function ' + name + '(');
  if(i < 0) throw new Error('missing ' + name);
  const start = src.lastIndexOf(src.slice(i - 6, i) === 'async ' ? 'async ' : 'function', i);
  let depth = 0, j = src.indexOf('{', i);
  for(; j < src.length; j++){ const ch = src[j]; if(ch === '{') depth++; else if(ch === '}' && --depth === 0) break; }
  return src.slice(src.slice(i - 6, i) === 'async ' ? i - 6 : i, j + 1);
}
const plan = JSON.parse(process.argv[2]);
let n = 0;
const answers = plan.answers;   // [{status, body}] or 'throw'
const ctx = {
  JSON, Promise, String, Array, Object, AbortController,
  setTimeout: (fn) => { queueMicrotask(fn); return 1; }, clearTimeout: () => {},
  fetch: async () => {
    const a = answers[Math.min(n++, answers.length - 1)];
    if(a === 'throw') throw new TypeError('Failed to fetch');
    return { ok: a.status >= 200 && a.status < 300, status: a.status,
             json: async () => JSON.parse(a.body) };
  },
  _instanceBase: () => 'https://poster.place',
};
vm.createContext(ctx);
vm.runInContext([fnSrc('_isPcConfig'), fnSrc('_probeInstance'), fnSrc('_confirmInstanceNotPc')].join('\n') +
  '\nglobalThis.__p=_probeInstance; globalThis.__c=_confirmInstanceNotPc;', ctx);
const out = plan.confirm ? await ctx.__c('https://poster.place', 0) : await ctx.__p('https://poster.place');
process.stdout.write(JSON.stringify({ out, asked: n }));
