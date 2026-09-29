/* A car's play button on a reloaded page resumes the SHARED playlist, not your library — the
 * SHIPPED musicplayer.js, run.
 *
 * Reported: "i was in the car, music from the shared playlist with me would not play, it kept playing
 * a song from my playlist instead." Android had reloaded the app's page in the background, so nothing
 * had registered the share, and _resumeOrPlay looked the last song up in YOUR library, missed, and
 * played the library's first song. Prints one JSON object: { scenario: {ok, detail} }.
 */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..', '..');
const SRC = fs.readFileSync(path.join(ROOT, 'static/js/client/musicplayer.js'), 'utf8');
const id = p => p.padEnd(64, '0');
const MINE = [id('m0'), id('m1'), id('m2'), id('m3')];
const SHARED = [id('s0'), id('s1'), id('s2')];
const KEY = 'a'.repeat(64) + ':road';

function player(opts){
  const o = opts || {};
  const audio = { src: '', paused: true, currentTime: 0, duration: 300,
                  play: async function(){ this.paused = false; }, pause(){ this.paused = true; } };
  const store = new Map(Object.entries(o.storage || {}));
  const toasts = [], blocked = [];
  const ctx = { console, Math, JSON, Promise, Map, Set, Array, Object, String, Number, Error,
                setTimeout: (f, ms) => setTimeout(f, Math.min(ms || 0, 5)), clearTimeout };
  ctx.window = ctx;
  ctx.localStorage = { getItem: k => store.has(k) ? store.get(k) : null, setItem: (k, v) => store.set(k, String(v)) };
  const registered = new Map();
  ctx.PCMusicShare = {
    restore: async key => {
      if(o.shareGone || key !== KEY) return null;
      for(const s of SHARED) registered.set(s, key);
      return SHARED.slice();
    },
    shareOf: sha => registered.get(sha) || '',
  };
  vm.createContext(ctx);
  vm.runInContext(SRC, ctx, { filename: 'musicplayer.js' });
  const S = { _audioEl: audio, LOGO: '' };
  const lib = MINE.map(sha => ({ sha, m: { name: sha.slice(0, 2) } }));
  const mod = ctx.PCMusicPlayerFactory({
    state: S, FilesIdx: { loadLocal(){} }, PC_setMusicPl: () => {}, _capPlugin: () => null, _fmtTime: () => '',
    _trackMeta: () => ({}), _updateMusicListBtns: () => {}, enc: s => String(s),
    musicTracks: () => lib.slice(), renderMusicApp: () => {}, toast: m => toasts.push(m),
    trackUrl: async sha => { if(SHARED.includes(sha) && !registered.has(sha)) throw new Error('not an encrypted track'); return 'blob:' + sha; },
  });
  const M = mod.MusicPlayer;
  M.ensure = function(){ this.el = this.el || { classList: { remove(){}, add(){}, toggle(){} } }; return this.el; };
  M._render = () => {}; M._nativePush = async () => true; M._media = () => {}; M._startViz = () => {};
  M._nativeBlocked = e => blocked.push(String(e && e.message || e));
  return { M, audio, store, toasts, blocked, registered };
}
const wait = async ms => { for(let i = 0; i < (ms || 40); i++) await new Promise(r => setTimeout(r, 1)); };

const out = {};
async function run(name, fn){
  try{ const d = await fn(); out[name] = { ok: !!(d && d.ok), detail: d }; }
  catch(e){ out[name] = { ok: false, detail: String(e && e.stack || e) }; }
}

(async () => {
  await run('a car press on a reloaded page resumes the shared song where it was', async () => {
    const last = { sha: SHARED[1], pos: 42, share: KEY, queue: SHARED.slice() };
    const { M, audio } = player({ storage: { pc_music_last: JSON.stringify(last) } });
    M._resumeOrPlay(); await wait();
    return { ok: M.cur === SHARED[1] && audio.src === 'blob:' + SHARED[1] && audio.currentTime === 42 && !MINE.includes(M.cur),
             cur: M.cur && M.cur.slice(0, 2), at: audio.currentTime };
  });

  await run('after it, the next song is the shared playlist’s next song', async () => {
    const last = { sha: SHARED[1], pos: 0, share: KEY, queue: SHARED.slice() };
    const { M } = player({ storage: { pc_music_last: JSON.stringify(last) } });
    M._resumeOrPlay(); await wait();
    M.next(); await wait();
    const a = M.cur; M.next(); await wait();
    return { ok: a === SHARED[2] && M.cur === SHARED[0], next: [a, M.cur].map(s => s && s.slice(0, 2)) };
  });

  await run('a stopped share is SAID, never replaced by one of your songs', async () => {
    const last = { sha: SHARED[1], pos: 0, share: KEY, queue: SHARED.slice() };
    const { M, blocked, toasts } = player({ storage: { pc_music_last: JSON.stringify(last) }, shareGone: true });
    M._resumeOrPlay(); await wait(80);
    return { ok: !MINE.includes(M.cur) && blocked.length === 1 && toasts.some(t => /shared playlist/.test(t)),
             cur: M.cur, blocked, toasts };
  });

  await run('what is remembered names the share and the queue', async () => {
    const { M, store, registered } = player({});
    for(const s of SHARED) registered.set(s, KEY);
    M.queue = SHARED.slice(); M.cur = SHARED[2];
    M._rememberLast();
    const saved = JSON.parse(store.get('pc_music_last'));
    return { ok: saved.sha === SHARED[2] && saved.share === KEY && saved.queue.join() === SHARED.join(), saved };
  });

  await run('your own playlist survives a reload too', async () => {
    const q = [MINE[3], MINE[1]];
    const { M } = player({ storage: { pc_music_last: JSON.stringify({ sha: MINE[3], pos: 5, share: '', queue: q }) } });
    M._resumeOrPlay(); await wait();
    M.next(); await wait();
    return { ok: M.cur === MINE[1] && M.queue.join() === q.join(), cur: M.cur && M.cur.slice(0, 2), queue: M.queue.length };
  });

  process.stdout.write(JSON.stringify(out));
})();
