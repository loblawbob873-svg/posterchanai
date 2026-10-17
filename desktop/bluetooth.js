/* BlueZ control for the PosterChanOS sound panel. bluetoothctl is used as argv, never through a
 * shell; device addresses are validated before they reach it. */
'use strict';
const { execFile, spawn } = require('child_process');
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
  // 15 s, not 7: a keyboard in pairing mode can take several seconds to answer with its NAME (an Arteck
  // never showed up by name in 7 s -- 2026-10-17), and until it does it is one more MAC address in the list.
  if(scan && yes(show.out,'Powered')) await run(['--timeout','15','scan','on'],18000);
  const d=await run(['devices']);
  const rows=[];
  for(const line of d.out.split(/\r?\n/)){
    const m=/^Device\s+([0-9A-F:]{17})\s+(.+)$/.exec(line.trim());if(!m)continue;
    const info=await run(['info',m[1]]); const s=info.out;
    // `devices` prints the ALIAS, which for a device that never sent a name is its address with dashes.
    // `named` is whether it really has one: the panel lists those, and folds the anonymous rest away.
    const realName=(/^\s*Name:\s*(.+)$/mi.exec(s)||[])[1];
    rows.push({address:m[1].toUpperCase(),name:(realName||m[2]).trim(),named:!!realName,paired:yes(s,'Paired'),
      trusted:yes(s,'Trusted'),connected:yes(s,'Connected'),audio:/Audio Sink|Audio Source|Headset|Headphones/i.test(s),
      input:/Icon:\s*input-|Human Interface Device/i.test(s),
      battery:deviceBattery(m[1].toUpperCase(), s)});
  }
  return {available:true,powered:yes(show.out,'Powered'),discovering:yes(show.out,'Discovering'),devices:rows};
}
/* A DEVICE'S BATTERY, AS A PERCENTAGE THAT MEANS WHAT IT SAYS.
 * Two sources: BlueZ's Battery Service ("Battery Percentage: 0x26 (38)") and, for a keyboard that reports it
 * through HID instead, the kernel's /sys/class/power_supply/hid-<address>-battery -- the Arteck-style keyboard on
 * the TV has only the second. And some of those keyboards declare their battery as 0-255 in the HID descriptor
 * while SENDING a percentage, so the kernel scales a full charge to ~39% (the kernel corrects only a fixed list of
 * devices, HID_BATTERY_QUIRK_PERCENT). Measured 2026-10-17: a just-charged keyboard (green LED) read 38%, its
 * descriptor `15 00 26 ff 00` = 0..255. The same correction, for the devices known to need it, by vendor:product. */
const HID_PERCENT_QUIRK = new Set(['045E:005F']);     // "Bluetooth 5.1 Keyboard" clones using a Microsoft id
const SYSFS = process.env.PC_SYSFS || '/sys';
function deviceBattery(mac, info){
  const m = /Battery Percentage:.*\((\d+)\)/i.exec(info || '');
  if(m) return Number(m[1]);
  try{
    const fs = require('fs'), path = require('path');
    const cap = Number(fs.readFileSync(path.join(SYSFS, 'class', 'power_supply', 'hid-' + mac.toLowerCase() + '-battery', 'capacity'), 'utf8').trim());
    if(!Number.isFinite(cap)) return null;
    const ma = /Modalias:\s*\S*v([0-9A-F]{4})p([0-9A-F]{4})/i.exec(info || '');
    const id = ma ? (ma[1] + ':' + ma[2]).toUpperCase() : '';
    return HID_PERCENT_QUIRK.has(id) ? Math.min(100, Math.round(cap * 255 / 100)) : cap;
  }catch(_){ return null; }
}

async function power(on){return run(['power',on?'on':'off']);}

/* PAIRING A KEYBOARD NEEDS AN AGENT, AND THE PERSON NEEDS TO SEE ITS CODE.
 * `bluetoothctl pair MAC` as a one-shot had no agent to answer with, so a keyboard -- which asks you to type
 * a code ON the keyboard, or (older ones) asks the computer for a PIN -- could not be paired from the panel
 * at all ("how am I supposed to connect my bluetooth keyboard!", 2026-10-17). This runs bluetoothctl
 * interactively with a KeyboardDisplay agent and hands every code it shows to `onCode`:
 *   "Passkey: 123456"            -> show it: type it on the keyboard, then Enter
 *   "Confirm passkey 123456"     -> answered yes (it is shown too)
 *   "Enter PIN code:"            -> a fresh 6-digit PIN is answered and shown, to type on the keyboard
 * Resolves on "Pairing successful" (then trust + connect) or the first failure line, never hangs past
 * `timeout`. */
function pairInteractive(mac, onCode, timeout=90000){
  return new Promise(resolve => {
    let done=false, buf='', paired=false;
    const p=spawn(BIN,[],{stdio:['pipe','pipe','pipe']});
    const say=line=>{ try{ p.stdin.write(line+'\n'); }catch(_){} };
    const finish=r=>{ if(done)return; done=true; clearTimeout(t); try{ say('quit'); }catch(_){} setTimeout(()=>{ try{p.kill();}catch(_){} },500); resolve(r); };
    const t=setTimeout(()=>finish({ok:false,error:'pairing timed out — put the device in pairing mode and try again'}),timeout);
    const code=c=>{ try{ onCode && onCode(String(c)); }catch(_){} };
    const seen=line=>{
      const clean=line.replace(/\x1b\[[0-9;]*[A-Za-z]/g,'');
      let m;
      if((m=/Confirm passkey\s+(\d{4,6})/i.exec(clean))){ code(m[1]); say('yes'); return; }
      if((m=/Passkey:\s*(\d{4,6})/i.exec(clean))){ code(m[1]); return; }
      if(/Enter PIN code/i.test(clean)){ const pin=String(100000+Math.floor(Math.random()*900000)); code(pin); say(pin); return; }
      if(/Authorize service/i.test(clean)){ say('yes'); return; }
      if(/Pairing successful/i.test(clean) && !paired){ paired=true; say('trust '+mac); say('connect '+mac); return; }
      if(paired && /Connection successful/i.test(clean)) return finish({ok:true});
      if(paired && /Failed to connect/i.test(clean)) return finish({ok:true, warning:'paired, but it did not connect yet — press Connect'});
      if(/Failed to pair|AuthenticationFailed|AuthenticationCanceled|not available/i.test(clean))
        return finish({ok:false,error:clean.replace(/^.*?(Failed|org\.bluez)/,'$1').trim() || 'pairing failed'});
    };
    const feed=d=>{ buf+=String(d); let i; while((i=buf.search(/\r?\n|\[agent\]|\(yes\/no\):/))>=0){
        const cut=buf.slice(0,i+ (buf[i]==='\r'?2:1)); buf=buf.slice(cut.length); seen(cut); }
      if(/Enter PIN code:|\(yes\/no\):\s*$/i.test(buf)){ seen(buf); buf=''; } };
    p.stdout.on('data',feed); p.stderr.on('data',feed);
    p.on('error',e=>finish({ok:false,error:String(e&&e.message||e)}));
    p.on('exit',()=>{ if(!done) finish(paired?{ok:true}:{ok:false,error:'bluetoothctl stopped before pairing finished'}); });
    say('agent KeyboardDisplay'); say('default-agent'); say('pair '+mac);
  });
}

async function device(address,action,onCode){
  const mac=String(address||'').toUpperCase();if(!MAC.test(mac))return {ok:false,error:'invalid Bluetooth address'};
  if(!['pair','connect','disconnect','remove'].includes(action))return {ok:false,error:'unknown Bluetooth action'};
  if(action==='pair') return pairInteractive(mac,onCode);
  return run([action,mac],30000);
}
module.exports={status,power,device,pairInteractive,deviceBattery,HID_PERCENT_QUIRK,MAC};
