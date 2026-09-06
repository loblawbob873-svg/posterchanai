/* Run the SHIPPED reaction path: which picker opens, and what a custom emoji publishes.
 *
 * Extracted rather than reimplemented because both answers are one line each in a very long
 * handler, and a copy here would keep agreeing with itself while the app shipped eight hardcoded
 * faces. */
import fs from 'node:fs';

const src = fs.readFileSync(new URL('../../static/js/client/concord.js', import.meta.url), 'utf8');
const plan = JSON.parse(process.argv[2]);
const out = { picker: null, published: null, tags: null, localUrl: null };

if (plan.what === 'picker') {
  // The guarded block that chooses which picker opens, run verbatim. Small and stable, where
  // extracting the whole handler would be a test of my slicing rather than of the app.
  const i = src.indexOf('      if(p.openEmojiPopover){');
  if (i < 0) throw new Error('the picker preference moved');
  const j = src.indexOf('\n      }', i) + 8;
  const block = src.slice(i, j).replace(/\n\s*return;\s*$/m, '');
  const p = plan.hasPopover ? { openEmojiPopover: () => { out.picker = 'app'; } } : {};
  const b = { dataset: { ccReact: 'm1' } };
  const noop = () => {};
  new Function('p', 'b', 'reactionTarget', 'closeMessageActions', 'toggleReaction',
               block + '\nreturn;')(p, b, 'm1', noop, noop);
  if (!out.picker) out.picker = 'inline';
}

if (plan.what === 'tags') {
  // The two lines that build a NIP-30 emoji tag, run verbatim.
  const a = src.indexOf('      const _sc=/^:(');
  const b = src.indexOf("const people=Array.isArray(found.reactions[emoji])", a);
  if (a < 0 || b < 0) throw new Error('the custom-emoji tag builder moved');
  const body = src.slice(a, b);
  const p = { instEmojiUrl: (sc) => (plan.known ? `https://emoji.example/${sc}.png` : '') };
  const fn = new Function('p', 'emoji', body + '\nreturn {sc:_sc&&_sc[1], url:_url, extra:_extra};');
  const r = fn(p, plan.emoji);
  out.tags = r.extra;
  out.localUrl = r.url;
}

process.stdout.write(JSON.stringify(out));
