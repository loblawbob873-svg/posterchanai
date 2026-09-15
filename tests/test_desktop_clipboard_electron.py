"""Real Electron 44 clipboard publication and trusted native paste into shipped Texts (isolated Xvfb or Xwayland)."""
import os,json,runpy,time,hashlib,select,shutil
from pathlib import Path

def test_native_image_copy_pastes_into_texts(tmp_path):
    root=Path(__file__).resolve().parents[1]
    from tests.test_native_window_reload_electron import _native_popen,_stop_native,_missing_runtime
    binary=os.environ.get('PC_ELECTRON_BINARY',str(root/'desktop/node_modules/electron/dist/electron'))
    xvfb=shutil.which('Xvfb')
    if not Path(binary).is_file(): _missing_runtime('Electron binary required')
    if not xvfb and not (shutil.which('wayfire') and shutil.which('Xwayland')): _missing_runtime('Xvfb or Wayfire plus Xwayland required')
    out=tmp_path
    page=runpy.run_path(str(root/'tests/client/test_sms_composer_belongs_to_the_conversation.py'))['PAGE'].split('<script src="/sms.js"></script>')[0]
    page=page.replace('<div id="feed">','<link rel="stylesheet" href="/client.css"><div id="feed">')
    page+='''<script>window.sentRequests=0;__PC.publish=async()=>{sentRequests++;throw Error('No sends allowed');};__PC.uiConfirm=async()=>true;window.pcShell={windowContext:{role:'app',view:'texts'},backgroundOwner:false};</script><script src="/sms.js"></script>'''
    (out/'page.html').write_text(page)
    script=r'''
    const {app,BrowserWindow,protocol,clipboard,nativeImage,ClipboardItem}=require('electron');
    const fs=require('fs'),assert=require('assert/strict');
    assert.equal(process.versions.electron.split('.')[0],'44');
    app.setPath('userData',OUT+'/profile');
    protocol.registerSchemesAsPrivileged([{scheme:'app',privileges:{standard:true,secure:true,supportFetchAPI:true,stream:true}}]);
    let win;const timer=setTimeout(()=>{console.error('PROOF_TIMEOUT');app.exit(2)},25000);
    const sleep=ms=>new Promise(r=>setTimeout(r,ms));
    async function until(expression){for(let i=0;i<100;i++){if(await win.webContents.executeJavaScript(expression))return;await sleep(30);}throw Error('Timed out: '+expression);}
    app.whenReady().then(async()=>{
     protocol.handle('app',req=>{const path=new URL(req.url).pathname;
      const file=path==='/sms.js'?ROOT+'/static/js/client/sms.js':path==='/client.css'?ROOT+'/static/css/client.css':OUT+'/page.html';
      return new Response(fs.readFileSync(file),{headers:{'content-type':path.endsWith('.js')?'application/javascript':path.endsWith('.css')?'text/css':'text/html'}});});
     win=new BrowserWindow({width:850,height:650,show:true,webPreferences:{contextIsolation:true,nodeIntegration:false,sandbox:true}});
     await win.loadURL('app://posterchan/index.html?pcwin=texts');win.show();win.focus();
     await until('!!window.PCSms');
     await win.webContents.executeJavaScript("PCSms.openNotification({address:'+15550100'});void 0");
     await until("!!document.querySelector('#sms-in')");
     await win.webContents.executeJavaScript("document.querySelector('#sms-in').focus();window.realPastes=[];document.querySelector('#feed').addEventListener('paste',e=>realPastes.push({trusted:e.isTrusted,files:e.clipboardData.files.length,types:[...e.clipboardData.types]}),true);void 0");
     const image=nativeImage.createFromPath(ROOT+'/static/icon-192.png');
     assert(!image.isEmpty());await clipboard.clear();
     const vm=require('vm'),handlers={},source=fs.readFileSync(ROOT+'/desktop/main.js','utf8');
     vm.runInNewContext(source.slice(source.indexOf("ipcMain.handle('pc:clip:write-image'"),source.indexOf('/* CLIPBOARD READ')), {ipcMain:{handle:(name,fn)=>handlers[name]=fn},fromOurPage:()=>true,clipboard,Buffer,Blob,ArrayBuffer,console,require:p=>p==='electron'?require('electron'):{isWayland:()=>false}});
     assert.equal(await handlers['pc:clip:write-image']({},image.toPNG()),true);assert(await clipboard.has('image/png'));
     win.webContents.paste();
     await until("!!document.querySelector('.sms-draft-preview')&&document.querySelector('.sms-draft-preview').naturalWidth>0");
     const first=await win.webContents.executeJavaScript("({events:realPastes,preview:document.querySelector('.sms-draft-preview').naturalWidth,name:document.querySelector('.sms-attachment-draft b').textContent,sentRequests,origin:location.origin})");
     assert.equal(first.origin,'app://posterchan');assert.equal(first.preview,192);assert.equal(first.preview,image.getSize().width);assert.equal(first.events.length,1);assert.equal(first.events[0].trusted,true);assert.equal(first.events[0].files,1);assert.equal(first.sentRequests,0);
     console.log('ELECTRON_CLIPBOARD_PASTE_PASS '+JSON.stringify({electron:process.versions.electron,...first}));
     fs.writeFileSync(OUT+'/preview.png',(await win.webContents.capturePage()).toPNG());
     await win.webContents.executeJavaScript("document.querySelector('#sms-attach-clear').click();document.querySelector('#sms-in').focus();void 0");
     assert.equal(await win.webContents.executeJavaScript("!!document.querySelector('.sms-draft-preview')"),false);
     clearTimeout(timer);app.exit(0);
    }).catch(e=>{console.error(e);app.exit(1)});
    '''
    script=script.replace('OUT',json.dumps(str(out))).replace('ROOT',json.dumps(str(root)))
    (out/'main.cjs').write_text(script)
    runtime=out/'runtime';runtime.mkdir(mode=0o700,exist_ok=True)
    (out/'wayfire.ini').write_text('[core]\nplugins = ipc ipc-rules\nxwayland = false\n')
    env={**os.environ,'XDG_RUNTIME_DIR':str(runtime),'WLR_BACKENDS':'headless','WLR_HEADLESS_OUTPUTS':'1','WLR_RENDERER':'pixman','GDK_BACKEND':'wayland'}
    for k in ('ELECTRON_RUN_AS_NODE','WAYLAND_DISPLAY','DISPLAY','WAYFIRE_SOCKET','DBUS_SESSION_BUS_ADDRESS'):env.pop(k,None)
    processes=[]
    try:
     with (out/'compositor.log').open('w') as log:
      if not xvfb:
       p=_native_popen(['wayfire','-c',str(out/'wayfire.ini')],env=env,stdout=log,stderr=log);processes.append(p)
       deadline=time.monotonic()+30
       while not any(not x.name.endswith('.lock') for x in runtime.glob('wayland-*')):
        assert p.poll() is None,'compositor failed';assert time.monotonic()<deadline,'compositor timeout';time.sleep(.05)
       env['WAYLAND_DISPLAY']=next(x.name for x in runtime.glob('wayland-*') if not x.name.endswith('.lock'))
      rd,wr=os.pipe()
      try:
       x=_native_popen(([xvfb,'-displayfd',str(wr),'-screen','0','1024x768x24','-nolisten','tcp'] if xvfb else ['Xwayland','-displayfd',str(wr),'-nolisten','tcp','-ac','-noreset','-shm']),env=env,pass_fds=(wr,),stdout=log,stderr=log);processes.append(x)
       os.close(wr);wr=None
       assert select.select([rd],[],[],10)[0],'Xwayland readiness timeout'
       display=os.read(rd,32).decode().strip();assert display.isdigit()
      finally:
       os.close(rd)
       if wr is not None:os.close(wr)
      env.update(DISPLAY=':'+display,GDK_BACKEND='x11')
      with (out/'electron.log').open('w') as elog:
       e=_native_popen([binary,'--no-sandbox','--disable-gpu','--ozone-platform=x11',str(out/'main.cjs')],env=env,stdout=elog,stderr=elog);processes.append(e)
       deadline=time.monotonic()+30
       while e.poll() is None and time.monotonic()<deadline:
        time.sleep(.05)
       assert e.poll()==0,(out/'electron.log').read_text()[-6000:]
     print((out/'electron.log').read_text())
     print('sms_sha256',hashlib.sha256((root/'static/js/client/sms.js').read_bytes()).hexdigest())
    finally:
     for p in reversed(processes):_stop_native(p)
