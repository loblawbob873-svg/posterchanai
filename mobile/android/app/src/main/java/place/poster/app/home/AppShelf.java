package place.poster.app.home;

import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;

/**
 * WHAT THE HOME SCREEN SHOWS, decided with no Android in the room.
 *
 * A launcher that fails takes the phone's home screen with it — there is no second home screen to
 * fall back to, and a person who cannot reach Settings cannot even change it back. So the decisions
 * live here, in a class with no imports outside java.util, and are RUN against generated states by
 * tests/test_android_launcher.py. HomeActivity does nothing but draw what this returns.
 *
 * Three rules exist purely because of that constraint, and each one is measured by a test that was
 * verified to fail without it:
 *
 *  1. AN ESSENTIAL ENTRY IS NEVER REMOVED. "Phone settings" is one, and it is the way back from
 *     every mistake this app can make — a bad deploy, a corrupt arrangement, an empty package
 *     query. Hiding, filtering and every guard below skip it.
 *  2. THE GRID IS NEVER EMPTIED BY AN ARRANGEMENT. A `hidden` set that would leave nothing (or
 *     nothing but our own tiles) is a broken arrangement, not an instruction, and it is ignored
 *     wholesale rather than obeyed. This is the launcher's version of folder sync's floor: a
 *     stored decision that would erase everything is far more likely to be wrong than right.
 *  3. A SEARCH ALWAYS SEES HIDDEN APPS. Hiding is about the grid, never about reachability —
 *     otherwise "hide" is a one-way door that needs another launcher to undo.
 */
public final class AppShelf {

    /** One tile. Either a phone app (pkg/activity) or one of PosterChan's own screens (view). */
    public static final class Entry {
        public final String pkg;
        public final String activity;
        public final String label;
        /** PosterChan view id — non-empty means this tile opens our app, not another one. */
        public final String view;
        /** Never hideable, never filtered out by a guard. See rule 1. */
        public final boolean essential;

        public Entry(String pkg, String activity, String label, String view, boolean essential) {
            this.pkg = pkg == null ? "" : pkg;
            this.activity = activity == null ? "" : activity;
            this.label = label == null ? "" : label;
            this.view = view == null ? "" : view;
            this.essential = essential;
        }

        public static Entry app(String pkg, String activity, String label) {
            return new Entry(pkg, activity, label, "", false);
        }

        public static Entry ours(String view, String label, boolean essential) {
            return new Entry("", "", label, view, essential);
        }

        public boolean isOurs() { return !view.isEmpty(); }

        /**
         * The stable identity used by the hidden set and the saved order. A package alone is not
         * enough: an app may publish several launcher activities and a real launcher shows them
         * all as separate icons, so hiding one must not hide the other.
         */
        public String key() {
            return isOurs() ? "pc:" + view : pkg + "/" + activity;
        }

        @Override public String toString() { return key() + " (" + label + ")"; }
    }

    private AppShelf() { }

    /**
     * @param installed   every launchable activity the package manager reported, in any order
     * @param ours        PosterChan's own tiles, in the order we want them offered
     * @param hidden      keys the user hid from the grid
     * @param order       keys the user dragged into an explicit position, first to last
     * @param query       the search box, or "" / null for the plain grid
     */
    public static List<Entry> arrange(List<Entry> installed, List<Entry> ours,
                                      Set<String> hidden, List<String> order, String query) {
        String q = query == null ? "" : query.trim().toLowerCase(Locale.ROOT);
        boolean searching = !q.isEmpty();

        // Dedupe by key, ours first so a self-published launcher activity never doubles a tile.
        Map<String, Entry> all = new LinkedHashMap<String, Entry>();
        if (ours != null) for (Entry e : ours) if (e != null) all.put(e.key(), e);
        if (installed != null) for (Entry e : installed) if (e != null && !all.containsKey(e.key())) all.put(e.key(), e);

        List<Entry> pool = new ArrayList<Entry>(all.values());

        // Rule 3: a search sees everything, hidden included. Rule 2: an arrangement that would
        // empty the grid is ignored rather than obeyed.
        Set<String> hide = new HashSet<String>();
        if (!searching && hidden != null) {
            for (String k : hidden) if (k != null) hide.add(k);
            if (wouldEmpty(pool, hide)) hide.clear();
        }

        List<Entry> kept = new ArrayList<Entry>();
        for (Entry e : pool) {
            if (!e.essential && hide.contains(e.key())) continue;       // rule 1
            if (searching && !matches(e, q)) continue;
            kept.add(e);
        }

        if (searching) {
            sortForSearch(kept, q);
        } else {
            sortForGrid(kept, order);
        }
        return kept;
    }

    /**
     * Would this hidden set leave a home screen with nothing on it worth calling a home screen?
     * "Nothing" counts our own tiles as nothing: a grid of PosterChan screens and no phone apps is
     * the kiosk this launcher must never become, and it is also indistinguishable from a package
     * query that came back empty.
     */
    private static boolean wouldEmpty(List<Entry> pool, Set<String> hide) {
        int phoneApps = 0, survivingPhoneApps = 0;
        for (Entry e : pool) {
            if (e.isOurs()) continue;
            phoneApps++;
            if (!hide.contains(e.key())) survivingPhoneApps++;
        }
        return phoneApps > 0 && survivingPhoneApps == 0;
    }

    static boolean matches(Entry e, String q) {
        if (e.label.toLowerCase(Locale.ROOT).contains(q)) return true;
        // The package name is searchable too: it is how you find an app whose label is in a script
        // you cannot type, and how "settings" finds com.android.settings on a localised phone.
        return !e.pkg.isEmpty() && e.pkg.toLowerCase(Locale.ROOT).contains(q);
    }

    /** Prefix beats word-start beats substring; then alphabetical. Ours never outrank a real match. */
    private static void sortForSearch(List<Entry> kept, final String q) {
        Collections.sort(kept, new Comparator<Entry>() {
            @Override public int compare(Entry a, Entry b) {
                int r = rank(a, q) - rank(b, q);
                if (r != 0) return r;
                return byLabel(a, b);
            }
        });
    }

    private static int rank(Entry e, String q) {
        String l = e.label.toLowerCase(Locale.ROOT);
        if (l.startsWith(q)) return 0;
        if (l.contains(" " + q)) return 1;
        if (l.contains(q)) return 2;
        return 3;                                   // matched on package name only
    }

    private static void sortForGrid(List<Entry> kept, List<String> order) {
        final Map<String, Integer> pos = new LinkedHashMap<String, Integer>();
        if (order != null) {
            int i = 0;
            for (String k : order) if (k != null && !pos.containsKey(k)) pos.put(k, i++);
        }
        Collections.sort(kept, new Comparator<Entry>() {
            @Override public int compare(Entry a, Entry b) {
                Integer pa = pos.get(a.key()), pb = pos.get(b.key());
                if (pa != null && pb != null) return pa - pb;
                if (pa != null) return -1;          // arranged tiles lead
                if (pb != null) return 1;
                return byLabel(a, b);
            }
        });
    }

    /**
     * Case-insensitive by label, then by key so the order is TOTAL. A comparator that can call two
     * different tiles equal reshuffles them on every redraw, which on a home screen reads as icons
     * that will not stay put.
     */
    private static int byLabel(Entry a, Entry b) {
        int r = a.label.compareToIgnoreCase(b.label);
        if (r != 0) return r;
        return a.key().compareTo(b.key());
    }

    /**
     * PIN A TILE TO THE FRONT. The saved `order` is a short list of keys that lead; everything else
     * follows alphabetically, so pinning is simply "put this key first".
     *
     * Deliberately NOT a full drag-and-drop arrangement. A launcher grid that can be reordered by
     * dragging needs the tile under the finger to move, the rest to reflow, and an autoscroll at the
     * edges — a lot of gesture code between somebody's finger and their home screen, on the one
     * screen in this app that has no fallback. Pinning is the ninety per cent of it that is a menu
     * item, and the saved order it writes is the same list a drag would eventually write.
     */
    public static List<String> pin(List<String> order, String key) {
        List<String> out = new ArrayList<String>();
        if (key != null && !key.isEmpty()) out.add(key);
        if (order != null) for (String k : order) if (k != null && !k.equals(key)) out.add(k);
        return out;
    }

    public static List<String> unpin(List<String> order, String key) {
        List<String> out = new ArrayList<String>();
        if (order != null) for (String k : order) if (k != null && !k.equals(key)) out.add(k);
        return out;
    }

    public static boolean pinned(List<String> order, String key) {
        return order != null && key != null && order.contains(key);
    }

    /**
     * An entry is hideable only if it is not essential. Returns the new hidden set; hiding
     * something essential is a no-op rather than a refusal, because the caller is our own long-press
     * menu and it should not be offering it in the first place.
     */
    public static Set<String> hide(Set<String> hidden, Entry e) {
        Set<String> out = new HashSet<String>(hidden == null ? new HashSet<String>() : hidden);
        if (e != null && !e.essential) out.add(e.key());
        return out;
    }

    public static Set<String> unhide(Set<String> hidden, String key) {
        Set<String> out = new HashSet<String>(hidden == null ? new HashSet<String>() : hidden);
        if (key != null) out.remove(key);
        return out;
    }
}
