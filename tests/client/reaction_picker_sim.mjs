/* Run the SHIPPED reaction-picker placement against stubbed geometry.
 *
 * The bug this covers is not a crash and not a wrong-looking number: a hidden anchor measures 0x0,
 * every term is relative to the anchor, and the result is a perfectly valid position that happens
 * to be the corner of the screen. Nothing throws, nothing logs, and the picker is on screen — just
 * nowhere near the message. So the assertion has to be about WHERE, which needs the real function
 * and real rectangles. */
import fs from 'node:fs';

const src = fs.readFileSync(new URL('../../static/js/client/concord.js', import.meta.url), 'utf8');
const a = src.indexOf('  function reactionPickerPosition(anchor,picker,viewport,anchorRect){');
if (a < 0) throw new Error('reactionPickerPosition moved or changed shape');
const b = src.indexOf('\n  }', a) + 4;
const place = new Function('return ' + src.slice(a, b).trim() + ';')();

const plan = JSON.parse(process.argv[2]);
const box = (o) => ({ getBoundingClientRect: () => o, offsetWidth: o.width, offsetHeight: o.height });
const ZERO = { width: 0, height: 0, left: 0, top: 0, right: 0, bottom: 0 };

const anchor = box(plan.anchor || ZERO);
const picker = box(plan.picker || { width: 140, height: 80, left: 0, top: 0, right: 140, bottom: 80 });
const at = place(anchor, picker, plan.viewport || { width: 1600, height: 900 }, plan.measured || undefined);
process.stdout.write(JSON.stringify({ at }));
