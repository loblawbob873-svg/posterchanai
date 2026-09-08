"""Run the actual time bridge against a simulated systemd service; never alter the test host clock."""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_system_time_validation_privileges_and_host_state():
    script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('node:assert/strict');
const calls=[];let auto=true,zone='America/Denver',synced=false,deny=false,fail='',hang=false,invalid=false,sudoDenied=false;
const execFile=(bin,args,opts,cb)=>{
 calls.push([bin,args]);assert.equal(opts.timeout,5000);assert.equal(opts.env.LC_ALL,'C');
 if(hang)return cb(Object.assign(new Error('timed out'),{code:'ETIMEDOUT'}),'','');
 if(fail)return cb(new Error(fail),'',fail);
 if(args.includes('show'))return cb(null,invalid?'NTP=yes':`Timezone=${zone}\nNTP=${auto?'yes':'no'}\nNTPSynchronized=${synced?'yes':'no'}\nCanNTP=yes`,'');
 if(args.includes('list-timezones'))return cb(null,'America/Denver\nAsia/Tokyo\nEtc/UTC\n','');
 if(bin==='sudo'&&sudoDenied)return cb(new Error('no grant'),'','sudo: a password is required');
 if(bin==='timedatectl'&&deny)return cb(new Error('denied'),'','Access denied');
 const action=args.find(v=>v.startsWith('set-'));
 if(action==='set-ntp')auto=args.at(-1)==='true';
 if(action==='set-timezone')zone=args.at(-1);
 cb(null,'','');
};
const context={module:{exports:{}},require:n=>{assert.equal(n,'child_process');return {execFile}},process:{platform:'linux',env:{}},Date};
vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),context);
const api=context.module.exports;
(async()=>{
 let s=await api.status();assert.equal(s.automatic,true);assert.equal(s.synchronized,false);
 const before=calls.length;
 for(const value of ['true',1,null,{},undefined])await assert.rejects(api.setAutomatic(value),/on or off/);
 for(const value of ['../../etc/passwd','--help','Asia/Tokyo; reboot',{},'Bogus/Zone'])await assert.rejects(api.setTimezone(value),/valid system time zone/);
 for(const value of ['2026-02-30T12:00','2026-09-08T24:00','2026-09-08T12:60','2026-09-08T12:00; reboot','1969-01-01T00:00'])await assert.rejects(api.setTime(value),/valid local/);
 assert(!calls.slice(before).some(c=>c[1].some(v=>v.startsWith('set-'))));
 await assert.rejects(api.setTime('2026-09-08T12:00'),/Turn off automatic/);
 deny=true;s=await api.setAutomatic(false);assert.equal(s.automatic,false);
 assert(calls.some(c=>c[0]==='sudo'&&c[1].join(' ')==='-n timedatectl --no-ask-password set-ntp false'));
 s=await api.setTimezone('Asia/Tokyo');assert.equal(s.timezone,'Asia/Tokyo');
 await api.setTime('2028-02-29T12:30:00');
 assert(calls.some(c=>c[1].includes('2028-02-29 12:30:00')));
 sudoDenied=true;await assert.rejects(api.setAutomatic(true),/password is required/);sudoDenied=false;
 fail='NTP service unavailable';const count=calls.length;await assert.rejects(api.setAutomatic(true),/NTP service unavailable/);
 assert.equal(calls.length,count+1,'service failure must not retry as root');fail='';
 hang=true;await assert.rejects(api.status(),/timed out/);hang=false;
 invalid=true;await assert.rejects(api.status(),/incomplete/);
})().catch(e=>{console.error(e);process.exit(1)});
'''
    run = subprocess.run(['node', '-e', script, str(ROOT/'desktop/datetime.js')], capture_output=True, text=True, timeout=15)
    assert run.returncode == 0, run.stderr


def test_date_time_ipc_preserves_validation_and_sender_guard():
    main = (ROOT/'desktop/main.js').read_text()
    for name in ['status','zones','automatic','timezone','time']:
        line = next(line for line in main.splitlines() if f"ipcMain.handle('pc:datetime:{name}'" in line)
        assert 'fsGuard(e); return dateTime.' in line
