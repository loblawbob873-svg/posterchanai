/* THE TASKBAR SHOWED A GENERIC SQUARE FOR FIREFOX AND TELEGRAM.
 *
 * The compositor reports `org.telegram.desktop` and `org.mozilla.firefox`; the scanned .desktop
 * entry matches on `telegram-desktop` and `firefox`. Neither is a prefix of the other with a
 * separator, so the two most-used apps on the machine matched nothing and every one of their
 * windows fell through to icon:'grid' — on both themes.
 *
 * This runs the SHIPPED matcher out of osshell.js rather than a copy of it.
 */
import fs from 'fs';
const src = fs.readFileSync(new URL('../../static/js/client/osshell.js', import.meta.url), 'utf8');

/* Lift the matcher: it is defined inside the window-list builder, so take it by its own text and
   evaluate exactly what ships. A copy here would pass while the file was broken. */
/* Lift whatever matcher ships, by finding the declaration of `same` and taking everything up to
   the line that uses it. Anchored on the token the FIX introduces, this test failed a revert with
   "the matcher moved" — a true statement that proves nothing about matching. */
const end = src.indexOf("      const meta=");
if (end < 0) throw new Error('the window-list builder moved — this test stopped checking');
const start = src.lastIndexOf("      const same=", end) >= 0
  ? Math.min(...[src.lastIndexOf("      const same=", end),
                 src.lastIndexOf("      const GENERIC=new Set(", end)].filter(i => i >= 0))
  : -1;
if (start < 0) throw new Error('no `same` matcher found before the app lookup');
const same = new Function(src.slice(start, end) + "; return same;")();

const yes = [
  ['org.telegram.desktop', 'telegram-desktop'],
  ['org.mozilla.firefox', 'firefox'],
  ['firefox', 'firefox'],
  ['org.gnome.Calculator', 'gnome-calculator'],
  ['virt-viewer', 'virt-viewer'],
  ['Telegram', 'telegram-desktop'],
];
for (const [a, b] of yes) {
  if (!same(a.toLowerCase(), b.toLowerCase()))
    throw new Error(`${a} should match ${b} — its window gets the generic square`);
  if (!same(b.toLowerCase(), a.toLowerCase()))
    throw new Error(`matching must be symmetric: ${b} vs ${a}`);
}

/* AND IT MUST STILL SAY NO. A matcher that pairs everything hands a window somebody else's icon,
   which is worse than the generic square because it looks deliberate. */
const no = [
  ['org.gnome.Calculator', 'gnome-calendar'],
  ['org.mozilla.firefox', 'thunderbird'],
  ['virt-viewer', 'virt-manager'],
  ['org.telegram.desktop', 'signal-desktop'],
];
for (const [a, b] of no) {
  if (same(a.toLowerCase(), b.toLowerCase()))
    throw new Error(`${a} must NOT match ${b} — that is another app's icon`);
}

console.log('osshell app match runtime ok');
