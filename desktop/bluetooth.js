/* BlueZ control for the PosterChanOS sound panel. bluetoothctl is used as argv, never through a
 * shell; device addresses are validated before they reach it. */
'use strict';
const { execFile } = require('child_process');
const BIN = process.env.PC_BLUETOOTHCTL || 'bluetoothctl';
const MAC = /^[0-9A-F]{2}(?::[0-9A-F]{2}){5}$/i;
function run(args, timeout=12000){
  return new Promise(resolve => execFile(BIN,args,{timeout,maxBuffer:1024*1024},(err,out,stderr)=>resolve({
    ok:!err,out:String(out||''),error:String(stderr||(err&&err.message)||'').trim().split('\n').pop()
  })));
}
const yes = (s,k) => new RegExp('^\\s*'+k+':\\s*yes\\s*$','mi').test(s);
async function status(scan){
  const show=await run(['show']);
  if(!show.ok) return {available:false,powered:false,devices:[],error:show.error};
  if(scan && yes(show.out,'Powered')) await run(['--timeout','7','scan','on'],10000);
  const d=await run(['devices']);
  const rows=[];
  for(const line of d.out.split(/\r?\n/)){
    const m=/^Device\s+([0-9A-F:]{17})\s+(.+)$/.exec(line.trim());if(!m)continue;
    const info=await run(['info',m[1]]); const s=info.out;
    rows.push({address:m[1].toUpperCase(),name:m[2].trim(),paired:yes(s,'Paired'),
      trusted:yes(s,'Trusted'),connected:yes(s,'Connected'),audio:/Audio Sink|Audio Source|Headset|Headphones/i.test(s)});
  }
  return {available:true,powered:yes(show.out,'Powered'),discovering:yes(show.out,'Discovering'),devices:rows};
}
async function power(on){return run(['power',on?'on':'off']);}
async function device(address,action){
  const mac=String(address||'').toUpperCase();if(!MAC.test(mac))return {ok:false,error:'invalid Bluetooth address'};
  if(!['pair','connect','disconnect','remove'].includes(action))return {ok:false,error:'unknown Bluetooth action'};
  if(action==='pair'){
    const p=await run(['pair',mac],45000);if(!p.ok)return p;
    await run(['trust',mac]);return run(['connect',mac],30000);
  }
  return run([action,mac],30000);
}
module.exports={status,power,device,MAC};
