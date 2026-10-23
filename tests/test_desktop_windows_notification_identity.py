"""THE DESKTOP APP'S MAIN PROCESS: WHAT IT ANSWERS WHEN A PAGE ASKS TO SHOW A NOTIFICATION.

Reported 2026-10-22: "I am not seeing any SMS notifications on OS and windows app". The renderer half
is driven end to end by tests/client/test_sms_phone_to_desktop_notification_full_app.py against
stand-ins for these two IPC answers; this file runs the SHIPPED handlers in desktop/main.js so the
stand-ins cannot drift from them:

* `pc:toast:show` (the always-on-top card window, desktop/toast-host.js) answers `false` outside
  PosterChanOS -- which is why a card handed to it on the Windows app was shown nowhere -- and
* `pc:host:notify` answers `false` exactly under --shell, where the desktop's own card is the
  notification, and shows a native one everywhere else;
* on Windows the process declares the AppUserModelID the NSIS installer stamps on the Start-menu
  shortcut (package.json build.appId). Without it Electron uses `electron.app.<name>`, which no
  shortcut carries, and Windows silently drops every toast. Only a real Windows machine can show the
  toast itself; this proves the ID is declared, before ready, and is the one the installer writes.
"""
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN = (ROOT / "desktop" / "main.js").read_text(encoding="utf-8")


def _node(script):
    r = subprocess.run(["node", "--input-type=module", "-"], cwd=ROOT, input=script,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stdout + r.stderr


def test_windows_declares_the_installers_app_id_before_ready():
    app_id = json.loads((ROOT / "desktop" / "package.json").read_text())["build"]["appId"]
    start = MAIN.index("const WINDOWS_APP_ID")
    block = MAIN[start:MAIN.index("\n", MAIN.index("setAppUserModelId", start)) + 1]
    # Declared at module load (Electron's pre-ready phase), not inside whenReady or a window.
    assert start < MAIN.index("app.whenReady(")
    _node(r'''
import vm from 'node:vm';import assert from 'node:assert/strict';
const block=%s;
for(const platform of ['win32','linux','darwin']){
  const set=[];
  vm.runInNewContext(block,{process:{platform},app:{setAppUserModelId:id=>set.push(id)}});
  assert.deepEqual(set,platform==='win32'?[%s]:[],platform);
}
''' % (json.dumps(block), json.dumps(app_id)))


def test_the_card_window_refuses_outside_posterchanos_and_native_refuses_inside():
    def handler(name, until):
        s = MAIN.index("ipcMain.handle('%s'" % name)
        return MAIN[s:MAIN.index(until, s)]
    toast = handler("pc:toast:show", "ipcMain.on('pc:toast:click'")
    notify = handler("pc:host:notify", "ipcMain.handle('pc:host:pickDirectory'")
    _node(r'''
import vm from 'node:vm';import assert from 'node:assert/strict';
for(const shell of [true,false]){
  const shown=[];let toastH,notifyH;
  class Notification{static isSupported(){return true}constructor(o){this.o=o}on(){}show(){shown.push(this.o.title)}}
  const sender={id:1,isDestroyed:()=>false,send(){},once(){}};
  const scope={SHELL_MODE:shell,fsGuard(){},electron:{Notification},BrowserWindow:{fromWebContents:()=>null},win:null,
    path:{join:()=>'/icon.png'},__dirname:'/',toastHost:{show:()=>{shown.push('card');return true},ownerGone(){}},
    ipcMain:{handle(n,fn){if(n==='pc:toast:show')toastH=fn;else notifyH=fn;}}};
  vm.runInNewContext(%s+%s,scope);
  assert.equal(toastH({sender},{id:'t1',html:'<b>+15550142</b>'}),shell?true:false,'card window');
  assert.equal(notifyH({sender},{title:'+15550142',body:'hi'}),!shell,'native notification');
  assert.deepEqual(shown,shell?['card']:['+15550142']);
}
''' % (json.dumps(toast), json.dumps(notify)))
