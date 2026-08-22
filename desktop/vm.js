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
async function list(){
  const a=await available(); if(!a.available) return Object.assign(a,{machines:[]});
  const r=await virsh(['list','--all','--name']);
  const names=r.out.split(/\r?\n/).map(x=>x.trim()).filter(Boolean);
  const machines=[];
  for(const name of names){
    const i=await virsh(['dominfo',name]);
    const get=k => ((i.out.match(new RegExp('^'+k+':\\s*(.*)$','mi'))||[])[1]||'').trim();
    machines.push({name,state:get('State').toLowerCase(),memoryKiB:Number((get('Max memory').match(/\d+/)||[0])[0]),
      cpus:Number(get('CPU\\(s\\)'))||0,autostart:/enable/i.test(get('Autostart'))});
  }
  return {available:true,uri:URI,machines};
}
async function action(name, what){
  name=cleanName(name); if(!name) return {ok:false,error:'invalid VM name'};
  const map={start:['start'],shutdown:['shutdown'],reboot:['reboot'],stop:['destroy']};
  if(!map[what]) return {ok:false,error:'unknown action'};
  return virsh(map[what].concat(name),30000);
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
      <interface type="user"><model type="virtio"/></interface>
      <graphics type="spice" autoport="yes"><listen type="none"/></graphics><video><model type="virtio"/></video>
      <channel type="spicevmc"><target type="virtio" name="com.redhat.spice.0"/></channel>
      <input type="tablet" bus="usb"/><input type="keyboard" bus="usb"/><sound model="ich9"/><audio id="1" type="spice"/>${tpm}
    </devices></domain>`;
  const xf=path.join(dir,'domain.xml'); await fs.promises.writeFile(xf,def,{mode:0o600});
  const d=await virsh(['define',xf],30000); if(!d.ok){ await fs.promises.rm(dir,{recursive:true,force:true}); return d; }
  const s=await virsh(['start',name],30000); return s.ok?{ok:true,name}:s;
}
async function view(name){
  name=cleanName(name); if(!name) return {ok:false,error:'invalid VM name'};
  const bin=['virt-viewer','remote-viewer'].find(b=>{ try{return fs.existsSync('/usr/bin/'+b);}catch(_){return false;} });
  if(!bin) return {ok:false,error:'SPICE viewer is not installed (install app-emulation/virt-viewer)'};
  /* Session VMs expose SPICE through libvirt's private socket, not a public TCP endpoint. Attach
   * through libvirt's pre-opened descriptor; a plain viewer connection can start successfully and
   * still show no display. --wait also covers the short start/view race. */
  const args=bin==='virt-viewer'?['--connect',URI,'--attach','--wait',name]:[];
  if(bin==='remote-viewer'){
    const d=await virsh(['domdisplay',name]); if(!d.ok) return d; args.push(d.out.trim());
  }
  const p=spawn(bin,args,{detached:true,stdio:'ignore'}); p.unref(); return {ok:true};
}
module.exports={available,list,create,action,remove,view,cleanName};
