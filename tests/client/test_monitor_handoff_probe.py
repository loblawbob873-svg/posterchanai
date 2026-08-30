from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_titlebar_has_no_move_to_monitor_button():
    source = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
    titlebar = source[source.index("el.innerHTML =", source.index("function openApp(")):
                      source.index("desk.appendChild(el)", source.index("function openApp("))]
    assert "osw-monitor" not in titlebar
    assert 'data-w="monitor"' not in titlebar
    assert "Move to other monitor" not in titlebar


def test_successful_frame_handoff_rearms_the_empty_source_for_the_return_trip():
    source = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
    block = source[source.index("function sendFrameHandoff"):
                   source.index("function nativeHandoffPlacement")]
    assert block.index("closeWin(w,{preserveFocus:true})") < block.index("rearmFrameHandoffDestination(pcWM)")
