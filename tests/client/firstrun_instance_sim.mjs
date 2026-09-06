/* Run the SHIPPED world-building in osfirstrunui.js against a stubbed shell.
 *
 * The question is one this cannot be asked by reading the file: does a machine that has NEVER been
 * configured see the instance step? `main.js` answers `instance()` with a built-in default whenever
 * nothing is set, so `apiBase()` and `__PC_API_BASE__` are both full on a completely fresh install
 * — which is why every LiveISO ever built came up already pointed at the developer's instance and
 * never asked. */
import fs from 'node:fs';

const src = fs.readFileSync(new URL('../../static/js/client/osfirstrunui.js', import.meta.url), 'utf8');
const a = src.indexOf('    /* A DEFAULT NOBODY CHOSE IS NOT AN ANSWER');
if (a < 0) throw new Error('the instance detection moved');
const b = src.indexOf('    w.instanceSkipped', a);
if (b < 0) throw new Error('the end of the instance block moved');
const body = src.slice(a, b);

const plan = JSON.parse(process.argv[2]);
const w = {};
const root = {};
if (plan.shell) root.pcShell = { instanceSync: plan.apiBase || '', instanceChosen: plan.chosen };
const PC = () => ({ apiBase: () => plan.apiBase || '' });
if (plan.bundled) root.__PC_API_BASE__ = plan.apiBase || '';

new Function('w', 'root', 'PC', body)(w, root, PC);
process.stdout.write(JSON.stringify({ instance: w.instance }));
