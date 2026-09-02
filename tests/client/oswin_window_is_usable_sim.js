'use strict';
/* A POSTERCHAN WINDOW HAS TO BE A USABLE WINDOW, and for its whole life it was not one.
 *
 * Measured on the real machine (build 1.0.1382), `swaymsg` reporting a correctly titled floating
 * `PosterChan Window — terminal` at 1100x760, and inside it:
 *
 *   window.pcClip        undefined     <- exposed UNCONDITIONALLY at the top of preload.js
 *   window.pcTerm        undefined
 *   window.pcWM          undefined
 *   PCOSShell.available  false
 *   document.body.class  "... os-on ..."   <- a second DESKTOP, built inside the window
 *   #feed                0x0               <- holding 477KB of timeline HTML nobody could see
 *
 * ...and one line per window in shell.log:
 *   TypeError: Cannot destructure property 'preloadScripts' of 'binding.startupData' as it is null.
 *
 * Three separate rules, each of which this harness runs against the SHIPPED files.
 */
const fs=require('fs'), path=require('path');
const ROOT=path.resolve(__dirname,'../..');
const rd=(rel,env)=>fs.readFileSync(process.env[env]||path.join(ROOT,rel),'utf8');
const main=rd('desktop/main.js','PC_INSTALLED_MAIN_JS');
const preload=rd('desktop/preload.js','PC_INSTALLED_PRELOAD_JS');
const client=rd('static/js/client/os.js','PC_INSTALLED_OS_JS');
const css=rd('static/css/client.css','PC_INSTALLED_CLIENT_CSS');
function ok(n,v){ if(!v) throw new Error(n); console.log('  ok   '+n); }

/* ---- 1. The window's webPreferences, RUN, not read. ------------------------------------------
 * A same-origin child shares the OPENER'S renderer process. Every shell surface is made by
 * createWindow(), which does not set `sandbox` and so gets Electron's default (sandboxed). Asking
 * for an unsandboxed preload inside that process is a contradiction Electron does not refuse — it
 * fails to bootstrap the preload and carries on with no bridges at all. */
const openStart=main.indexOf('created.webContents.setWindowOpenHandler(');
const openEnd=main.indexOf('  // A 302 out to the provider fires will-redirect', openStart);
if(openStart<0||openEnd<0) throw new Error('setWindowOpenHandler not found');
const handlerSrc=main.slice(openStart,openEnd)
  .replace('created.webContents.setWindowOpenHandler(','globalThis.__openHandler=(');
const ctx={ isOurs:(u)=>String(u).startsWith('app://posterchan'),
            path:{join:(...a)=>a.join('/')}, __dirname:'/opt/app.asar',
            shell:{openExternal(){}}, created:{webContents:{setWindowOpenHandler(){}}} };
new Function(...Object.keys(ctx), handlerSrc)(...Object.values(ctx));
const decision=globalThis.__openHandler({url:'app://posterchan/index.html?pcwin=terminal',
                                         features:'width=1100,height=760'});
ok('a pcwin url is allowed as an app window',decision && decision.action==='allow');
const wp=(decision.overrideBrowserWindowOptions||{}).webPreferences||{};
ok('the window is given the preload',/preload\.js$/.test(String(wp.preload||'')));
ok('it does NOT request a sandbox setting its process cannot have',
   !Object.prototype.hasOwnProperty.call(wp,'sandbox'));
/* The parity that makes that rule true: createWindow leaves `sandbox` at Electron's default. */
const cw=main.slice(main.indexOf('function createWindow(assignment)'),
                    main.indexOf('  if(primary) win = created;'));
ok('and the surfaces it shares a process with do not set it either',!/\bsandbox\s*:/.test(cw));

/* ---- 2. A window is never the folder-sync background owner. ----------------------------------
 * `--pc-secondary-surface` is a PROCESS argument and the child runs in the OPENER'S process, so a
 * window popped out of the PRIMARY surface inherits `backgroundOwner: true` — a second writer over
 * one tree with the same device identity, which is the failure that marker exists to prevent. */
/* Extract from whatever the file declares — an older build has only the argv line, and this test
 * must FAIL on it rather than refuse to run. */
const bgOwnerAt=preload.indexOf('const backgroundOwner');
if(bgOwnerAt<0) throw new Error('backgroundOwner not declared');
const bgWinAt=preload.indexOf('const _isWindowDoc');
const bgStart=(bgWinAt>=0 && bgWinAt<bgOwnerAt) ? bgWinAt : bgOwnerAt;
const bgEnd=preload.indexOf(';', bgOwnerAt)+1;
const bgSrc=preload.slice(bgStart,bgEnd)+'\nreturn backgroundOwner;';
const bg=(search,argv)=>new Function('location','process',bgSrc)(
  {search}, {argv});
ok('the primary surface still owns background work',bg('',['electron'])===true);
ok('a secondary monitor surface still does not',
   bg('',['electron','--pc-secondary-surface'])===false);
ok('a WINDOW never does, even opened from the primary surface',
   bg('?pcwin=terminal',['electron'])===false);

/* ---- 3. A window must not build a desktop inside itself. -------------------------------------
 * It is a same-origin child, so it reads the same remembered `osMode` and it is over MIN_WIDTH on
 * any real monitor. `html.pc-oswin` hides `#os-root`, and `enter()` has already moved `#feed` into
 * it — which is a window showing the client's background gradient and nothing else. */
const entStart=client.indexOf('  function enter(){');
const entEnd=client.indexOf('    if(!fits() && !isSystemShell()){',entStart);
if(entStart<0||entEnd<0) throw new Error('enter() prologue not found');
const guardSrc=client.slice(entStart,entEnd)
  .replace('  function enter(){','  function enter(){')+'    return "ENTERED";\n  }\n'+
  '  globalThis.__enter=enter;';
function runEnter(win){
  const c={on:false,window:win,URLSearchParams};
  return new Function(...Object.keys(c),guardSrc+'\nreturn globalThis.__enter();')(...Object.values(c));
}
ok('the desktop still enters',
   runEnter({location:{search:''}})==='ENTERED');
ok('a window opened with ?pcwin= does not',
   runEnter({location:{search:'?pcwin=terminal'},
             PCOSWin:{isWindow:()=>true}})===undefined);
/* THE URL IS GONE BY THEN. The client rewrites its own address during boot, so `?pcwin=` has
 * usually been erased before anything calls enter() — which is why oswin.js latches. Measured on
 * the machine: `location.href` was `app://posterchan/` while `PCOSWin.viewOf()` still said
 * `terminal`. A guard that only reads the URL would pass every test and fail on the desk. */
ok('...nor one whose URL has already been rewritten',
   runEnter({location:{search:''},__PC_WIN_STATE__:{view:'terminal'}})===undefined);

/* ---- 4. One view, one column. ----------------------------------------------------------------
 * `.app` is a GRID (`300px 1fr`), so hiding `.sidebar` empties the first track without removing it:
 * `.main` stays in a 300px column and `width:100%` is 100% OF THAT. Measured in the terminal window:
 * `#feed` 201x723 inside an 1100px window, with nine hundred pixels of empty ground beside it. */
const block=css.slice(css.indexOf('html.pc-oswin, html.pc-oswin body'),
                      css.indexOf('.oswin-probe{'));
ok('a window collapses the app grid to one column',
   /html\.pc-oswin #app\{[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\)!important/.test(block));
ok('and hides the right bar with the sidebar',/html\.pc-oswin \.rightbar/.test(block));

console.log('OK a PosterChan window is a usable window');
