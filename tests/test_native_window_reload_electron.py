import json
import subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def _missing_runtime(reason):
    import os
    import pytest
    if os.environ.get('PC_REQUIRE_NATIVE_IPC_TEST') == '1':
        pytest.fail(reason)
    pytest.skip(reason)

def test_real_electron_sync_reply_survives_native_child_reload(tmp_path):
    """A real sendSync reply is sent at assignment, unlike an ordinary JS fixture property."""
    import os
    import pytest
    import shutil,time,signal,select
    candidates = [Path(os.environ.get('PC_ELECTRON_BINARY','/nonexistent-electron')),
                  ROOT/'desktop/node_modules/electron/dist/electron',
                  ROOT.parent.parent/'desktop/node_modules/electron/dist/electron']
    electron = next((p for p in candidates if p.is_file()), None)
    if electron is None:
        _missing_runtime('Electron runtime is required for native IPC lifecycle coverage')
    main = Path(os.environ.get('PC_NATIVE_MAIN_SOURCE',ROOT/'desktop/main.js')).read_text()
    handler = main[main.index("ipcMain.on('pc:window:context'"):main.index('/* ── THE COMPOSITOR', main.index("ipcMain.on('pc:window:context'"))]
    pre = (ROOT/'desktop/preload.js').read_text()
    role = pre[pre.index('const windowContext ='):pre.index('// Clipboard WRITE,', pre.index('const windowContext ='))]
    preload = tmp_path/'preload.cjs'
    preload.write_text("const {ipcRenderer,contextBridge}=require('electron');const isOurPage=location.protocol==='app:'&&location.hostname==='posterchan';\n" + role + "\ncontextBridge.exposeInMainWorld('roleProbe',{windowContext,backgroundOwner});contextBridge.exposeInMainWorld('nativeIPCProbe',{readContext:()=>ipcRenderer.sendSync('pc:window:context')});")
    (tmp_path/'profile').mkdir()
    script = r'''
console.log('ELECTRON_VERSION='+process.versions.electron);
process.on('uncaughtException',error=>{console.error(error);process.exit(3)});
const {app,BrowserWindow,ipcMain,protocol}=require('electron');
const assert=require('node:assert/strict');
app.setPath('userData',USERDATA);
protocol.registerSchemesAsPrivileged([{scheme:'app',privileges:{standard:true,secure:true,supportFetchAPI:true}}]);
const pcAppWindows=new Map();
const {isTrustedPage}=require(TRUST);
const fsGuard=e=>{if(!isTrustedPage(e.senderFrame.url,LOCALDIR))throw Error('denied')};
HANDLER
let owner,child;
const watchdog=setTimeout(()=>{console.error('Native lifecycle timed out');app.exit(2)},15000);
app.whenReady().then(async()=>{
 protocol.handle('app',()=>new Response('<!doctype html><html><body>Native role probe</body></html>',{headers:{'content-type':'text/html'}}));
 owner=new BrowserWindow({show:false,webPreferences:{preload:PRELOAD,contextIsolation:true}});
 owner.webContents.setWindowOpenHandler(()=>({action:'allow',overrideBrowserWindowOptions:{show:false,webPreferences:{preload:PRELOAD,contextIsolation:true}}}));
 owner.webContents.on('did-create-window',(win,details)=>{child=win;pcAppWindows.set(new URL(details.url).searchParams.get('pcwin'),win)});
 await owner.loadURL('app://posterchan/index.html');
 assert.deepEqual(await owner.webContents.executeJavaScript('window.roleProbe'),{windowContext:null,backgroundOwner:true});
 assert.equal(await owner.webContents.executeJavaScript('window.nativeIPCProbe.readContext()'),null,'unmanaged sender');
 await owner.webContents.executeJavaScript("window.open('app://posterchan/index.html?pcwin=settings');void 0");
 while(!child||child.webContents.isLoading())await new Promise(r=>setTimeout(r,20));
 assert.deepEqual(await child.webContents.executeJavaScript('window.roleProbe'),{windowContext:{role:'app',view:'settings'},backgroundOwner:false});
 console.log('COLD_CHILD_PASS');
 await child.webContents.executeJavaScript("history.replaceState({},'', '/');void 0");
 await new Promise((resolve,reject)=>{child.webContents.once('did-finish-load',resolve);child.webContents.once('did-fail-load',()=>reject(Error('load failed')));child.reload()});
 assert.deepEqual(await child.webContents.executeJavaScript('window.roleProbe'),{windowContext:{role:'app',view:'settings'},backgroundOwner:false});
 assert.equal(child.webContents.getURL(),'app://posterchan/');
 assert.deepEqual(await child.webContents.executeJavaScript('window.nativeIPCProbe.readContext()'),{role:'app',view:'settings'});
 console.log('RELOADED_CHILD_PASS');
 await child.loadURL('app://untrusted/index.html');
 assert.equal(await child.webContents.executeJavaScript('window.nativeIPCProbe.readContext()'),null,'untrusted frame in registered child');
 console.log('REAL_NATIVE_RELOAD_PASS');clearTimeout(watchdog);app.exit(0);
}).catch(e=>{console.error(e);app.exit(1)});
'''
    for name,value in {'USERDATA':str(tmp_path/'profile'),'TRUST':str(ROOT/'desktop/page-trust.js'), 'LOCALDIR':str(ROOT/'desktop'),'PRELOAD':str(preload)}.items():
        script=script.replace(name,json.dumps(value))
    script=script.replace('HANDLER',handler)
    entry=tmp_path/'main.js';entry.write_text(script)
    xvfb=shutil.which('Xvfb')
    if not xvfb and not all(shutil.which(binary) for binary in ('wayfire','Xwayland')):
        _missing_runtime('Xvfb or Wayfire plus Xwayland is required for the isolated Electron display')
    runtime=tmp_path/'runtime';runtime.mkdir(mode=0o700)
    config=tmp_path/'wayfire.ini';config.write_text('[core]\nplugins = ipc ipc-rules\nxwayland = false\n')
    env={**os.environ,'XDG_RUNTIME_DIR':str(runtime),'WLR_BACKENDS':'headless','WLR_HEADLESS_OUTPUTS':'1','WLR_RENDERER':'pixman','GDK_BACKEND':'wayland'}
    for key in ('ELECTRON_RUN_AS_NODE','WAYLAND_DISPLAY','DISPLAY','WAYFIRE_SOCKET','DBUS_SESSION_BUS_ADDRESS'):
        env.pop(key,None)
    with (tmp_path/'compositor.log').open('w') as log:
        compositor=None
        native=None
        xserver=None
        try:
            if not xvfb:
                compositor=subprocess.Popen(['wayfire','-c',str(config)],env=env,stdout=log,stderr=log,start_new_session=True)
                # A REAL COMPOSITOR STARTING ON A BUSY MACHINE. Ten seconds is plenty when this
                # file runs alone (it comes up in ~2s) and not enough inside the full suite, which
                # runs ~100 browser checks beside it — measured: these tests failed the gate on
                # 'fixture compositor failed to start' and then passed in isolation, 5 in 2.49s.
                # A test that only passes on an idle box reports the load, not the code.
                deadline=time.monotonic()+60
                while not [p for p in runtime.glob('wayland-*') if not p.name.endswith('.lock')]:
                    assert compositor.poll() is None,(tmp_path/'compositor.log').read_text()
                    assert time.monotonic()<deadline,(
                        'fixture compositor failed to start within 60s: '
                        + (tmp_path/'compositor.log').read_text()[-800:])
                    time.sleep(.05)
                env['WAYLAND_DISPLAY']=next(p.name for p in runtime.glob('wayland-*') if not p.name.endswith('.lock'))
            readfd,writefd=os.pipe()
            command=([xvfb,'-displayfd',str(writefd),'-screen','0','1280x800x24','-nolisten','tcp','-ac','-noreset'] if xvfb else
                     ['Xwayland','-displayfd',str(writefd),'-nolisten','tcp','-ac','-noreset','-shm'])
            xserver=subprocess.Popen(command,env=env,pass_fds=(writefd,),stdout=log,stderr=log,start_new_session=True)
            os.close(writefd)
            try:
                assert select.select([readfd],[],[],10)[0], 'isolated X display did not start'
                display=os.read(readfd,32).decode().strip()
                assert display.isdigit(), 'invalid isolated X display display'
            finally:os.close(readfd)
            env['DISPLAY']=':'+display
            env['GDK_BACKEND']='x11'
            native=subprocess.Popen([str(electron),'--no-sandbox','--disable-gpu','--ozone-platform=x11',str(entry)],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,start_new_session=True)
            try:stdout,stderr=native.communicate(timeout=25)
            except subprocess.TimeoutExpired as error:
                raise AssertionError((error.stdout or b'').decode(errors='replace')+'\n'+(error.stderr or b'').decode(errors='replace')) from error
            (tmp_path/'electron.log').write_text(stdout+'\n'+stderr)
            assert native.returncode==0,stdout+'\n'+stderr
            assert 'REAL_NATIVE_RELOAD_PASS' in stdout
        finally:
            for proc in (native,xserver,compositor):
                if proc is not None:
                    try:os.killpg(proc.pid,signal.SIGTERM)
                    except ProcessLookupError:pass
                    try:proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:os.killpg(proc.pid,signal.SIGKILL);proc.wait()
