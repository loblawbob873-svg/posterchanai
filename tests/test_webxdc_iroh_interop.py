import base64
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]
CONCORD = (ROOT / "static/js/client/concord.js").read_text()
WEBXDC = (ROOT / "static/js/client/webxdc.js").read_text()


def b32decode_unpadded(value: str) -> bytes:
    return base64.b32decode(value + "=" * (-len(value) % 8))


def test_armada_topic_and_node_address_wire_fixture():
    got = json.loads(subprocess.check_output(
        ["node", "tests/client/webxdc_iroh_runtime.mjs"], cwd=ROOT, text=True
    ))
    topic = "FBSTBOCHXLXTUPWPCLPAGBZDFNBSK5HKMTTMTVAKHY2EGNJFYMLQ"
    assert bytes(got["topicBytes"]) == b32decode_unpadded(topic)
    assert b32decode_unpadded(got["encoded"]).decode() == got["decoded"]
    assert got["payload"] == [0, 1, 2, 250, 255]
    assert got["frame"][-36:-32] == [0x12, 0x34, 0x56, 0x78]
    assert got["sender"] == bytes(range(32)).hex()
    assert got["signal"]["topic"] == topic


def test_concord_realtime_uses_iroh_only_and_cord04_signals():
    assert "PCWebxdcIroh.join(this.app.uuid,this.transport" in WEBXDC
    assert "if(this.rtIroh)" in WEBXDC
    assert "webxdcPeerPublish(ctx,JSON.stringify({op:'ad'" in (ROOT / "static/js/client/webxdc-iroh.js").read_text()
    assert "createWebxdcWrap(x.bundle,x.controls,x.channel.id,content,viewer.pubkey,x.p.signTemplate,[],false)" in CONCORD
    assert "inspectWebxdcSignals" in CONCORD


def test_current_topic_tag_wins_and_new_uploads_mint_interop_topic():
    assert "uuid:f['webxdc-topic']||f.webxdc" in CONCORD
    assert "`webxdc-topic ${topic}`" in CONCORD
    assert "application/vnd.webxdc+zip" in CONCORD


def test_wasm_transport_is_shipped_not_optional_at_runtime():
    js = ROOT / "static/vendor/webxdc-rt/webxdc_rt.js"
    wasm = ROOT / "static/vendor/webxdc-rt/webxdc_rt_bg.wasm"
    assert js.stat().st_size > 10_000
    assert wasm.stat().st_size > 1_000_000
    assert "/static/vendor/webxdc-rt/webxdc_rt_bg.wasm" in js.read_text()
    assert '<script src="/static/js/client/webxdc-iroh.js?v={{ ver }}"></script>' in (ROOT / "templates/client.html").read_text()
