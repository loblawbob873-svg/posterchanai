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

    public static String classify(boolean voiceCapable) {
        return voiceCapable ? "phone" : "tablet";
    }
}
