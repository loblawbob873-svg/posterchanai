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
  /* THE SAME FOUR SLOTS BEFORE ANY CALIBRATION. Without these the panel cannot answer the only
     question that matters when an axis looks wrong — did the DRIVER report that, or did this code
     compute it? A calibrated 1.0 and a raw 1.0 need opposite fixes, and three builds were spent
     unable to tell them apart. */
  private static final float[] rawAxes = new float[4];
  // Which pair the right stick was found on, for the diagnostics panel.
  private static String rightStick = "Z/RZ";
  private static String padName = "";

  // Counters, read by status(). The failure this plugin addresses reports SUCCESS from every side —
  // the page sees no pad and the Activity sees nothing wrong — so the only way to tell "Android never
  // delivered an event" from "the page ignored it" is to count both ends. Static because the Activity
  // forwards events whether or not JS has ever attached a listener.
  private static int motionEvents = 0, keyEvents = 0, emits = 0;
  // Did the LAST event's range lookup succeed? See status().calibrated.
  private static boolean lastRangeFound = false;

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
    InputDevice dev = ev.getDevice();
    axes[0] = centred(ev, dev, MotionEvent.AXIS_X);
    axes[1] = centred(ev, dev, MotionEvent.AXIS_Y);
    /* THE RIGHT STICK IS Z/RZ ON SOME PADS AND RX/RY ON OTHERS, and the triggers take whichever pair
     * the sticks did not. Reading Z/RZ unconditionally is right for one family and reports a DEAD
     * right stick for the other — measured on a real pad as "left joystick is doing everything,
     * right joystick doing nothing", with Z/RZ sitting at their resting 0 because they were the
     * triggers all along.
     *
     * The declared range settles it without a device database: a stick straddles zero, a trigger
     * rests at its minimum. Z/RZ stay the default so a pad that describes nothing behaves exactly as
     * it did before. */
    int rsX = MotionEvent.AXIS_Z, rsY = MotionEvent.AXIS_RZ;
    if (!straddles(ev, dev, MotionEvent.AXIS_Z) && straddles(ev, dev, MotionEvent.AXIS_RX)) {
      rsX = MotionEvent.AXIS_RX;
      rsY = MotionEvent.AXIS_RY;
    }
    rightStick = (rsX == MotionEvent.AXIS_RX) ? "RX/RY" : "Z/RZ";
    axes[2] = centred(ev, dev, rsX);
    axes[3] = centred(ev, dev, rsY);
    rawAxes[0] = ev.getAxisValue(MotionEvent.AXIS_X);
    rawAxes[1] = ev.getAxisValue(MotionEvent.AXIS_Y);
    rawAxes[2] = ev.getAxisValue(rsX);
    rawAxes[3] = ev.getAxisValue(rsY);
    // The hat IS the d-pad on the pads that report it that way. Folded into the button slots so the
    // page never has to know which kind of controller it is talking to.
    float hx = ev.getAxisValue(MotionEvent.AXIS_HAT_X);
    float hy = ev.getAxisValue(MotionEvent.AXIS_HAT_Y);
    down[12] = hy < -0.5f;
    down[13] = hy > 0.5f;
    down[14] = hx < -0.5f;
    down[15] = hx > 0.5f;
    // Analogue triggers, for the pads that report them as axes rather than as L2/R2 keys.
    float lt = Math.max(unit(ev, dev, MotionEvent.AXIS_LTRIGGER), unit(ev, dev, MotionEvent.AXIS_BRAKE));
    float rt = Math.max(unit(ev, dev, MotionEvent.AXIS_RTRIGGER), unit(ev, dev, MotionEvent.AXIS_GAS));
    if (lt > 0.01f) down[6] = lt > 0.5f;
    if (rt > 0.01f) down[7] = rt > 0.5f;
    named(ev.getDevice());
    push();
    return true;
  }

  /**
   * A STICK'S REST POSITION IS NOT ZERO, AND ITS RANGE IS NOT ALWAYS -1..1.
   *
   * `getAxisValue` returns whatever the driver reports, in the DEVICE's own units. The Gamepad API
   * this feeds promises -1..1 with a centred rest position, and a desktop browser supplies both —
   * which is exactly why the same pad is perfect in Firefox and drifts here. Both corrections come
   * from the device's own declared MotionRange:
   *
   *   flat     the manufacturer's rest region. Inside it the stick IS centred; passed through raw, a
   *            resting stick walks the character across the screen for ever.
   *   min/max  a pad reporting 0..255 (plenty of HID descriptors do) is not slightly miscalibrated,
   *            it is pinned to one corner.
   *
   * Rescaled outside the deadzone rather than clipped, so the first movement past `flat` starts from
   * zero instead of jumping to it — clipping alone trades drift for a lurch.
   *
   * NO RANGE MEANS PASS THROUGH. A driver that declines to describe an axis is not a reason to
   * invent a calibration for it: the raw value is the best information available, and it is what
   * every pad that reports a sane -1..1 was already giving.
   */
  private static float centred(MotionEvent ev, InputDevice dev, int axis) {
    float v = ev.getAxisValue(axis);
    if (Float.isNaN(v) || Float.isInfinite(v)) return 0f;
    InputDevice.MotionRange r = rangeOf(ev, dev, axis);
    if (r == null) return clamped(v);
    /* AN AXIS THAT CANNOT GO NEGATIVE IS NOT A CENTRED STICK — it is a trigger, and centring one
     * reads it as HARD OVER while it sits untouched. AXIS_Z and AXIS_RZ are the right stick on many
     * pads and the two analogue triggers on many others, and nothing in the event says which; the
     * declared range is the only thing that tells them apart, because a stick straddles zero and a
     * trigger rests at its minimum. Getting this wrong is not a small error: normalising a resting
     * 0..1 trigger to -1 pins the right stick to a corner for the whole session, which is worse than
     * the raw passthrough this replaced — measured on a real pad, "joystick movement even worse now".
     * Passing it through unchanged is exactly the old behaviour for these axes, so the calibration
     * can only help the axes it understands and can never damage one it has misread. */
    if (r.getMin() >= 0f) return clamped(v);
    float half = (r.getMax() - r.getMin()) / 2f;
    if (half <= 0f) return clamped(v);
    float n = (v - (r.getMin() + r.getMax()) / 2f) / half;
    float flat = r.getFlat() / half;
    // A driver claiming the whole travel is deadzone would otherwise divide by zero and report NaN,
    // which JSONArray.put refuses — one bad descriptor would cost every frame, not one axis.
    if (flat >= 1f) return 0f;
    float m = Math.abs(n);
    if (m <= flat) return 0f;
    return clamped(Math.signum(n) * (m - flat) / (1f - flat));
  }

  /**
   * A TRIGGER RESTS AT ITS MINIMUM, NOT AT ITS CENTRE, so it must never go through `centred` — that
   * would read an untouched trigger as fully pulled in the negative direction. Normalised to 0..1
   * against its own range instead, which leaves the usual 0..1 pads bit-identical and fixes the ones
   * reporting 0..255, where the 0.5 threshold below is otherwise crossed by a feather touch.
   */
  private static float unit(MotionEvent ev, InputDevice dev, int axis) {
    float v = ev.getAxisValue(axis);
    if (Float.isNaN(v) || Float.isInfinite(v)) return 0f;
    InputDevice.MotionRange r = rangeOf(ev, dev, axis);
    if (r == null) return v;
    float span = r.getMax() - r.getMin();
    if (span <= 0f) return v;
    float n = (v - r.getMin()) / span;
    return n < 0f ? 0f : (n > 1f ? 1f : n);
  }

  private static float clamped(float v) {
    return v < -1f ? -1f : (v > 1f ? 1f : v);
  }

  /**
   * THE AXIS RANGE, LOOKED UP THE WAY IT IS ACTUALLY PUBLISHED — and the reason every calibration
   * before this was a no-op.
   *
   * `getMotionRange(axis, source)` matches the range's OWN source exactly, while
   * `MotionEvent.getSource()` returns a BITMASK: a pad reports SOURCE_JOYSTICK | SOURCE_GAMEPAD, and
   * that combined value equals neither, so the lookup returned null on every event and every axis
   * fell through to raw passthrough. Silently — a null range is also what a device that declines to
   * describe itself returns, which is the case the fallback exists for.
   *
   * Measured on a Nintendo Switch Pro Controller, whose own report gave it away: RZ declares
   * flat=0.0153 and the page was still being handed -0.0088, a value inside that deadzone which
   * `centred` would have returned as 0 had it ever seen the range. Meanwhile `status()` asked with a
   * plain SOURCE_JOYSTICK, found all four axes, and printed "stick (centred)" about axes nothing was
   * centring — a panel reporting the rule rather than the result, which is how three fixes shipped
   * against a calibration that was never running.
   *
   * So: the event's own source first (correct when it is a single flag), then the two sources a pad
   * can publish under.
   */
  private static InputDevice.MotionRange rangeOf(MotionEvent ev, InputDevice dev, int axis) {
    if (dev == null) return null;
    InputDevice.MotionRange r = dev.getMotionRange(axis, ev.getSource());
    if (r == null) r = dev.getMotionRange(axis, InputDevice.SOURCE_JOYSTICK);
    if (r == null) r = dev.getMotionRange(axis, InputDevice.SOURCE_GAMEPAD);
    if (r != null) lastRangeFound = true;
    return r;
  }

  /** Does this axis go negative? A stick does; a trigger, resting at its minimum, does not. */
  private static boolean straddles(MotionEvent ev, InputDevice dev, int axis) {
    InputDevice.MotionRange r = rangeOf(ev, dev, axis);
    return r != null && r.getMin() < 0f;
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

  /**
   * WHAT THE PAD ACTUALLY DECLARES, because the same controller is perfect in Firefox and wrong
   * here, and the difference is information Android does not give us.
   *
   * A browser reads the HID descriptor and therefore KNOWS which axis is a stick and which is a
   * trigger. Android hands over `getAxisValue` and leaves that judgement to us, and AXIS_Z/AXIS_RZ
   * are the right stick on some pads and the two triggers on others. Guessing it wrong pinned a
   * stick to a corner for a whole session. So rather than guess a fourth time, this reports the
   * declared range of every axis this code reads plus its live value: `min`/`max` say whether an
   * axis straddles zero (a stick) or rests at its minimum (a trigger), and `flat` is the deadzone
   * the manufacturer asked for. Read it from Games → the controller panel.
   */
  private static final int[] READ_AXES = {
    MotionEvent.AXIS_X, MotionEvent.AXIS_Y, MotionEvent.AXIS_Z, MotionEvent.AXIS_RZ,
    MotionEvent.AXIS_RX, MotionEvent.AXIS_RY, MotionEvent.AXIS_HAT_X, MotionEvent.AXIS_HAT_Y,
    MotionEvent.AXIS_LTRIGGER, MotionEvent.AXIS_RTRIGGER,
    MotionEvent.AXIS_BRAKE, MotionEvent.AXIS_GAS,
  };
  private static final String[] READ_NAMES = {
    "X", "Y", "Z", "RZ", "RX", "RY", "HAT_X", "HAT_Y", "LTRIGGER", "RTRIGGER", "BRAKE", "GAS",
  };

  @PluginMethod
  public void status(PluginCall call) {
    JSObject o = new JSObject();
    o.put("attached", anyPad());
    o.put("name", padName);
    o.put("motion", motionEvents);
    o.put("key", keyEvents);
    o.put("emits", emits);
    o.put("rightStick", rightStick);
    // The four slots the page is being handed right now, so a wrong one can be seen rather than
    // described. A stick at rest reads 0 here; anything else is the bug.
    JSONArray live = new JSONArray();
    try {
      // put(double) refuses NaN/Infinity — the same refusal `push` guards against. A driver
      // reporting one must cost this row, never the whole panel, which exists to explain such a pad.
      for (int i = 0; i < axes.length; i++) live.put((double) axes[i]);
    } catch (JSONException e) {
      o.put("axesError", String.valueOf(e));
    }
    o.put("axes", live);
    JSONArray raw = new JSONArray();
    try {
      for (int i = 0; i < rawAxes.length; i++) raw.put((double) rawAxes[i]);
    } catch (JSONException e) {
      o.put("rawError", String.valueOf(e));
    }
    o.put("axesRaw", raw);
    // Whether the calibration is running AT ALL, answered from the same lookup it uses rather than
    // from a tidier one: a panel that asks a different question than the code reports the RULE and
    // not the RESULT, which is exactly how three fixes shipped against a calibration that was
    // silently a no-op on every event.
    o.put("calibrated", lastRangeFound);

    JSONArray ranges = new JSONArray();
    try {
      for (int id : InputDevice.getDeviceIds()) {
        InputDevice d = InputDevice.getDevice(id);
        if (d == null || !fromPad(d.getSources())) continue;
        for (int i = 0; i < READ_AXES.length; i++) {
          InputDevice.MotionRange r = d.getMotionRange(READ_AXES[i], InputDevice.SOURCE_JOYSTICK);
          if (r == null) r = d.getMotionRange(READ_AXES[i], InputDevice.SOURCE_GAMEPAD);
          if (r == null) continue;
          JSObject a = new JSObject();
          a.put("axis", READ_NAMES[i]);
          a.put("min", (double) r.getMin());
          a.put("max", (double) r.getMax());
          a.put("flat", (double) r.getFlat());
          // The rule this code applies, spelled out, so the report says what was DECIDED and not
          // only what was read.
          a.put("treatedAs", r.getMin() >= 0f ? "trigger (not centred)" : "stick (centred)");
          ranges.put(a);
        }
      }
    } catch (Exception e) {
      // A device that will not describe itself must not cost the whole panel.
      o.put("rangeError", String.valueOf(e));
    }
    o.put("ranges", ranges);
    call.resolve(o);
  }
}
