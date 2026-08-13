package place.poster.app.gamepad;

import android.view.InputDevice;
import android.view.KeyEvent;
import android.view.MotionEvent;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

import org.json.JSONArray;
import org.json.JSONException;

/**
 * The controller, read at the ANDROID layer and handed to the WebView.
 *
 * WHY THIS EXISTS, measured rather than reasoned about: the same webxdc game, on the same tablet,
 * with the same controller, works in Firefox and does nothing in the APK. That is one variable — the
 * engine — and it is the half Android takes away, exactly like the media session the music controls
 * had to reimplement. Chrome for Android, Firefox for Android, Samsung Internet and Opera Mobile are
 * each named as supporting the Gamepad API; a WebView embedded in somebody else's Activity is named
 * by nobody, and the platform data it would need is fed from generic-motion and key events routed
 * through the content view — events that arrive at OUR Activity first.
 *
 * So rather than keep trying to settle whether WebView implements the Gamepad API, this bypasses the
 * question: the Activity forwards what it receives, this turns it into a Gamepad-API-SHAPED snapshot,
 * and the page consumes it through the same code path it already uses for a real pad. If the WebView
 * does support gamepads the page prefers the real one and nothing here is used — so this cannot make
 * a working device worse, which matters when the device is not in the room.
 *
 * WHAT ANDROID MAKES AWKWARD, all of it documented and none of it obvious:
 *
 *  - A D-pad arrives EITHER as KEYCODE_DPAD_* key events OR as the HAT_X/HAT_Y axes, depending on
 *    the pad. Android's own guidance is to treat them as the same input, so both are folded into the
 *    same four button slots here.
 *  - Analogue triggers arrive as axes on some pads and as L2/R2 key events on others; both are
 *    accepted for the same reason.
 *  - A joystick MotionEvent is BATCHED: the newest sample is in the event and older ones are in its
 *    history. Only the newest is used — this is a state snapshot, not a gesture recogniser, and the
 *    page samples it once a frame anyway.
 *  - Button indices follow the W3C STANDARD MAPPING, not Android's key order, because the page-side
 *    shim already speaks that and the whole point is for a native pad to be indistinguishable from a
 *    browser one.
 */
@CapacitorPlugin(name = "Gamepad")
public class GamepadPlugin extends Plugin {

  /** The live plugin, so the Activity can push events in without holding a Bridge reference. */
  private static GamepadPlugin INSTANCE;

  // Standard-mapping button count, and the axes the page cares about (LX, LY, RX, RY).
  private static final int BUTTONS = 16;
  private static final boolean[] down = new boolean[BUTTONS];
  private static final float[] axes = new float[4];
  private static String padName = "";

  // Counters, read by status(). The failure this plugin addresses reports SUCCESS from every side —
  // the page sees no pad and the Activity sees nothing wrong — so the only way to tell "Android never
  // delivered an event" from "the page ignored it" is to count both ends. Static because the Activity
  // forwards events whether or not JS has ever attached a listener.
  private static int motionEvents = 0, keyEvents = 0, emits = 0;

  @Override
  public void load() {
    INSTANCE = this;
  }

  /** True when this build has a controller attached that Android calls a joystick. */
  private static boolean anyPad() {
    for (int id : InputDevice.getDeviceIds()) {
      InputDevice d = InputDevice.getDevice(id);
      if (d == null) continue;
      int s = d.getSources();
      if ((s & InputDevice.SOURCE_JOYSTICK) == InputDevice.SOURCE_JOYSTICK
          || (s & InputDevice.SOURCE_GAMEPAD) == InputDevice.SOURCE_GAMEPAD) return true;
    }
    return false;
  }

  private static boolean fromPad(int source) {
    return (source & InputDevice.SOURCE_JOYSTICK) == InputDevice.SOURCE_JOYSTICK
        || (source & InputDevice.SOURCE_GAMEPAD) == InputDevice.SOURCE_GAMEPAD
        || (source & InputDevice.SOURCE_DPAD) == InputDevice.SOURCE_DPAD;
  }

  /** W3C standard-mapping index for an Android gamepad keycode, or -1 if it is not one. */
  private static int slot(int keyCode) {
    switch (keyCode) {
      case KeyEvent.KEYCODE_BUTTON_A: return 0;
      case KeyEvent.KEYCODE_BUTTON_B: return 1;
      case KeyEvent.KEYCODE_BUTTON_X: return 2;
      case KeyEvent.KEYCODE_BUTTON_Y: return 3;
      case KeyEvent.KEYCODE_BUTTON_L1: return 4;
      case KeyEvent.KEYCODE_BUTTON_R1: return 5;
      case KeyEvent.KEYCODE_BUTTON_L2: return 6;
      case KeyEvent.KEYCODE_BUTTON_R2: return 7;
      case KeyEvent.KEYCODE_BUTTON_SELECT: return 8;
      case KeyEvent.KEYCODE_BUTTON_START: return 9;
      case KeyEvent.KEYCODE_BUTTON_THUMBL: return 10;
      case KeyEvent.KEYCODE_BUTTON_THUMBR: return 11;
      case KeyEvent.KEYCODE_DPAD_UP: return 12;
      case KeyEvent.KEYCODE_DPAD_DOWN: return 13;
      case KeyEvent.KEYCODE_DPAD_LEFT: return 14;
      case KeyEvent.KEYCODE_DPAD_RIGHT: return 15;
      default: return -1;
    }
  }

  /**
   * A controller key event. Returns true when it was consumed — and consuming matters beyond this
   * plugin: an unconsumed KEYCODE_DPAD_* moves Android's own focus between views, so a player
   * pressing "up" would walk the focus ring around the page while the game sat still.
   */
  public static boolean onKey(KeyEvent ev) {
    if (ev == null || !fromPad(ev.getSource())) return false;
    int i = slot(ev.getKeyCode());
    if (i < 0) return false;
    int a = ev.getAction();
    if (a != KeyEvent.ACTION_DOWN && a != KeyEvent.ACTION_UP) return false;
    keyEvents++;
    // A held button repeats; the page wants state, so a repeat is simply "still down".
    down[i] = (a == KeyEvent.ACTION_DOWN);
    named(ev.getDevice());
    push();
    return true;
  }

  /** A joystick/trigger/hat sample. */
  public static boolean onMotion(MotionEvent ev) {
    if (ev == null || !fromPad(ev.getSource())) return false;
    if (ev.getAction() != MotionEvent.ACTION_MOVE) return false;
    motionEvents++;
    axes[0] = ev.getAxisValue(MotionEvent.AXIS_X);
    axes[1] = ev.getAxisValue(MotionEvent.AXIS_Y);
    axes[2] = ev.getAxisValue(MotionEvent.AXIS_Z);
    axes[3] = ev.getAxisValue(MotionEvent.AXIS_RZ);
    // The hat IS the d-pad on the pads that report it that way. Folded into the button slots so the
    // page never has to know which kind of controller it is talking to.
    float hx = ev.getAxisValue(MotionEvent.AXIS_HAT_X);
    float hy = ev.getAxisValue(MotionEvent.AXIS_HAT_Y);
    down[12] = hy < -0.5f;
    down[13] = hy > 0.5f;
    down[14] = hx < -0.5f;
    down[15] = hx > 0.5f;
    // Analogue triggers, for the pads that report them as axes rather than as L2/R2 keys.
    float lt = Math.max(ev.getAxisValue(MotionEvent.AXIS_LTRIGGER), ev.getAxisValue(MotionEvent.AXIS_BRAKE));
    float rt = Math.max(ev.getAxisValue(MotionEvent.AXIS_RTRIGGER), ev.getAxisValue(MotionEvent.AXIS_GAS));
    if (lt > 0.01f) down[6] = lt > 0.5f;
    if (rt > 0.01f) down[7] = rt > 0.5f;
    named(ev.getDevice());
    push();
    return true;
  }

  private static void named(InputDevice d) {
    if (padName.isEmpty() && d != null && d.getName() != null) padName = d.getName();
  }

  /** Ship the snapshot to JS. No throttle: a pad only reports when something actually moved. */
  private static void push() {
    GamepadPlugin p = INSTANCE;
    if (p == null) return;
    JSObject o = new JSObject();
    JSONArray b = new JSONArray(), a = new JSONArray();
    for (int i = 0; i < BUTTONS; i++) b.put(down[i] ? 1 : 0);
    // JSONArray.put(double) throws on NaN/Infinity, which an axis can legitimately never be — but a
    // driver reporting a broken range should cost this ONE snapshot, not the whole controller. So the
    // failure drops the frame rather than propagating: the next event brings a fresh one.
    try {
      for (int i = 0; i < axes.length; i++) {
        float v = axes[i];
        a.put(Float.isNaN(v) || Float.isInfinite(v) ? 0.0 : (double) v);
      }
    } catch (JSONException e) {
      return;
    }
    o.put("buttons", b);
    o.put("axes", a);
    o.put("id", padName);
    emits++;
    p.notifyListeners("padstate", o);
  }

  /**
   * What the PHONE measured, which is the whole reason the last three rounds of this took three
   * rounds. Read from the Activity's own counters, so it answers even when JS has never seen an
   * event: `attached` without `motion`/`key` means Android is not routing controller input to us,
   * and `key` without `emits` would mean the bridge is the broken half.
   */
  /**
   * Test hooks. The mapping — which Android keycode and which axis land in which W3C standard-mapping
   * slot — is the part most likely to be wrong and the part no regex can check, and it is pure static
   * state, so javac and java can answer it directly (tests/test_android_gamepad.py). Exposed rather
   * than reached by reflection so the test reads as the question it is asking.
   */
  public static boolean probeButton(int i) {
    return i >= 0 && i < BUTTONS && down[i];
  }

  public static float probeAxis(int i) {
    return i >= 0 && i < axes.length ? axes[i] : 0f;
  }

  @PluginMethod
  public void status(PluginCall call) {
    JSObject o = new JSObject();
    o.put("attached", anyPad());
    o.put("name", padName);
    o.put("motion", motionEvents);
    o.put("key", keyEvents);
    o.put("emits", emits);
    call.resolve(o);
  }
}
