from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
OS = (ROOT / "static/js/client/os.js").read_text()
WIN = (ROOT / "static/js/client/oswin.js").read_text()


def test_existing_native_app_is_routed_before_it_is_focused():
    branch = OS.split("const mine = nativeTasks.find", 1)[1].split("const existing = wins.find", 1)[0]
    assert "PCOSWin.routeExisting(view)" in branch
    assert branch.index("PCOSWin.routeExisting(view)") < branch.index("pcWM.show")


def test_window_route_crosses_renderer_and_monitor_boundaries():
    assert "new root.BroadcastChannel(ROUTE_CHANNEL)" in WIN
    # THE RULE IS THAT THE VIEW CROSSES THE CHANNEL, not the exact shape of the object it rides in.
    # Pinning `postMessage({view:v})` broke the day the route also began carrying WHICH conversation
    # ("message this person" opens a window in another renderer, where `dmActive` does not exist) —
    # a strictly larger message, doing strictly more, failing a test about whether it is sent at all.
    assert "ch.postMessage(" in WIN and "view:v" in WIN
    assert "String(state.view||'')!==v" in WIN
    assert "root.__PC.switchView(v)" in WIN
    assert "routeExisting" in WIN.split("const API =", 1)[1]


def test_social_launcher_resets_native_timeline_but_focus_and_other_apps_do_not():
    import subprocess
    script='''
const vm=require('node:vm'),fs=require('node:fs'),assert=require('node:assert/strict');
const calls=[],channels=[];
const root={__PC_WIN_STATE__:{view:'global'},focus:()=>calls.push('focus'),
 __PC:{switchView:v=>calls.push('view:'+v),timelineTop:v=>calls.push('top:'+v)},
 BroadcastChannel:class{constructor(){channels.push(this)}}};
vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),root);
channels[0].onmessage({data:{view:'global'}});
assert.deepEqual(calls,['top:global','focus']);
calls.length=0;root.__PC_WIN_STATE__.view='trending';
channels[0].onmessage({data:{view:'trending'}});assert.deepEqual(calls,['top:trending','focus']);
calls.length=0;root.focus();assert.deepEqual(calls,['focus']);
calls.length=0;channels[0].onmessage({data:{view:'messages'}});assert.deepEqual(calls,[]);
root.__PC_WIN_STATE__.view='messages';channels[0].onmessage({data:{view:'messages'}});
assert.deepEqual(calls,['view:messages','focus']);
'''
    result=subprocess.run(['node','-e',script,str(ROOT/'static/js/client/oswin.js')],capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stderr


def test_in_page_social_launcher_resolves_preference_and_resets_existing_window():
    import subprocess
    body=OS[OS.index('  function openLauncherApp('):OS.index('  /* ✨ is a WINDOW-MANAGER')]
    script='''
const assert=require('node:assert/strict');const calls=[],windowObject={view:'home'},wins=[windowObject];
const sameAppWindow=(a,b)=>a===b;let target='home';
const _menuAct=()=>false,apps=()=>[{view:'global',label:'Social',icon:'social'}];
const PC=()=>({socialTimeline:()=> target,timelineTop:v=>calls.push('top:'+v)});
const openApp=(v)=>{calls.push('open:'+v);return windowObject};
'''+body+'''
assert.equal(openLauncherApp('global'),windowObject);
assert.deepEqual(calls,['open:home','top:home']);
calls.length=0;openLauncherApp('notes');assert.deepEqual(calls,['open:notes']);
calls.length=0;wins.length=0;openLauncherApp('global');
assert.deepEqual(calls,['open:home'],'new Home already resets in openApp');
calls.length=0;target='trending';openLauncherApp('global');
assert.deepEqual(calls,['open:trending','top:trending']);
'''
    result=subprocess.run(['node','-e',script],capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stderr
