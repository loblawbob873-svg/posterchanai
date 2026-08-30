from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
OS = (ROOT / "static/js/client/os.js").read_text()
XDC = (ROOT / "static/js/client/webxdc.js").read_text()


def test_webxdc_window_exports_reconstructible_monitor_state():
    block = XDC[XDC.index("const w = PCOS.openDoc('webxdc:"):
                XDC.index("host.classList.add('xdc-slot')")]
    assert "w.handoffState = () => ({ app:" in block
    for field in ("url:", "sha:", "uuid:", "urlTopicMessageId:", "name:", "transport:"):
        assert field in block
    assert "Object.assign({},app.transport)" in block


def test_webxdc_destination_reopens_game_before_generic_document_fallback():
    receive = OS[OS.index("if(pcWM.onHandoffFrame)"):
                 OS.index("if(pcWM.onPreviewFrame)")]
    special = receive.index("/^doc:webxdc:/.test")
    generic = receive.index("const w=reconstructHandoffWindow(p)")
    assert special < generic
    assert "PCWebxdc.acceptHandoff(p.state)" in receive
    assert "catch(()=>{});return;" in receive[special:generic]
    moved = receive[special:generic]
    assert "Number(p.width)" in moved and "Number(p.height)" in moved
    assert "keepFrameReachable(moved)" in moved
    assert "restoreHandoffUI(moved,p.ui)" in moved


def test_webxdc_accept_handoff_reuses_normal_open_path_and_room_identity():
    block = XDC[XDC.index("async function acceptHandoff(state)"):
                XDC.index("window.PCWebxdc =", XDC.index("async function acceptHandoff(state)"))]
    assert "return open(app)" in block
    assert "acceptHandoff" in XDC[XDC.index("window.PCWebxdc ="):]
    assert "session.window = w" in XDC
