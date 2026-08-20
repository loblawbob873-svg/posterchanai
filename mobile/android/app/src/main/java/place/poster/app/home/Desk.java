package place.poster.app.home;

import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.List;

/**
 * THE HOME SCREEN'S GRID — where each icon and each widget sits, and how big it is.
 *
 * Pure java.util, no Android, because this is the part of a launcher that quietly loses things. A
 * desktop is the one screen whose contents the person arranged BY HAND: an icon that moves on its
 * own, a widget that shrinks after a rotation, or a row that disappears when the phone is held the
 * other way is not a cosmetic bug, it is their work being thrown away. So every rule is here and
 * `tests/test_android_launcher.py` runs them.
 *
 * The three that matter most, each with a test:
 *
 *  1. NOTHING IS EVER DROPPED FOR NOT FITTING. The grid changes size — rotation, a tablet, a
 *     different phone reading the same saved arrangement — and an item that no longer fits is
 *     re-placed somewhere it does, never deleted. Deleting is what a naive `if (!fits) continue`
 *     does, and it is silent.
 *  2. NOTHING OVERLAPS. Two widgets in the same cells draw on top of each other, and the one
 *     underneath is unreachable.
 *  3. A RESIZE THAT WOULD COLLIDE IS REFUSED, not clamped to something the person did not ask for.
 */
public final class Desk {

    /** One thing on the desktop: an app icon (1x1) or a widget (spanX by spanY). */
    public static final class Item {
        /** An app's AppShelf key, or "widget:<appWidgetId>" for a hosted widget. */
        public final String key;
        public int col, row, spanX, spanY;

        public Item(String key, int col, int row, int spanX, int spanY) {
            this.key = key == null ? "" : key;
            this.col = col; this.row = row;
            this.spanX = Math.max(1, spanX);
            this.spanY = Math.max(1, spanY);
        }

        public boolean isWidget() { return key.startsWith(WIDGET); }

        /** The hosted widget id, or -1. */
        public int widgetId() {
            if (!isWidget()) return -1;
            try { return Integer.parseInt(key.substring(WIDGET.length())); }
            catch (Throwable t) { return -1; }
        }

        boolean covers(int c, int r) {
            return c >= col && c < col + spanX && r >= row && r < row + spanY;
        }

        @Override public String toString() {
            return key + "@" + col + "," + row + " " + spanX + "x" + spanY;
        }
    }

    public static final String WIDGET = "widget:";

    private Desk() { }

    public static String widgetKey(int appWidgetId) { return WIDGET + appWidgetId; }

    // ---------------------------------------------------------------- geometry

    /** Would `it` sit entirely inside the grid without touching anything else? */
    public static boolean free(List<Item> items, Item it, int cols, int rows) {
        if (it == null) return false;
        if (it.col < 0 || it.row < 0) return false;
        if (it.col + it.spanX > cols || it.row + it.spanY > rows) return false;
        if (items != null) for (Item o : items) {
            if (o == it) continue;
            if (overlaps(o, it)) return false;
        }
        return true;
    }

    static boolean overlaps(Item a, Item b) {
        return a.col < b.col + b.spanX && b.col < a.col + a.spanX
            && a.row < b.row + b.spanY && b.row < a.row + a.spanY;
    }

    /**
     * Put `it` in the first free spot, scanning left-to-right then top-to-bottom — the order a
     * person reads, so a newly added icon appears where they are looking. Returns false and changes
     * nothing when the desktop is full: a full desktop must say so, not silently swallow the app.
     */
    public static boolean place(List<Item> items, Item it, int cols, int rows) {
        for (int r = 0; r + it.spanY <= rows; r++) {
            for (int c = 0; c + it.spanX <= cols; c++) {
                it.col = c; it.row = r;
                if (free(items, it, cols, rows)) return true;
            }
        }
        return false;
    }

    public static boolean add(List<Item> items, Item it, int cols, int rows) {
        if (!place(items, it, cols, rows)) return false;
        items.add(it);
        return true;
    }

    /** Move an item, if the target is clear. Refused moves leave it exactly where it was. */
    public static boolean moveTo(List<Item> items, Item it, int col, int row, int cols, int rows) {
        int wasC = it.col, wasR = it.row;
        it.col = col; it.row = row;
        if (free(items, it, cols, rows)) return true;
        it.col = wasC; it.row = wasR;
        return false;
    }

    /**
     * Resize, if the new footprint is clear. REFUSED rather than clamped: a widget that comes back a
     * different size from the one the person dragged to is a widget that feels broken, and they
     * cannot tell whether it was them or the app.
     */
    public static boolean resize(List<Item> items, Item it, int spanX, int spanY,
                                 int minX, int minY, int cols, int rows) {
        int wasX = it.spanX, wasY = it.spanY;
        it.spanX = Math.max(Math.max(1, minX), spanX);
        it.spanY = Math.max(Math.max(1, minY), spanY);
        if (free(items, it, cols, rows)) return true;
        it.spanX = wasX; it.spanY = wasY;
        return false;
    }

    public static Item at(List<Item> items, int col, int row) {
        if (items != null) for (Item it : items) if (it.covers(col, row)) return it;
        return null;
    }

    public static Item byKey(List<Item> items, String key) {
        if (items != null && key != null) for (Item it : items) if (key.equals(it.key)) return it;
        return null;
    }

    /**
     * MAKE A SAVED ARRANGEMENT FIT A GRID IT WAS NOT MADE FOR — a rotation, a tablet, a restored
     * backup from a bigger phone.
     *
     * Everything that still fits stays exactly where it is; everything else is re-placed, biggest
     * first so the widgets get the room. Anything that cannot be placed at all is returned in
     * `overflow` rather than dropped, so the caller can say so instead of the person simply finding
     * their calendar widget gone.
     */
    public static List<Item> fit(List<Item> items, int cols, int rows) {
        List<Item> overflow = new ArrayList<Item>();
        if (items == null) return overflow;
        List<Item> kept = new ArrayList<Item>();
        List<Item> homeless = new ArrayList<Item>();
        for (Item it : items) {
            it.spanX = Math.min(it.spanX, Math.max(1, cols));
            it.spanY = Math.min(it.spanY, Math.max(1, rows));
            if (free(kept, it, cols, rows)) kept.add(it); else homeless.add(it);
        }
        Collections.sort(homeless, new Comparator<Item>() {
            @Override public int compare(Item a, Item b) {
                return (b.spanX * b.spanY) - (a.spanX * a.spanY);
            }
        });
        for (Item it : homeless) {
            if (place(kept, it, cols, rows)) kept.add(it); else overflow.add(it);
        }
        items.clear();
        items.addAll(kept);
        return overflow;
    }

    // ---------------------------------------------------------------- storage

    /**
     * `key|col|row|spanX|spanY` per line. `|` and newline cannot appear in a package name, an
     * activity name or a widget id, so nothing needs escaping and a hand-edited file cannot inject
     * a field.
     */
    public static String serialize(List<Item> items) {
        StringBuilder b = new StringBuilder();
        if (items != null) for (Item it : items) {
            if (it == null || it.key.isEmpty()) continue;
            if (it.key.indexOf('|') >= 0 || it.key.indexOf('\n') >= 0) continue;
            if (b.length() > 0) b.append('\n');
            b.append(it.key).append('|').append(it.col).append('|').append(it.row)
             .append('|').append(it.spanX).append('|').append(it.spanY);
        }
        return b.toString();
    }

    /**
     * A malformed line is SKIPPED, never fatal. This string comes off disk and may have been written
     * by an older build; throwing here would take the home screen down with it, and a home screen
     * that will not start is the one failure this whole feature is written to avoid.
     */
    public static List<Item> parse(String raw) {
        List<Item> out = new ArrayList<Item>();
        if (raw == null || raw.isEmpty()) return out;
        for (String line : raw.split("\n")) {
            String[] p = line.split("\\|");
            if (p.length != 5 || p[0].isEmpty()) continue;
            try {
                out.add(new Item(p[0], Integer.parseInt(p[1]), Integer.parseInt(p[2]),
                                 Integer.parseInt(p[3]), Integer.parseInt(p[4])));
            } catch (Throwable ignored) { }
        }
        return out;
    }
}
