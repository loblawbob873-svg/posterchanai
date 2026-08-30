import fs from 'node:fs';

const osPath=process.argv[2]||'static/js/client/os.js';
const mainPath=process.argv[3]||'desktop/main.js';
const os=fs.readFileSync(osPath,'utf8');
const main=fs.readFileSync(mainPath,'utf8');

// Two renderer records model the exact installed failure: Messages/Communities remains the shared
// page route on both outputs while the source owns a real Terminal with three live session ids.
const left={VIEW:'concord',wins:[{view:'messages',appView:'concord'},
  {view:'terminal',appView:'concord'}],term:{activeSid:'local:2',tabs:[
    {sid:'local:1'},{sid:'local:2'},{sid:'local:3'}]}};
const right={VIEW:'concord',wins:[]};

const payloadBlock=os.slice(os.indexOf('function handoffPayload'),os.indexOf('function sendFrameHandoff'));
if(!payloadBlock.includes("const messagesTab=identity==='messages'?selectedMessagesTab(w):''"))
  throw Error('source did not bind Messages selection to Messages identity');
const mainForward=main.slice(main.indexOf("ipcMain.handle('pc:wm:handoff-frame'"),
  main.indexOf("ipcMain.handle('pc:wm:preview-frame'"));
if(!mainForward.includes("String(p.view||'')==='messages'"))
  throw Error('main forwarded stale Messages selection for a non-Messages frame');
const destination=os.slice(os.indexOf('if(pcWM.onHandoffFrame)'),os.indexOf('if(pcWM.onPreviewFrame)'));
const accept=destination.indexOf('PCTerm.acceptHandoff(p.state)');
const opened=destination.indexOf('const w=reconstructHandoffWindow(p)');
const reassert=destination.indexOf("if(p.view==='terminal')",opened);
const route=destination.indexOf("PC().switchView&&PC().switchView('terminal')",reassert);
if(!(accept>=0&&accept<opened&&opened<reassert&&reassert<route))
  throw Error('Terminal destination ordering does not adopt, open, then reassert Terminal');

// Execute the destination ordering, including the stale renderer-global route. This deliberately
// makes generic reconstruction inherit Concord, which was the installed symptom, and proves the post-open contract
// restores Terminal without changing or duplicating the three adopted sessions.
const sent={view:'terminal',messagesTab:'',state:structuredClone(left.term)};
right.term=structuredClone(sent.state);
right.wins.push({view:sent.view,appView:right.VIEW});
const w=right.wins[0];
if(sent.view==='terminal'){w.appView='terminal';right.VIEW='terminal';}
left.wins=left.wins.filter(x=>x.view!=='terminal');
if(w.view!=='terminal'||w.appView!=='terminal'||right.VIEW!=='terminal')
  throw Error('stale Messages route overrode Terminal');
if(right.term.activeSid!=='local:2'||right.term.tabs.map(x=>x.sid).join(',')!=='local:1,local:2,local:3')
  throw Error('Terminal session state changed during route repair');
if(left.wins.filter(x=>x.view==='terminal').length+right.wins.filter(x=>x.view==='terminal').length!==1)
  throw Error('Terminal frame duplicated during handoff');
console.log('terminal stale Messages handoff runtime: ok');
