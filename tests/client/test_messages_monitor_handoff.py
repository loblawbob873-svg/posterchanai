import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
OS = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
CONCORD = (ROOT / "static/js/client/concord.js").read_text(encoding="utf-8")
APP = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")


def test_messages_handoff_keeps_the_selected_communities_tab():
    assert "(opened==='messages'||opened==='concord')" in OS
    assert "if(opened==='messages'||opened==='concord') return 'messages'" in OS
    assert "messagesTab==='concord'" in OS


def test_community_and_channel_identity_cross_to_the_other_renderer():
    assert "handoffState,acceptHandoff" in CONCORD
    assert "PCConcord.handoffState()" in OS
    assert "PCConcord.acceptHandoff(p.state)" in OS
    assert "room.communityId||room.naddr||room.url" in CONCORD
    assert "channel:state.channel||'general'" in CONCORD


def test_cold_destination_adopts_communities_state_before_first_render():
    run = subprocess.run(
        ["node", str(ROOT / "tests/client/messages_handoff_destination_runtime.mjs")],
        capture_output=True, text=True, timeout=30,
    )
    assert run.returncode == 0, run.stderr
    assert "messages handoff destination runtime: ok" in run.stdout


def test_direct_messages_carries_the_selected_conversation():
    assert "messagesHandoffState: () => ({peer:dmActive||''})" in APP
    assert "acceptMessagesHandoff: value =>" in APP
    assert "messagesTab==='messages'&&PC().messagesHandoffState" in OS
    assert "PC().acceptMessagesHandoff(p.state)" in OS


def test_handoff_uses_one_canonical_messages_window_then_restores_its_tab():
    assert "return {view:identity,messagesTab" in OS
    assert "if(p.messagesTab==='concord'||p.messagesTab==='messages')" in OS
    assert "w.appView=p.messagesTab;w.messagesTab=p.messagesTab;w.appPath='';repainting++" in OS
    assert "PC().switchView&&PC().switchView(p.messagesTab)" in OS


def test_chat_scroll_pin_crosses_with_the_messages_window():
    assert "pinned:scroll.pinned!==false" in CONCORD
    assert "writeScroll(key,st)" in CONCORD


def test_dragged_communities_window_is_reused_when_direct_messages_is_clicked():
    """Exact regression: Messages → Communities → other monitor → Direct remains one frame."""
    assert "if(p.messagesTab==='concord'||p.messagesTab==='messages')" in OS
    opened = OS[OS.index("function openApp(view, label, icon, render, noFeed, direct)"):]
    opened = opened[:opened.index("function ", 20)]
    assert "wins.find(w => sameAppWindow(w.view, view))" in opened
    assert "!shouldSelectMessagesTab(existing, view)" in opened
    assert "focusWin(existing, false); existing.appView = view" in opened
    assert "PC().switchView && PC().switchView(view)" in opened


def test_handed_off_communities_frame_selects_direct_messages_at_runtime():
    """The destination frame is canonical `messages`, but its restored tab is `concord`."""
    os_js = ROOT / "static/js/client/os.js"
    boot = f"""
global.window = {{}};
global.document = {{ addEventListener(){{}}, querySelector(){{ return null; }},
                    querySelectorAll(){{ return []; }} }};
global.getComputedStyle = () => ({{ zoom: '1' }});
require({json.dumps(str(os_js))});
const select = window.PCOS.__shouldSelectMessagesTab;
console.log(JSON.stringify([
  select({{view:'messages', appView:'concord'}}, 'messages'),
  select({{view:'messages', appView:'messages'}}, 'messages'),
  select({{view:'messages', appView:'messages'}}, 'concord'),
  select({{view:'concord', appView:'concord'}}, 'messages'),
  select({{view:'mail', appView:'mail'}}, 'messages')
]));
"""
    run = subprocess.run(["node", "-e", boot], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout) == [True, False, True, True, False]


def test_handoff_reads_the_live_tab_when_the_frame_owns_the_feed():
    """Click Communities and immediately press the titlebar monitor action."""
    os_js = ROOT / "static/js/client/os.js"
    boot = f"""
const feed={{}}; const body={{querySelector:s=>s==='#feed'?feed:null}};
global.window={{__PC:{{VIEW:'concord'}}}};
global.document={{ addEventListener(){{}}, querySelector(s){{return s==='#feed'?feed:null;}},
                  querySelectorAll(){{return [];}} }};
global.getComputedStyle=()=>({{zoom:'1'}});
require({json.dumps(str(os_js))});
const selected=window.PCOS.__selectedMessagesTab;
console.log(JSON.stringify([
  selected({{view:'messages',appView:'messages',body}}),
  selected({{view:'messages',appView:'messages',body:{{querySelector:()=>null}}}})
]));
"""
    run = subprocess.run(["node", "-e", boot], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout) == ["concord", "messages"]


def test_messages_identity_and_tab_survive_a_stale_cross_window_repaint():
    """Communities -> move -> Direct must not become a new Social/DM popout."""
    os_js = ROOT / "static/js/client/os.js"
    boot = f"""
global.window={{__PC:{{VIEW:'global'}}}};
global.document={{addEventListener(){{}},querySelector(){{return null;}},querySelectorAll(){{return [];}}}};
global.getComputedStyle=()=>({{zoom:'1'}});
require({json.dumps(str(os_js))});
const identity=window.PCOS.__handoffIdentity;
const selected=window.PCOS.__selectedMessagesTab;
const moved={{view:'messages',appView:'global',messagesTab:'concord',body:{{querySelector:()=>null}}}};
console.log(JSON.stringify([identity(moved),selected(moved)]));
"""
    run = subprocess.run(["node", "-e", boot], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout) == ["messages", "concord"]


def test_destination_records_restored_tab_on_the_window_not_only_global_view():
    receive = OS[OS.index("if(pcWM.onHandoffFrame)"):OS.index("if(pcWM.onPreviewFrame)")]
    assert "w.appView=p.messagesTab;w.messagesTab=p.messagesTab;w.appPath=''" in receive
    assert "if(v==='messages'||v==='concord')w.messagesTab=v" in OS


def test_main_process_forwards_only_the_two_messages_tabs():
    main = (ROOT / "desktop/main.js").read_text(encoding="utf-8")
    assert "messagesTab:String(p.view||'')==='messages'&&" in main
    assert "(p.messagesTab==='concord'||p.messagesTab==='messages')?p.messagesTab:''" in main
