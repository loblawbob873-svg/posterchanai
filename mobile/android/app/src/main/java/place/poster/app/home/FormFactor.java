package place.poster.app.home;

/**
 * PHONE OR TABLET, as Android itself decides it -- not from the screen's size.
 *
 * Telegram is deliberately off on a phone (the phone already runs Telegram; a second live client only
 * doubles every notification) and on on a tablet. The web client used to guess from the short side in
 * CSS px, and a Samsung tablet failed that guess ("telegram is missing from tablet"): One UI's Screen
 * zoom raises the density, so an 800px-wide tablet reports ~420-450dp, which is phone-sized.
 *
 * `TelephonyManager.isVoiceCapable()` is the device's own configuration (config_voice_capable): a
 * phone can place calls; a tablet -- LTE ones included -- cannot. It does not move with Screen zoom,
 * rotation, split screen or a missing SIM. Pure on purpose: tests/test_android_form_factor.py runs it.
 */
public final class FormFactor {
    private FormFactor() {}

    /* SIZE DECIDES, EXCEPT IN THE BAND WHERE THE BIGGEST PHONES MEET THE SMALLEST TABLETS. The largest
     * phones are ~6.9"; a "7-inch" Fire 7 measures 6.98" of active panel. So under 6.5" is a phone, over
     * 7.5" is a tablet, and between them the voice radio decides (a big phone calls, a 7" Wi-Fi tablet
     * does not). Measured from the panel's PHYSICAL density (DisplayMetrics.xdpi/ydpi), which Screen zoom
     * does not touch. */
    public static final double PHONE_MAX_INCHES = 6.5;
    public static final double TABLET_MIN_INCHES = 7.5;

    /** Physical diagonal from real pixels and physical dpi; 0 when the display does not report it. */
    public static double inches(int widthPx, int heightPx, float xdpi, float ydpi) {
        if (widthPx <= 0 || heightPx <= 0 || !(xdpi > 20f) || !(ydpi > 20f)) return 0;
        double w = widthPx / (double) xdpi, h = heightPx / (double) ydpi;
        double d = Math.sqrt(w * w + h * h);
        return d > 1.5 && d < 60 ? d : 0;          // a nonsense dpi (some emulators report 0 or 1) is "unknown"
    }

    /**
     * "I need telegram to be available on android tablets": isVoiceCapable() alone called a Samsung
     * Galaxy Tab a phone -- LTE Tabs CAN place calls in many regions, and this one runs Texts. So the
     * physical screen size decides; voice capability is only the fallback when the size is unknown.
     */
    public static String classify(boolean voiceCapable, double inches) {
        if (inches >= TABLET_MIN_INCHES) return "tablet";
        if (inches > 0 && inches < PHONE_MAX_INCHES) return "phone";
        return voiceCapable ? "phone" : "tablet";
    }

    public static String classify(boolean voiceCapable) { return classify(voiceCapable, 0); }
}
