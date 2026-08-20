package place.poster.app.home;

import android.content.Context;
import android.content.SharedPreferences;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashSet;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;

/**
 * The home screen's arrangement: which tiles are hidden and what order they were dragged into.
 *
 * SharedPreferences, deliberately — not a Nostr document. This is per-DEVICE state about apps that
 * exist on THIS phone: an order referring to packages another device does not have is meaningless
 * there, and a launcher that cannot draw until a relay answers is a launcher that shows a blank
 * screen when the network is down. The one thing a home screen must never do.
 *
 * Stored as newline-joined keys rather than a Set<String>: getStringSet has no order, and the order
 * IS the arrangement. (It also returns a live instance the platform may reuse, which has bitten
 * enough people to be worth avoiding on principle.)
 */
public final class LauncherPrefs {
    private static final String FILE = "pc_home";
    private static final String K_HIDDEN = "hidden";
    private static final String K_ORDER = "order";
    private static final String K_OPTED_IN = "opted_in";
    private static final String K_SEEDED = "seeded";
    private static final String K_DESK = "desk";
    private static final String K_DOCK = "dock";

    private final SharedPreferences sp;

    public LauncherPrefs(Context ctx) {
        this.sp = ctx.getApplicationContext().getSharedPreferences(FILE, Context.MODE_PRIVATE);
    }

    /**
     * FIRST RUN ONLY. The catalogue of PosterChan screens is far longer than any home screen wants,
     * so the ones that are not in the default set start hidden — written ONCE, here, and never again.
     *
     * The "once" is the whole point. Re-seeding on every start would put back every tile the person
     * had just removed; not seeding at all would put forty icons on a fresh phone. And because it is
     * written once, a view the app gains LATER is simply absent from this set, so it joins the
     * "PosterChan apps" checklist unchecked rather than appearing on a home screen somebody had
     * already arranged.
     */
    public void seedOnce(Set<String> hide) {
        if (sp.getBoolean(K_SEEDED, false)) return;
        Set<String> merged = new LinkedHashSet<String>(split(sp.getString(K_HIDDEN, "")));
        if (hide != null) merged.addAll(hide);
        sp.edit().putString(K_HIDDEN, join(new ArrayList<String>(merged)))
                 .putBoolean(K_SEEDED, true).apply();
    }

    /**
     * THE DESKTOP: what is on it, where, and how big — serialized by Desk.
     *
     * Kept apart from `hidden` and `order`, which belong to the DRAWER. They are two different
     * questions ("is this app on my home screen" versus "is it in my list of all apps") and merging
     * them is how removing an icon from the desktop would uninstall it from view entirely.
     */
    public String desk() { return sp.getString(K_DESK, ""); }

    public void setDesk(String serialized) {
        sp.edit().putString(K_DESK, serialized == null ? "" : serialized).apply();
    }

    /**
     * ONE DESKTOP PER GRID SHAPE — "4x5", "6x4" — and that is what makes a rotation non-destructive.
     *
     * A single arrangement re-flowed through Desk.fit on every geometry change is lossy in the way
     * that matters: nothing is deleted, but rotate to landscape and back and your icons are not
     * where you left them, for ever. A tablet rotates all the time.
     *
     * A SHAPE THAT HAS NEVER BEEN SEEN INHERITS, IT DOES NOT START EMPTY. The first read for a new
     * geometry falls back to the legacy single arrangement, which the caller then fits and writes
     * back under this shape. Starting blank would read as the launcher having thrown the desktop
     * away — the exact fear Desk.fit exists to answer.
     */
    public String desk(String geometry) {
        if (geometry == null || geometry.isEmpty()) return desk();
        String v = sp.getString(K_DESK + "." + geometry, null);
        return v == null ? desk() : v;
    }

    public void setDesk(String geometry, String serialized) {
        String v = serialized == null ? "" : serialized;
        if (geometry == null || geometry.isEmpty()) { setDesk(v); return; }
        // The legacy key is written too, so a build that rolls back — or the very first read on a
        // shape this device has not been in yet — still finds a desktop.
        sp.edit().putString(K_DESK + "." + geometry, v).putString(K_DESK, v).apply();
    }

    /** The dock — the toolbar of main icons that stays put while the desktop scrolls. */
    public List<String> dock() { return split(sp.getString(K_DOCK, "")); }

    public void setDock(List<String> keys) { sp.edit().putString(K_DOCK, join(keys)).apply(); }

    public boolean seeded() { return sp.getBoolean(K_SEEDED, false); }

    public Set<String> hidden() {
        return new HashSet<String>(split(sp.getString(K_HIDDEN, "")));
    }

    public void setHidden(Set<String> keys) {
        sp.edit().putString(K_HIDDEN, join(new ArrayList<String>(new LinkedHashSet<String>(keys)))).apply();
    }

    public List<String> order() {
        return split(sp.getString(K_ORDER, ""));
    }

    public void setOrder(List<String> keys) {
        sp.edit().putString(K_ORDER, join(keys)).apply();
    }

    /**
     * Whether the person ever asked for the launcher. Read by the settings screen so it can show
     * "you are using PosterChan as your home screen" without asking RoleManager on every draw, and
     * by the un-set path so it knows the component was ours to disable.
     */
    public boolean optedIn() { return sp.getBoolean(K_OPTED_IN, false); }

    public void setOptedIn(boolean v) { sp.edit().putBoolean(K_OPTED_IN, v).apply(); }

    static List<String> split(String s) {
        List<String> out = new ArrayList<String>();
        if (s == null || s.isEmpty()) return out;
        for (String p : s.split("\n")) if (!p.isEmpty()) out.add(p);
        return out;
    }

    static String join(List<String> keys) {
        if (keys == null || keys.isEmpty()) return "";
        StringBuilder b = new StringBuilder();
        for (String k : keys) {
            if (k == null || k.isEmpty() || k.indexOf('\n') >= 0) continue;
            if (b.length() > 0) b.append('\n');
            b.append(k);
        }
        return b.toString();
    }
}
