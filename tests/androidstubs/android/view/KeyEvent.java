package android.view;

/** Stub: the gamepad keycodes and the two actions GamepadPlugin branches on. */
public class KeyEvent {
  public static final int ACTION_DOWN = 0;
  public static final int ACTION_UP = 1;

  public static final int KEYCODE_DPAD_UP = 19;
  public static final int KEYCODE_DPAD_DOWN = 20;
  public static final int KEYCODE_DPAD_LEFT = 21;
  public static final int KEYCODE_DPAD_RIGHT = 22;
  public static final int KEYCODE_BUTTON_A = 96;
  public static final int KEYCODE_BUTTON_B = 97;
  public static final int KEYCODE_BUTTON_X = 99;
  public static final int KEYCODE_BUTTON_Y = 100;
  public static final int KEYCODE_BUTTON_L1 = 102;
  public static final int KEYCODE_BUTTON_R1 = 103;
  public static final int KEYCODE_BUTTON_L2 = 104;
  public static final int KEYCODE_BUTTON_R2 = 105;
  public static final int KEYCODE_BUTTON_THUMBL = 106;
  public static final int KEYCODE_BUTTON_THUMBR = 107;
  public static final int KEYCODE_BUTTON_START = 108;
  public static final int KEYCODE_BUTTON_SELECT = 109;

  public int getAction() { return 0; }
  public int getKeyCode() { return 0; }
  public int getSource() { return 0; }
  public InputDevice getDevice() { return null; }
}
