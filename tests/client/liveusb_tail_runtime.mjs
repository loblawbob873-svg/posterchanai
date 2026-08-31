/* The installation-media log must show the NEWEST line, and must not steal the scrollbar.
 *
 * `.os-liveusb-status` is `max-height:120px;overflow:auto`, and the text it is handed is the TAIL of
 * the build log (`output.slice(-5000)`) — but a <pre> keeps its scroll position, which is the top.
 * A build printing steadily therefore sat on whatever it said 5,000 characters ago and looked
 * frozen. Reported as "looks bad and like it's not working", which is exactly right: from outside it
 * is indistinguishable from a job that has stopped.
 *
 * Runs the SHIPPED helper against a stub that behaves like a scrolling <pre>.
 */
import fs from 'node:fs';
const os = fs.readFileSync(new URL('../../static/js/client/os.js', import.meta.url), 'utf8');
const start = os.indexOf('        const showTail=(text)=>{');
/* NO HELPER MEANS THE OLD BEHAVIOUR, not a broken harness. Throwing "showTail moved" here would
   make this file fail for the wrong reason against pre-fix code — a harness complaint where a
   finding belongs — and the finding is the whole point: a plain assignment leaves the box showing
   the top. So the absent case is modelled as what the code used to do. */
const HELPER = start >= 0
  ? os.slice(start, os.indexOf('};', start) + 2)
  : 'const showTail=(text)=>{ stat.textContent=text; };';

/* A <pre> that scrolls: 120px tall, ~16px a line, scrollHeight grows with the text. */
function box(){
  return { clientHeight: 120, scrollTop: 0, _text: '',
           get scrollHeight(){ return Math.max(120, String(this._text).split('\n').length * 16); },
           set textContent(v){ this._text = String(v); },
           get textContent(){ return this._text; } };
}
const lines = n => Array.from({length:n}, (_,i)=>'step '+i).join('\n');
const write = (stat, text) =>
  new Function('stat', 'text', HELPER + '\nshowTail(text);')(stat, text);

/* 1. A fresh box, then a long log: the reader is at the end, so it follows. */
const a = box();
write(a, lines(200));
const atBottom = a.scrollTop === a.scrollHeight;

/* 2. The reader scrolls UP to read an error while the build is still printing. The next poll must
      leave them where they are. */
const b = box();
write(b, lines(200));
b.scrollTop = 100;
const before = b.scrollTop;
write(b, lines(400));
const stayedPut = b.scrollTop === before;

/* 3. And it follows again once they scroll back to the end. */
const c = box();
write(c, lines(200));
c.scrollTop = c.scrollHeight - c.clientHeight;
write(c, lines(400));
const resumed = c.scrollTop === c.scrollHeight;

process.stdout.write(JSON.stringify({ atBottom, stayedPut, resumed }));
