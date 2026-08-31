"""PosterChan Direct must stay first-party, native, authenticated and restartable."""
from pathlib import Path


ROOT = Path(__file__).parents[1]
ANDROID = ROOT / "mobile/android"
JAVA = ANDROID / "app/src/main/java/place/poster/app"
PLUGIN = (JAVA / "push/PushPlugin.java").read_text()
SERVICE = (JAVA / "push/DirectPushService.java").read_text()
STORE = (JAVA / "push/DirectPushStore.java").read_text()
EVENTS = (JAVA / "push/PushEventService.java").read_text()
BOOT = (JAVA / "push/BootReceiver.java").read_text()
NOTE = (JAVA / "RunningNote.java").read_text()
MANIFEST = (ANDROID / "app/src/main/AndroidManifest.xml").read_text()
GRADLE = (ANDROID / "app/build.gradle").read_text()


def test_no_distributor_or_gcompat_runtime_remains():
    joined = "\n".join((PLUGIN, EVENTS, MANIFEST, GRADLE))
    assert "org.unifiedpush" not in joined.lower()
    assert "PUSH_EVENT" not in MANIFEST
    assert "PushEventService extends" not in EVENTS
    assert "UnifiedPush.getDistributors" not in PLUGIN


def test_stable_device_id_exists_before_registration_without_exposing_secrets():
    assert "DirectPushStore.deviceId(getContext())" in PLUGIN
    endpoint_body = PLUGIN[PLUGIN.index("public void getEndpoint"):PLUGIN.index(
        "public void unregister")]
    assert 'out.put("deviceId", device)' in endpoint_body
    assert 'configured ? "pcdirect:" + device : ""' in endpoint_body
    assert 'out.put("token"' not in endpoint_body
    assert 'out.put("socketUrl"' not in endpoint_body


def test_registration_seals_credentials_and_starts_native_service():
    assert "DirectPushStore.save(ctx, socketUrl, token, stable)" in PLUGIN
    assert "DirectPushService.kick(ctx)" in PLUGIN
    assert 'auth.put("type", "auth")' in SERVICE
    assert 'auth.put("token", credentials.token)' in SERVICE
    assert "AndroidKeyStore" in STORE
    assert "AES/GCM/NoPadding" in STORE
    assert "setUserAuthenticationRequired(false)" in STORE
    assert 'putString("token"' not in STORE


def test_transport_reconnects_but_does_not_retry_revoked_credentials():
    assert "MAX_BACKOFF_MS" in SERVICE
    assert "handler.postDelayed(this::connectNow, delay)" in SERVICE
    assert "START_STICKY" in SERVICE
    assert "code == 1008" in SERVICE
    assert "DirectPushStore.clear(DirectPushService.this)" in SERVICE
    assert "connectionPool().evictAll()" in SERVICE


def test_control_frames_never_become_user_notifications():
    for frame in ('"ping"', '"pong"', '"ready"', '"auth-ok"', '"ack"'):
        assert frame in SERVICE
    assert "PushEventService.deliver(DirectPushService.this, text)" in SERVICE
    assert '"notification".equals(type)' in SERVICE
    assert '!"notification".equals(type)' in SERVICE
    assert 'j.optJSONObject("payload")' in EVENTS


def test_delivery_is_persisted_then_acknowledged_and_replays_are_suppressed():
    message = SERVICE[SERVICE.index("@Override public void onMessage"):SERVICE.index(
        "@Override public void onClosing")]
    duplicate = message.index("DirectPushStore.wasDelivered")
    duplicate_ack = message.index("sendAck(webSocket, deliveryId)", duplicate)
    display = message.index("PushEventService.deliver", duplicate_ack)
    persist = message.index("DirectPushStore.markDelivered", display)
    new_ack = message.index("sendAck(webSocket, deliveryId)", persist)
    assert duplicate < duplicate_ack < display, "a replay is displayed before it is ACKed"
    assert display < persist < new_ack, "a new delivery is ACKed before display and durable receipt"
    assert 'ack.put("type", "ack")' in SERVICE
    assert 'long numericId = Long.parseLong(id)' in SERVICE
    assert 'ack.put("id", numericId)' in SERVICE


def test_receipt_ledger_survives_process_death_and_is_bounded():
    assert 'private static final int MAX_RECEIPTS = 256' in STORE
    assert 'putString(RECEIPTS, encoded.toString()).commit()' in STORE
    assert 'while (ids.size() > MAX_RECEIPTS)' in STORE
    assert 'encoded.length() - MAX_RECEIPTS' in STORE
    assert 'id.length() > 256' in STORE


def test_boot_and_foreground_contract_are_complete():
    assert "DirectPushService.configured(ctx)" in BOOT
    assert "DirectPushService.kick(ctx)" in BOOT
    assert 'android:name=".push.DirectPushService"' in MANIFEST
    assert 'android:foregroundServiceType="specialUse"' in MANIFEST
    assert "RunningNote.ID" in SERVICE and "RunningNote.build(this)" in SERVICE
    assert "DirectPushService.running" in NOTE
    assert "DirectPushService.connected" in NOTE
    assert "me != DIRECT" in NOTE


def test_socket_url_and_token_are_bounded():
    assert '"wss".equalsIgnoreCase(uri.getScheme())' in SERVICE
    assert '"127.0.0.1".equals(host)' in SERVICE
    assert "token.length() > 8192" in PLUGIN
    assert "MAX_MESSAGE_BYTES" in SERVICE


def test_unregister_erases_secret_and_stops_service():
    body = PLUGIN[PLUGIN.index("public void unregister"):PLUGIN.index(
        "public void batteryStatus")]
    assert "DirectPushStore.clear(getContext())" in body
    assert "DirectPushService.ACTION_STOP" in body
    assert "remove(SEALED)" in STORE
    assert "remove(DEVICE)" not in STORE[STORE.index("static void clear"):]


def test_denied_notification_permission_never_stores_direct_credentials():
    callback = PLUGIN[PLUGIN.index("private void afterNotifPermission"):PLUGIN.index(
        "private void doRegister")]
    assert "PermissionState.GRANTED" in callback
    assert '"notification permission denied"' in callback
    assert callback.index("PermissionState.GRANTED") < callback.index("doRegister(call)")
