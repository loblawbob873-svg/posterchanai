'use strict';
/* WHAT THE CLIENT ACTUALLY SIGNS WHEN YOU REPLY.
 *
 * Runs the SHIPPED `_commentScope` / `replyKindFor` / `replyTags` from app.js against a set of
 * parent shapes and prints the kind + tags for each. The Python side then asks the SHIPPED bridge
 * parsers whether they can read them.
 *
 * This exists because neither side was individually wrong. The client moved replies to NIP-22
 * (kind 1111) and the bridge went on looking for NIP-10 — a disagreement no test of either half
 * could see, and every reply to a fediverse post silently stopped federating.
 */
const fs = require('fs'), vm = require('vm'), path = require('path');
const root = path.resolve(__dirname, '../..');
const src = fs.readFileSync(root + '/static/js/client/app.js', 'utf8');

function fn(decl) {
  const at = src.indexOf(decl);
  if (at < 0) throw new Error('missing: ' + decl);
  /* Brace-balanced, so a slice never runs into the next helper — and COMMENT-AWARE, because this
     file's prose is full of apostrophes ("gitworkshop and its peers"). A scanner that treats one as
     a string quote swallows the rest of the function and produces a slice that will not parse. */
  let i = src.indexOf('{', at), depth = 0, j = i, q = null, line = false, block = false;
  for (; j < src.length; j++) {
    const c = src[j], n = src[j + 1];
    if (line) { if (c === '\n') line = false; continue; }
    if (block) { if (c === '*' && n === '/') { block = false; j++; } continue; }
    if (q) { if (c === '\\') { j++; continue; } if (c === q) q = null; continue; }
    if (c === '/' && n === '/') { line = true; j++; continue; }
    if (c === '/' && n === '*') { block = true; j++; continue; }
    if (c === "'" || c === '"' || c === '`') { q = c; continue; }
    if (c === '{') depth++;
    else if (c === '}' && --depth === 0) return src.slice(at, j + 1);
  }
  throw new Error('unterminated: ' + decl);
}

const PARENTS = {
  // An ordinary top-level note — by far the most common thing anybody replies to, and the shape a
  // mirrored fediverse post takes.
  plain_note:        { id: 'a'.repeat(64), kind: 1, pubkey: 'p'.repeat(64), tags: [] },
  // A NIP-10 reply: replying to a reply.
  nip10_reply:       { id: 'b'.repeat(64), kind: 1, pubkey: 'q'.repeat(64),
                       tags: [['e', 'r'.repeat(64), '', 'root'], ['e', 's'.repeat(64), '', 'reply']] },
  // A NIP-22 comment: replying inside a thread that has already moved to 1111.
  nip22_comment:     { id: 'c'.repeat(64), kind: 1111, pubkey: 't'.repeat(64),
                       tags: [['E', 'u'.repeat(64), '', 'v'.repeat(64)], ['K', '1'],
                              ['P', 'v'.repeat(64)], ['e', 'w'.repeat(64)], ['k', '1']] },
  // A long-form article.
  article:           { id: 'd'.repeat(64), kind: 30023, pubkey: 'x'.repeat(64), tags: [['d', 'slug']] },
};

const ctx = {
  console,
  CFG: { relay_url: 'wss://relay.example' },
  Store: { get: () => null },       // nothing else cached: the common case, and the strictest one
};
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext([fn('function _commentScope(parent){'),
                 fn('function replyKindFor(parent){'),
                 fn('function replyTags(parent, id, pk){'),
                 'this.__api={_commentScope,replyKindFor,replyTags};'].join('\n'), ctx);

const out = {};
for (const [name, parent] of Object.entries(PARENTS)) {
  out[name] = {
    parentId: parent.id,
    parentKind: parent.kind,
    kind: ctx.__api.replyKindFor(parent),
    tags: ctx.__api.replyTags(parent, parent.id, parent.pubkey),
  };
}
console.log('REPLIES ' + JSON.stringify(out));
