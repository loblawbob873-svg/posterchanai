"""The role screen must describe the carrier MMS support the APK actually ships."""

from pathlib import Path


ROOT = Path(__file__).parents[2]


def test_phone_role_screen_does_not_claim_mms_is_unsupported():
    shell = (ROOT / "static/js/client/phoneshell.js").read_text(encoding="utf-8")
    plugin = (ROOT / "mobile/android/app/src/main/java/place/poster/app/sms/SmsPlugin.java").read_text(
        encoding="utf-8"
    )
    assert 'o.put("mmsFetch", true)' in plugin
    assert "MMS (picture messages) is not supported" not in shell
    assert "encrypted originals" in shell
    assert "MMS</strong> folder" in shell
