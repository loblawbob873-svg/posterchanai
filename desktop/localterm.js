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
const fs = require('fs');
const posterfetch = require('./posterfetch.js');

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
  /* The welcome is part of this tab's buffered output, so it appears exactly once even though the
   * renderer attaches after start and can later reload/reconnect. */
  const welcome = posterfetch.render(process.env);
  const s = { id, proc, buf: welcome, seq: welcome.length, subs: new Set(), alive: true, at: Date.now() };
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

/* Find the slave PTY owned by the shell below util-linux `script`.
 *
 * `script` keeps the PTY master while its child shell has /dev/pts/N on fd 0. Reading procfs is
 * Linux-only, as is this PosterChanOS local-terminal bridge. The traversal is ownership-safe: it
 * starts at the exact process we spawned and follows only its descendants, then accepts only a
 * kernel PTY path. */
function terminalTty(s) {
  if (s.tty && /^\/dev\/pts\/\d+$/.test(s.tty)) return s.tty;
  const todo = [s.proc.pid], seen = new Set();
  while (todo.length) {
    const pid = Number(todo.shift());
    if (!pid || seen.has(pid)) continue;
    seen.add(pid);
    if (pid !== s.proc.pid) {
      try {
        const tty = fs.readlinkSync(`/proc/${pid}/fd/0`);
        if (/^\/dev\/pts\/\d+$/.test(tty)) { s.tty = tty; return tty; }
      } catch (_) {}
    }
    try {
      const kids = fs.readFileSync(`/proc/${pid}/task/${pid}/children`, 'utf8').trim();
      if (kids) todo.push(...kids.split(/\s+/).map(Number));
    } catch (_) {}
  }
  return '';
}

function applySize(s, cols, rows, attempt) {
  if (!s || !s.alive) return;
  const tty = terminalTty(s);
  if (!tty) {
    /* The child appears a few milliseconds after spawn. Keep only the newest desired size and find
     * the PTY once it exists; no bytes are ever written to the interactive stdin. */
    if ((attempt || 0) < 20)
      setTimeout(() => applySize(s, s.wantCols, s.wantRows, (attempt || 0) + 1), 25);
    return;
  }
  try {
    const p = spawn('stty', ['-F', tty, 'cols', String(cols), 'rows', String(rows)], {
      stdio: 'ignore', windowsHide: true,
    });
    p.on('error', () => {});
    p.unref();
  } catch (_) {}
}

function resize(id, cols, rows) {
  const s = sessions.get(id);
  if (!s || !s.alive) return { ok: false };
  const c = Math.max(20, Math.min(500, Number(cols) || 80));
  const r = Math.max(5, Math.min(200, Number(rows) || 24));
  /* Resize the PTY itself. Writing `stty …\n` to proc.stdin injected that text into whatever the
   * user was typing, executed it as a command, and made multiple terminals collide. `stty -F`
   * performs the same TIOCSWINSZ ioctl without touching the interactive byte stream. */
  s.wantCols = c; s.wantRows = r;
  applySize(s, c, r, 0);
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
