package android.view;

/** Stub: the joystick axes and the ACTION_MOVE GamepadPlugin reads. */
public class MotionEvent {
  public static final int ACTION_MOVE = 2;

  public static final int AXIS_X = 0;
  public static final int AXIS_Y = 1;
  public static final int AXIS_Z = 11;
  public static final int AXIS_RZ = 14;
  public static final int AXIS_HAT_X = 15;
  public static final int AXIS_HAT_Y = 16;
  public static final int AXIS_LTRIGGER = 17;
  public static final int AXIS_RTRIGGER = 18;
  public static final int AXIS_BRAKE = 23;
  public static final int AXIS_GAS = 22;

  public int getAction() { return 0; }
  public int getSource() { return 0; }
  public float getAxisValue(int axis) { return 0f; }
  public InputDevice getDevice() { return null; }
}
