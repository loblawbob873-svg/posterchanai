/* A local shell for the PosterChanOS terminal.
 *
 * The client already has a terminal — resumable sessions, a cursor a reconnect replays from — and
 * it speaks to a PTY over SSH. On PosterChanOS the machine IS the node, and a shell that has to go
 * out over the network and back to reach its own computer is absurd; worse, PosterChanOS can run
 * with no PosterChan server at all, in which case there is nothing to SSH to.
 *
 * NO NATIVE MODULE. node-pty would be the obvious way and it is a compiled addon: one per platform,
 * rebuilt against every Electron version, in an app that ships as a single AppImage. `script` from
 * util-linux allocates a real PTY and is on every Linux system that has a shell at all — which is
 * the whole requirement. `-q` no banner, `-f` flush so output arrives keystroke by keystroke rather
 * than in blocks, and /dev/null because the typescript FILE is the one thing we do not want: a
 * verbatim log of the session on disk, including everything typed at a password prompt.
 *
 * WHAT THIS IS NOT: it is not a sandbox. The shell runs as the session user with that user's
 * rights, exactly as pressing Super+Return does. The account model is what limits it — every
 * signed-in identity gets its own Unix account with a 0700 home and no sudo.
 */
'use strict';
const { spawn } = require('child_process');

const MAX_SESSIONS = 8;      // a shell each for a person who has lost count is still not eight
const sessions = new Map();  // id -> { proc, buf, seq, subscribers }
let nextId = 1;

/* Output is BUFFERED as well as pushed, because the renderer can be reloaded — the WebView is
 * recreated under memory pressure and on a crash — and a terminal that loses its scrollback because
 * the page reloaded is a terminal nobody trusts with a long-running command. Bounded, because a
 * `yes` left running is otherwise a memory leak with a cursor. */
const MAX_BUF = 256 * 1024;

function start(opts) {
  if (sessions.size >= MAX_SESSIONS) throw new Error('too many terminals open');
  const o = opts || {};
  const shell = process.env.SHELL || '/bin/bash';
  const cols = Math.max(20, Math.min(500, Number(o.cols) || 80));
  const rows = Math.max(5, Math.min(200, Number(o.rows) || 24));
  /* `stty` inside the pty is how the size is set: `script` gives a PTY but no way to resize it from
   * outside, and a shell that thinks it is 80x24 on a 1920px screen wraps every long line in the
   * wrong place. Set at start and again on every resize. */
  const cmd = `stty cols ${cols} rows ${rows} 2>/dev/null; exec ${shell} -l`;
  const proc = spawn('script', ['-qfc', cmd, '/dev/null'], {
    cwd: o.cwd || process.env.HOME || '/',
    env: Object.assign({}, process.env, { TERM: 'xterm-256color' }),
    stdio: ['pipe', 'pipe', 'pipe'],
  });

  const id = String(nextId++);
  const s = { id, proc, buf: '', seq: 0, subs: new Set(), alive: true, at: Date.now() };
  sessions.set(id, s);

  const push = (chunk) => {
    const text = chunk.toString('utf8');
    s.seq += text.length;
    s.buf += text;
    s.at = Date.now();
    if (s.buf.length > MAX_BUF) s.buf = s.buf.slice(-MAX_BUF);
    for (const fn of s.subs) { try { fn({ t: 'out', d: text, seq: s.seq }); } catch (_) {} }
  };
  proc.stdout.on('data', push);
  proc.stderr.on('data', push);
  proc.on('exit', (code) => {
    s.alive = false;
    for (const fn of s.subs) { try { fn({ t: 'end', code }); } catch (_) {} }
    /* Kept briefly so a renderer that reconnects can see WHY it ended — a terminal that vanishes
     * the instant a command kills the shell tells nobody what happened. */
    setTimeout(() => sessions.delete(id), 60000);
  });
  proc.on('error', () => { s.alive = false; });

  return { id, cols, rows };
}

function write(id, data) {
  const s = sessions.get(id);
  if (!s || !s.alive) return { ok: false };
  try { s.proc.stdin.write(String(data == null ? '' : data)); } catch (_) { return { ok: false }; }
  return { ok: true };
}

function resize(id, cols, rows) {
  const s = sessions.get(id);
  if (!s || !s.alive) return { ok: false };
  const c = Math.max(20, Math.min(500, Number(cols) || 80));
  const r = Math.max(5, Math.min(200, Number(rows) || 24));
  /* Written into the pty rather than signalled: without node-pty there is no ioctl to send, and a
   * shell told its own size by stty behaves correctly from the next prompt. */
  try { s.proc.stdin.write(`stty cols ${c} rows ${r} 2>/dev/null\n`); } catch (_) {}
  return { ok: true, cols: c, rows: r };
}

/** Everything produced so far, so a reloaded page can redraw rather than start blank. */
function backlog(id, since) {
  const s = sessions.get(id);
  if (!s) return null;
  const from = Number(since) || 0;
  if (from >= s.seq) return { d: '', seq: s.seq, alive: s.alive };
  /* The buffer is bounded, so a cursor older than what is kept cannot be honoured exactly. Sending
   * everything held and saying where it really starts beats silently returning a fragment as though
   * it were the whole gap. */
  const kept = Math.max(0, s.seq - s.buf.length);
  const start = Math.max(0, from - kept);
  return { d: s.buf.slice(start), seq: s.seq, alive: s.alive, truncated: from < kept };
}

function subscribe(id, fn) {
  const s = sessions.get(id);
  if (!s) return () => {};
  s.subs.add(fn);
  return () => s.subs.delete(fn);
}

function close(id) {
  const s = sessions.get(id);
  if (!s) return { ok: true };
  try { s.proc.kill('SIGHUP'); } catch (_) {}
  sessions.delete(id);
  return { ok: true };
}

/* `idle` is what the session strip ages them by — the same "still running · 4m" a server-side
 * session gets, because from the person's side they are the same thing. */
const list = () => [...sessions.values()].map(s => ({
  id: s.id, alive: s.alive, seq: s.seq, idle: Math.round((Date.now() - (s.at || 0)) / 1000) }));
/** Every shell dies with the app: a session outliving the desktop is a process nobody can reach. */
const closeAll = () => { for (const id of [...sessions.keys()]) close(id); };

module.exports = { start, write, resize, backlog, subscribe, close, list, closeAll, MAX_SESSIONS };
