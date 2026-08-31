/* SAVE HAD TO ASK THE EDITOR TO SAVE, AND IT NEVER DID.
 *
 * `#office-save` waited 700ms and downloaded the session document. The only thing that ever WRITES
 * that document is Collabora's own PutFile, sent when IT decides to — so a save clicked promptly
 * after typing read back the bytes the session opened with, uploaded them, moved the drive index to
 * a fresh hash and said "document saved" over an unchanged file. Reported as "clicking save when
 * opening a blossom file did nothing"; it did the same from a synced folder and from Download,
 * because all three share this one handler.
 *
 * This runs the SHIPPED `askEditorToSave` — extracted from app.js, not re-typed — against a fake
 * editor frame, and asserts three things: it posts Action_Save, it waits for the acknowledgement,
 * and an editor that never answers still lets the save proceed (bounded, never a hang).
 */
import fs from 'fs';
const src = fs.readFileSync(new URL('../../static/js/client/app.js', import.meta.url), 'utf8');

const start = src.indexOf('const askEditorToSave = (root) => new Promise(resolve => {');
if (start < 0) throw new Error('askEditorToSave is gone from app.js — the save no longer asks');
const end = src.indexOf('\n      });', start);
if (end < 0) throw new Error('could not find the end of askEditorToSave');
const body = src.slice(start, end + '\n      });'.length);

/* A frame whose contentWindow records what the host posted, and a window that can carry a reply. */
function harness(answer) {
  const posted = [];
  const listeners = [];
  const contentWindow = { postMessage: (s) => posted.push(JSON.parse(s)) };
  const frame = { contentWindow };
  const win = {
    addEventListener: (t, fn) => { if (t === 'message') listeners.push(fn); },
    removeEventListener: (t, fn) => { const i = listeners.indexOf(fn); if (i >= 0) listeners.splice(i, 1); },
  };
  const reply = (data) => listeners.slice().forEach(fn => fn({ source: contentWindow, data }));
  const timers = [];
  const setTimeout_ = (fn, ms) => { timers.push({ fn, ms }); return timers.length; };
  const clearTimeout_ = () => {};
  const $ = () => (answer === 'noframe' ? null : frame);
  const make = new Function('$', 'window', 'setTimeout', 'clearTimeout',
    body + '\n; return askEditorToSave;');
  return { fn: make($, win, setTimeout_, clearTimeout_), posted, reply, timers, listeners };
}

/* 1. It tells the editor the host is listening, then asks it to save. */
{
  const h = harness();
  const p = h.fn({});
  const ids = h.posted.map(m => m.MessageId);
  if (!ids.includes('Host_PostmessageReady'))
    throw new Error('the editor was never told the host is listening: ' + ids.join(','));
  if (!ids.includes('Action_Save'))
    throw new Error('THE SAVE NEVER ASKED THE EDITOR TO SAVE — posted: ' + ids.join(','));
  const save = h.posted.find(m => m.MessageId === 'Action_Save');
  if (!save.Values || save.Values.Notify !== true)
    throw new Error('Action_Save without Notify never acknowledges, so the wait is pointless');

  /* 2. It resolves on the acknowledgement, and stops listening afterwards. */
  h.reply(JSON.stringify({ MessageId: 'Action_Save_Resp', Values: { success: true } }));
  const ok = await p;
  if (ok !== true) throw new Error('the acknowledgement was not recognised');
  if (h.listeners.length) throw new Error('the message listener leaked — every save would add one');
}

/* 3. An editor that never answers must not hang the button: the wait is bounded and falls back. */
{
  const h = harness();
  const p = h.fn({});
  if (!h.timers.length) throw new Error('nothing bounds the wait — a silent editor hangs Save for ever');
  if (h.timers[0].ms > 15000) throw new Error('the fallback waits ' + h.timers[0].ms + 'ms — too long to sit on Save');
  h.timers[0].fn();
  if (await p !== false) throw new Error('the timeout must report that the editor did not answer');
}

/* 4. A message from somewhere else on the page is not an acknowledgement. */
{
  const h = harness();
  const p = h.fn({});
  h.listeners.slice().forEach(fn => fn({ source: {}, data: JSON.stringify({ MessageId: 'Action_Save_Resp' }) }));
  let settled = false;
  p.then(() => { settled = true; });
  await new Promise(r => setTimeout(r, 0));
  if (settled) throw new Error('a message from another frame was accepted as the editor saving');
}

/* 5. No frame at all is an immediate, honest "no" — never a rejection into the void. */
{
  const h = harness('noframe');
  if (await h.fn({}) !== false) throw new Error('a missing frame must resolve false, not throw or hang');
}

console.log('office save handshake runtime ok');
