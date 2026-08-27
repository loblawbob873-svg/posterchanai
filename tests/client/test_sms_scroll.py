import json
import pathlib
import subprocess


ROOT = pathlib.Path(__file__).resolve().parents[2]
SMS = (ROOT / "static/js/client/sms.js").read_text()
OS = (ROOT / "static/js/client/os.js").read_text()


def test_texts_scroll_runtime_preserves_reader_and_bottom_pin():
    out = subprocess.check_output(
        ["node", str(ROOT / "tests/client/sms_sim.js"), json.dumps({"scrollProbe": True})],
        text=True,
    )
    probe = json.loads(out)["scrollProbe"]
    assert probe["savedReading"] == {"top": 240, "bottom": False}
    assert probe["restoredReading"] == 240
    assert probe["savedPinned"]["bottom"] is True
    assert probe["restoredPinned"] == 1600


def test_texts_repaint_and_desktop_parking_use_the_same_scroll_contract():
    assert 'data-thread-key="${enc(t.key)}"' in SMS
    assert "S.scroll[oldKey] = scrollState(oldList)" in SMS
    assert "putScroll(list, saved)" in SMS
    assert "if(list.dataset.osParking === '1') return" in SMS
    assert "restoreHydratedScroll(l, before)" in SMS
    assert "['sms-msgs','#sms-msgs']" in OS


def test_stale_attachment_hydration_cannot_move_a_repainted_conversation():
    hydration = SMS.split("hydrateAtt(feed, t.msgs).then(() => {", 1)[1].split(
        "}, () => {});", 1
    )[0]
    guard = "if(!l || l !== list || !before) return"
    assert guard in hydration
    assert hydration.index(guard) < hydration.index("restoreHydratedScroll(l, before)")


def test_attachment_hydration_anchors_the_visible_message_not_total_height():
    out = subprocess.check_output(
        ["node", str(ROOT / "tests/client/sms_sim.js"),
         json.dumps({"hydrationProbe": True})], text=True,
    )
    probe = json.loads(out)["hydrationProbe"]
    assert probe["belowTop"] == 240, "media below the reader moved the thread"
    assert probe["aboveTop"] == 440, "media above the reader did not preserve its message anchor"
