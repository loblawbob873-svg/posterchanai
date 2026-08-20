/* Volume, mute and device selection for the PosterChanOS shell.
 *
 * PipeWire and WirePlumber are what the OS installs, so `wpctl` is the interface — not pactl, which
 * speaks to the PulseAudio compatibility layer and reports a different device list with different
 * ids, and not ALSA, which is a layer below the thing actually mixing.
 *
 * VOLUME IS A FRACTION TO wpctl AND A PERCENTAGE TO PEOPLE, and that conversion is the whole bug
 * surface. `wpctl set-volume @DEFAULT_AUDIO_SINK@ 0.5` is half; passing 50 is FIVE THOUSAND PERCENT,
 * which wpctl accepts. On most hardware that is not merely loud, it is clipped, distorted and
 * capable of damaging a speaker or an ear — so this clamps at a documented ceiling and refuses
 * anything that is not a number, rather than passing it through to be interpreted.
 *
 * A MUTED SINK STILL HAS A VOLUME, and the two are separate facts. A UI that infers "muted" from
 * "volume is 0" cannot restore the level afterwards, and unmuting leaves silence.
 */
'use strict';
const { execFile } = require('child_process');

const WPCTL = process.env.PC_WPCTL || 'wpctl';
/* Above 1.0 is software gain — real, occasionally wanted, and the reason quiet recordings are
 * audible at all. 1.5 is where most hardware is still clean; beyond that it is distortion with a
 * volume number attached. */
const MAX = 1.5;

function run(args, ms) {
  return new Promise((resolve, reject) => {
    execFile(WPCTL, args, { timeout: ms || 10000 }, (err, stdout, stderr) => {
      if (err) return reject(new Error(String(stderr || err.message || err).trim().split('\n').pop()));
      resolve(String(stdout || ''));
    });
  });
}

const available = () => run(['--version']).then(() => true, () => false);

/* `wpctl get-volume` answers "Volume: 0.65" or "Volume: 0.65 [MUTED]". The MUTED marker is the only
 * place the mute state appears — it is not a separate query — so it is parsed here rather than
 * inferred from the number. */
function parseVolume(out) {
  const m = /Volume:\s*([0-9.]+)/.exec(String(out));
  return { volume: m ? parseFloat(m[1]) : null,
           percent: m ? Math.round(parseFloat(m[1]) * 100) : null,
           muted: /\[MUTED\]/i.test(String(out)) };
}

const sink = () => run(['get-volume', '@DEFAULT_AUDIO_SINK@']).then(parseVolume);
const source = () => run(['get-volume', '@DEFAULT_AUDIO_SOURCE@']).then(parseVolume);

function clamp(percent) {
  const n = Number(percent);
  if (!isFinite(n)) throw new Error('volume must be a number');
  return Math.max(0, Math.min(MAX, n / 100));
}

/** Set the output level, as a PERCENTAGE. */
async function setVolume(percent, which) {
  const id = which === 'source' ? '@DEFAULT_AUDIO_SOURCE@' : '@DEFAULT_AUDIO_SINK@';
  const v = clamp(percent);
  await run(['set-volume', id, v.toFixed(3)]);
  return { percent: Math.round(v * 100) };
}

/* Mute is set EXPLICITLY, never toggled blind. `wpctl set-mute … toggle` exists, but a UI with a
 * mute button and a toggle underneath disagrees with itself the moment anything else changes the
 * state — another app, a headset button, a second window of this one. */
async function setMuted(on, which) {
  const id = which === 'source' ? '@DEFAULT_AUDIO_SOURCE@' : '@DEFAULT_AUDIO_SINK@';
  await run(['set-mute', id, on ? '1' : '0']);
  return { muted: !!on };
}

/* THE DEVICE LIST, from `wpctl status`. Its output is a tree drawn with box characters, and the
 * DEFAULT device is marked with an asterisk — which is the only way to know which one sound is
 * coming out of. Ids are what everything else takes, so they travel with the name. */
function parseStatus(out) {
  const lines = String(out).split('\n');
  const sinks = [], sources = [];
  let section = '';
  for (const raw of lines) {
    const line = raw.replace(/[│├└─]/g, ' ');
    if (/^\s*Sinks:/.test(line)) { section = 'sink'; continue; }
    if (/^\s*Sources:/.test(line)) { section = 'source'; continue; }
    if (/^\s*(Filters|Streams|Devices|Clients):/.test(line)) { section = ''; continue; }
    if (!section) continue;
    const m = /^\s*(\*?)\s*(\d+)\.\s+(.+?)\s*(?:\[vol:\s*([0-9.]+)[^\]]*\])?\s*$/.exec(line);
    if (!m) continue;
    const row = { id: Number(m[2]), name: m[3].trim(), isDefault: m[1] === '*',
                  volume: m[4] ? parseFloat(m[4]) : null };
    (section === 'sink' ? sinks : sources).push(row);
  }
  return { sinks, sources };
}

const devices = () => run(['status']).then(parseStatus);

async function setDefault(id) {
  const n = Number(id);
  if (!Number.isInteger(n) || n <= 0) throw new Error('not a device id');
  await run(['set-default', String(n)]);
  return { id: n };
}

/** Everything the mixer needs to draw itself, in one call. */
async function status() {
  const [out, inp, devs] = await Promise.all([
    sink().catch(() => ({ volume: null, percent: null, muted: false })),
    source().catch(() => ({ volume: null, percent: null, muted: false })),
    devices().catch(() => ({ sinks: [], sources: [] })),
  ]);
  return { output: out, input: inp, sinks: devs.sinks, sources: devs.sources };
}

module.exports = { available, sink, source, setVolume, setMuted, devices, setDefault, status,
                   parseVolume, parseStatus, MAX };
