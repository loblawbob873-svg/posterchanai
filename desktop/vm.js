/* Small libvirt/qemu backend for PosterChanOS.  This deliberately uses qemu:///session: a Nostr
 * identity's VMs and disks belong to that Unix identity, just like the rest of its private home. */
'use strict';
const { execFile, spawn } = require('child_process');
const fs = require('fs');
const path = require('path');
const os = require('os');
const crypto = require('crypto');

const URI = 'qemu:///session';
const root = () => path.join(os.homedir(), '.local', 'share', 'PosterChanOS', 'vms');
const run = (cmd, args, timeout=20000) => new Promise(resolve => execFile(cmd, args,
  {timeout, maxBuffer: 2*1024*1024}, (error, stdout, stderr) => resolve({
    ok: !error, out: String(stdout||''), error: String(stderr || (error && error.message) || '').trim()
  })));
const virsh = (args, timeout) => run('virsh', ['--connect', URI].concat(args), timeout);
const cleanName = n => String(n||'').trim().replace(/[^A-Za-z0-9_.-]+/g, '-')
  .replace(/^[.-]+|[.-]+$/g,'').slice(0,48);
const xml = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&apos;'}[c]));

async function available(){
  const r=await virsh(['version']);
  return {available:r.ok, uri:URI, error:r.ok?'':r.error};
}
async function successorInstaller(source){
  /* PosterChanOS puts dated Live images beside one another. An update may replace
   * posterchan-live-YYYYMMDD.iso while an existing libvirt definition still names yesterday's
   * file. Repair only this unambiguous first-party sequence: never guess that an unrelated Ubuntu,
   * Windows, or rescue ISO is the replacement for somebody's chosen installer. */
  const old=path.basename(String(source||''));
  if(!/^posterchan-live-\d{8}(?:-[A-Za-z0-9._-]+)?\.iso$/i.test(old))return '';
  const dir=path.dirname(String(source||''));
  let names;try{names=await fs.promises.readdir(dir);}catch(_){return '';}
  const candidates=names.filter(n=>/^posterchan-live-\d{8}(?:-[A-Za-z0-9._-]+)?\.iso$/i.test(n)&&n>old)
    .sort().reverse();
  for(const name of candidates){
    const file=path.join(dir,name);
    try{if((await fs.promises.stat(file)).isFile())return file;}catch(_){}
  }
  return '';
}
async function list(){
  const a=await available(); if(!a.available) return Object.assign(a,{machines:[]});
  const r=await virsh(['list','--all','--name']);
  const names=r.out.split(/\r?\n/).map(x=>x.trim()).filter(Boolean);
  const machines=[];
  for(const name of names){
    const i=await virsh(['dominfo',name]);
    const get=k => ((i.out.match(new RegExp('^'+k+':\\s*(.*)$','mi'))||[])[1]||'').trim();
    const blocks=await virsh(['domblklist',name,'--details']);
    const missingMedia=blocks.ok?blocks.out.split(/\r?\n/).slice(2).map(x=>x.trim().split(/\s+/))
      .filter(x=>x.length>=4&&x[1]==='cdrom').map(x=>x.slice(3).join(' '))
      .filter(source=>source&&source!=='-'&&!fs.existsSync(source)):[];
    machines.push({name,state:get('State').toLowerCase(),memoryKiB:Number((get('Max memory').match(/\d+/)||[0])[0]),
      cpus:Number(get('CPU\\(s\\)'))||0,autostart:/enable/i.test(get('Autostart')),missingMedia});
  }
  return {available:true,uri:URI,machines};
}
async function action(name, what){
  name=cleanName(name); if(!name) return {ok:false,error:'invalid VM name'};
  const map={start:['start'],shutdown:['shutdown'],reboot:['reboot'],stop:['destroy']};
  if(!map[what]) return {ok:false,error:'unknown action'};
  if(what==='start'){
    let d=await details(name);if(!d.ok)return d;
    let missing=d.disks.find(x=>x.source&&x.source!=='-'&&!fs.existsSync(x.source));
    if(missing&&missing.device==='cdrom'){
      const next=await successorInstaller(missing.source);
      if(next){
        const changed=await changeIso(d.name,next);if(!changed.ok)return changed;
        d=await details(name);if(!d.ok)return d;
        missing=d.disks.find(x=>x.source&&x.source!=='-'&&!fs.existsSync(x.source));
      }
    }
    if(missing)return {ok:false,error:'Attached media is missing: '+missing.source+'. Replace or eject it in VM Settings.'};
  }
  return virsh(map[what].concat(name),30000);
}
async function details(name){
  name=cleanName(name); if(!name) return {ok:false,error:'invalid VM name'};
  const info=await virsh(['dominfo',name]), blocks=await virsh(['domblklist',name,'--details']),
    xml=await virsh(['dumpxml',name]);
  if(!info.ok) return info;
  const get=k=>((info.out.match(new RegExp('^'+k+':\\s*(.*)$','mi'))||[])[1]||'').trim();
  const disks=blocks.ok?blocks.out.split(/\r?\n/).slice(2).map(x=>x.trim().split(/\s+/))
    .filter(x=>x.length>=4).map(x=>({type:x[0],device:x[1],target:x[2],source:x.slice(3).join(' ')})):[];
  const body=xml.ok?xml.out:'';
  const boots=[...body.matchAll(/<boot\s+dev=['"](hd|cdrom)['"]\s*\/>/g)].map(x=>x[1]);
  return {ok:true,name,state:get('State').toLowerCase(),ramMiB:Math.round(Number((get('Max memory').match(/\d+/)||[0])[0])/1024),
    cpus:Number(get('CPU\\(s\\)'))||1,autostart:/enable/i.test(get('Autostart')),disks,
    bootOrder:boots[0]==='cdrom'?'cdrom':'disk',
    gamingMouse:/<input type=['"]mouse['"] bus=['"]ps2['"]/.test(body),
    networks:(body.match(/<interface\b/g)||[]).length};
}
async function setBootOrder(name, first){
  const d=await details(name);if(!d.ok)return d;
  if(!/shut off|shutoff|inactive/.test(d.state))return {ok:false,error:'Shut down the VM before changing boot order'};
  const x=await virsh(['dumpxml',d.name]);if(!x.ok)return x;
  const order=first==='cdrom'?['cdrom','hd']:['hd','cdrom'];
  let body=x.out;
  const m=body.match(/<os\b[^>]*>[\s\S]*?<\/os>/);
  if(!m)return {ok:false,error:'The VM has no editable boot configuration'};
  const osBody=m[0].replace(/\s*<boot\s+dev=['"](?:hd|cdrom)['"]\s*\/>/g,'')
    .replace(/\s*<\/os>/,`\n    <boot dev="${order[0]}"/>\n    <boot dev="${order[1]}"/>\n  </os>`);
  body=body.replace(m[0],osBody);
  const dir=path.join(root(),d.name);await fs.promises.mkdir(dir,{recursive:true,mode:0o700});
  const file=path.join(dir,'domain-boot.xml');await fs.promises.writeFile(file,body,{mode:0o600});
  const r=await virsh(['define',file],30000);try{await fs.promises.unlink(file);}catch(_){}
  if(!r.ok)return r;
  const after=await details(d.name);
  return after.ok&&after.bootOrder===(first==='cdrom'?'cdrom':'disk')?{ok:true}: {ok:false,error:'Boot order did not persist'};
}
async function update(name, opts){
  const d=await details(name); if(!d.ok)return d;
  if(!/shut off|shutoff|inactive/.test(d.state)) return {ok:false,error:'Shut down the VM before changing its hardware'};
  const ram=Math.max(512,Math.min(65536,Number(opts&&opts.ramMiB)||d.ramMiB));
  const cpus=Math.max(1,Math.min(32,Number(opts&&opts.cpus)||d.cpus));
  for(const args of [['setmaxmem',d.name,`${ram}MiB`,'--config'],['setmem',d.name,`${ram}MiB`,'--config'],
    ['setvcpus',d.name,String(cpus),'--maximum','--config'],['setvcpus',d.name,String(cpus),'--config']]){
    const r=await virsh(args,30000); if(!r.ok)return r;
  }
  const a=await virsh(['autostart',d.name].concat(opts&&opts.autostart?[]:['--disable']));
  if(!a.ok)return a;
  if(opts&&opts.bootOrder){const b=await setBootOrder(d.name,opts.bootOrder);if(!b.ok)return b;}
  return details(d.name);
}
async function addDisk(name, gib){
  const d=await details(name); if(!d.ok)return d;
  if(!/shut off|shutoff|inactive/.test(d.state))return {ok:false,error:'Shut down the VM before adding a disk'};
  gib=Math.max(1,Math.min(2048,Number(gib)||20));
  const used=new Set(d.disks.map(x=>x.target)); let target='';
  for(const c of 'bcdefghijklmnopqrstuvwxyz')if(!used.has('vd'+c)){target='vd'+c;break;}
  if(!target)return {ok:false,error:'No virtual disk slots are available'};
  const dir=path.join(root(),d.name); await fs.promises.mkdir(dir,{recursive:true,mode:0o700});
  const file=path.join(dir,`disk-${target}.qcow2`); const q=await run('qemu-img',['create','-f','qcow2',file,`${gib}G`],30000);if(!q.ok)return q;
  const r=await virsh(['attach-disk',d.name,file,target,'--persistent','--subdriver','qcow2','--targetbus','virtio'],30000);
  if(!r.ok)try{await fs.promises.unlink(file);}catch(_){} return r.ok?details(d.name):r;
}
async function changeIso(name, iso){
  const d=await details(name);if(!d.ok)return d; iso=path.resolve(String(iso||''));
  try{if(!(await fs.promises.stat(iso)).isFile())throw Error();}catch(_){return {ok:false,error:'ISO file was not found'};}
  const cd=d.disks.find(x=>x.device==='cdrom'); if(!cd)return {ok:false,error:'This VM has no CD/DVD device'};
  /* --insert only works for an empty tray. A configured source (even a moved/missing file) must
   * be replaced with --update or libvirt leaves the VM permanently unable to start. */
  const mode=cd.source&&cd.source!=='-'?'--update':'--insert';
  return virsh(['change-media',d.name,cd.target,iso,mode,'--config'],30000);
}
async function ejectIso(name){
  const d=await details(name);if(!d.ok)return d;
  if(!/shut off|shutoff|inactive/.test(d.state))
    return {ok:false,error:'Shut down the VM before ejecting its installation disc'};
  const cd=d.disks.find(x=>x.device==='cdrom');
  if(!cd)return {ok:false,error:'This VM has no CD/DVD device'};
  /* --config changes the next boot, which is exactly what an installer test needs.  Verify by
   * reading the domain again: virsh success without a changed source is not an ejected disc. */
  const r=await virsh(['change-media',d.name,cd.target,'--eject','--config'],30000);if(!r.ok)return r;
  const after=await details(d.name);if(!after.ok)return after;
  const left=after.disks.find(x=>x.device==='cdrom' && x.source && x.source!=='-');
  if(left)return {ok:false,error:'The installation disc is still attached'};
  return setBootOrder(d.name,'disk');
}
async function bootDisk(name){
  let d=await details(name);if(!d.ok)return d;
  if(!/shut off|shutoff|inactive/.test(d.state)){
    const stop=await virsh(['shutdown',d.name],30000);if(!stop.ok)return stop;
    const deadline=Date.now()+90000;
    do{
      await new Promise(resolve=>setTimeout(resolve,1000));
      d=await details(d.name);if(!d.ok)return d;
      if(/shut off|shutoff|inactive/.test(d.state))break;
    }while(Date.now()<deadline);
    if(!/shut off|shutoff|inactive/.test(d.state))
      return {ok:false,error:'The guest did not shut down. Shut it down inside the VM, then try again.'};
  }
  const cd=d.disks.find(x=>x.device==='cdrom');
  const mounted=cd&&cd.source&&cd.source!=='-';
  /* One operation for the post-installer transition. An empty optical drive does not need another
   * virsh change-media (some libvirt versions reject ejecting empty media); it only needs disk-first
   * persisted in the domain XML. */
  return mounted?ejectIso(d.name):setBootOrder(d.name,'disk');
}
async function addNetwork(name){ const d=await details(name);if(!d.ok)return d;
  if(!/shut off|shutoff|inactive/.test(d.state))return {ok:false,error:'Shut down the VM before adding a network adapter'};
  const x=await virsh(['dumpxml',d.name]);if(!x.ok)return x;
  let body=x.out;
  const iface='      <interface type="user"><model type="e1000e"/></interface>';
  body=body.replace(/\s*<\/devices>/,`\n${iface}\n    </devices>`);
  const dir=path.join(root(),d.name);await fs.promises.mkdir(dir,{recursive:true,mode:0o700});
  const file=path.join(dir,'domain-network.xml');await fs.promises.writeFile(file,body,{mode:0o600});
  const r=await virsh(['define',file],30000);try{await fs.promises.unlink(file);}catch(_){}
  return r.ok?details(d.name):r; }
async function gamingMouse(name, enabled){
  const d=await details(name);if(!d.ok)return d;
  if(!/shut off|shutoff|inactive/.test(d.state))return {ok:false,error:'Shut down the VM before changing mouse mode'};
  const x=await virsh(['dumpxml',d.name]);if(!x.ok)return x;
  let body=x.out.replace(/\s*<input type=['"](?:tablet|mouse)['"] bus=['"](?:usb|ps2)['"]\s*\/>/g,'');
  const input=enabled?'      <input type="mouse" bus="ps2"/>':'      <input type="tablet" bus="usb"/>';
  body=body.replace(/\s*<\/devices>/,`\n${input}\n    </devices>`);
  const dir=path.join(root(),d.name);await fs.promises.mkdir(dir,{recursive:true,mode:0o700});
  const file=path.join(dir,'domain-edit.xml');await fs.promises.writeFile(file,body,{mode:0o600});
  const r=await virsh(['define',file],30000);try{await fs.promises.unlink(file);}catch(_){}
  return r.ok?{ok:true,gamingMouse:!!enabled}:r;
}
async function remove(name, disks){
  name=cleanName(name); if(!name) return {ok:false,error:'invalid VM name'};
  const r=await virsh(['undefine',name,'--nvram'],30000);
  if(!r.ok && !/no nvram/i.test(r.error)) return r;
  if(disks){
    const dir=path.join(root(),name);
    if(path.dirname(dir)===root()) await fs.promises.rm(dir,{recursive:true,force:true});
  }
  return {ok:true};
}
async function create(opts){
  const name=cleanName(opts && opts.name), iso=path.resolve(String(opts&&opts.iso||''));
  const ram=Math.max(512,Math.min(65536,Number(opts&&opts.ramMiB)||4096));
  const cpus=Math.max(1,Math.min(32,Number(opts&&opts.cpus)||2));
  const gib=Math.max(4,Math.min(2048,Number(opts&&opts.diskGiB)||40));
  const guest=opts&&opts.guest==='windows'?'windows':'linux';
  const firmware=opts&&opts.firmware==='bios'?'bios':'efi';
  if(!name) return {ok:false,error:'Give the VM a name'};
  try{ if(!(await fs.promises.stat(iso)).isFile()) throw Error(); }catch(_){ return {ok:false,error:'ISO file was not found'}; }
  const exists=await virsh(['dominfo',name]); if(exists.ok) return {ok:false,error:'A VM with that name already exists'};
  const dir=path.join(root(),name); await fs.promises.mkdir(dir,{recursive:true,mode:0o700});
  const disk=path.join(dir,'disk.qcow2');
  const q=await run('qemu-img',['create','-f','qcow2',disk,`${gib}G`],30000);
  if(!q.ok) return q;
  /* Let libvirt select a compatible edk2 image instead of hardcoding a distro-specific OVMF path.
   * Windows gets Secure Boot + TPM 2.0; Linux defaults to ordinary UEFI. Legacy BIOS remains a
   * deliberate choice for old installers. SPICE carries display AND audio to the lightweight
   * virt-viewer process, so no GTK virt-manager stack is involved. */
  const osXml=firmware==='efi'?`<os firmware="efi"><type arch="x86_64" machine="q35">hvm</type><firmware><feature enabled="${guest==='windows'?'yes':'no'}" name="secure-boot"/></firmware><boot dev="cdrom"/><boot dev="hd"/></os>`:
    `<os><type arch="x86_64" machine="q35">hvm</type><boot dev="cdrom"/><boot dev="hd"/></os>`;
  const tpm=guest==='windows'?'<tpm model="tpm-crb"><backend type="emulator" version="2.0"/></tpm>':'';
  const def=`<domain type="kvm"><name>${xml(name)}</name><uuid>${crypto.randomUUID()}</uuid>
    <memory unit="MiB">${ram}</memory><currentMemory unit="MiB">${ram}</currentMemory><vcpu>${cpus}</vcpu>
    ${osXml}
    <features><acpi/><apic/></features><cpu mode="host-passthrough" check="none"/>
    <devices><emulator>/usr/bin/qemu-system-x86_64</emulator>
      <disk type="file" device="disk"><driver name="qemu" type="qcow2"/><source file="${xml(disk)}"/><target dev="vda" bus="virtio"/></disk>
      <disk type="file" device="cdrom"><driver name="qemu" type="raw"/><source file="${xml(iso)}"/><target dev="sda" bus="sata"/><readonly/></disk>
      <!-- User-mode NAT requires no root bridge. e1000e is intentionally used for the installer
           path because Linux and Windows installation media carry it without an extra VirtIO
           driver disc; an unreachable guest cannot download that missing driver. -->
      <interface type="user"><model type="e1000e"/></interface>
      <!-- A virtio GPU without VirGL/3D advertises a DRM device but cannot initialize EGL. Sway
           then owns the display yet paints only black. Keep SPICE local-only (required for GL) and
           expose the accelerated renderer that both Linux desktops and Windows drivers expect. -->
      <graphics type="spice" autoport="yes"><listen type="none"/><gl enable="yes"/></graphics>
      <video><model type="virtio" heads="1" primary="yes"><acceleration accel3d="yes"/></model></video>
      <channel type="spicevmc"><target type="virtio" name="com.redhat.spice.0"/></channel>
      <!-- Absolute input is the safe desktop default: entering the viewer does not imprison the
           host pointer. Relative PS/2 capture remains an explicit gaming-mode choice. -->
      <input type="tablet" bus="usb"/><input type="keyboard" bus="usb"/><sound model="ich9"/><audio id="1" type="spice"/>${tpm}
    </devices></domain>`;
  const xf=path.join(dir,'domain.xml'); await fs.promises.writeFile(xf,def,{mode:0o600});
  const d=await virsh(['define',xf],30000); if(!d.ok){ await fs.promises.rm(dir,{recursive:true,force:true}); return d; }
  const s=await virsh(['start',name],30000); return s.ok?{ok:true,name}:s;
}
function launchViewer(command,args,start=spawn){
  return new Promise(resolve=>{
    let child,settled=false;
    const finish=result=>{ if(settled) return; settled=true; resolve(result); };
    const failed=error=>finish({ok:false,error:'Could not open VM display: '+String(error&&error.message||error)});
    try{
      child=start(command,args,{detached:true,stdio:'ignore'});
      child.once('error',failed);
      child.once('spawn',()=>{
        try{ child.unref(); }catch(_){}
        finish({ok:true});
      });
    }catch(error){ failed(error); }
  });
}
async function view(name){
  name=cleanName(name); if(!name) return {ok:false,error:'invalid VM name'};
  const bin=['virt-viewer','remote-viewer'].find(b=>{ try{return fs.existsSync('/usr/bin/'+b);}catch(_){return false;} });
  if(!bin) return {ok:false,error:'SPICE viewer is not installed (install app-emulation/virt-viewer)'};
  /* Session VMs expose SPICE through libvirt's private socket, not a public TCP endpoint. Attach
   * through libvirt's pre-opened descriptor; a plain viewer connection can start successfully and
   * still show no display. --wait also covers the short start/view race. */
  /* Keep the guest framebuffer and the GTK drawing area the same size.  Leaving this at the
   * viewer default is not cosmetic on a scaled/multi-monitor Wayland desktop: spice-gtk can draw
   * a scaled framebuffer while sending absolute tablet coordinates for the unscaled one.  The
   * pointer then appears over a button while the guest receives the click somewhere else.  A
   * host-drawn cursor also makes capture/release unambiguous when gaming mouse mode is enabled. */
  const viewerArgs=['--auto-resize=always','--cursor=local'];
  const args=bin==='virt-viewer'?[...viewerArgs,'--connect',URI,'--attach','--wait',name]:[];
  if(bin==='remote-viewer'){
    const d=await virsh(['domdisplay',name]); if(!d.ok) return d; args.push(...viewerArgs,d.out.trim());
  }
  /* Wait for Node's spawn acknowledgement.  Returning before it arrives makes an EACCES/ENOENT
   * race look successful to the UI even though no display was opened.  Use the path we inspected
   * above as well, rather than allowing a different PATH entry to win. */
  return launchViewer('/usr/bin/'+bin,args);
}
module.exports={available,list,details,update,addDisk,changeIso,ejectIso,bootDisk,addNetwork,gamingMouse,create,action,remove,view,launchViewer,cleanName,successorInstaller};
