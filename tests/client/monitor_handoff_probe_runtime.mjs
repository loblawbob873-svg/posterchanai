import fs from 'node:fs';
import vm from 'node:vm';

const source=fs.readFileSync(process.argv[2]||new URL('../../static/js/client/os.js',import.meta.url),'utf8');
const window={};
const document={addEventListener(){},querySelector(){return null},querySelectorAll(){return []}};
const context={window,document,getComputedStyle:()=>({zoom:'1'}),console,setTimeout,clearTimeout};
vm.createContext(context);vm.runInContext(source,context);
const probe=window.PCOS.__tryMonitorDirections;

const calls=[];let recoveries=0;
const moved=await probe(direction=>{calls.push(direction);return direction==='left'},()=>recoveries++);
if(!moved||calls.join(',')!=='right,left'||recoveries!==0)
  throw Error('opposite-direction success was disturbed: '+JSON.stringify({moved,calls,recoveries}));

calls.length=0;
const rejected=await probe(direction=>{calls.push(direction);return false},()=>recoveries++);
if(rejected||calls.join(',')!=='right,left,down,up'||recoveries!==1)
  throw Error('all-direction rejection did not recover exactly once: '+JSON.stringify({rejected,calls,recoveries}));
console.log('monitor handoff probe runtime: ok');
