/* A dependency-free nostr relay for the real two-browser sync test. Raw-node WebSocket (no `ws`
 * package), so the test needs nothing installed beyond node + Brave. Handles REQ/EVENT/CLOSE,
 * replaceable addressable events (kind 30078 keyed by pubkey+kind+d-tag, newest created_at wins),
 * broadcasts new events to matching live subscriptions, and serves a stats JSON on port+1000. */
'use strict';
const http = require('http');
const crypto = require('crypto');
const PORT = Number(process.argv[2] || 7447);

const events = new Map();        // key -> event
const subs = new Map();          // socket -> Map(subId -> filter)
let stats = { events: 0, reqs: 0 };

const dOf = (ev) => ((ev.tags || []).find(t => t[0] === 'd') || [])[1] || '';
const keyOf = (ev) => ((ev.kind >= 30000 && ev.kind < 40000) || ev.kind === 0 || (ev.kind >= 10000 && ev.kind < 20000))
  ? `${ev.pubkey}|${ev.kind}|${dOf(ev)}` : ev.id;
function matches(f, ev) {
  if (f.ids && !f.ids.includes(ev.id)) return false;
  if (f.authors && !f.authors.includes(ev.pubkey)) return false;
  if (f.kinds && !f.kinds.includes(ev.kind)) return false;
  for (const k of Object.keys(f)) if (k[0] === '#') {
    const want = f[k]; const have = (ev.tags || []).filter(t => t[0] === k.slice(1)).map(t => t[1]);
    if (!have.some(v => want.includes(v))) return false;
  }
  return true;
}

// ---- minimal WebSocket framing ------------------------------------------------------------------
function accept(key) { return crypto.createHash('sha1').update(key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest('base64'); }
function encode(str) {
  const payload = Buffer.from(str);
  const len = payload.length; let header;
  if (len < 126) { header = Buffer.from([0x81, len]); }
  else if (len < 65536) { header = Buffer.alloc(4); header[0] = 0x81; header[1] = 126; header.writeUInt16BE(len, 2); }
  else { header = Buffer.alloc(10); header[0] = 0x81; header[1] = 127; header.writeBigUInt64BE(BigInt(len), 2); }
  return Buffer.concat([header, payload]);
}
function* frames(buf) {                        // yields {opcode, payload}, mutating via a returned rest
  let off = 0;
  while (off + 2 <= buf.length) {
    const b0 = buf[off], b1 = buf[off + 1];
    const opcode = b0 & 0x0f, masked = (b1 & 0x80) !== 0; let len = b1 & 0x7f, p = off + 2;
    if (len === 126) { if (p + 2 > buf.length) break; len = buf.readUInt16BE(p); p += 2; }
    else if (len === 127) { if (p + 8 > buf.length) break; len = Number(buf.readBigUInt64BE(p)); p += 8; }
    let mask; if (masked) { if (p + 4 > buf.length) break; mask = buf.slice(p, p + 4); p += 4; }
    if (p + len > buf.length) break;
    let payload = buf.slice(p, p + len);
    if (masked) { const out = Buffer.alloc(len); for (let i = 0; i < len; i++) out[i] = payload[i] ^ mask[i & 3]; payload = out; }
    off = p + len;
    yield { opcode, payload };
  }
  frames.rest = buf.slice(off);
}

const server = http.createServer((req, res) => {
  res.setHeader('content-type', 'application/json');
  res.end(JSON.stringify({ ...stats, stored: events.size }));
});
server.on('upgrade', (req, socket) => {
  socket.write('HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ' + accept(req.headers['sec-websocket-key']) + '\r\n\r\n');
  subs.set(socket, new Map());
  const send = (arr) => { try { socket.write(encode(JSON.stringify(arr))); } catch (_) {} };
  let buf = Buffer.alloc(0);
  socket.on('data', (data) => {
    buf = Buffer.concat([buf, data]);
    for (const f of frames(buf)) {
      if (f.opcode === 8) { socket.end(); return; }
      if (f.opcode !== 1 && f.opcode !== 2) continue;
      let m; try { m = JSON.parse(f.payload.toString()); } catch (_) { continue; }
      if (m[0] === 'EVENT') {
        const ev = m[1]; stats.events++;
        const k = keyOf(ev), cur = events.get(k);
        if (!cur || (ev.created_at || 0) >= (cur.created_at || 0)) events.set(k, ev);
        send(['OK', ev.id, true, '']);
        for (const [csock, cmap] of subs) for (const [subId, filt] of cmap)
          if (matches(filt, ev)) { try { csock.write(encode(JSON.stringify(['EVENT', subId, ev]))); } catch (_) {} }
      } else if (m[0] === 'REQ') {
        stats.reqs++; const subId = m[1]; const filters = m.slice(2);
        subs.get(socket).set(subId, filters[0] || {});
        for (const ev of events.values()) if (filters.some(f => matches(f, ev))) send(['EVENT', subId, ev]);
        send(['EOSE', subId]);
      } else if (m[0] === 'CLOSE') { subs.get(socket).delete(m[1]); }
    }
    buf = frames.rest || buf;
  });
  socket.on('close', () => subs.delete(socket));
  socket.on('error', () => {});
});
server.listen(PORT + 1000, '127.0.0.1');           // stats + upgrade share this listener
console.log(JSON.stringify({ relay: `ws://127.0.0.1:${PORT + 1000}`, stats: `http://127.0.0.1:${PORT + 1000}` }));
