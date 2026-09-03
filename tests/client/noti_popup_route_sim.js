/* RUN the shell's popup-action router against every action a notification popup can send.
 *
 * The router is the half that cannot be checked by reading it: it parses one opaque string into a
 * call, and the strings carry hex ids and pubkeys with colons between them. A `reply:<id>:<pk>`
 * split on the wrong colon calls compose with half an event id and no author — which publishes a
 * reply to nothing, silently.
 *
 * Usage: node noti_popup_route_sim.js ../../static/js/client/os.js
 */
const fs = require('fs');
const path = require('path');

const src = fs.readFileSync(path.resolve(__dirname, process.argv[2]), 'utf8');

/* Lift the `pc:act:` branch out of the tick handler and make it callable. It is written as an
 * `else if`, so a dead `if` in front of it is all the framing it needs — the branch itself is
 * verbatim, which is the point: a test that retypes the code cannot catch the code being wrong. */
function liftRouter() {
  const at = src.indexOf("else if(p.indexOf('pc:act:') === 0){");
  if (at < 0) throw new Error("the pc:act: branch is gone — the popup can no longer reach the shell");
  let depth = 0, end = -1;
  for (let i = src.indexOf('{', at); i < src.length; i++) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') { depth--; if (depth === 0) { end = i + 1; break; } }
  }
  const branch = src.slice(at, end);
  return new Function('p', 'ctx', `
    let notiOpen = true;
    const PC = () => ctx.pc;
    const openLauncherApp = ctx.openLauncherApp;
    const PCOSShell = ctx.shell;
    const window = { PCOSShell };
    const drawBar = ctx.drawBar;
    if (false) {}
    ${branch}
    return { notiOpen };
  `);
}

const route = liftRouter();
let failures = 0, ran = 0;
function check(name, fn) {
  ran++;
  try { fn(); } catch (e) { failures++; console.log('  FAIL ' + name + ': ' + e.message); }
}
function eq(a, b, what) {
  const A = JSON.stringify(a), B = JSON.stringify(b);
  if (A !== B) throw new Error((what || '') + ' expected ' + B + ' got ' + A);
}

function ctx() {
  const calls = [];
  return {
    calls,
    pc: {
      openProfile: (x) => calls.push(['profile', x]),
      openThread: (x) => calls.push(['thread', x]),
      compose: (o) => calls.push(['compose', o]),
    },
    openLauncherApp: (v) => calls.push(['view', v]),
    shell: { launch: (v) => { calls.push(['app', v]); return Promise.resolve({}); } },
    drawBar: () => calls.push(['drawBar']),
  };
}

const ID = 'a'.repeat(64);
const PK = 'b'.repeat(64);

check('a view name opens the app', () => {
  const c = ctx(); route('pc:act:view:mail', c);
  eq(c.calls[0], ['view', 'mail']);
});

check('an installed desktop entry launches through the authoritative shell', () => {
  const c = ctx(); route('pc:act:app:app%3Afirefox-bin', c);
  eq(c.calls[0], ['app', 'app:firefox-bin']);
});

check('a profile action opens that profile', () => {
  const c = ctx(); route('pc:act:profile:' + PK, c);
  eq(c.calls[0], ['profile', PK]);
});

check('a thread action opens that post', () => {
  const c = ctx(); route('pc:act:thread:' + ID, c);
  eq(c.calls[0], ['thread', ID]);
});

check('THE SPLIT: reply carries an id AND an author, and both arrive whole', () => {
  /* `reply:<id>:<pk>` is the ONE action with two colons, so it is the only one that can tell a
   * first-colon split from a last-colon one. Every other action survives either spelling, which is
   * precisely why this case has to exist: without it the kind split can be wrong and the two
   * actions people use most still work, so nothing looks broken until somebody hits Reply. */
  const c = ctx(); route('pc:act:reply:' + ID + ':' + PK, c);
  eq(c.calls[0], ['compose', { reply: ID, replyPk: PK }],
     'reply was split on the wrong colon — the composer opens against half an event id, or the '
     + 'action is not recognised as a reply at all');
});

check('a reply with no author still composes against the right event', () => {
  const c = ctx(); route('pc:act:reply:' + ID, c);
  eq(c.calls[0], ['compose', { reply: ID }]);
});

check('the bell is closed once the shell has acted', () => {
  const c = ctx();
  eq(route('pc:act:thread:' + ID, c).notiOpen, false,
     'the shell still believes the notification centre is open');
});

check('an unknown action does nothing rather than throwing', () => {
  const c = ctx(); route('pc:act:nonsense:x', c);
  eq(c.calls.filter(x => x[0] !== 'drawBar').length, 0);
});

check('a thrown handler cannot take the tick loop down with it', () => {
  const c = ctx();
  c.pc.openThread = () => { throw new Error('relay is down'); };
  route('pc:act:thread:' + ID, c);          // must not throw
});

console.log((failures ? 'FAILED ' : 'ok ') + (ran - failures) + '/' + ran);
process.exit(failures ? 1 : 0);
