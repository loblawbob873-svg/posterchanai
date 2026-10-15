package place.poster.app.home;

import java.util.Locale;
import java.util.regex.Pattern;

/**
 * A LINK ANOTHER APP HANDS US, as the launch view the client already knows how to land on.
 *
 * Two kinds, both reported missing on 2026-10-15:
 *   * `nostr:` -- "Other apps that have functions like 'open in another nostr app' don't show posterchan" (a
 *     user on Todd's T-Bone). Android lists an app for a link only when its manifest declares a VIEW filter for
 *     that scheme; ours declared none, so PosterChan was never offered. The client opens the entity exactly as a
 *     poster.place/<bech32> path does.
 *   * `mailto:` -- "other apps with email links should open in posterchan email". The client hands it to Mail's
 *     own mailto parser (the one an in-app mailto link already uses), so to/cc/subject/body arrive the same way.
 *
 * Pure Java on purpose, so tests/test_android_link_intents.py compiles and RUNS it. Anything that is not a
 * well-formed entity or mail address is "" -- the app then opens on its usual screen, never on a half-parsed one.
 */
public final class IntentView {
    private IntentView() { }

    private static final Pattern BECH32 =
            Pattern.compile("^(npub1|nprofile1|note1|nevent1|naddr1)[023456789acdefghjklmnpqrstuvwxyz]{6,2000}$");

    /** The launch view for an intent's action and data URI, or "". */
    public static String of(String action, String data) {
        if (data == null) return "";
        String d = data.trim();
        if (d.isEmpty() || d.length() > 4096) return "";
        boolean view = "android.intent.action.VIEW".equals(action);
        int colon = d.indexOf(':');
        if (colon <= 0) return "";
        String scheme = d.substring(0, colon).toLowerCase(Locale.ROOT);
        if ("nostr".equals(scheme) && view) {
            String rest = d.substring(colon + 1);
            while (rest.startsWith("/")) rest = rest.substring(1);        // nostr://npub1… as some apps write it
            int cut = rest.indexOf('?');
            if (cut >= 0) rest = rest.substring(0, cut);
            rest = rest.toLowerCase(Locale.ROOT);
            return BECH32.matcher(rest).matches() ? "nostr:" + rest : "";
        }
        if ("mailto".equals(scheme) && (view || "android.intent.action.SENDTO".equals(action))) {
            // Control characters never belong in a mail link; one would end up inside a header.
            for (int i = 0; i < d.length(); i++) if (d.charAt(i) < 0x20 || d.charAt(i) == 0x7f) return "";
            return "mailto:" + d.substring(colon + 1);
        }
        return "";
    }
}
