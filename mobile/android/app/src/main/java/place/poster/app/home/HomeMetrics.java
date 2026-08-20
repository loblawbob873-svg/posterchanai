package place.poster.app.home;

/**
 * HOW BIG THE HOME SCREEN IS, ON THE SCREEN IT IS ACTUALLY ON.
 *
 * The grid was four columns and the dock five slots, both written as constants, and on a phone that
 * is right. On a tablet it is a phone layout stretched: four icons the size of coasters across ten
 * inches, a dock of five in the middle of a metre of bar, and a landscape desktop three rows tall
 * with the columns still counted for portrait. Reported as "the launcher needs to work on tablet
 * mode too".
 *
 * Pure — no Android — so tests/test_android_launcher.py RUNS these against real device dimensions
 * instead of reading the constants back. Everything is in DP, which is the only unit in which
 * "a comfortable icon" means the same thing on two densities.
 *
 * TWO RULES THAT ARE NOT ARITHMETIC:
 *
 *  * COLUMNS COME FROM THE SHORT SIDE, NEVER FROM THE CURRENT WIDTH. `smallestScreenWidthDp` is the
 *    same number in both orientations, so a rotation does not change the number of columns. If it
 *    did, every rotation would re-flow the arrangement through Desk.fit — and re-flowing is
 *    lossy in the sense that matters: rotate to landscape and back and your icons are not where you
 *    left them. Width still decides for a device whose short side is a tablet's, which is the case
 *    the feature is about.
 *  * A PHONE IN LANDSCAPE IS STILL A PHONE. Its width in dp is tablet-sized and its ergonomics are
 *    not; six columns of thumb-reachable icons is the most that helps.
 */
public final class HomeMetrics {

    /** Below this the device is a phone, by the platform's own definition of a large screen. */
    public static final int TABLET_SW_DP = 600;

    private HomeMetrics() { }

    public static boolean isTablet(int smallestWidthDp) { return smallestWidthDp >= TABLET_SW_DP; }

    /**
     * Columns on the desktop.
     *
     * @param smallestWidthDp the device's short side — what decides phone vs tablet, and what keeps
     *                        the count stable across a rotation.
     */
    public static int deskCols(int smallestWidthDp) {
        if (!isTablet(smallestWidthDp)) return 4;
        // A tablet's short side is 600-800dp; its long side is 960-1400. One column per ~118dp of
        // the SHORT side gives 5-6 in portrait and the same 5-6 in landscape, where the extra width
        // becomes bigger cells rather than more of them — which is what a tablet home screen looks
        // like, and what keeps a rotation from re-flowing anything.
        return clamp(5, 7, smallestWidthDp / 118);
    }

    /** Rows, from the height the desktop actually got after the dock and the now-playing strip. */
    public static int deskRows(int usableHeightDp, int smallestWidthDp) {
        if (usableHeightDp <= 0) return isTablet(smallestWidthDp) ? 6 : 5;
        int cell = isTablet(smallestWidthDp) ? 116 : 92;
        return clamp(3, 8, usableHeightDp / cell);
    }

    /**
     * How many icons the dock holds. A phone keeps five — a sixth is not reachable with one thumb.
     * A tablet's dock is a bar with room, and five icons floating in the middle of it is the
     * stretched-phone look this whole class exists to stop.
     */
    public static int dockMax(int widthDp, int smallestWidthDp) {
        if (!isTablet(smallestWidthDp)) return 5;
        return clamp(6, 9, widthDp / 130);
    }

    /** The dock icon's side, in dp. Bigger on a tablet for the same reason the cells are. */
    public static int dockIconDp(int smallestWidthDp) { return isTablet(smallestWidthDp) ? 64 : 52; }

    /**
     * The drawer is a GridView with `numColumns="auto_fit"`, so its column count is this number and
     * the screen width. 80dp on a tablet is a wall of tiny icons; 104 gives about nine across a
     * landscape 10-inch, which reads as a list rather than as a mosaic.
     */
    public static int drawerColumnDp(int smallestWidthDp) { return isTablet(smallestWidthDp) ? 104 : 80; }

    /**
     * THE KEY THE ARRANGEMENT IS STORED UNDER. One desktop per grid shape, so a tablet's landscape
     * and its portrait are two arrangements rather than one that keeps being re-flowed into the
     * other. Rotating back puts every icon where it was.
     */
    public static String geometry(int cols, int rows) { return cols + "x" + rows; }

    private static int clamp(int lo, int hi, int v) { return v < lo ? lo : (v > hi ? hi : v); }
}
