package android.view;

/** Stub: only the source flags and the device lookup GamepadPlugin uses. */
public class InputDevice {
  public static final int SOURCE_JOYSTICK = 0x01000010;
  public static final int SOURCE_GAMEPAD = 0x00000401;
  public static final int SOURCE_DPAD = 0x00000201;

  /**
   * The per-axis calibration the platform actually publishes. Modelled rather than omitted because a
   * stub weaker than the platform is worse than none: without `flat`/`min`/`max` here, a test of the
   * deadzone and range normalisation cannot fail, and the drift they exist to remove would ship
   * looking checked.
   */
  public static class MotionRange {
    private final float min, max, flat;

    public MotionRange(float min, float max, float flat) {
      this.min = min;
      this.max = max;
      this.flat = flat;
    }

    public float getMin() { return min; }
    public float getMax() { return max; }
    public float getRange() { return max - min; }
    public float getFlat() { return flat; }
    public float getFuzz() { return 0f; }
  }

  public static int[] getDeviceIds() { return new int[0]; }
  public static InputDevice getDevice(int id) { return null; }
  public int getSources() { return 0; }
  public String getName() { return null; }

  /** Null for an axis the device does not describe — the case the plugin must pass through. */
  public MotionRange getMotionRange(int axis, int source) { return null; }
}
