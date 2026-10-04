/* Concord community folders: the SHIPPED layout code (concord.js), Armada's model, under node. */
import fs from 'node:fs';
import vm from 'node:vm';
const src = fs.readFileSync(new URL('../../static/js/client/concord.js', import.meta.url), 'utf8');
function lift(decl){
  const start = src.indexOf(decl); if(start < 0) throw new Error('missing: ' + decl);
  let depth = 0;
  for(let i = src.indexOf('{', start); i < src.length; i++){
    if(src[i] === '{') depth++; else if(src[i] === '}' && --depth === 0) return src.slice(start, i + 1);
  }
}
const code = ['function railKeyOf(r){', 'function sameRailKey(a,b){', 'function railNormalize(nodes){', 'function railWithout(nodes,key){',
  'function railApply(nodes,op){', 'function railView(rooms,layout){'].map(lift).join('\n');
const ctx = {}; vm.runInNewContext(code + '\nthis.api={railKeyOf,railNormalize,railApply,railView};', ctx);
const { railKeyOf, railNormalize, railApply, railView } = ctx.api;
const fails = []; const eq = (a, b, what) => { if(JSON.stringify(a) !== JSON.stringify(b)) fails.push(what + ': ' + JSON.stringify(a) + ' != ' + JSON.stringify(b)); };
const A = 'a'.repeat(64), B = 'b'.repeat(64), C = 'c'.repeat(64);
const rooms = [{ communityId: A, name: 'Lounge' }, { communityId: B, name: 'Politics' }, { communityId: C, name: 'Games' }, { protocol: 'nip29', relay: 'wss://groups.example/' }];

eq(railKeyOf(rooms[0]), 'c2:' + A, 'a Concord room is c2:<id>');
eq(railKeyOf(rooms[3]), 'wss://groups.example', 'a NIP-29 server is its normalized relay URL');
// Armada's rules: a one-item folder dissolves, duplicates go, an empty folder goes.
eq(railNormalize([{ type: 'folder', id: 'f', name: 'Solo', keys: ['c2:' + A] }, { type: 'item', key: 'c2:' + A }, { type: 'folder', id: 'e', name: 'Empty', keys: [] }]),
   [{ type: 'item', key: 'c2:' + A }], 'one-item folder dissolves; duplicate and empty dropped');
// A folder made in Armada with c1: keys draws over c2 rooms; a room the layout never mentions still shows.
const armada = [{ type: 'folder', id: 'x', name: 'News', keys: ['c1:' + B, 'c2:' + C] }, { type: 'item', key: 'c2:' + A }];
eq(railView(rooms, armada), [{ type: 'folder', id: 'x', name: 'News', members: [1, 2] }, { type: 'item', i: 0 }, { type: 'item', i: 3 }], 'Armada layout over these rooms');
// Edits.
let L = railApply([], { t: 'into', key: 'c2:' + A, with: 'c2:' + B, id: 'g', name: 'Games' });
eq(L, [{ type: 'folder', id: 'g', name: 'Games', keys: ['c2:' + B, 'c2:' + A] }], 'new folder from two communities');
L = railApply(L, { t: 'into', key: 'c2:' + C, id: 'g' });
eq(railView(rooms, L)[0].members, [1, 0, 2], 'add to an existing folder');
L = railApply(L, { t: 'rename', id: 'g', name: 'Fun' }); eq(L[0].name, 'Fun', 'rename');
L = railApply(L, { t: 'out', key: 'c2:' + A }); eq(L, [{ type: 'folder', id: 'g', name: 'Fun', keys: ['c2:' + B, 'c2:' + C] }, { type: 'item', key: 'c2:' + A }], 'remove from folder lands right after it');
L = railApply(L, { t: 'out', key: 'c2:' + C }); eq(L.map(n => n.type), ['item', 'item', 'item'], 'a folder left with one community dissolves');
L = railApply([{ type: 'folder', id: 'g', name: 'G', keys: ['c2:' + A, 'c2:' + B] }], { t: 'ungroup', id: 'g' });
eq(L, [{ type: 'item', key: 'c2:' + A }, { type: 'item', key: 'c2:' + B }], 'ungroup keeps the order in place');
// An edit re-applied to a copy another device changed keeps that device's folder too.
const other = [{ type: 'folder', id: 'o', name: 'Theirs', keys: ['c2:' + C, 'wss://groups.example'] }];
eq(railApply(other, { t: 'into', key: 'c2:' + A, with: 'c2:' + B, id: 'm', name: 'Mine' }).map(n => n.name || n.key), ['Theirs', 'Mine'], 'concurrent edit keeps both');
if(fails.length){ console.log(fails.join('\n')); process.exit(1); }
console.log('ok');
