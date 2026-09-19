package place.poster.app.phone;

/**
 * HOW BIG A DIALPAD KEY CAN BE IN THE SPACE IT ACTUALLY HAS. Pure, so the arithmetic is RUN.
 *
 * The keys used to be sized from the whole screen minus a guessed 300dp of "everything else". The
 * everything else is a header, the number, the call row, the tab bar, the status and navigation
 * bars and, sometimes, a notice — well over 300dp on an ordinary phone — so four rows of keys came
 * out taller than the space the layout gave them. That space centres its content, so the overflow
 * was split between the two ends: the top row lost its top and the bottom row lost its bottom
 * ("the number circle buttons are cut off at the top and bottom").
 *
 * The dialer now measures the pad's real box after layout and asks this for the largest key whose
 * FOUR ROWS plus the number line fit inside it, margins included. The margin is decided here too,
 * so Keypad and the fit can never disagree about how much room a key takes.
 */
public final class PadFit {

    public static final int MAX_KEY = 88;
    public static final int MIN_KEY = 44;

    /**
     * HEADROOM THE ARITHMETIC LEAVES FOR SUB-PIXEL ROUNDING, in dp. keyDp reasons in dp, but Keypad
     * lays each key and its two margins out with `Skin.dp` (a dp→px ROUND) — twelve conversions down
     * the pad, each of which can round up by ~half a pixel. Across four rows that is up to ~6px, which
     * a box measured with a floor()'d dp height cannot always absorb: the bottom row then ends a pixel
     * or two past the pad and is clipped (`DialerDeviceTest.everyKeyFitsInsideThePadsBox` on Android
     * 14). Reserving a few dp guarantees the built pad fits the box it was sized from; the key shrinks
     * by at most one step at the boundary, which is imperceptible.
     */
    public static final int FIT_SLACK = 6;

    private PadFit() { }

    /** The space around one key, per side. */
    public static int marginDp(int key) { return key >= 70 ? 9 : 7; }

    /** One key plus its margins, which is what a row and a column are made of. */
    public static int cellDp(int key) { return key + 2 * marginDp(key); }

    /**
     * The largest key diameter (dp) for a pad of 3 columns x 4 rows that fits `widthDp` by
     * `heightDp`, with `numberDp` of that height already taken by the number above it. When even the
     * smallest key does not fit, the smallest key — a key too small to read is worse than a clip.
     */
    public static int keyDp(int widthDp, int heightDp, int numberDp) {
        for (int k = MAX_KEY; k > MIN_KEY; k--) {
            int cell = cellDp(k);
            if (3 * cell <= widthDp && 4 * cell + numberDp + FIT_SLACK <= heightDp) return k;
        }
        return MIN_KEY;
    }
}
