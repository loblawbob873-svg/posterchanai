/* A PICTURE SENT FROM A COMPUTER IS SHOWN ONCE, NOT ONCE PLUS A "SENDING" COPY FOR EVER.
 *
 *     "it sent the picture twice when on posterchanOS" / "phone shows the image sent once,
 *      desktop shows twice" / "ah i see, it says sending"
 *
 * Measured on the reporter's archive: the outbox receipt (done, ok, pending) carried image.png,
 * 28,227 bytes as pasted; the phone fitted it for MMS and archived image.jpg, 20,587 bytes, 0.7 s
 * EARLIER by the provider's second-rounded clock. Different files -> different document ids -> the
 * placeholder was never replaced. Runs the SHIPPED rebuild() from sms.js.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const code = fs.readFileSync(new URL('../../static/js/client/sms.js', import.meta.url), 'utf8');
function fn(header){
  const i = code.indexOf(header); assert(i >= 0, 'gone from sms.js: ' + header);
  let depth = 0;
  for (let k = code.indexOf('{', i); k < code.length; k++){
    if (code[k] === '{') depth++; else if (code[k] === '}' && --depth === 0) return code.slice(i, k + 1);
  }
}
const src = ['function key(addr)', 'function convKey(addr)', 'function settlePictureSends(msgs)', 'function rebuild()'].map(fn).join('\n');

function threadAfter(msgs){
  const S = { msgs: new Map(msgs.map(m => [m.doc, m])), threads: [], open: '' };
  const c = { S, Map, Set, Array, String, Number, Math };
  vm.createContext(c);
  vm.runInContext(src, c);
  c.rebuild();
  return S.threads[0].msgs.map(m => m.doc);
}

const T = 1790901568000, TO = '+17195557355';
const asked  = { doc:'pending', address:TO, date:T + 701, incoming:false, pending:true, outbox:'pcai:smsout:99ec',
  body:'', parts:[{ ct:'image/png', name:'image.png', bytes:28227 }] };
const sent   = { doc:'sent', address:TO, date:T, incoming:false, body:'', mms:true,
  parts:[{ ct:'image/jpeg', name:'image.jpg', bytes:20587 }] };
const before = { doc:'older', address:TO, date:T - 5*3600000, incoming:false, body:'', mms:true,
  parts:[{ ct:'image/jpeg', name:'old.jpg', bytes:1000 }] };
const reply  = { doc:'reply', address:TO, date:T - 600000, incoming:true, body:'ok', parts:[] };

/* 1. THE INCIDENT: the phone's re-encoded copy has arrived -> the picture is shown ONCE. */
assert.deepEqual([...threadAfter([reply, asked, sent])], ['reply', 'sent'],
  'a picture sent from this computer is shown twice: the "sending" placeholder survived its sent copy');

/* 2. Still genuinely sending (no sent copy yet) -> the placeholder stays. */
assert.deepEqual([...threadAfter([reply, asked])], ['reply', 'pending'], 'a send still in flight lost its bubble');

/* 3. An OLDER sent picture never settles a new send. */
assert.deepEqual([...threadAfter([before, reply, asked])], ['older', 'reply', 'pending'],
  'a picture sent hours earlier was taken as this send\'s copy');

/* 4. Two pictures sent, one has arrived -> exactly one placeholder remains. */
const asked2 = { ...asked, doc:'pending2', date:T + 5000, outbox:'pcai:smsout:aaaa' };
const shown = [...threadAfter([asked, asked2, sent])];
assert.equal(shown.filter(d => d.startsWith('pending')).length, 1, 'one sent copy settled two placeholders: ' + shown);
assert.ok(shown.includes('sent'));

console.log('OK a picture sent from a computer is shown once');
