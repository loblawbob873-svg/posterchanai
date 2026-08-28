"""Cross-monitor handoff must preserve a post's document window.

The two outputs run separate renderers.  A post window is opened as
``doc:post:<event id>`` while the client view painted inside it is ``thread``.  Recreating the
receiver as ``thread`` loses the document identity and can make the generic app path fall back to
Social before the post URL is routed.
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
OS = (ROOT / "static/js/client/os.js").read_text()


def _block(start: str, end: str) -> str:
    return OS[OS.index(start):OS.index(end, OS.index(start))]


def test_document_identity_wins_over_internal_thread_view():
    identity = _block("function handoffIdentity(w){", "function handoffPayload")
    doc = "if(opened.indexOf('doc:')===0) return opened;"
    addressed = "if(String(w&&w.appPath||'')"
    assert doc in identity
    assert identity.index(doc) < identity.index(addressed)


def test_receiver_builds_document_before_routing_exact_post_path():
    receive = _block("if(pcWM.onHandoffFrame)", "if(pcWM.onPreviewFrame)")
    create = "const w=openApp(String(p.view)"
    route = "PC().routePath && PC().routePath(String(p.path))"
    assert create in receive and route in receive
    assert receive.index(create) < receive.index(route)
    assert "p.path!=='/'" in receive and "p.path!=='/index.html'" in receive


def test_payload_carries_document_key_and_deep_path_together():
    payload = _block("function handoffPayload(w, overflow){", "function sendFrameHandoff")
    assert "const identity=handoffIdentity(w)" in payload
    assert "view:identity" in payload
    assert "path:topPath ? '' : appPath" in payload
