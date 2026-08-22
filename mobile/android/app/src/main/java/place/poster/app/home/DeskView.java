package place.poster.app.home;

import android.content.Context;
import android.graphics.Canvas;
import android.graphics.Paint;
import android.graphics.Rect;
import android.view.MotionEvent;
import android.view.VelocityTracker;
import android.view.ViewConfiguration;
import android.view.View;
import android.view.ViewGroup;

import java.util.ArrayList;
import java.util.List;

import place.poster.app.ui.PcTheme;
import place.poster.app.ui.Skin;

/**
 * THE DESKTOP ITSELF: a cell grid you can drag things around on and resize widgets in.
 *
 * A ViewGroup rather than a GridView, because a home screen is not a list — items have a POSITION
 * and a SIZE that the person chose, and both have to survive a rotation, a redraw and the app being
 * killed. Where those live is `Desk`, which is pure and tested; this class does three things and
 * defers every decision to it: measure the cells, lay the children out on them, and turn finger
 * movement into `Desk.moveTo` / `Desk.resize` calls that may be REFUSED.
 *
 * That refusal is the point. Dropping an icon on an occupied cell puts it back where it came from,
 * visibly, rather than stacking two things in one place — and a resize that would collide simply
 * does not happen, so a widget never comes back a size nobody asked for.
 *
 * EDIT MODE IS EXPLICIT. A long press lifts one item; while it is lifted it can be dragged, a widget
 * shows resize handles, and a tap anywhere else puts it down. Without that, every scroll of a widget's
 * own content would be a drag of the widget, which is the thing that makes a hand-rolled launcher
 * feel broken.
 */
public class DeskView extends ViewGroup {

    /** What the desktop needs from whoever owns it. */
    public interface Host {
        /** Build the view for an item — an icon or a widget's AppWidgetHostView. */
        View viewFor(Desk.Item item);
        void onOpen(Desk.Item item);
        void onLongPress(Desk.Item item);
        /** A long press on empty space — the launcher's own menu. */
        void onLongPressEmpty();
        /** Swiped up from the home surface: open the app drawer. */
        void onSwipeUp();
        /** Save: the arrangement changed. */
        void onChanged();
        /** The smallest this item may be, in cells. Icons are 1x1; a widget states its own. */
        int minSpanX(Desk.Item item);
        int minSpanY(Desk.Item item);
        /**
         * The LARGEST this item may be, in cells — the provider's own ceiling, or the grid when it
         * declares none. Asked here as well as on redraw so the two agree: clamping only on the
         * redraw would let somebody drag a widget wide, see it stay, and find it shrunk on the next
         * resume, which is the "it moves on its own" failure this whole class is built to avoid.
         */
        int maxSpanX(Desk.Item item);
        int maxSpanY(Desk.Item item);
        boolean resizable(Desk.Item item);
        /** Tell the provider its new pixel size — a widget that is not told draws its old layout. */
        void onResized(Desk.Item item, int cellW, int cellH);
    }

    private Host host;
    private PcTheme.Palette pal;
    private int cols = 4, rows = 5;
    private final List<Desk.Item> items = new ArrayList<Desk.Item>();
    private final List<View> views = new ArrayList<View>();

    /** The lifted item, or null. */
    private Desk.Item editing;
    private boolean dragging, resizing;
    private int grabDx, grabDy, resizeEdge;
    private float lastX, lastY;
    private final Rect tmp = new Rect();
    /* THE SWIPE-UP THAT OPENS THE DRAWER.
     *
     * Measured against ViewConfiguration rather than a hand-picked pixel count: slop and fling
     * velocity are density- and platform-derived, and a number tuned on one phone feels wrong on
     * every other. It also has to LOSE cleanly to the two gestures that share this surface — a long
     * press that becomes a drag, and a resize — so it is only ever considered while nothing is
     * lifted and nothing is being dragged. */
    private VelocityTracker vel;
    private final int slop = ViewConfiguration.get(getContext()).getScaledTouchSlop();
    private final int flingMin = ViewConfiguration.get(getContext()).getScaledMinimumFlingVelocity();
    private float downX, downY;
    private boolean swiping;
    private final Paint grid = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint frame = new Paint(Paint.ANTI_ALIAS_FLAG);

    private static final int EDGE_NONE = 0, EDGE_L = 1, EDGE_T = 2, EDGE_R = 3, EDGE_B = 4;

    public DeskView(Context c) {
        super(c);
        setWillNotDraw(false);
        setClipChildren(false);
    }

    public void bind(Host h, PcTheme.Palette p) { host = h; pal = p; }

    public void setGrid(int c, int r) {
        cols = Math.max(1, c); rows = Math.max(1, r);
        requestLayout();
    }

    /** What is currently lifted, if anything. Read by the device test that proves a long press
     *  reaches a WIDGET — the gesture an AppWidgetHostView's clickable children used to swallow. */
    Desk.Item editingItem() { return editing; }

    public int cols() { return cols; }
    public int rows() { return rows; }
    public List<Desk.Item> items() { return items; }

    /** Replace everything on the desktop. Re-inflates every child, so it is not called per frame. */
    public void setItems(List<Desk.Item> list) {
        editing = null; dragging = false; resizing = false;
        items.clear();
        views.clear();
        removeAllViews();
        if (list != null) items.addAll(list);
        for (Desk.Item it : items) {
            View v = host == null ? null : host.viewFor(it);
            if (v == null) v = new View(getContext());
            // The child must not eat the gesture: the desktop decides what a press means (open,
            // lift, drag), and a widget's own content would otherwise swallow the long press that
            // is the only way to move it.
            v.setClickable(false);
            v.setLongClickable(false);
            views.add(v);
            addView(v);
        }
        requestLayout();
    }

    public int cellW() { return Math.max(1, getWidth() / cols); }
    public int cellH() { return Math.max(1, getHeight() / rows); }

    @Override
    protected void onMeasure(int wSpec, int hSpec) {
        int w = MeasureSpec.getSize(wSpec), h = MeasureSpec.getSize(hSpec);
        setMeasuredDimension(w, h);
        int cw = Math.max(1, w / cols), ch = Math.max(1, h / rows);
        for (int i = 0; i < views.size() && i < items.size(); i++) {
            Desk.Item it = items.get(i);
            views.get(i).measure(
                    MeasureSpec.makeMeasureSpec(cw * it.spanX, MeasureSpec.EXACTLY),
                    MeasureSpec.makeMeasureSpec(ch * it.spanY, MeasureSpec.EXACTLY));
        }
    }

    @Override
    protected void onLayout(boolean changed, int l, int t, int r, int b) {
        int cw = cellW(), ch = cellH();
        for (int i = 0; i < views.size() && i < items.size(); i++) {
            Desk.Item it = items.get(i);
            View v = views.get(i);
            // The lifted item follows the finger instead of its cell.
            if (it == editing && dragging) continue;
            v.layout(it.col * cw, it.row * ch, (it.col + it.spanX) * cw, (it.row + it.spanY) * ch);
        }
    }

    // ---------------------------------------------------------------- painting

    @Override
    protected void dispatchDraw(Canvas canvas) {
        if (editing != null && pal != null) {
            // The grid appears only while something is lifted — a permanently visible grid is a
            // wireframe, not a home screen.
            grid.setStyle(Paint.Style.STROKE);
            grid.setStrokeWidth(1f);
            grid.setColor(Skin.alpha(pal.accent, 0.18));
            int cw = cellW(), ch = cellH();
            for (int c = 1; c < cols; c++) canvas.drawLine(c * cw, 0, c * cw, getHeight(), grid);
            for (int r = 1; r < rows; r++) canvas.drawLine(0, r * ch, getWidth(), r * ch, grid);
        }
        super.dispatchDraw(canvas);
        if (editing != null && pal != null) drawFrame(canvas);
    }

    private void drawFrame(Canvas canvas) {
        int cw = cellW(), ch = cellH();
        int x0 = editing.col * cw, y0 = editing.row * ch;
        int x1 = (editing.col + editing.spanX) * cw, y1 = (editing.row + editing.spanY) * ch;
        frame.setStyle(Paint.Style.STROKE);
        frame.setStrokeWidth(Skin.dp(getContext(), 2));
        frame.setColor(pal.accent);
        float r = Skin.dp(getContext(), Math.max(4, pal.radiusDp));
        canvas.drawRoundRect(x0, y0, x1, y1, r, r, frame);
        if (host == null || !host.resizable(editing)) return;
        frame.setStyle(Paint.Style.FILL);
        float k = Skin.dp(getContext(), 7);
        canvas.drawCircle(x0, (y0 + y1) / 2f, k, frame);
        canvas.drawCircle(x1, (y0 + y1) / 2f, k, frame);
        canvas.drawCircle((x0 + x1) / 2f, y0, k, frame);
        canvas.drawCircle((x0 + x1) / 2f, y1, k, frame);
    }

    // ---------------------------------------------------------------- gestures

    private Runnable pending;
    /** The item whose menu is owed at lift-off, if the finger never moved. See beginTouch. */
    private Desk.Item menuFor;
    /** Whether the owed menu is the wallpaper's rather than an item's. */
    private boolean menuEmpty;
    /** True once a long press has fired: from then on this view takes the gesture off its child. */
    private boolean stealing;
    /** Whether the DOWN bookkeeping has already run for this gesture (intercept sees it first). */
    private boolean begun;

    /**
     * RemoteViews may call this on DOWN (sliders and other clickable widget children commonly do).
     * Honouring it before our 400 ms decision means the parent never receives the later UP, so the
     * widget can consume the only gesture that exposes "Remove from home".  Keep observing while a
     * long press is undecided or owed. A short tap is still never intercepted; movement cancels the
     * pending press through onInterceptTouchEvent and the widget then owns its ordinary control.
     */
    @Override
    public void requestDisallowInterceptTouchEvent(boolean disallowIntercept) {
        if (disallowIntercept && (pending != null || menuFor != null || stealing)) return;
        super.requestDisallowInterceptTouchEvent(disallowIntercept);
    }

    /**
     * A WIDGET COULD NOT BE LONG-PRESSED, WHICH MEANT IT COULD NOT BE REMOVED — reported as "no way
     * to remove widgets", and it was also why one could not be moved or resized.
     *
     * An icon cell is an inert View, so its touches fall straight through to `onTouchEvent` here. An
     * `AppWidgetHostView` is not: the RemoteViews inside it carry PendingIntents, so its children
     * are clickable and CONSUME the DOWN. This view was never told a finger had gone down on a
     * widget, so the long-press was never armed, and every menu that hangs off it — Remove from
     * home, Resize, Add a widget — was unreachable on the one kind of item that most needs them.
     *
     * That is the same shape as 247a1be8 on the desktop, where `.os-wgt-body` being `overflow:auto`
     * handed the touch to the scroller and `touch-action:none` on the ancestor could not save it: a
     * child that consumes the gesture is invisible in the parent's code.
     *
     * So the DOWN is watched HERE, before the child sees it, and the gesture is only STOLEN once the
     * long press has actually fired. A short tap still reaches the widget's own buttons, which is
     * the half a blanket intercept would break.
     */
    @Override
    public boolean onInterceptTouchEvent(MotionEvent e) {
        float x = e.getX(), y = e.getY();
        switch (e.getActionMasked()) {
            case MotionEvent.ACTION_DOWN:
                stealing = false;
                beginTouch(e, x, y);
                // A RESIZE HANDLE IS TAKEN IMMEDIATELY, and without this a WIDGET could not be
                // resized at all — reported as "can't resize it or nothing".
                //
                // `beginTouch` grabs the handle and returns before it arms a long press, so nothing
                // ever sets `stealing`; returning false here left the gesture with the widget's own
                // RemoteViews, which consume it, and every MOVE went to them instead of to
                // `resizeTo`. An ICON is an inert view, so its DOWN fell through to onTouchEvent and
                // resizing worked — which is why this looked fine everywhere except on the one kind
                // of item that has resize handles in the first place.
                if (resizing) { stealing = true; return true; }
                return false;                       // let the child have its tap
            case MotionEvent.ACTION_MOVE:
                if (!stealing && (Math.abs(x - downX) > slop || Math.abs(y - downY) > slop)) {
                    cancelPending();
                }
                return stealing;
            case MotionEvent.ACTION_UP:
            case MotionEvent.ACTION_CANCEL:
                cancelPending();
                begun = false;
                boolean was = stealing;
                stealing = false;
                // THE OWED MENU IS FLUSHED HERE TOO, and it has to be. Once a long press has fired
                // `stealing` is true, so this intercept returns true for the UP — and an event that
                // causes an interception is delivered to the child as ACTION_CANCEL and never
                // reaches this view's own onTouchEvent. That is only ever the case when a CHILD
                // consumed the DOWN, which is exactly a widget: the one item whose menu holds the
                // only Remove there is. Both sites clear the fields, so it cannot fire twice.
                if (e.getActionMasked() == MotionEvent.ACTION_UP) flushMenu();
                else { menuFor = null; menuEmpty = false; }
                return was;
            default:
                return stealing;
        }
    }

    /**
     * The DOWN bookkeeping, shared by the intercept above and `onTouchEvent`.
     *
     * `begun` makes it idempotent for one gesture: over empty space and over an icon BOTH run —
     * intercept first, then onTouchEvent, because nothing consumed the DOWN — and arming the
     * long-press twice would fire two menus.
     */
    private void beginTouch(MotionEvent e, float x, float y) {
        if (begun) return;
        begun = true;
        int cw = cellW(), ch = cellH();
        lastX = x; lastY = y;
        downX = x; downY = y; swiping = false;
        if (vel != null) vel.recycle();
        vel = VelocityTracker.obtain();
        vel.addMovement(e);
        resizeEdge = editing == null ? EDGE_NONE : edgeAt(x, y);
        if (resizeEdge != EDGE_NONE) { resizing = true; return; }
        final int c = (int) (x / cw), r = (int) (y / ch);
        final Desk.Item hit = Desk.at(items, c, r);
        // A long press LIFTS; a tap opens. Posted rather than using a GestureDetector so the
        // same code path serves both and there is one place the two can be told apart.
        pending = new Runnable() {
            @Override public void run() {
                pending = null;
                // FROM HERE THE GESTURE IS OURS. The child gets an ACTION_CANCEL from the framework
                // the moment the next event is intercepted, so a widget's button does not also fire.
                stealing = true;
                // THE MENU IS NOT OPENED YET, and that is the fix for "moving a app is hard when
                // that window pop hides where you want to put the app".
                //
                // A long press means two things at once here — LIFT THIS, so it can be dragged, and
                // OFFER ITS MENU — and the menu is a dialog that covers the desktop. Opening it the
                // instant the press fires put a panel over the very cells the person was dragging
                // towards, while the item was already lifted and following their finger underneath
                // it. So the lift happens now and the menu is DEFERRED to the lift-off, and only if
                // the finger never went anywhere. That is what every launcher does, and it is the
                // difference between "long press to move" and "long press to be interrupted".
                menuFor = hit;
                menuEmpty = (hit == null);
                if (hit != null) {
                    lift(hit);
                }
            }
        };
        postDelayed(pending, 400);
    }

    @Override
    public boolean onTouchEvent(MotionEvent e) {
        int cw = cellW(), ch = cellH();
        float x = e.getX(), y = e.getY();
        switch (e.getActionMasked()) {
            case MotionEvent.ACTION_DOWN: {
                beginTouch(e, x, y);
                if (resizing) return true;
                return true;
            }
            case MotionEvent.ACTION_MOVE: {
                if (vel != null) vel.addMovement(e);
                // A swipe is only ever on the table while nothing is lifted and nothing is being
                // dragged — a drag has to win, or moving an icon upward would open the drawer.
                if (editing == null && !dragging && !resizing) {
                    float sx = x - downX, sy = y - downY;
                    if (Math.abs(sy) > slop * 2 && Math.abs(sy) > Math.abs(sx) * 1.5f) {
                        swiping = true;
                        cancelPending();
                    }
                }
                float dx = x - lastX, dy = y - lastY;
                if (Math.abs(dx) > Skin.dp(getContext(), 8) || Math.abs(dy) > Skin.dp(getContext(), 8)) {
                    cancelPending();
                    // A finger that has travelled is not asking for a menu — on empty space that is
                    // a swipe, and on an item it is a move.
                    menuFor = null; menuEmpty = false;
                }
                if (resizing && editing != null) { resizeTo(x, y); return true; }
                if (editing != null && !dragging && hits(editing, x, y)) {
                    // MOVING IS ANSWERING. Once the item is under way the menu would only be in the
                    // way of where it is going.
                    menuFor = null; menuEmpty = false;
                    dragging = true;
                    grabDx = (int) x - editing.col * cw;
                    grabDy = (int) y - editing.row * ch;
                }
                if (dragging && editing != null) {
                    View v = viewOf(editing);
                    if (v != null) {
                        int left = (int) x - grabDx, top = (int) y - grabDy;
                        v.layout(left, top, left + v.getMeasuredWidth(), top + v.getMeasuredHeight());
                    }
                    invalidate();
                    return true;
                }
                return true;
            }
            case MotionEvent.ACTION_UP:
            case MotionEvent.ACTION_CANCEL: {
                boolean wasTap = pending != null;
                begun = false;
                stealing = false;
                cancelPending();
                if (swiping && e.getActionMasked() == MotionEvent.ACTION_UP) {
                    float vy = 0;
                    if (vel != null) { vel.addMovement(e); vel.computeCurrentVelocity(1000); vy = vel.getYVelocity(); }
                    releaseVel();
                    swiping = false;
                    // Up, and either far enough or fast enough. Both, because a slow deliberate drag
                    // and a quick flick are the same intention and people do each.
                    //
                    // "Far enough" scales with the SCREEN, not only with the density — six times the
                    // touch slop is a deliberate drag on a phone and a twitch on a tablet. The fling
                    // half is unchanged, so a flick opens the drawer at any distance either way.
                    if (y - downY < -HomeMetrics.swipeUpMinPx(slop, getHeight()) || vy < -flingMin) {
                        if (host != null) host.onSwipeUp();
                        return true;
                    }
                    return true;
                }
                releaseVel();
                swiping = false;
                if (resizing) { resizing = false; commitResize(); return true; }
                if (dragging && editing != null) { drop(x, y); return true; }
                // THE MENU A LONG PRESS OWED, opened now that the finger is off and it is plain the
                // person was not dragging. CANCEL does not count: an ACTION_CANCEL is the framework
                // taking the gesture away, not somebody letting go.
                if (e.getActionMasked() == MotionEvent.ACTION_UP && flushMenu()) return true;
                menuFor = null; menuEmpty = false;
                if (wasTap) {
                    Desk.Item hit = Desk.at(items, (int) (x / cw), (int) (y / ch));
                    // A tap while something is lifted PUTS IT DOWN rather than opening whatever was
                    // tapped — otherwise leaving edit mode always launches something.
                    if (editing != null) { editing = null; invalidate(); return true; }
                    if (hit != null && host != null) host.onOpen(hit);
                }
                return true;
            }
        }
        return super.onTouchEvent(e);
    }

    private void releaseVel() {
        if (vel != null) { vel.recycle(); vel = null; }
    }

    /** Open the menu a long press owed, if it is still owed. True when one was opened. */
    private boolean flushMenu() {
        Desk.Item owed = menuFor;
        boolean empty = menuEmpty;
        menuFor = null; menuEmpty = false;
        if (host == null) return false;
        if (owed != null) { host.onLongPress(owed); return true; }
        if (empty) { host.onLongPressEmpty(); return true; }
        return false;
    }

    private void cancelPending() {
        if (pending != null) { removeCallbacks(pending); pending = null; }
    }

    private void lift(Desk.Item it) {
        editing = it;
        dragging = false;
        performHapticFeedback(android.view.HapticFeedbackConstants.LONG_PRESS);
        invalidate();
    }

    /** Put the dragged item down on the cell under the finger, or back where it came from. */
    private void drop(float x, float y) {
        dragging = false;
        int cw = cellW(), ch = cellH();
        int c = Math.max(0, Math.min(cols - editing.spanX, (int) ((x - grabDx + cw / 2f) / cw)));
        int r = Math.max(0, Math.min(rows - editing.spanY, (int) ((y - grabDy + ch / 2f) / ch)));
        if (Desk.moveTo(items, editing, c, r, cols, rows) && host != null) host.onChanged();
        requestLayout();
        invalidate();
    }

    private static int clampSpan(int want, int lo, int hi) {
        int a = Math.max(1, lo), b = Math.max(a, hi);
        return want < a ? a : (want > b ? b : want);
    }

    private int edgeAt(float x, float y) {
        if (host == null || !host.resizable(editing)) return EDGE_NONE;
        int cw = cellW(), ch = cellH();
        float x0 = editing.col * cw, y0 = editing.row * ch;
        float x1 = (editing.col + editing.spanX) * cw, y1 = (editing.row + editing.spanY) * ch;
        float k = Skin.dp(getContext(), 22);
        if (Math.abs(x - x0) < k && y > y0 - k && y < y1 + k) return EDGE_L;
        if (Math.abs(x - x1) < k && y > y0 - k && y < y1 + k) return EDGE_R;
        if (Math.abs(y - y0) < k && x > x0 - k && x < x1 + k) return EDGE_T;
        if (Math.abs(y - y1) < k && x > x0 - k && x < x1 + k) return EDGE_B;
        return EDGE_NONE;
    }

    /** Live resize: recompute spans from the dragged edge, and let Desk refuse a collision. */
    private void resizeTo(float x, float y) {
        int cw = cellW(), ch = cellH();
        int minX = host.minSpanX(editing), minY = host.minSpanY(editing);
        int maxX = Math.max(minX, host.maxSpanX(editing)), maxY = Math.max(minY, host.maxSpanY(editing));
        int col = editing.col, row = editing.row, sx = editing.spanX, sy = editing.spanY;
        // BOUNDED AT BOTH ENDS. The floor is the smallest shape the provider will draw; the ceiling
        // is the biggest it says it wants. Without the ceiling a drag could put a widget back at the
        // gigantic span the redraw exists to correct, and the next resume would shrink it again.
        if (resizeEdge == EDGE_R) sx = clampSpan((int) Math.round(x / cw) - col, minX, maxX);
        else if (resizeEdge == EDGE_B) sy = clampSpan((int) Math.round(y / ch) - row, minY, maxY);
        else if (resizeEdge == EDGE_L) {
            int right = col + sx;
            col = Math.max(0, Math.min(right - minX, Math.max(right - maxX, (int) Math.round(x / cw))));
            sx = right - col;
        } else if (resizeEdge == EDGE_T) {
            int bottom = row + sy;
            row = Math.max(0, Math.min(bottom - minY, Math.max(bottom - maxY, (int) Math.round(y / ch))));
            sy = bottom - row;
        }
        int wasC = editing.col, wasR = editing.row;
        editing.col = col; editing.row = row;
        if (!Desk.resize(items, editing, sx, sy, minX, minY, cols, rows)) {
            editing.col = wasC; editing.row = wasR;
        }
        requestLayout();
        invalidate();
    }

    private void commitResize() {
        if (editing == null || host == null) return;
        host.onResized(editing, cellW(), cellH());
        host.onChanged();
    }

    private boolean hits(Desk.Item it, float x, float y) {
        int cw = cellW(), ch = cellH();
        tmp.set(it.col * cw, it.row * ch, (it.col + it.spanX) * cw, (it.row + it.spanY) * ch);
        return tmp.contains((int) x, (int) y);
    }

    private View viewOf(Desk.Item it) {
        int i = items.indexOf(it);
        return i >= 0 && i < views.size() ? views.get(i) : null;
    }

    /** Put down whatever is lifted. Called when the drawer opens or HOME is pressed. */
    public void clearEditing() {
        if (editing == null) return;
        editing = null; dragging = false; resizing = false;
        requestLayout();
        invalidate();
    }

    public Desk.Item lifted() { return editing; }
}
