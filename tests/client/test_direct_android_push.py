"""Regression coverage for PosterChan's first-party Android notification transport."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
APP = (ROOT / "static/js/client/app.js").read_text(errors="replace")
GRADLE = (ROOT / "mobile/android/app/build.gradle").read_text(errors="replace")
MANIFEST = (ROOT / "mobile/android/app/src/main/AndroidManifest.xml").read_text(errors="replace")


def test_native_registration_is_device_bound_and_signed():
    assert "posterchan-direct:${action}:${deviceId}" in APP
    assert "'/api/push/direct/register'" in APP
    assert "device_id:deviceId" in APP
    assert "issued.token" in APP
    assert "issued.websocket_url" in APP
    assert "_directPushAuth('unregister',deviceId)" in APP


def test_native_revocation_is_authenticated_before_local_credentials_are_erased():
    block = APP[APP.index("async function disablePush()") : APP.index("async function _wirePushToggle()")]
    assert "_directPushAuth('unregister',deviceId)" in block
    assert "'/api/push/direct/unregister'" in block
    assert block.index("/api/push/direct/unregister") < block.index("P.unregister()")


def test_browser_web_push_remains_supported():
    assert "reg.pushManager.subscribe" in APP
    assert "'/api/push/subscribe'" in APP
    assert "applicationServerKey:_urlB64ToUint8(publicKey)" in APP


def test_distributor_transport_is_not_shipped():
    combined = APP + GRADLE + MANIFEST
    assert "UnifiedPush" not in combined
    assert "needsDistributor" not in combined
    assert "org.unifiedpush" not in combined
    assert "ntfy" not in combined.lower()
