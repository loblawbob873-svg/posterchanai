/* Shuffle inside a playlist stays inside the playlist -- the SHIPPED musicplayer.js, run.
 *
 * Reported as "shuffle is no longer working on playlists". Every path that rebuilt the play queue
 * (⏭ on an empty queue, a track the queue did not hold, the desktop widget's Shuffle) rebuilt it
 * from the whole LIBRARY, so shuffle wandered out of the playlist; and tapping a song in a playlist
 * silently switched shuffle OFF. This drives the real MusicPlayer object: a 10-song library, a
 * 3-song playlist, an audio element whose songs "end", and counts where playback actually goes.
 *
 * Prints one JSON object: { scenario: {ok, detail} }.
 */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..', '..');
const SRC = fs.readFileSync(path.join(ROOT, 'static/js/client/musicplayer.js'), 'utf8');

const LIB = Array.from({ length: 10 }, (_, i) => ('t' + i).padEnd(64, '0'));
const PLAYLIST = [LIB[2], LIB[5], LIB[7]];

function player(opts){
  const o = opts || {};
  const audio = { src: '', paused: true, currentTime: 0, duration: 100,
                  play: async function(){ this.paused = false; }, pause(){ this.paused = true; } };
  const ctx = { console, Math, JSON, Promise, Map, Set, Array, Object, String, Number, setTimeout, clearTimeout };
  ctx.window = ctx;
  ctx.window.PCPlaylists = { get: id => id === 'p1' ? { id: 'p1', name: 'Road', tracks: PLAYLIST.slice() } : null,
                             all: () => [{ id: 'p1', name: 'Road', tracks: PLAYLIST.slice() }] };
  vm.createContext(ctx);
  vm.runInContext(SRC, ctx, { filename: 'musicplayer.js' });
  const S = { _audioEl: audio, LOGO: '' };
  const lib = LIB.map(sha => ({ sha, m: { name: sha.slice(0, 2) } }));
  const mod = ctx.PCMusicPlayerFactory({
    state: S, FilesIdx: {}, PC_setMusicPl: () => {}, _capPlugin: () => null, _fmtTime: () => '',
    _trackMeta: () => ({}), _updateMusicListBtns: () => {}, enc: s => String(s),
    musicTracks: () => lib.slice(), renderMusicApp: () => {}, toast: () => {},
    trackUrl: async sha => 'blob:' + sha,
  });
  const M = mod.MusicPlayer;
  // The DOM half is out of scope here: what is measured is where playback GOES.
  M.ensure = function(){ this.el = this.el || { classList: { remove(){}, add(){}, toggle(){} } }; return this.el; };
  M._render = () => {}; M._nativePush = async () => true; M._media = () => {}; M._startViz = () => {};
  M._pl = o.playlist || null;
  return { M, audio };
}
const settle = () => new Promise(r => setTimeout(r, 0));

const out = {};
async function run(name, fn){
  try{ const d = await fn(); out[name] = { ok: !!(d && d.ok), detail: d }; }
  catch(e){ out[name] = { ok: false, detail: String(e && e.stack || e) }; }
}

(async () => {
  await run('shuffle in a playlist never leaves it', async () => {
    const { M } = player({ playlist: 'p1' });
    M.shuffle = true; M.queue = [];
    const played = [];
    for(let i = 0; i < 200; i++){ M.next(); await settle(); played.push(M.cur); }
    const outside = played.filter(s => !PLAYLIST.includes(s));
    const repeats = played.filter((s, i) => i && s === played[i - 1]).length;
    const seen = new Set(played);
    return { ok: !outside.length && !repeats && seen.size === 3, outside: outside.length, repeats, seen: seen.size };
  });

  await run("the desktop widget's Shuffle stays in the chosen playlist", async () => {
    const { M } = player({ playlist: 'p1' });
    // exactly what app.js's widget shuffle does
    M.shuffle = true; M.refreshQueue();
    const q = M.queue.slice();
    return { ok: q.length === 3 && q.every(s => PLAYLIST.includes(s)), q: q.length };
  });

  await run('a song ending moves on inside the playlist, shuffled', async () => {
    const { M, audio } = player({ playlist: 'p1' });
    M.shuffle = true; M.queue = PLAYLIST.slice();
    await M.play(PLAYLIST[0]); await settle();
    const played = [M.cur];
    for(let i = 0; i < 60; i++){ M.next(); await settle(); played.push(M.cur); }   // onended → next()
    return { ok: played.every(s => PLAYLIST.includes(s)) && new Set(played).size === 3 && !audio.paused,
             n: new Set(played).size };
  });

  await run('with no playlist chosen, shuffle covers the library', async () => {
    const { M } = player({});
    M.shuffle = true; M.queue = [];
    const seen = new Set();
    for(let i = 0; i < 300; i++){ M.next(); await settle(); seen.add(M.cur); }
    return { ok: seen.size === 10, seen: seen.size };
  });

  await run('shuffle picking the song already playing restarts it, never pauses', async () => {
    const { M, audio } = player({ playlist: 'p1' });
    M.queue = [PLAYLIST[0]]; M.shuffle = true;
    await M.play(PLAYLIST[0]); await settle();
    await M.play(PLAYLIST[0], { force: true }); await settle();
    return { ok: !audio.paused && M.cur === PLAYLIST[0], paused: audio.paused };
  });

  process.stdout.write(JSON.stringify(out));
})();
