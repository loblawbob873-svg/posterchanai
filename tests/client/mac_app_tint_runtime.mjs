/* The shipped appHue, run against real launcher keys. */
import fs from 'node:fs';
const os = fs.readFileSync(new URL('../../static/js/client/os.js', import.meta.url), 'utf8');
const start = os.indexOf('  function appHue(key){');
const end = os.indexOf('  const tint = (key) =>', start);
if (start < 0 || end < 0) throw new Error('appHue moved');
const appHue = new Function(os.slice(start, end) + '\n;return appHue;')();

const VIEWS = ['home','global','notifications','messages','drafts','bookmarks','articles','market',
               'streams','communities','calls','settings','translate','news','websearch','terminal',
               'code','calendar','contacts','texts','notes','music','concord','git','files'];
const hues = {};
for (const v of VIEWS) hues[v] = appHue(v);

process.stdout.write(JSON.stringify({
  hues,
  stable: VIEWS.every(v => appHue(v) === hues[v]),
  /* Position must not be an input: the same key called from anywhere gives the same answer. */
  distinct: new Set(Object.values(hues)).size,
  total: VIEWS.length,
  /* A char-code SUM would collide on anagrams; FNV-1a must not. */
  anagram: appHue('notes') !== appHue('stone'),
  inRange: Object.values(hues).every(h => Number.isInteger(h) && h >= 0 && h < 360),
  empty: appHue(''),
}));
