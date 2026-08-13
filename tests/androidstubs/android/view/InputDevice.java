package android.view;

/** Stub: only the source flags and the device lookup GamepadPlugin uses. */
public class InputDevice {
  public static final int SOURCE_JOYSTICK = 0x01000010;
  public static final int SOURCE_GAMEPAD = 0x00000401;
  public static final int SOURCE_DPAD = 0x00000201;

  public static int[] getDeviceIds() { return new int[0]; }
  public static InputDevice getDevice(int id) { return null; }
  public int getSources() { return 0; }
  public String getName() { return null; }
}
