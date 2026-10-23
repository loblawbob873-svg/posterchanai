package place.poster.app;

import android.accessibilityservice.AccessibilityServiceInfo;
import android.app.Instrumentation;
import android.app.UiAutomation;
import android.graphics.Rect;
import android.os.SystemClock;
import android.view.accessibility.AccessibilityNodeInfo;
import android.view.accessibility.AccessibilityWindowInfo;

import androidx.test.platform.app.InstrumentationRegistry;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.util.ArrayList;
import java.util.List;

/**
 * A FINGER, for the device tests of the native screens.
 *
 * The native screens (Messages, Phone, the launcher, the widget, the notification shade) were covered
 * by tests that call the Activity's methods or `performClick()` on a View they found themselves. That
 * proves the handler works; it does not prove a PERSON can press it -- a button drawn off-screen, under
 * another view, in a window that is not focusable, or not drawn at all passes every one of them.
 *
 * So this reads the screen the way TalkBack does (the accessibility tree of EVERY window on screen,
 * the system shade and a launcher's hosted widget included), finds a control by the words a person
 * sees on it or hears for it, and presses it with a real touch event at the centre of where Android
 * says it is drawn (`input tap`, the same injected MotionEvent a finger produces). Nothing here holds
 * a reference to the app's views.
 *
 * Every wait is a poll on the condition with a deadline -- never a fixed sleep -- and every miss says
 * what WAS on screen, because "button not found" with no picture of the screen is a red build nobody
 * can read.
 */
public final class RealUi {
    private RealUi() { }

    public interface Check { boolean ok() throws Exception; }

    /** A control on screen: what it says and where it is drawn. */
    public static final class Hit {
        public final String text, desc, id, cls, pkg;
        public final Rect bounds;
        public final boolean enabled;
        Hit(AccessibilityNodeInfo n) {
            text = n.getText() == null ? "" : n.getText().toString();
            desc = n.getContentDescription() == null ? "" : n.getContentDescription().toString();
            id = n.getViewIdResourceName() == null ? "" : n.getViewIdResourceName();
            cls = n.getClassName() == null ? "" : n.getClassName().toString();
            pkg = n.getPackageName() == null ? "" : n.getPackageName().toString();
            bounds = new Rect();
            n.getBoundsInScreen(bounds);
            enabled = n.isEnabled();
        }
        @Override public String toString() {
            return "[" + (text.isEmpty() ? "" : "'" + text + "'") + (desc.isEmpty() ? "" : " desc='" + desc + "'")
                    + (id.isEmpty() ? "" : " #" + id.replaceAll(".*:id/", "")) + " " + bounds.toShortString() + "]";
        }
    }

    public interface Match { boolean is(Hit h); }

    /** Exactly this text or content description (case-insensitive). */
    public static Match named(final String words) {
        return h -> h.text.equalsIgnoreCase(words) || h.desc.equalsIgnoreCase(words);
    }

    /** Text or description CONTAINING these words (case-insensitive). */
    public static Match saying(final String words) {
        final String w = words.toLowerCase(java.util.Locale.ROOT);
        return h -> h.text.toLowerCase(java.util.Locale.ROOT).contains(w)
                || h.desc.toLowerCase(java.util.Locale.ROOT).contains(w);
    }

    /** A view by its resource id name (e.g. "pc_dl_number"). */
    public static Match id(final String name) {
        return h -> h.id.endsWith(":id/" + name);
    }

    /** `m`, drawn inside `area` (e.g. a "5" ON THE KEYPAD, not in a list of recent calls). */
    public static Match inside(final Rect area, final Match m) {
        return h -> m.is(h) && area.contains(h.bounds.centerX(), h.bounds.centerY());
    }

    /** A long press where it is drawn: a finger held still (`input swipe` to the same point). */
    public static void hold(Hit h, int ms) {
        shell("input swipe " + h.bounds.centerX() + " " + h.bounds.centerY() + " "
                + h.bounds.centerX() + " " + h.bounds.centerY() + " " + ms);
    }

    public static UiAutomation ua() {
        UiAutomation u = InstrumentationRegistry.getInstrumentation().getUiAutomation();
        AccessibilityServiceInfo info = u.getServiceInfo();
        if (info != null && (info.flags & AccessibilityServiceInfo.FLAG_RETRIEVE_INTERACTIVE_WINDOWS) == 0) {
            info.flags |= AccessibilityServiceInfo.FLAG_RETRIEVE_INTERACTIVE_WINDOWS
                    | AccessibilityServiceInfo.FLAG_REPORT_VIEW_IDS
                    | AccessibilityServiceInfo.FLAG_INCLUDE_NOT_IMPORTANT_VIEWS;
            u.setServiceInfo(info);
        }
        return u;
    }

    /** Everything visible on screen, in every window, that a person could read or press. */
    public static List<Hit> screen() {
        List<Hit> out = new ArrayList<Hit>();
        UiAutomation u = ua();
        // FRESH NODES EVERY READ. The accessibility cache is refreshed by events; a cached node answers
        // "unchanged" to exactly the question these tests ask. (Once in three runs on the 320x640
        // emulator a hosted widget AND the launcher strip both still read "Pause" 8s after the session
        // went PAUSED; whether the cache or the views were stale was not separated -- clearing the cache
        // takes the cache out of that question.)
        if (android.os.Build.VERSION.SDK_INT >= 34) { try { u.clearCache(); } catch (Throwable ignored) { } }
        List<AccessibilityWindowInfo> windows = null;
        try { windows = u.getWindows(); } catch (Throwable ignored) { }
        boolean any = false;
        if (windows != null) {
            for (AccessibilityWindowInfo w : windows) {
                AccessibilityNodeInfo root = null;
                try { root = w.getRoot(); } catch (Throwable ignored) { }
                if (root != null) { any = true; walk(root, out, 0); }
            }
        }
        if (!any) {
            AccessibilityNodeInfo root = u.getRootInActiveWindow();
            if (root != null) walk(root, out, 0);
        }
        return out;
    }

    private static void walk(AccessibilityNodeInfo n, List<Hit> out, int depth) {
        if (n == null || depth > 60) return;
        if (n.isVisibleToUser()) {
            Hit h = new Hit(n);
            if (!h.bounds.isEmpty()) out.add(h);
        }
        for (int i = 0; i < n.getChildCount(); i++) {
            AccessibilityNodeInfo c = null;
            try { c = n.getChild(i); } catch (Throwable ignored) { }
            walk(c, out, depth + 1);
        }
    }

    public static Hit find(Match m) {
        for (Hit h : screen()) if (m.is(h)) return h;
        return null;
    }

    /** Waits for a control to be on screen, or fails saying what was there instead. */
    public static Hit waitFor(String what, Match m, long ms) {
        long end = SystemClock.uptimeMillis() + ms;
        Hit h;
        while ((h = find(m)) == null && SystemClock.uptimeMillis() < end) SystemClock.sleep(150);
        if (h == null) throw new AssertionError(what + " is not on screen after " + ms + "ms. On screen: " + describe());
        return h;
    }

    /** A real touch at the centre of the control, the way a finger presses it. */
    public static void tap(Hit h) {
        if (h == null) throw new AssertionError("nothing to tap");
        shell("input tap " + h.bounds.centerX() + " " + h.bounds.centerY());
    }

    /** Find it (waiting), then press it. */
    public static Hit tap(String what, Match m, long ms) {
        Hit h = waitFor(what, m, ms);
        tap(h);
        return h;
    }

    public static void waitUntil(String what, long ms, Check c) throws Exception {
        long end = SystemClock.uptimeMillis() + ms;
        while (true) {
            if (c.ok()) return;
            if (SystemClock.uptimeMillis() >= end) throw new AssertionError(what + " (waited " + ms + "ms). On screen: " + describe());
            SystemClock.sleep(150);
        }
    }

    public static String describe() {
        StringBuilder b = new StringBuilder();
        int n = 0;
        for (Hit h : screen()) {
            if (h.text.isEmpty() && h.desc.isEmpty()) continue;
            if (n++ > 60) { b.append(" ..."); break; }
            b.append(h).append(' ');
        }
        return b.length() == 0 ? "(nothing readable)" : b.toString();
    }

    public static String shell(String cmd) {
        Instrumentation in = InstrumentationRegistry.getInstrumentation();
        try (InputStream is = new android.os.ParcelFileDescriptor.AutoCloseInputStream(
                in.getUiAutomation().executeShellCommand(cmd))) {
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buf = new byte[8192];
            int n;
            while ((n = is.read(buf)) > 0) out.write(buf, 0, n);
            return out.toString("UTF-8");
        } catch (Throwable t) {
            return "shell failed: " + t;
        }
    }

    /** Screen on and unlocked: a tap on a sleeping or keyguarded display presses nothing. */
    public static void awake() {
        shell("input keyevent KEYCODE_WAKEUP");
        shell("wm dismiss-keyguard");
        shell("svc power stayon true");
    }
}
