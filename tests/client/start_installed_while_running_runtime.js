'use strict';
/* A PROGRAM INSTALLED WHILE THE SHELL IS RUNNING MUST LAUNCH FROM THE MENU THAT LISTS IT.
 *
 * Reported: "if user installs chrome with emerge, i click on chrome from start menu, says no such
 * app in toaster". The scan is cached for the SESSION and the start menu is its own popup WINDOW.
 * The popup rescans every time it opens, so a freshly emerged Chrome appears in the menu at once;
 * the click is then posted back to the DESKTOP window, whose cache was filled before the install.
 * The menu and the launcher were reading two different lists, and only one could see Chrome.
 *
 * TWO MODULE INSTANCES, because that is the actual shape of the bug. A single instance shares one
 * cache, so the popup's forced rescan repairs the desktop's list for free and the test passes
 * whether or not the fix is there — verified: with the repair removed, the one-instance version
 * still went green. Two windows, two realms, two caches.
 */
const path=require('path');
const MOD=path.resolve(__dirname,'../../static/js/client/osshell.js');

const CHROME={id:'google-chrome',name:'Google Chrome',match:'google-chrome-stable',
              argv:['/usr/bin/google-chrome-stable'],group:'Internet'};
let onDisk=[{id:'firefox-bin',name:'Firefox',match:'firefox',argv:['/usr/bin/firefox-bin']}];
const launched=[];
let scans=0;

/* One window: its own copy of the module, reading the same (changing) disk. */
function newWindow(){
  global.pcApps={list:async()=>{scans++;return {apps:onDisk};}};
  global.pcWM={windows:async()=>[],
               launch:async(argv)=>{launched.push(argv);return {pid:1,window:{id:2}};},
               focus:async()=>true};
  delete require.cache[require.resolve(MOD)];
  return require(MOD);
}

function ok(name,value){if(!value)throw new Error(name);console.log('  ok   '+name);}
(async()=>{
  const desktop=newWindow();          // the shell itself
  await desktop.detect();
  await desktop.machineApps();        // scans early, before the emerge
  ok('the desktop scanned before the install',
     (await desktop.machineApps()).every(a=>a.id!=='app:google-chrome'));
  const afterFirst=scans;
  await desktop.launch('app:firefox-bin');
  ok('a known app launches without rescanning',scans===afterFirst);

  onDisk=onDisk.concat([CHROME]);     // emerge www-client/google-chrome

  const popup=newWindow();            // Start opens: a separate window, a separate cache
  await popup.detect();
  const listed=await popup.allApps(true);
  ok('the menu lists Chrome as soon as it is installed',
     listed.some(a=>a.id==='app:google-chrome'&&a.name==='Google Chrome'));
  ok('the desktop still cannot see it in its own cache',
     (await desktop.machineApps()).every(a=>a.id!=='app:google-chrome'));

  /* THE CLICK. The popup posts the id back and the DESKTOP window launches it. */
  const before=launched.length;
  const r=await desktop.launch('app:google-chrome');
  ok('Chrome launches instead of "no such app"',launched.length===before+1);
  ok('it runs the argv its .desktop names',
     JSON.stringify(launched.at(-1))===JSON.stringify(CHROME.argv));
  ok('the launch reports a window',!!(r&&r.window));

  /* The rescan is a REPAIR, not a blanket "everything works": a real typo still says so. */
  let threw='';
  try{ await desktop.launch('app:not-a-real-program'); }catch(e){ threw=String(e&&e.message||e); }
  ok('an id that names nothing still says so',threw==='no such app');

  console.log('OK a program installed while the shell runs launches from the menu');
})().catch(e=>{console.error(e.stack||e);process.exitCode=1;});
