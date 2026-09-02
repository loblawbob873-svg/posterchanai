'use strict';
/* A POSTERCHAN WINDOW IS A WINDOW — it gets a taskbar button and an Alt+Tab row like anything else.
 *
 * `taskbarRows` skips every surface with our own app_id, and it has to: a taskbar button for the
 * DESKTOP is recursive, and closing it removes the only visible surface on the screen. Since
 * oswin.js, opening Terminal/Notes/Files makes a real compositor toplevel with that same app_id —
 * so the filter swallowed them too. Measured on the machine: `PosterChan Window — terminal`
 * floating at 986,664 1100x760, the DP-1 taskbar listing only Telegram, and
 * `PCOS.__switchRows()` answering `[{"key":"n:60",...}]` — one row, for somebody else's app.
 * The window was frameless with no title bar of its own, so it had no close, minimise or maximise
 * anywhere either.
 */
const fs=require('fs'), path=require('path');
const ROOT=path.resolve(__dirname,'../..');
const src=fs.readFileSync(process.env.PC_INSTALLED_OSSHELL_JS||
  path.join(ROOT,'static/js/client/osshell.js'),'utf8');
function ok(n,v){ if(!v) throw new Error(n); console.log('  ok   '+n); }

const start=src.indexOf('  function taskbarRows(windows){');
const end=src.indexOf('  /* LAUNCHING SOMETHING THAT IS ALREADY OPEN',start);
if(start<0||end<0) throw new Error('taskbarRows not found');
const ctx={_apps:[{match:'firefox',iconUri:'data:image/png;base64,ff'}]};
const rows=new Function(...Object.keys(ctx),
  src.slice(start,end)+'\nreturn taskbarRows;')(...Object.values(ctx));

const WINDOWS=[
  {id:236,app:'place.poster.desktop',title:'PosterChan · Nostr',focused:false},   // the desktop
  {id:235,app:'place.poster.desktop',title:'PosterChan · Nostr',focused:false},   // the other output
  {id:237,app:'place.poster.desktop',title:'PosterChan Window — terminal',focused:true},
  {id:240,app:'place.poster.desktop',title:'PosterChan Window',focused:false},    // no view suffix
  {id:60, app:'TelegramDesktop',title:'Linda Tribble',focused:false},
  {id:204,app:'firefox-bin',title:'PosterChan · Nostr — Mozilla Firefox',focused:false},
];
const out=rows(WINDOWS);
const byId=Object.fromEntries(out.map(r=>[r.id,r]));

ok('the desktop surfaces are still never tasks',!byId[235]&&!byId[236]);
ok('a PosterChan window IS a task',!!byId[237]);
ok('and is labelled by the view it was opened for, not by the window prefix',
   byId[237].title==='terminal'&&byId[237].label==='terminal');
ok('it is marked as one of ours',byId[237].own===true&&byId[237].view==='terminal');
ok('its focus is the compositor\'s answer',byId[237].focused===true);
ok('a window with no view suffix still gets a row rather than vanishing',
   !!byId[240]&&byId[240].title==='Window');
ok('other applications are untouched',!!byId[60]&&!!byId[204]&&!byId[60].own);
ok('an em dash and a plain hyphen are both accepted',
   rows([{id:9,app:'place.poster.desktop',title:'PosterChan Window - notes'}])[0].view==='notes');
/* The one thing this must never do: a title that merely CONTAINS the prefix is not one of ours. */
ok('a lookalike title from a foreign app is not treated as a desktop surface',
   rows([{id:8,app:'evil',title:'PosterChan Window — terminal'}])[0].own!==true);

console.log('OK a PosterChan window is a task');
