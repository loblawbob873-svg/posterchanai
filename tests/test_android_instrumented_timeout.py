from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_instrumented_runner_bounds_each_test_not_only_the_whole_gradle_task():
    """A wedged ActivityScenario previously consumed CI's entire 25-minute outer timeout.

    AndroidJUnitRunner's timeout_msec applies to each test and therefore preserves the test name and
    lets the remaining device cases run. The outer shell timeout remains an infrastructure backstop.
    """
    gradle = (ROOT / "mobile/android/app/build.gradle").read_text(encoding="utf-8")
    assert 'testInstrumentationRunner "androidx.test.runner.AndroidJUnitRunner"' in gradle
    assert 'testInstrumentationRunnerArguments timeout_msec: "90000"' in gradle
    assert "25m bash scripts/android_instrumented.sh" in (
        ROOT / ".github/workflows/android-emulator.yml").read_text(encoding="utf-8")


def test_contact_monitor_cannot_intercept_the_activity_scenario_launch_it_depends_on():
    """Install intent capture after the subject Activity is resumed, then block only Contacts."""
    src = (ROOT / "mobile/android/app/src/androidTest/java/place/poster/app/sms/"
           / "SmsContactDeviceTest.java").read_text(encoding="utf-8")
    assert src.index("ActivityScenario.launch(launch)") < src.index("instrumentation.addMonitor(monitor)")
    assert "onStartActivity(Intent intent)" in src
    assert "new Instrumentation.ActivityResult(Activity.RESULT_CANCELED, null)" in src
    assert "editor.getStringExtra(ContactsContract.Intents.Insert.PHONE)" in src
