#!/usr/bin/env node
/* Move a contiguous block of app.js's top-level code into its own lazily-built module — mechanically.
 *
 *   node scripts/split_client_module.mjs --from "<first line text>" --to "<line text after the block>" \
 *        --file calls.js --factory PCCallsFactory --stem calls [--write]
 *
 * THE ONLY RISK OF AN EXTRACTION IS A NAME THAT USED TO RESOLVE AND NO LONGER DOES (see
 * tests/test_client_module_deps.py), so nothing here is done by eye. acorn parses app.js and every
 * identifier is RESOLVED through real scopes (function, block, catch, class, named function
 * expressions, parameters, hoisted var/function), then:
 *
 *   * a reference inside the block to one of app.js's own bindings declared OUTSIDE it becomes
 *       - `S.<name>` when that binding is ever ASSIGNED anywhere (a live `let`) — read through a getter,
 *         and written through a setter when the block writes it;
 *       - a plain dependency otherwise (functions, consts, never-reassigned lets), destructured from
 *         `dep` exactly like every existing split module;
 *   * a reference OUTSIDE the block to a FUNCTION declared inside it gets a one-line entry point in
 *     app.js — `function f(){ return _lzRun(_xMod, _xLoad, 'f', arguments); }` — the pattern
 *     streams.js/cards.js/… already use, so nothing outside changes;
 *   * a reference outside to a NON-function binding declared inside it is REFUSED: a variable cannot
 *     be forwarded by a function stub, and moving it silently would be the bug this tool exists to
 *     prevent. Move that declaration out of the range, or leave the block where it is.
 *   * the live-state object is `S` — or `_S` when the block itself declares or uses an `S` of its own
 *     (a local `const S=_capPlugin(…)` would otherwise SHADOW it, and any live read rewritten to
 *     `S.<name>` inside that scope would silently read the wrong object). `--state NAME` forces a name;
 *     tests/test_client_module_deps.py finds the name from `const <NAME> = dep.state` either way.
 *   * `--proxy A,B` is the one exception, for a `const` OBJECT (MusicPlayer, FilesIdx, MusicOffline):
 *     app.js keeps the name, bound to `_lzProxy(_xMod, 'A')`, which forwards every read, write and
 *     method call to the real object in the module — only for a module that SHIPS WITH THE PAGE (its
 *     own <script> tag), because a property read cannot wait for a load.
 *
 * Nothing is written without --write. The block's text is copied BYTE-FOR-BYTE apart from the
 * identifier rewrites, so a reviewer can diff the module against the removed lines.
 */
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';

const args = Object.fromEntries(process.argv.slice(2).reduce((out, a, i, all) => {
  if (a.startsWith('--')) out.push([a.slice(2), all[i + 1] && !all[i + 1].startsWith('--') ? all[i + 1] : true]);
  return out;
}, []));
const ROOT = path.resolve(path.dirname(new URL(import.meta.url).pathname), '..');
const APP = path.join(ROOT, 'static/js/client/app.js');
const need = ['from', 'to', 'file', 'factory', 'stem'];
for (const k of need) if (!args[k]) { console.error('missing --' + k); process.exit(2); }

function findAcorn() {
  // acorn 8+ only: 7.x (still in some home directories) never finishes parsing app.js's syntax.
  const tries = [process.env.PC_ACORN, path.join(ROOT, 'node_modules/acorn'), '/opt/flood/node_modules/acorn',
                 path.join(process.env.HOME || '', 'node_modules/acorn'), '/usr/lib/node_modules/acorn'].filter(Boolean);
  for (const t of tries) {
    try { if (fs.existsSync(t) && Number(JSON.parse(fs.readFileSync(path.join(t, 'package.json'), 'utf8')).version.split('.')[0]) >= 8)
      return createRequire(import.meta.url)(t); } catch (_) { }
  }
  console.error('acorn not found (set PC_ACORN)'); process.exit(2);
}
const acorn = findAcorn();
const src = fs.readFileSync(APP, 'utf8');
const T0=Date.now(); const ast = acorn.parse(src, { ecmaVersion: 'latest', sourceType: 'script', locations: true, ranges: true, allowHashBang: true });

if(process.env.PC_SPLIT_TIMING) console.error('parse', Date.now()-T0);
// ---- the IIFE whose body is app.js ---------------------------------------------------------------
let iife = null;
for (const st of ast.body) {
  const e = st.type === 'ExpressionStatement' ? st.expression : null;
  const call = e && (e.type === 'CallExpression' ? e : e.type === 'UnaryExpression' ? e.argument : null);
  const fn = call && call.callee;
  if (fn && (fn.type === 'FunctionExpression' || fn.type === 'ArrowFunctionExpression') && fn.body.type === 'BlockStatement'
      && fn.body.body.length > 100) { iife = fn; break; }
}
if (!iife) { console.error('could not find the app.js IIFE'); process.exit(1); }
const TOP = iife.body.body;

// ---- the block ------------------------------------------------------------------------------------
const lineStart = n => { let i = 0; for (let l = 1; l < n; l++) i = src.indexOf('\n', i) + 1; return i; };
function lineOf(text) {
  const i = src.indexOf(text);
  if (i < 0 || src.indexOf(text, i + 1) >= 0) { console.error('marker must occur exactly once: ' + text); process.exit(1); }
  return src.slice(0, i).split('\n').length;
}
const fromLine = lineOf(args.from), toLine = lineOf(args.to);
const block = TOP.filter(s => s.loc.start.line >= fromLine && s.loc.end.line < toLine);
if (!block.length) { console.error('no top-level statements between the markers'); process.exit(1); }
const straddle = TOP.filter(s => (s.loc.start.line < fromLine && s.loc.end.line >= fromLine) || (s.loc.start.line < toLine && s.loc.end.line >= toLine));
if (straddle.length) { console.error('a statement straddles a marker at line ' + straddle[0].loc.start.line); process.exit(1); }
const B0 = lineStart(fromLine), B1 = lineStart(toLine);    // whole lines, comments included
const inBlock = n => n.start >= B0 && n.end <= B1;
const PROXY = new Set(String(args.proxy && args.proxy !== true ? args.proxy : '').split(',').filter(Boolean));
// Every identifier the block spells, declared or referenced, in any scope — to pick a state name that
// nothing in the moved code can shadow.
const blockIds = new Set();
{ const walk = n => { if (!n || typeof n.type !== 'string') return; if (n.type === 'Identifier') blockIds.add(n.name);
    for (const k of Object.keys(n)) { if (k === 'loc' || k === 'range') continue; const v = n[k];
      if (Array.isArray(v)) v.forEach(walk); else if (v && typeof v.type === 'string') walk(v); } };
  block.forEach(walk); }
const SN = args.state && args.state !== true ? String(args.state) : (blockIds.has('S') ? '_S' : 'S');
if (blockIds.has(SN)) { console.error(`the block already uses the name ${SN}; pass --state <an unused name>`); process.exit(1); }

// ---- scopes ---------------------------------------------------------------------------------------
class Scope { constructor(parent, fn) { this.parent = parent; this.fn = fn; this.decls = new Map(); } }
const declOf = new Map();          // Identifier node (reference) -> {scope, name}
const writes = new Set();          // "scopeId:name" written anywhere
let sid = 0; const idOf = new WeakMap(); const sId = s => (idOf.has(s) || idOf.set(s, ++sid), idOf.get(s));
function names(p, out = []) {
  if (!p) return out;
  if (p.type === 'Identifier') out.push(p);
  else if (p.type === 'ObjectPattern') p.properties.forEach(q => names(q.type === 'RestElement' ? q.argument : q.value, out));
  else if (p.type === 'ArrayPattern') p.elements.forEach(e => names(e, out));
  else if (p.type === 'AssignmentPattern') names(p.left, out);
  else if (p.type === 'RestElement') names(p.argument, out);
  return out;
}
const fnScope = s => { while (!s.fn) s = s.parent; return s; };
// Pass 1: declare. Pass 2: resolve. Both walk with the same scope construction.
function hoist(body, scope) {                       // var + function declarations of one function body
  const walk = n => {
    if (!n || typeof n.type !== 'string') return;
    if (n.type === 'VariableDeclaration' && n.kind === 'var') n.declarations.forEach(d => names(d.id).forEach(id => scope.decls.set(id.name, { kind: 'var', node: id })));
    if (n !== body && /Function/.test(n.type)) return;           // not into nested functions
    for (const k of Object.keys(n)) { if (k === 'loc' || k === 'range') continue; const v = n[k];
      if (Array.isArray(v)) v.forEach(walk); else if (v && typeof v.type === 'string') walk(v); }
  };
  walk(body);
}
function lexical(stmts, scope) {                    // let/const/class/function in one block
  for (const st of stmts) {
    if (st.type === 'VariableDeclaration' && st.kind !== 'var') st.declarations.forEach(d => names(d.id).forEach(id => scope.decls.set(id.name, { kind: st.kind, node: id })));
    if (st.type === 'FunctionDeclaration' && st.id) scope.decls.set(st.id.name, { kind: 'function', node: st.id, stmt: st });
    if (st.type === 'ClassDeclaration' && st.id) scope.decls.set(st.id.name, { kind: 'class', node: st.id });
  }
}
function lookup(scope, name) { for (let s = scope; s; s = s.parent) if (s.decls.has(name)) return s; return null; }
const refs = [];                                    // {node, scope}
function visit(n, scope, parent) {
  if (!n || typeof n.type !== 'string') return;
  switch (n.type) {
    case 'FunctionDeclaration': case 'FunctionExpression': case 'ArrowFunctionExpression': {
      let outer = scope;
      if (n.type === 'FunctionExpression' && n.id) { outer = new Scope(scope, false); outer.decls.set(n.id.name, { kind: 'fname', node: n.id }); }
      const fs_ = new Scope(outer, true);
      n.params.forEach(p => names(p).forEach(id => fs_.decls.set(id.name, { kind: 'param', node: id })));
      if (n.body.type === 'BlockStatement') { hoist(n.body, fs_); lexical(n.body.body, fs_); }
      n.params.forEach(p => visitPattern(p, fs_));
      if (n.body.type === 'BlockStatement') n.body.body.forEach(s => visit(s, fs_, n.body)); else visit(n.body, fs_, n);
      return;
    }
    case 'ClassDeclaration': case 'ClassExpression': {
      if (n.superClass) visit(n.superClass, scope, n);
      const cs = new Scope(scope, false); if (n.id && n.type === 'ClassExpression') cs.decls.set(n.id.name, { kind: 'class', node: n.id });
      n.body.body.forEach(m => { if (m.computed) visit(m.key, cs, m); if (m.value) visit(m.value, cs, m); });
      return;
    }
    case 'BlockStatement': case 'StaticBlock': { const bs = new Scope(scope, false); lexical(n.body, bs); n.body.forEach(s => visit(s, bs, n)); return; }
    case 'SwitchStatement': { visit(n.discriminant, scope, n); const bs = new Scope(scope, false); n.cases.forEach(c => lexical(c.consequent, bs)); n.cases.forEach(c => { if (c.test) visit(c.test, bs, c); c.consequent.forEach(s => visit(s, bs, c)); }); return; }
    case 'ForStatement': case 'ForInStatement': case 'ForOfStatement': {
      const bs = new Scope(scope, false); const init = n.init || n.left;
      if (init && init.type === 'VariableDeclaration' && init.kind !== 'var') lexical([init], bs);
      for (const k of ['init', 'left', 'test', 'update', 'right', 'body']) if (n[k]) visit(n[k], bs, n);
      return;
    }
    case 'CatchClause': { const cs = new Scope(scope, false); if (n.param) names(n.param).forEach(id => cs.decls.set(id.name, { kind: 'catch', node: id })); if (n.param) visitPattern(n.param, cs); lexical(n.body.body, cs); n.body.body.forEach(s => visit(s, cs, n.body)); return; }
    case 'VariableDeclaration': n.declarations.forEach(d => { visitPattern(d.id, scope); if (d.init) visit(d.init, scope, d); }); return;
    case 'Identifier': refs.push({ node: n, scope, parent }); return;
    case 'MemberExpression': visit(n.object, scope, n); if (n.computed) visit(n.property, scope, n); return;
    case 'Property': if (n.computed) visit(n.key, scope, n); if (n.shorthand) { refs.push({ node: n.value, scope, parent: n, shorthand: true }); return; } visit(n.value, scope, n); return;
    case 'MethodDefinition': case 'PropertyDefinition': if (n.computed) visit(n.key, scope, n); if (n.value) visit(n.value, scope, n); return;
    case 'LabeledStatement': visit(n.body, scope, n); return;
    case 'BreakStatement': case 'ContinueStatement': return;
    case 'MetaProperty': return;
    case 'AssignmentExpression': markWrite(n.left, scope); visit(n.left.type === 'Identifier' ? n.left : n.left, scope, n); visit(n.right, scope, n); return;
    case 'UpdateExpression': markWrite(n.argument, scope); visit(n.argument, scope, n); return;
    case 'ExportNamedDeclaration': case 'ImportDeclaration': return;
  }
  for (const k of Object.keys(n)) { if (k === 'loc' || k === 'range' || k === 'type') continue; const v = n[k];
    if (Array.isArray(v)) v.forEach(c => c && typeof c.type === 'string' && visit(c, scope, n)); else if (v && typeof v.type === 'string') visit(v, scope, n); }
}
function visitPattern(p, scope) {                  // defaults and computed keys inside a binding pattern
  if (!p) return;
  if (p.type === 'AssignmentPattern') { visitPattern(p.left, scope); visit(p.right, scope, p); }
  else if (p.type === 'ObjectPattern') p.properties.forEach(q => { if (q.type === 'RestElement') visitPattern(q.argument, scope); else { if (q.computed) visit(q.key, scope, q); visitPattern(q.value, scope); } });
  else if (p.type === 'ArrayPattern') p.elements.forEach(e => visitPattern(e, scope));
  else if (p.type === 'RestElement') visitPattern(p.argument, scope);
}
const pendingWrites = [];
function markWrite(target, scope) { names(target.type === 'Identifier' || /Pattern/.test(target.type) ? target : null).forEach(id => pendingWrites.push({ id, scope })); }

if(process.env.PC_SPLIT_TIMING) console.error('block', Date.now()-T0);
const TOPSCOPE = new Scope(null, true);
hoist(iife.body, TOPSCOPE); lexical(TOP, TOPSCOPE);
TOP.forEach(s => visit(s, TOPSCOPE, iife.body));
for (const w of pendingWrites) { const s = lookup(w.scope, w.id.name); if (s) writes.add(sId(s) + ':' + w.id.name); }

if(process.env.PC_SPLIT_TIMING) console.error('scope', Date.now()-T0);
// ---- classify ---------------------------------------------------------------------------------------
const topDeclInBlock = name => { const d = TOPSCOPE.decls.get(name); return d && inBlock(d.node); };
const live = new Set(), plain = new Set(), writtenInBlock = new Set(), exported = new Set(), proxied = new Set(), refused = new Map();
const rewrites = [];                                // {start,end,text}
for (const r of refs) {
  const name = r.node.name, s = lookup(r.scope, name);
  if (s !== TOPSCOPE) continue;                     // local, or a true global
  const decl = TOPSCOPE.decls.get(name);
  const here = inBlock(r.node), declHere = inBlock(decl.node);
  if (r.node === decl.node) continue;               // the declaration itself
  if (here && !declHere) {
    const isLive = (decl.kind === 'let' || decl.kind === 'var') && writes.has(sId(TOPSCOPE) + ':' + name);
    if (isLive) {
      live.add(name);
      rewrites.push({ start: r.node.start, end: r.node.end, text: r.shorthand ? name + ':' + SN + '.' + name : SN + '.' + name });
    } else plain.add(name);
  } else if (!here && declHere) {
    if (decl.kind === 'function') exported.add(name);
    else if (decl.kind === 'const' && PROXY.has(name)) proxied.add(name);
    else (refused.get(name) || refused.set(name, []).get(name)).push(r.node.loc.start.line);
  }
}
for (const w of pendingWrites) if (inBlock(w.id) && lookup(w.scope, w.id.name) === TOPSCOPE && !topDeclInBlock(w.id.name)) writtenInBlock.add(w.id.name);
for (const n of writtenInBlock) if (plain.has(n)) { plain.delete(n); live.add(n); }

const report = { from: fromLine, to: toLine - 1, statements: block.length, lines: src.slice(B0, B1).split('\n').length - 1, state: SN,
  deps: [...plain].sort(), live: [...live].sort(), liveWritten: [...writtenInBlock].sort(), entryPoints: [...exported].sort(), proxied: [...proxied].sort(),
  refused: Object.fromEntries([...refused].map(([k, v]) => [k, v.slice(0, 5)])) };
if (refused.size) { console.error(JSON.stringify(report, null, 1)); console.error('\nREFUSED: variables declared in the block are used outside it (see `refused`).'); process.exit(3); }

// ---- emit -------------------------------------------------------------------------------------------
let body = src.slice(B0, B1);
for (const w of rewrites.sort((a, b) => b.start - a.start)) body = body.slice(0, w.start - B0) + w.text + body.slice(w.end - B0);
const depList = [...plain].sort(), liveList = [...live].sort(), exp = [...exported, ...proxied].sort(), stem = args.stem;
const wrapList = (xs, ind) => { const out = []; let line = ind; for (const x of xs) { if (line.length + x.length + 2 > 100) { out.push(line.trimEnd()); line = ind; } line += x + ', '; } if (line.trim()) out.push(line.trimEnd()); return out.join('\n'); };
const moduleText = `/* ${args.file} — split out of app.js by scripts/split_client_module.mjs.
 *
 * Built on first use by app.js's lazy-module loader (\`_${stem}Mod\` / \`_${stem}Load\`). The code below is
 * app.js's own, moved byte-for-byte; its reads of app.js's live \`let\` bindings were rewritten to
 * \`${SN}.<name>\` (getters/setters on \`dep.state\`) at exact identifier offsets, and everything else it uses
 * arrives through \`dep\`. tests/test_client_module_deps.py proves every name resolves.${SN === 'S' ? '' : `
 * The state object is \`${SN}\`, not \`S\`: the moved code declares an \`S\` of its own.`}
 */
window.${args.factory} = function(dep){
  const ${SN} = dep.state;   // live app.js bindings: ${liveList.map(n => SN + '.' + n).join(', ') || '(none)'}
  const {
${wrapList(depList, '    ')}
  } = dep;

${body}
  return {
${wrapList(exp, '    ')}
  };
};
`;
const stateLines = liveList.map(n => `      get ${n}(){ return ${n}; },` + (writtenInBlock.has(n) ? ` set ${n}(v){ ${n} = v; },` : '')).join('\n');
const glue = `  /* ---------- ${args.file}: moved out of app.js, built on first use ----------
   * Entry points below keep every caller unchanged; see scripts/split_client_module.mjs. */
  function _${stem}Deps(){ return {
    state: {
${stateLines}
    },
${wrapList(depList, '    ')}
  }; }
  function _${stem}Mod(){ return _lzGet('${args.file}', '${args.factory}', _${stem}Deps); }
  function _${stem}Load(){ return _lzLoad('${args.file}', '${args.factory}', _${stem}Deps); }
${[...exported].sort().map(n => `  function ${n}(){ return _lzRun(_${stem}Mod, _${stem}Load, '${n}', arguments); }`).join('\n')}
${[...proxied].sort().map(n => `  const ${n} = _lzProxy(_${stem}Mod, '${n}');   // the module's own object, reached through a Proxy`).join('\n')}
`;
console.log(JSON.stringify(report, null, 1));
if (args.write) {
  fs.writeFileSync(path.join(ROOT, 'static/js/client', args.file), moduleText);
  fs.writeFileSync(APP, src.slice(0, B0) + glue + src.slice(B1));
  console.log('\nwrote static/js/client/' + args.file + ' and rewrote app.js');
}
