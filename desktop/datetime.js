/* System time belongs to systemd; the renderer never stores a second clock configuration. */
'use strict';
const { execFile } = require('child_process');
const OPTIONS = { timeout: 5000, maxBuffer: 512 * 1024, env: { ...process.env, LC_ALL: 'C' } };
function run(bin, args){
  return new Promise((resolve,reject)=>execFile(bin,args,OPTIONS,(error,stdout,stderr)=>{
    if(error){ const e=new Error(String(stderr||error.message||error).trim()); e.code=error.code; reject(e); }
    else resolve(String(stdout||''));
  }));
}
async function status(){
  if(process.platform!=='linux') return {available:false,reason:'Date and time controls require systemd on Linux.'};
  const raw=await run('timedatectl',['--no-pager','show','--property=Timezone','--property=NTP','--property=NTPSynchronized','--property=CanNTP']);
  const values=Object.fromEntries(raw.trim().split('\n').map(line=>{const i=line.indexOf('=');return [line.slice(0,i),line.slice(i+1)];}));
  if(!values.Timezone || !['yes','no'].includes(values.NTP)) throw new Error('The system time service returned incomplete settings.');
  return {available:true,timezone:values.Timezone,automatic:values.NTP==='yes',synchronized:values.NTPSynchronized==='yes',canAutomatic:values.CanNTP==='yes',now:Date.now()};
}
async function timezones(){
  return (await run('timedatectl',['--no-pager','list-timezones'])).trim().split('\n').filter(Boolean);
}
async function change(args){
  try{await run('timedatectl',['--no-ask-password',...args]);}
  catch(e){
    // Identity accounts have no Unix password. Only the existing administrator sudo grant applies.
    if(!/access denied|not authorized|authentication (?:is )?required|permission denied/i.test(e.message))throw e;
    await run('sudo',['-n','timedatectl','--no-ask-password',...args]);
  }
  return status();
}
async function setAutomatic(on){
  if(typeof on!=='boolean')throw new Error('Automatic time must be on or off.');
  return change(['set-ntp',on?'true':'false']);
}
async function setTimezone(zone){
  if(typeof zone!=='string'||zone.length>128||!/^[A-Za-z0-9_+./-]+$/.test(zone)||!(await timezones()).includes(zone))
    throw new Error('Choose a valid system time zone.');
  return change(['set-timezone',zone]);
}
async function setTime(value){
  const m=typeof value==='string'&&/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/.exec(value);
  if(!m)throw new Error('Enter a valid local date and time.');
  const [y,mo,d,h,mi,se]=m.slice(1).map(n=>Number(n||0));
  const check=new Date(Date.UTC(y,mo-1,d,h,mi,se));
  if(y<1970||y>9999||check.getUTCFullYear()!==y||check.getUTCMonth()!==mo-1||check.getUTCDate()!==d||h>23||mi>59||se>59)
    throw new Error('Enter a valid local date and time.');
  if((await status()).automatic)throw new Error('Turn off automatic time before setting the clock manually.');
  return change(['set-time',value.replace('T',' ')]);
}
module.exports={status,timezones,setAutomatic,setTimezone,setTime};
