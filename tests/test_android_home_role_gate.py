from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "scripts/android_device_checks.sh").read_text()
TEST = (ROOT / "mobile/android/app/src/androidTest/java/place/poster/app/music/MusicBackgroundDeviceTest.java").read_text()
DEBUG_MANIFEST = (ROOT / "mobile/android/app/src/debug/AndroidManifest.xml").read_text()
RECEIVER = (ROOT / "mobile/android/app/src/debug/java/place/poster/app/home/HomeTestReceiver.java").read_text()
RELEASE_MANIFEST = (ROOT / "mobile/android/app/src/main/AndroidManifest.xml").read_text()


def test_emulator_enables_home_from_inside_debug_app_not_forbidden_pm_shell():
    assert "place.poster.app.test.SET_HOME_COMPONENT" in SCRIPT
    assert "am broadcast" in SCRIPT
    assert "HomeRoles.enableLauncherComponent" in RECEIVER
    assert 'android:permission="android.permission.DUMP"' in DEBUG_MANIFEST
    assert "HomeTestReceiver" not in RELEASE_MANIFEST


def test_home_role_gate_fails_closed_instead_of_skipping_launcher():
    assert 'fail "debug app-owned enable did not expose HomeActivity as a HOME candidate"' in SCRIPT
    assert "SKIP: the shell could not make our HomeActivity" not in SCRIPT
    assert "--ez enabled false" in SCRIPT


def test_launcher_presence_check_does_not_press_home_twice():
    block = SCRIPT.split('say "bring the launcher up"', 1)[1].split(
        'say "double HOME opens the active feed at its top"', 1)[0]
    assert "to_home" not in block
    assert '*HomeActivity*) ok "the launcher is what came up"' in block


def test_back_gate_clears_the_full_double_home_window_first():
    block = SCRIPT.split('ok "double HOME took the native feed-top path"', 1)[1].split(
        'say "what the home screen costs"', 1)[0]
    assert "HomeDoublePress deliberately accepts a 2-second delivery gap" in block
    assert block.index("sleep 3") < block.index("to_home\n  adb shell input keyevent KEYCODE_BACK")
    assert "to_home\n  adb shell input keyevent KEYCODE_BACK" in block


def test_music_device_test_requires_our_launcher_and_restores_prior_state():
    assert 'cmd role add-role-holder android.app.role.HOME " + ctx.getPackageName()' in TEST
    assert 'assertTrue("the emulator did not assign PosterChan the HOME role"' in TEST
    assert 'assertTrue("Home backgrounded the player but did not show PosterChan\'s launcher"' in TEST
    assert "if (HomeRoles.isDefaultHome(ctx))" not in TEST
    assert "HomeRoles.enableLauncherComponent(ctx, wasEnabled)" in TEST


def test_background_track_uses_the_shipped_player_and_plugin_path():
    body = TEST.split("aRealMusicPlayerTrackKeepsAdvancingAfterHome", 1)[1].split(
        "private static void restoreDeviceState", 1)[0]
    assert "__PC.MusicPlayer" in body
    assert "p._media()" in body
    assert "__PC.filesIdx()" in body
    assert "ContextCompat.startForegroundService" not in body
    assert "new Intent(ctx, MusicService.class).setAction(MusicService.ACTION_UPDATE)" not in body


def test_music_device_cleanup_works_while_main_activity_is_stopped_at_home():
    cleanup = TEST.split("private static void restoreDeviceState", 1)[1].split(
        "private static String shell", 1)[0]
    assert "scenario.onActivity" not in cleanup
    assert "runOnMainSync" in cleanup
    assert 'SCREEN_ORIENTATION_UNSPECIFIED' in cleanup
    assert 'cmd role remove-role-holder android.app.role.HOME' in cleanup
    assert "FLAG_ACTIVITY_REORDER_TO_FRONT" in cleanup
    assert "relaunchMainTask(ctx, activity)" in cleanup
    assert TEST.count("restoreDeviceState(ctx, oldHome, wasEnabled, scenario, activity)") == 2


def test_tablet_returns_from_real_home_by_relaunching_the_existing_main_task():
    body = TEST.split("tabletDesktopStateSurvivesHomeAndRotationInBothTasks", 1)[1].split(
        "aRealMusicPlayerTrackKeepsAdvancingAfterHome", 1)[0]
    helper = TEST.split("private static void relaunchMainTask", 1)[1].split(
        "private static String shell", 1)[0]
    assert "scenario.moveToState(Lifecycle.State.RESUMED)" not in body
    assert "relaunchMainTask(ctx, activity)" in body
    assert "FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_REORDER_TO_FRONT" in helper
    assert "ActivityLifecycleMonitorRegistry.getInstance()" in helper
    assert "getLifecycleStageOf(a)" in helper
    assert "stage.get() == Stage.RESUMED" in helper
    assert 'throw new AssertionError("relaunch did not resume existing Desktop activity")' in helper
