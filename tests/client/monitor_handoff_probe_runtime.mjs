import fs from 'node:fs';
import vm from 'node:vm';

const source=fs.readFileSync(process.argv[2]||new URL('../../static/js/client/os.js',import.meta.url),'utf8');
const window={};
const document={addEventListener(){},querySelector(){return null},querySelectorAll(){return []}};
const context={window,document,getComputedStyle:()=>({zoom:'1'}),console,setTimeout,clearTimeout};
vm.createContext(context);vm.runInContext(source,context);
const probe=window.PCOS.__tryMonitorDirections;
const rearm=window.PCOS.__rearmFrameHandoffDestination;

const calls=[];let recoveries=0;
const moved=await probe(direction=>{calls.push(direction);return direction==='left'},()=>recoveries++);
if(!moved||calls.join(',')!=='right,left'||recoveries!==0)
  throw Error('opposite-direction success was disturbed: '+JSON.stringify({moved,calls,recoveries}));

calls.length=0;
const rejected=await probe(direction=>{calls.push(direction);return false},()=>recoveries++);
if(rejected||calls.join(',')!=='right,left,down,up'||recoveries!==1)
  throw Error('all-direction rejection did not recover exactly once: '+JSON.stringify({rejected,calls,recoveries}));

/* Both successful legs close their source frame. Each now-empty source must be re-eligible as the
 * destination of the opposite leg; this is the installed left→right→left regression. */
const ready=[];
const bridge={handoffReady:value=>{ready.push(value);return Promise.resolve(true)}};
if(!rearm(bridge)||!rearm(bridge)||ready.join(',')!=='true,true')
  throw Error('move-and-return did not rearm both source renderers: '+JSON.stringify(ready));
console.log('monitor handoff probe runtime: ok');
