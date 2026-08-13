"""The APK's native controller bridge — TYPE-CHECKED and RUN, not grepped.

Run: venv-unified/bin/python -m unittest tests.test_android_gamepad

WHY IT EXISTS. A webxdc game driven by a Bluetooth pad works in Firefox on a tablet and does nothing
in the APK on the SAME tablet with the SAME pad. One variable, the engine: a WebView embedded in
another app's Activity is on nobody's list of Gamepad API implementers, and the platform data the API
would need is fed from generic-motion and key events that arrive at the ACTIVITY. So Android reads
the controller and hands a Gamepad-shaped snapshot to the page.

The Gradle build only runs on CI, so without this the first check on any of this Java is a four-minute
round trip through a release build — and the mapping itself would never be checked at all. javac
against tests/androidstubs answers the type question here, and because the mapping is pure static
state, `java` can answer the BEHAVIOUR question too: the assertions below drive real KeyEvents and
MotionEvents through the real code and read the snapshot back.

The mapping matters more than it looks. Android delivers a D-pad EITHER as KEYCODE_DPAD_* key events
OR as the HAT_X/HAT_Y axes depending on the pad, and analogue triggers as axes on some and as L2/R2
keys on others — so a shim that handles only one form works perfectly on the controller its author
owned. The indices are the W3C standard mapping rather than Android's key order, because the page-side
shim already speaks that; get it wrong and every button is off by some amount, which reads as "the
controller does something random" rather than as a table being wrong.
"""
import os
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JAVA_ROOT = os.path.join(ROOT, "mobile", "android", "app", "src", "main", "java")
PLUGIN = os.path.join(JAVA_ROOT, "place", "poster", "app", "gamepad", "GamepadPlugin.java")
MAIN = os.path.join(JAVA_ROOT, "place", "poster", "app", "MainActivity.java")

# A driver that pokes the real static entry points and prints the resulting snapshot. Written as a
# subclass so the protected notifyListeners hook can be captured without a Capacitor bridge.
DRIVER = r"""
import android.view.InputDevice;
import android.view.KeyEvent;
import android.view.MotionEvent;
import place.poster.app.gamepad.GamepadPlugin;

public class Driver {
  static class Key extends KeyEvent {
    int a, c, s;
    Key(int a, int c, int s){ this.a=a; this.c=c; this.s=s; }
    public int getAction(){ return a; }
    public int getKeyCode(){ return c; }
    public int getSource(){ return s; }
    public InputDevice getDevice(){ return null; }
  }
  /* A pad that DESCRIBES its axes, which is what every real one does. The stub's getDevice() returns
     null, so without this the calibration path is never entered and a passthrough test passes while
     the deadzone and range normalisation go unchecked. */
  static class Pad extends InputDevice {
    float min, max, flat;
    Pad(float min, float max, float flat){ this.min=min; this.max=max; this.flat=flat; }
    public InputDevice.MotionRange getMotionRange(int axis, int source){
      return new InputDevice.MotionRange(min, max, flat);
    }
  }
  static class Ranged extends MotionEvent {
    float v; int s; InputDevice d;
    Ranged(float v, int s, InputDevice d){ this.v=v; this.s=s; this.d=d; }
    public int getAction(){ return MotionEvent.ACTION_MOVE; }
    public int getSource(){ return s; }
    public float getAxisValue(int axis){ return axis == MotionEvent.AXIS_X ? v : 0f; }
    public InputDevice getDevice(){ return d; }
  }
  static class Motion extends MotionEvent {
    float x, y, hx, hy; int s;
    Motion(float x, float y, float hx, float hy, int s){ this.x=x; this.y=y; this.hx=hx; this.hy=hy; this.s=s; }
    public int getAction(){ return MotionEvent.ACTION_MOVE; }
    public int getSource(){ return s; }
    public float getAxisValue(int axis){
      if(axis == MotionEvent.AXIS_X) return x;
      if(axis == MotionEvent.AXIS_Y) return y;
      if(axis == MotionEvent.AXIS_HAT_X) return hx;
      if(axis == MotionEvent.AXIS_HAT_Y) return hy;
      return 0f;
    }
    public InputDevice getDevice(){ return null; }
  }
  public static void main(String[] a) throws Exception {
    int PAD = InputDevice.SOURCE_GAMEPAD, JOY = InputDevice.SOURCE_JOYSTICK;
    // A face button, pressed then released.
    System.out.println("A_down=" + GamepadPlugin.onKey(new Key(KeyEvent.ACTION_DOWN, KeyEvent.KEYCODE_BUTTON_A, PAD)));
    System.out.println("btn0=" + GamepadPlugin.probeButton(0));
    System.out.println("A_up=" + GamepadPlugin.onKey(new Key(KeyEvent.ACTION_UP, KeyEvent.KEYCODE_BUTTON_A, PAD)));
    System.out.println("btn0after=" + GamepadPlugin.probeButton(0));
    // A d-pad delivered as KEYS.
    GamepadPlugin.onKey(new Key(KeyEvent.ACTION_DOWN, KeyEvent.KEYCODE_DPAD_UP, PAD));
    System.out.println("dpadUpFromKey=" + GamepadPlugin.probeButton(12));
    GamepadPlugin.onKey(new Key(KeyEvent.ACTION_UP, KeyEvent.KEYCODE_DPAD_UP, PAD));
    // …and the SAME d-pad delivered as a HAT axis, which is the other half of Android's own advice.
    GamepadPlugin.onMotion(new Motion(0f, 0f, -1f, 0f, JOY));
    System.out.println("dpadLeftFromHat=" + GamepadPlugin.probeButton(14));
    // A stick.
    GamepadPlugin.onMotion(new Motion(-0.8f, 0.6f, 0f, 0f, JOY));
    System.out.println("axis0=" + GamepadPlugin.probeAxis(0));
    System.out.println("axis1=" + GamepadPlugin.probeAxis(1));
    // CALIBRATION. A stick resting slightly off centre, inside the flat the driver declares, is
    // CENTRED — reported raw it walks the character across the screen for ever.
    GamepadPlugin.onMotion(new Ranged(0.06f, JOY, new Pad(-1f, 1f, 0.12f)));
    System.out.println("rest=" + GamepadPlugin.probeAxis(0));
    // …and a pad reporting 0..255 is not slightly off, it is pinned to a corner. Mid-travel is 0.
    GamepadPlugin.onMotion(new Ranged(127.5f, JOY, new Pad(0f, 255f, 0f)));
    System.out.println("mid255=" + GamepadPlugin.probeAxis(0));
    GamepadPlugin.onMotion(new Ranged(255f, JOY, new Pad(0f, 255f, 0f)));
    System.out.println("max255=" + GamepadPlugin.probeAxis(0));
    // A driver claiming the whole travel is deadzone must not divide by zero into NaN.
    GamepadPlugin.onMotion(new Ranged(0.9f, JOY, new Pad(-1f, 1f, 4f)));
    System.out.println("allflat=" + GamepadPlugin.probeAxis(0));
    // A KEYBOARD key must not be claimed: consuming it would eat ordinary typing.
    System.out.println("keyboard=" + GamepadPlugin.onKey(new Key(KeyEvent.ACTION_DOWN, KeyEvent.KEYCODE_BUTTON_A, 0x101)));
  }
}
"""


def _compile_and_run():
    if shutil.which("javac") is None or shutil.which("java") is None:
        raise unittest.SkipTest("javac/java not installed")
    out = tempfile.mkdtemp()
    drv = os.path.join(out, "Driver.java")
    with open(drv, "w") as f:
        f.write(DRIVER)
    r = subprocess.run(
        ["javac", "-nowarn", "-d", out,
         "-sourcepath", os.path.join(ROOT, "tests", "androidstubs") + os.pathsep + JAVA_ROOT,
         PLUGIN, drv],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise AssertionError("javac failed:\n" + r.stderr[-3000:])
    run = subprocess.run(["java", "-cp", out, "Driver"], capture_output=True, text=True, timeout=60)
    if run.returncode != 0:
        raise AssertionError("java failed:\n" + run.stderr[-3000:])
    return dict(
        line.split("=", 1) for line in run.stdout.strip().splitlines() if "=" in line
    )


class NativeGamepad(unittest.TestCase):
    def test_it_type_checks(self):
        """The Gradle build only runs on CI. Without this the first check on this file is a
        four-minute release build, which is how a one-character mistake costs a round trip."""
        _compile_and_run()

    def test_a_face_button_sets_and_clears_its_standard_mapping_slot(self):
        o = _compile_and_run()
        self.assertEqual(o["A_down"], "true", "a controller key must be CONSUMED")
        self.assertEqual(o["btn0"], "true")
        self.assertEqual(o["btn0after"], "false", "release must clear it, or the game holds fire")

    def test_a_dpad_arrives_as_keys_on_some_pads_and_as_a_hat_on_others(self):
        """Android's own guidance, and the thing a shim written against one controller gets wrong:
        both forms have to land in the same four slots or half the pads on the market do nothing."""
        o = _compile_and_run()
        self.assertEqual(o["dpadUpFromKey"], "true")
        self.assertEqual(o["dpadLeftFromHat"], "true")

    def test_stick_axes_are_passed_through(self):
        o = _compile_and_run()
        self.assertAlmostEqual(float(o["axis0"]), -0.8, places=4)
        self.assertAlmostEqual(float(o["axis1"]), 0.6, places=4)

    def test_a_declared_deadzone_is_applied_so_a_resting_stick_reads_zero(self):
        o = _compile_and_run()
        self.assertEqual(float(o["rest"]), 0.0,
                         "a stick inside the driver's declared flat must read centred, not drift")

    def test_an_axis_is_normalised_to_the_gamepad_apis_range_whatever_the_driver_reports(self):
        o = _compile_and_run()
        self.assertAlmostEqual(float(o["mid255"]), 0.0, places=3,
                               msg="mid-travel on a 0..255 pad is centre, not hard-over")
        self.assertAlmostEqual(float(o["max255"]), 1.0, places=3)

    def test_a_broken_range_costs_one_axis_not_a_NaN(self):
        o = _compile_and_run()
        v = float(o["allflat"])
        self.assertEqual(v, 0.0)
        self.assertFalse(v != v, "NaN would be refused by JSONArray and cost every frame")

    def test_a_keyboard_key_is_never_claimed(self):
        """The overrides consume what they handle, and consuming a keyboard event would eat typing
        app-wide. The source check is the only thing standing between those two."""
        o = _compile_and_run()
        self.assertEqual(o["keyboard"], "false")

    def test_mainactivity_forwards_the_events_and_registers_the_plugin(self):
        """The plugin cannot see an event the Activity does not hand it, and an in-app plugin that is
        not registered before super.onCreate() is invisible to JS — two ways for all of the above to
        be correct and unreachable."""
        src = open(MAIN).read()
        self.assertIn("registerPlugin(place.poster.app.gamepad.GamepadPlugin.class);", src)
        self.assertRegex(src, r"public boolean dispatchGenericMotionEvent\(MotionEvent")
        self.assertRegex(src, r"public boolean dispatchKeyEvent\(KeyEvent")
        self.assertIn("GamepadPlugin.onMotion(ev)", src)
        self.assertIn("GamepadPlugin.onKey(ev)", src)
        # Registration must precede the bridge being built.
        self.assertLess(src.index("registerPlugin(place.poster.app.gamepad.GamepadPlugin.class)"),
                        src.index("super.onCreate(savedInstanceState)"))

    def test_the_client_forwards_the_snapshot_into_the_running_app(self):
        """The last hop. Android → plugin → client → the app frame; a missing listener here is a
        native half that works perfectly and reaches nothing."""
        js = open(os.path.join(ROOT, "static", "js", "client", "webxdc.js")).read()
        self.assertIn("capPlugin('Gamepad'", js)
        self.assertIn("addListener('padstate'", js)
        self.assertIn("method:'webxdc.padstate'", js)


if __name__ == "__main__":
    unittest.main()
