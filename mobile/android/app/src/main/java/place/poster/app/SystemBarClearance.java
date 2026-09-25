package place.poster.app;

/**
 * How far the WebView must move so no system bar covers it -- decided from where it IS, not from the
 * insets somebody delivered to it.
 *
 * The first fix margined the WebView by the insets ITS listener received. On a Galaxy S25 (One UI 8,
 * Android 16) the window is drawn under the status bar anyway and that listener never got them: the
 * search box sat under the clock on Nostrverse and Notifications, on the build carrying the fix
 * ("I thought you fixed the search being cut off"). The emulator honours the edge-to-edge opt-out, so
 * its device test could not see it. So: take the bars from the window's own insets, take the view's
 * position in the window, and push it clear by the difference. Where the system already insets the
 * window this answers 0 on every side; where it does not, it answers exactly the overlap.
 *
 * Pure arithmetic on purpose -- tests/test_android_system_bar_clearance.py runs it with javac.
 */
final class SystemBarClearance {
    private SystemBarClearance() {}

    /**
     * @param left,top,right,bottom where the view is in its window NOW (px)
     * @param margins               the margins currently applied {left, top, right, bottom}
     * @param winW,winH             the window's size
     * @param bars                  the system bars + cutout {left, top, right, bottom}
     * @return the margins that put the view's edges clear of every bar
     */
    static int[] margins(int left, int top, int right, int bottom, int[] margins,
                         int winW, int winH, int[] bars) {
        // Where the view would sit with none of our margins -- so a margin already applied is not
        // counted twice, and one that is no longer needed is taken back off.
        int nl = left - margins[0], nt = top - margins[1];
        int nr = right + margins[2], nb = bottom + margins[3];
        return new int[] {
            Math.max(0, bars[0] - nl),
            Math.max(0, bars[1] - nt),
            Math.max(0, nr - (winW - bars[2])),
            Math.max(0, nb - (winH - bars[3])),
        };
    }
}
