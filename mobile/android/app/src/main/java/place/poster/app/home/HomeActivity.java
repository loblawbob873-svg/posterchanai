package place.poster.app.home;

import android.app.Activity;
import android.app.AlertDialog;
import android.appwidget.AppWidgetHostView;
import android.appwidget.AppWidgetProviderInfo;
import android.content.Intent;
import android.graphics.drawable.Drawable;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.text.Editable;
import android.text.TextWatcher;
import android.util.Log;
import android.view.Gravity;
import android.view.KeyEvent;
import android.view.LayoutInflater;
import android.view.View;
import android.view.ViewConfiguration;
import android.view.ViewGroup;
import android.view.inputmethod.InputMethodManager;
import android.widget.AdapterView;
import android.widget.BaseAdapter;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.GridView;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;

import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

import place.poster.app.MainActivity;
import place.poster.app.R;
import place.poster.app.music.MusicService;
import place.poster.app.music.MusicWidget;
import place.poster.app.ui.PcTheme;
import place.poster.app.ui.PcThemeStore;
import place.poster.app.ui.Skin;

/**
 * THE HOME SCREEN. Native, top to bottom, and that is the entire safety argument.
 *
 * A launcher that fails takes the phone's home screen with it. There is no other app to fall back to
 * and no way to reach Settings without knowing a hardware key sequence. This app's WebView renderer
 * is MEASURED to die under memory pressure — that is what MainActivity.surviveRenderProcessDeath
 * exists for — so the home screen must not be able to notice. Nothing in this package inflates a
 * WebView, starts the Capacitor bridge, reads a Nostr key or touches the network.
 *
 * IT IS SHAPED LIKE A HOME SCREEN, because the first version was not and was reported as "missing
 * traditional home desktop view". Three layers:
 *
 *   * the DESKTOP (DeskView) — a cell grid holding icons AND other apps' widgets, dragged into
 *     place and resized by hand. Where things sit is Desk, which is pure and tested.
 *   * the DOCK — the toolbar of main icons along the bottom, on screen always. Its last slot is
 *     always "Phone settings", because that tile is the way back from every mistake this app can
 *     make and the dock is the one place that never scrolls away.
 *   * the DRAWER — every app on the phone, alphabetical and searchable, over the top.
 *
 * BATTERY. With the HOME role this process is foreground whenever nothing else is, i.e. resident for
 * the life of the battery — so anything it polls, it polls for ever. There is no timer, no handler
 * loop, no periodic refresh and no wake lock anywhere in this package. The app list changes only
 * when Android says a package changed; now-playing only when MusicService pushes; the widget host
 * listens only while the screen is up (a listening host receives every update from every provider it
 * holds, which is exactly the poll this package refuses to have).
 *
 * OPT-IN. This component ships DISABLED and is switched on by HomePlugin when the person asks.
 */
public class HomeActivity extends Activity implements DeskView.Host {

    private static final String TAG = "PosterChan";
    public static final String EXTRA_VIEW = "pc_home_view";
    public static final String EXTRA_VIEW_AT = "pc_home_view_at";

    /** How many icons fit across the dock before it starts squeezing them. */
    private static final int DOCK_MAX = 5;

    private AppRepo repo;
    private LauncherPrefs prefs;
    private Widgets widgets;
    private DeskView desk;
    private LinearLayout dock, drawer, nowRow;
    private GridView grid;
    private EditText search;
    private TextView empty, nowText, hint;
    private ImageView nowToggle;
    private Shelf adapter;
    private PcTheme.Palette pal;
    private final Handler main = new Handler(Looper.getMainLooper());

    private List<AppShelf.Entry> installed = new ArrayList<AppShelf.Entry>();
    private List<AppShelf.Entry> ourTiles = new ArrayList<AppShelf.Entry>();
    /** Everything, drawer order — the lookup a desktop key resolves through. */
    private List<AppShelf.Entry> everything = new ArrayList<AppShelf.Entry>();

    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        repo = new AppRepo(this);
        prefs = new LauncherPrefs(this);
        widgets = new Widgets(this);
        boolean firstRun = !prefs.seeded();
        prefs.seedOnce(HomeTiles.defaultHidden());
        pal = PcThemeStore.palette(this);
        setContentView(R.layout.home_activity);

        dock = (LinearLayout) findViewById(R.id.pc_home_dock);
        drawer = (LinearLayout) findViewById(R.id.pc_home_drawer);
        grid = (GridView) findViewById(R.id.pc_home_grid);
        search = (EditText) findViewById(R.id.pc_home_search);
        empty = (TextView) findViewById(R.id.pc_home_empty);
        hint = (TextView) findViewById(R.id.pc_home_hint);
        nowRow = (LinearLayout) findViewById(R.id.pc_home_now);
        nowText = (TextView) findViewById(R.id.pc_home_now_text);
        nowToggle = (ImageView) findViewById(R.id.pc_home_now_toggle);

        desk = new DeskView(this);
        desk.bind(this, pal);
        ((FrameLayout) findViewById(R.id.pc_home_desk)).addView(desk,
                new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT,
                                             ViewGroup.LayoutParams.MATCH_PARENT));

        adapter = new Shelf();
        grid.setAdapter(adapter);
        grid.setOnItemClickListener(new AdapterView.OnItemClickListener() {
            @Override public void onItemClick(AdapterView<?> p, View v, int i, long id) {
                open(adapter.at(i));
                closeDrawer();
            }
        });
        grid.setOnItemLongClickListener(new AdapterView.OnItemLongClickListener() {
            @Override public boolean onItemLongClick(AdapterView<?> p, View v, int i, long id) {
                drawerMenu(adapter.at(i));
                return true;
            }
        });
        search.addTextChangedListener(new TextWatcher() {
            @Override public void beforeTextChanged(CharSequence s, int a, int b, int c) { }
            @Override public void onTextChanged(CharSequence s, int a, int b, int c) { }
            @Override public void afterTextChanged(Editable e) { redrawDrawer(); }
        });
        nowToggle.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { toggleMusic(); }
        });
        nowText.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { openApp("music"); }
        });

        applyTheme();
        refreshRoles();
        reload(firstRun);
    }

    // ---------------------------------------------------------------- theme

    private void applyTheme() {
        pal = PcThemeStore.palette(this);
        if (desk != null) desk.bind(this, pal);
        View root = findViewById(R.id.pc_home_root);
        // A SCRIM, not a paint: the window declares windowShowWallpaper and this keeps the wallpaper
        // visible through the theme instead of replacing it.
        if (root != null) root.setBackground(Skin.page(pal, 0.42));
        // THE DRAWER IS GLASS, NOT A BLACK RECTANGLE. `page(…, 0.97)` is very nearly the flagship's
        // --bg, which is #0a0a0f — "the app drawer is also black and unstylish", fairly. The
        // wallpaper shows through it now, with the same lit edge the bars carry.
        if (drawer != null) {
            drawer.setBackground(Skin.page(pal, 0.72));
            View head = findViewById(R.id.pc_home_search);
            if (head != null) head.setBackground(Skin.glass(this, pal, 0.55, true));
        }
        if (search != null) {
            search.setBackground(Skin.panel(this, pal));
            search.setTextColor(pal.text);
            search.setHintTextColor(pal.muted);
            int p = Skin.dp(this, 11);
            search.setPadding(p, p, p, p);
            search.setCompoundDrawablePadding(Skin.dp(this, 8));
            search.setCompoundDrawablesRelativeWithIntrinsicBounds(
                    tinted(R.drawable.ic_pc_search, pal.muted), null, null, null);
        }
        if (empty != null) empty.setTextColor(pal.muted);
        if (hint != null) { hint.setTextColor(Skin.alpha(pal.text, 0.55)); Skin.legible(hint, pal); }
        if (nowRow != null) nowRow.setBackground(Skin.panel(this, pal));
        if (nowText != null) { nowText.setTextColor(pal.text); Skin.glow(nowText, pal); }
        if (nowToggle != null) nowToggle.setBackground(Skin.pill(this, pal, Skin.alpha(pal.accent, 0.18), true));
        // THE DOCK IS A GLASS PILL. It was a flat panel over the wallpaper, which on the dark
        // palettes is a black bar — "the black dock looks too plain, make it stylish".
        if (dock != null) {
            dock.setBackground(Skin.glass(this, pal, 0.42, true));
            int dp = Skin.dp(this, 10);
            dock.setPadding(dp, dp, dp, dp);
        }
        if (adapter != null) adapter.notifyDataSetChanged();
    }

    private Drawable tinted(int res, int color) { return Skin.icon(this, res, color); }

    /**
     * THE GLYPH FOR ONE OF OUR OWN TILES — and never a letter.
     *
     * Every PosterChan screen has a real icon, transcribed from the client's own sprite, so a letter
     * here would not be a fallback: it would be a bug wearing a disguise. Reported as "the icons are
     * mostly letters for posterchan apps on launcher, ugly", and the letter is worse than ugly —
     * it hides which tile failed and why.
     *
     * So a glyph that will not resolve falls back to the APP'S OWN LAUNCHER ICON, which is in every
     * build and always draws, and says so in the log with the tile's name. The initial-letter
     * fallback stays where it belongs: a third-party app whose icon the package manager would not
     * give us.
     */
    private Drawable ourGlyph(AppShelf.Entry e) {
        HomeTiles.Tile t = HomeTiles.tile(e.view);
        Drawable d = tinted(t == null ? 0 : TileIcons.of(t.icon), pal.accent);
        if (d != null) return d;
        Log.w(TAG, "home: no glyph for tile '" + e.view + "' (icon '"
                + (t == null ? "?" : t.icon) + "') — falling back to the app icon");
        Drawable app = Skin.icon(this, R.mipmap.ic_launcher, 0);
        if (app != null) return app;
        try { return getResources().getDrawable(R.mipmap.ic_launcher); }
        catch (Throwable ignored) { return Skin.letter(this, pal, e.label); }
    }

    // ---------------------------------------------------------------- lifecycle

    @Override
    protected void onStart() {
        super.onStart();
        repo.watch(new AppRepo.Changed() {
            @Override public void onPackagesChanged() {
                main.removeCallbacks(reloadSoon);
                main.postDelayed(reloadSoon, 400);
            }
        });
        MusicService.setWatcher(new MusicService.Watcher() {
            @Override public void onNowPlaying(final String t, final String a, final boolean p) {
                main.post(new Runnable() { @Override public void run() { paintNowPlaying(t, a, p); } });
            }
        });
        // ONLY WHILE ON SCREEN. A listening host receives every update from every provider it holds.
        widgets.start();
        applyTheme();
        refreshRoles();
        redrawAll();
        paintNowPlaying(MusicService.nowTitle(), MusicService.nowArtist(), MusicService.nowPlaying());
    }

    @Override
    protected void onStop() {
        super.onStop();
        repo.stopWatching();
        MusicService.setWatcher(null);
        widgets.stop();
        main.removeCallbacks(reloadSoon);
    }

    private final Runnable reloadSoon = new Runnable() { @Override public void run() { reload(false); } };

    /**
     * WHICH OF OUR TILES ARE REAL — asked of the package manager, not assumed from the catalogue.
     *
     * A TILE THAT CANNOT LAUNCH MUST NOT BE DRAWN. Not greyed, not showing an error when tapped:
     * absent. That was reported from the dock — "there is some P icon on the dock that says this app
     * would not open, useless does nothing" — which is the worst version of it, because the dock is
     * the one row that is always on screen and it was seeded before anything checked.
     *
     * The check is `resolveActivity`, so a screen that is in the catalogue but not in THIS build
     * (an older APK, a stripped variant, a class that moved) simply is not offered.
     */
    private void refreshRoles() {
        List<AppShelf.Entry> offered = HomeTiles.ours(
                HomeRoles.isDefaultDialer(this), HomeRoles.isDefaultSms(this));
        List<AppShelf.Entry> live = new ArrayList<AppShelf.Entry>();
        for (AppShelf.Entry e : offered) if (canLaunch(e)) live.add(e);
        ourTiles = live;
    }

    /** Would tapping this tile actually open something? */
    private boolean canLaunch(AppShelf.Entry e) {
        if (e == null) return false;
        if (!e.isOurs()) return true;                       // a phone app the package manager listed
        if (HomeTiles.VIEW_SETTINGS.equals(e.view)) {
            return resolves(new Intent(Settings.ACTION_SETTINGS));
        }
        String cls = HomeTiles.nativeTarget(e.view);
        if (!cls.isEmpty()) {
            return resolves(new Intent().setClassName(getPackageName(), cls));
        }
        return resolves(new Intent(this, MainActivity.class));
    }

    private boolean resolves(Intent i) {
        try { return getPackageManager().resolveActivity(i, 0) != null; }
        catch (Throwable t) { return false; }
    }

    /** HOME while already home: close the drawer, put down anything lifted, go back to the top. */
    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        closeDrawer();
        if (desk != null) desk.clearEditing();
    }

    /**
     * BACK MUST NOT FINISH THE HOME SCREEN. Finishing it leaves the phone showing whatever is behind
     * it — on a fresh boot, nothing at all. The only things back does here are close the drawer and
     * put down a lifted icon.
     */
    @Override
    public void onBackPressed() {
        if (drawer.getVisibility() == View.VISIBLE) { closeDrawer(); return; }
        if (desk != null && desk.lifted() != null) { desk.clearEditing(); return; }
        // Deliberately no super.onBackPressed().
    }

    @Override
    public boolean onKeyDown(int code, KeyEvent ev) {
        if (code == KeyEvent.KEYCODE_BACK) { onBackPressed(); return true; }
        return super.onKeyDown(code, ev);
    }

    // ---------------------------------------------------------------- loading

    private void reload(final boolean firstRun) {
        new Thread(new Runnable() {
            @Override public void run() {
                final List<AppShelf.Entry> found = repo.installed();
                main.post(new Runnable() {
                    @Override public void run() {
                        installed = found;
                        if (firstRun) seedHome();
                        redrawAll();
                        warmFirstScreen();
                    }
                });
            }
        }, "pc-home-scan").start();
    }

    /** A home screen somebody has never arranged should still look like one. */
    private void seedHome() {
        List<Desk.Item> items = new ArrayList<Desk.Item>();
        int cols = deskCols(), rows = deskRows();
        for (HomeTiles.Tile t : HomeTiles.catalogue()) {
            if (!t.defaultOn || HomeTiles.isEssential(t.view)) continue;
            if (HomeTiles.VIEW_PHONE.equals(t.view) || HomeTiles.VIEW_TEXTS.equals(t.view)) continue;
            Desk.add(items, new Desk.Item("pc:" + t.view, 0, 0, 1, 1), cols, rows);
        }
        prefs.setDesk(Desk.serialize(items));
        // THE DOCK IS NEVER SEEDED WITH SOMETHING UNVERIFIED. `ourTiles` has already been filtered
        // to what resolves, so seeding from it means the very first thing a new person sees cannot
        // be a dead button — which is exactly what was reported.
        List<String> d = new ArrayList<String>();
        for (String view : new String[]{ HomeTiles.VIEW_PHONE, HomeTiles.VIEW_TEXTS, HomeTiles.VIEW_APP }) {
            String key = "pc:" + view;
            if (AppShelf.byKey(ourTiles, key) != null) d.add(key);
        }
        prefs.setDock(d);
    }

    private int deskCols() { return 4; }

    /** Rows from the space actually available, so a tall phone gets more and a rotation adapts. */
    private int deskRows() {
        View host = findViewById(R.id.pc_home_desk);
        int h = host == null ? 0 : host.getHeight();
        if (h <= 0) return 5;
        int cell = Skin.dp(this, 92);
        return Math.max(3, Math.min(8, h / cell));
    }

    private void redrawAll() {
        everything = AppShelf.arrange(installed, ourTiles, null, null, "");
        redrawDesk();
        redrawDock();
        redrawDrawer();
    }

    // ---------------------------------------------------------------- the desktop

    private void redrawDesk() {
        List<Desk.Item> items = Desk.parse(prefs.desk());
        int cols = deskCols(), rows = deskRows();
        // NOTHING IS DROPPED FOR NOT FITTING — a rotation or a different phone re-places it. See
        // Desk.fit; the alternative silently deletes what somebody arranged.
        List<Desk.Item> overflow = Desk.fit(items, cols, rows);
        // An item whose app is gone (uninstalled) is removed here, and a widget's id given back.
        List<Desk.Item> live = new ArrayList<Desk.Item>();
        for (Desk.Item it : items) {
            if (it.isWidget()) {
                if (widgets.infoOf(it.widgetId()) == null) { widgets.release(it.widgetId()); continue; }
            } else if (AppShelf.byKey(everything, it.key) == null) {
                continue;
            }
            live.add(it);
        }
        if (live.size() != items.size() || !overflow.isEmpty()) prefs.setDesk(Desk.serialize(live));
        desk.setGrid(cols, rows);
        desk.setItems(live);
        // An empty desktop says how to fill it; a full one goes back to the swipe hint.
        if (hint != null) hint.setText(live.isEmpty() ? R.string.home_empty_hint : R.string.home_swipe_hint);
    }

    @Override
    public View viewFor(Desk.Item item) {
        if (item.isWidget()) {
            AppWidgetHostView v = widgets.view(this, item.widgetId());
            if (v != null) return v;
            TextView t = new TextView(this);
            t.setText(R.string.home_widget_gone);
            t.setTextColor(pal.muted);
            t.setGravity(Gravity.CENTER);
            return t;
        }
        AppShelf.Entry e = AppShelf.byKey(everything, item.key);
        View cell = LayoutInflater.from(this).inflate(R.layout.home_cell, desk, false);
        bindCell(cell, e);
        return cell;
    }

    @Override public void onOpen(Desk.Item item) {
        if (item.isWidget()) return;                 // a widget's own content handles its taps
        open(AppShelf.byKey(everything, item.key));
    }

    @Override public void onLongPress(Desk.Item item) { deskMenu(item); }

    @Override public void onLongPressEmpty() { homeMenu(); }

    /**
     * SWIPE UP FOR ALL APPS — the gesture every Android launcher has had since the button went away,
     * and the reason there is no drawer button on the dock any more. A button for it reads as a
     * launcher that has not caught up, and it costs a dock slot that belongs to an app somebody uses.
     */
    @Override public void onSwipeUp() { openDrawer(); }

    @Override public void onChanged() { prefs.setDesk(Desk.serialize(desk.items())); }

    @Override public int minSpanX(Desk.Item item) {
        if (!item.isWidget()) return 1;
        AppWidgetProviderInfo i = widgets.infoOf(item.widgetId());
        if (i == null) return 1;
        int dp = i.minResizeWidth > 0 ? i.minResizeWidth : i.minWidth;
        return Math.min(desk.cols(), Widgets.spanFor(dp, cellDp(desk.cellW())));
    }

    @Override public int minSpanY(Desk.Item item) {
        if (!item.isWidget()) return 1;
        AppWidgetProviderInfo i = widgets.infoOf(item.widgetId());
        if (i == null) return 1;
        int dp = i.minResizeHeight > 0 ? i.minResizeHeight : i.minHeight;
        return Math.min(desk.rows(), Widgets.spanFor(dp, cellDp(desk.cellH())));
    }

    @Override public boolean resizable(Desk.Item item) {
        if (item == null || !item.isWidget()) return false;
        return widgets.resizableWide(item.widgetId()) || widgets.resizableTall(item.widgetId());
    }

    @Override public void onResized(Desk.Item item, int cellW, int cellH) {
        if (!item.isWidget()) return;
        int w = cellDp(cellW) * item.spanX, h = cellDp(cellH) * item.spanY;
        // Told in DP, and told at all: a widget whose provider is not updated keeps drawing the
        // layout for its old size inside the new hole.
        widgets.resized(item.widgetId(), w, h, w, h);
    }

    private int cellDp(int px) {
        float d = getResources().getDisplayMetrics().density;
        return d <= 0 ? px : Math.max(1, (int) (px / d));
    }

    // ---------------------------------------------------------------- the dock

    private void redrawDock() {
        if (dock == null) return;
        dock.removeAllViews();
        List<AppShelf.Entry> row = AppShelf.dock(everything, prefs.dock(), DOCK_MAX);
        for (final AppShelf.Entry e : row) dock.addView(dockIcon(e));
        // NO ALL-APPS BUTTON. The drawer opens by swiping up from the home surface (DeskView), which
        // is what every Android launcher has done since Pixel dropped the button and what people's
        // hands already expect. Taking it off gives the slot back to an app somebody actually uses,
        // which is the point of a dock.
    }

    private LinearLayout.LayoutParams dockParams() {
        int s = Skin.dp(this, 52);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(s, s);
        int m = Skin.dp(this, 6);
        lp.setMargins(m, 0, m, 0);
        return lp;
    }

    private View dockIcon(final AppShelf.Entry e) {
        ImageView v = new ImageView(this);
        v.setLayoutParams(dockParams());
        v.setContentDescription(e.label);
        if (e.isOurs()) {
            v.setImageDrawable(ourGlyph(e));
            v.setBackground(Skin.pill(this, pal, Skin.alpha(pal.accent, 0.16), true));
            int p = Skin.dp(this, 12);
            v.setPadding(p, p, p, p);
        } else {
            Drawable d = repo.cachedIcon(e);
            if (d != null) v.setImageDrawable(d);
            else repo.icon(e, main, new Runnable() { @Override public void run() { redrawDock(); } });
        }
        v.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View x) { open(e); }
        });
        v.setOnLongClickListener(new View.OnLongClickListener() {
            @Override public boolean onLongClick(View x) { dockMenu(e); return true; }
        });
        return v;
    }

    // ---------------------------------------------------------------- the drawer

    private void openDrawer() {
        if (desk != null) desk.clearEditing();
        drawer.setVisibility(View.VISIBLE);
        drawer.setAlpha(0f);
        drawer.animate().alpha(1f).setDuration(140).start();
        wireDrawerDismiss();
        redrawDrawer();
    }

    /**
     * A DRAWER YOU CAN ONLY LEAVE ONE WAY is the same complaint in reverse, so there are three:
     * BACK, a swipe DOWN, and pressing HOME. The swipe only counts while the grid is already at the
     * top — otherwise flicking back up through a long app list would close it under your finger.
     */
    private void wireDrawerDismiss() {
        final android.view.GestureDetector g = new android.view.GestureDetector(this,
            new android.view.GestureDetector.SimpleOnGestureListener() {
                @Override public boolean onFling(android.view.MotionEvent a, android.view.MotionEvent b,
                                                 float vx, float vy) {
                    if (a == null || b == null) return false;
                    if (vy <= ViewConfiguration.get(HomeActivity.this).getScaledMinimumFlingVelocity()) return false;
                    if (b.getY() - a.getY() < ViewConfiguration.get(HomeActivity.this).getScaledTouchSlop() * 6) return false;
                    if (grid.getFirstVisiblePosition() != 0) return false;
                    View top = grid.getChildAt(0);
                    if (top != null && top.getTop() < 0) return false;
                    closeDrawer();
                    return true;
                }
            });
        View.OnTouchListener t = new View.OnTouchListener() {
            @Override public boolean onTouch(View v, android.view.MotionEvent e) {
                g.onTouchEvent(e);
                return false;                     // never consume — the list still scrolls
            }
        };
        drawer.setOnTouchListener(t);
        grid.setOnTouchListener(t);
    }

    private void closeDrawer() {
        if (drawer.getVisibility() != View.VISIBLE) return;
        search.setText("");
        hideKeyboard();
        drawer.setVisibility(View.GONE);
    }

    private void redrawDrawer() {
        String q = search == null ? "" : search.getText().toString();
        List<AppShelf.Entry> rows = AppShelf.arrange(installed, ourTiles,
                prefs.hidden(), prefs.order(), q);
        adapter.set(rows);
        boolean none = rows.isEmpty();
        if (empty != null) empty.setVisibility(none ? View.VISIBLE : View.GONE);
        if (grid != null) grid.setVisibility(none ? View.GONE : View.VISIBLE);
    }

    private void warmFirstScreen() {
        List<AppShelf.Entry> rows = adapter.rows;
        int n = Math.min(rows.size(), 30);
        for (int i = 0; i < n; i++) {
            final AppShelf.Entry e = rows.get(i);
            repo.icon(e, main, new Runnable() {
                @Override public void run() { adapter.iconArrived(e); }
            });
        }
    }

    // ---------------------------------------------------------------- opening

    private void open(AppShelf.Entry e) {
        if (e == null) return;
        if (!e.isOurs()) {
            if (!repo.launch(e)) toast(getString(R.string.home_cannot_open));
            return;
        }
        if (HomeTiles.VIEW_SETTINGS.equals(e.view)) {
            // THE WAY BACK. By intent ACTION, never by package name: the Settings app is called
            // something different on every OEM, and a name lookup is how this would silently stop
            // being the escape hatch.
            try {
                startActivity(new Intent(Settings.ACTION_SETTINGS).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
            } catch (Throwable t) { toast(getString(R.string.home_cannot_open)); }
            return;
        }
        if (HomeTiles.VIEW_PHONE.equals(e.view)) { startNative("place.poster.app.phone.DialerActivity"); return; }
        if (HomeTiles.VIEW_TEXTS.equals(e.view)) { startNative("place.poster.app.sms.ThreadListActivity"); return; }
        openApp(e.view);
    }

    private void startNative(String className) {
        try {
            startActivity(new Intent().setClassName(getPackageName(), className)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
        } catch (Throwable t) { toast(getString(R.string.home_cannot_open)); }
    }

    /** The only WebView start in this package. */
    private void openApp(String view) {
        try {
            Intent i = new Intent(this, MainActivity.class)
                    .setAction(Intent.ACTION_MAIN)
                    .addCategory(Intent.CATEGORY_LAUNCHER)
                    .setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
            if (view != null && !view.isEmpty() && !HomeTiles.VIEW_APP.equals(view)) {
                i.putExtra(EXTRA_VIEW, view);
                i.putExtra(EXTRA_VIEW_AT, System.currentTimeMillis());
            }
            startActivity(i);
        } catch (Throwable t) {
            Log.w(TAG, "home: could not open the app", t);
            toast(getString(R.string.home_cannot_open));
        }
    }

    // ---------------------------------------------------------------- menus

    /** Long press on the wallpaper — where every launcher puts its own settings. */
    private void homeMenu() {
        final List<String> labels = new ArrayList<String>();
        labels.add(getString(R.string.home_add_widget));
        labels.add(getString(R.string.home_apps));
        labels.add(getString(R.string.home_unhide));
        labels.add(getString(R.string.home_wallpaper));
        labels.add(getString(R.string.home_settings));
        // THE WAY BACK, with no dock slot spent on it. Always here, needs no stored state, and works
        // however the dock and the desktop have been arranged.
        labels.add(getString(R.string.home_phone_settings));
        show(getString(R.string.app_name), labels, new Pick() {
            @Override public void pick(int w) {
                switch (w) {
                    case 0: widgets.pick(HomeActivity.this); break;
                    case 1: pickOurApps(); break;
                    case 2: showHidden(); break;
                    case 3: fire(new Intent(Intent.ACTION_SET_WALLPAPER)); break;
                    case 4: openApp("settings"); break;
                    case 5: fire(new Intent(Settings.ACTION_SETTINGS)); break;
                }
            }
        });
    }

    /** Long press on something ON the desktop. */
    private void deskMenu(final Desk.Item item) {
        final List<String> labels = new ArrayList<String>();
        final List<Integer> acts = new ArrayList<Integer>();
        if (resizable(item)) { labels.add(getString(R.string.home_resize_hint)); acts.add(9); }
        labels.add(getString(R.string.home_remove_from_home)); acts.add(0);
        final AppShelf.Entry e = item.isWidget() ? null : AppShelf.byKey(everything, item.key);
        if (e != null) {
            labels.add(getString(R.string.home_add_to_dock)); acts.add(1);
            if (!e.isOurs()) { labels.add(getString(R.string.home_app_info)); acts.add(2); }
        }
        labels.add(getString(R.string.home_add_widget)); acts.add(3);
        show(item.isWidget() ? getString(R.string.home_add_widget) : (e == null ? "" : e.label),
             labels, new Pick() {
            @Override public void pick(int w) {
                switch (acts.get(w)) {
                    case 0: removeFromDesk(item); break;
                    case 1: addToDock(e); break;
                    case 2: repo.appInfo(e); break;
                    case 3: widgets.pick(HomeActivity.this); break;
                    default: break;                        // 9 = the hint; the frame is already up
                }
            }
        });
    }

    /** Long press in the drawer. */
    private void drawerMenu(final AppShelf.Entry e) {
        if (e == null) return;
        final List<String> labels = new ArrayList<String>();
        final List<Integer> acts = new ArrayList<Integer>();
        labels.add(getString(R.string.home_add_to_home)); acts.add(0);
        labels.add(getString(R.string.home_add_to_dock)); acts.add(1);
        // WIDGETS ARE FINDABLE FROM HERE TOO — "no widgets can be added to posterchan launcher home
        // screen". The flow existed, behind a long press on the wallpaper, which is not somewhere
        // anybody looks first.
        labels.add(getString(R.string.home_add_widget)); acts.add(6);
        if (!e.isOurs()) {
            labels.add(getString(R.string.home_app_info)); acts.add(2);
            labels.add(getString(R.string.home_uninstall)); acts.add(3);
        }
        if (!e.essential) { labels.add(getString(R.string.home_hide)); acts.add(4); }
        labels.add(getString(R.string.home_apps)); acts.add(5);
        show(e.label, labels, new Pick() {
            @Override public void pick(int w) {
                switch (acts.get(w)) {
                    case 0: addToDesk(e); break;
                    case 1: addToDock(e); break;
                    case 2: if (!repo.appInfo(e)) toast(getString(R.string.home_cannot_open)); break;
                    case 3: if (!repo.uninstall(e)) toast(getString(R.string.home_no_uninstaller)); break;
                    case 4: prefs.setHidden(AppShelf.hide(prefs.hidden(), e)); redrawDrawer(); break;
                    case 5: pickOurApps(); break;
                    case 6: closeDrawer(); widgets.pick(HomeActivity.this); break;
                }
            }
        });
    }

    /** EVERY dock item can be removed, including the ones seeded on the first run. */
    private void dockMenu(final AppShelf.Entry e) {
        List<String> labels = new ArrayList<String>();
        labels.add(getString(R.string.home_remove_from_dock));
        labels.add(getString(R.string.home_add_to_home));
        show(e.label, labels, new Pick() {
            @Override public void pick(int w) {
                if (w == 0) {
                    List<String> d = new ArrayList<String>(prefs.dock());
                    d.remove(e.key());
                    prefs.setDock(d);
                    redrawDock();
                } else { addToDesk(e); }
            }
        });
    }

    private void addToDesk(AppShelf.Entry e) {
        if (e == null) return;
        List<Desk.Item> items = new ArrayList<Desk.Item>(desk.items());
        if (Desk.byKey(items, e.key()) != null) { toast(getString(R.string.home_already_there)); return; }
        if (!Desk.add(items, new Desk.Item(e.key(), 0, 0, 1, 1), desk.cols(), desk.rows())) {
            // A FULL DESKTOP SAYS SO. Silently swallowing the app is how somebody taps "add to home"
            // four times and then reports that the launcher does nothing.
            toast(getString(R.string.home_desktop_full));
            return;
        }
        prefs.setDesk(Desk.serialize(items));
        closeDrawer();
        redrawDesk();
    }

    private void removeFromDesk(Desk.Item item) {
        List<Desk.Item> items = new ArrayList<Desk.Item>(desk.items());
        items.remove(item);
        // A widget removed from the desktop must give its id back, or it is a row in the system's
        // own table that nothing will ever reclaim.
        if (item.isWidget()) widgets.release(item.widgetId());
        prefs.setDesk(Desk.serialize(items));
        redrawDesk();
    }

    private void addToDock(AppShelf.Entry e) {
        if (e == null) return;
        List<String> d = new ArrayList<String>(prefs.dock());
        if (d.contains(e.key())) { toast(getString(R.string.home_already_there)); return; }
        if (d.size() >= DOCK_MAX - 1) { toast(getString(R.string.home_dock_full)); return; }
        d.add(e.key());
        prefs.setDock(d);
        closeDrawer();
        redrawDock();
    }

    /** WHICH POSTERCHAN SCREENS ARE OFFERED. One checklist, writing the same `hidden` set an app uses. */
    private void pickOurApps() {
        final HomeTiles.Tile[] cat = HomeTiles.catalogue();
        final List<HomeTiles.Tile> rows = new ArrayList<HomeTiles.Tile>();
        for (HomeTiles.Tile t : cat) {
            if (HomeTiles.isEssential(t.view)) continue;
            if (HomeTiles.VIEW_PHONE.equals(t.view) && !HomeRoles.isDefaultDialer(this)) continue;
            if (HomeTiles.VIEW_TEXTS.equals(t.view) && !HomeRoles.isDefaultSms(this)) continue;
            rows.add(t);
        }
        Set<String> hidden = prefs.hidden();
        final boolean[] on = new boolean[rows.size()];
        CharSequence[] items = new CharSequence[rows.size()];
        for (int i = 0; i < rows.size(); i++) {
            items[i] = rows.get(i).label;
            on[i] = !hidden.contains("pc:" + rows.get(i).view);
        }
        try {
            new AlertDialog.Builder(this)
                .setTitle(R.string.home_apps)
                .setMultiChoiceItems(items, on, new android.content.DialogInterface.OnMultiChoiceClickListener() {
                    @Override public void onClick(android.content.DialogInterface d, int i, boolean c) { on[i] = c; }
                })
                .setPositiveButton(android.R.string.ok, new android.content.DialogInterface.OnClickListener() {
                    @Override public void onClick(android.content.DialogInterface d, int w) {
                        Set<String> next = new HashSet<String>(prefs.hidden());
                        for (int i = 0; i < rows.size(); i++) {
                            String key = "pc:" + rows.get(i).view;
                            if (on[i]) next.remove(key); else next.add(key);
                        }
                        prefs.setHidden(next);
                        redrawDrawer();
                    }
                })
                .setNegativeButton(android.R.string.cancel, null)
                .show();
        } catch (Throwable ignored) { }
    }

    private void showHidden() {
        Set<String> hidden = prefs.hidden();
        final List<AppShelf.Entry> rows = new ArrayList<AppShelf.Entry>();
        for (AppShelf.Entry e : everything) if (hidden.contains(e.key())) rows.add(e);
        if (rows.isEmpty()) { toast(getString(R.string.home_hidden_none)); return; }
        List<String> labels = new ArrayList<String>();
        for (AppShelf.Entry e : rows) labels.add(e.label);
        show(getString(R.string.home_unhide), labels, new Pick() {
            @Override public void pick(int w) {
                prefs.setHidden(AppShelf.unhide(prefs.hidden(), rows.get(w).key()));
                redrawDrawer();
            }
        });
    }

    // ---------------------------------------------------------------- widgets

    @Override
    protected void onActivityResult(int request, int result, Intent data) {
        super.onActivityResult(request, result, data);
        if (request != Widgets.REQ_PICK && request != Widgets.REQ_BIND
                && request != Widgets.REQ_CONFIGURE) return;
        int id = widgets.onResult(this, request, result, data);
        if (id < 0) return;                       // still asking, or the person changed their mind
        AppWidgetProviderInfo info = widgets.infoOf(id);
        int sx = 1, sy = 1;
        if (info != null) {
            sx = Math.min(desk.cols(), Widgets.spanFor(info.minWidth, cellDp(desk.cellW())));
            sy = Math.min(desk.rows(), Widgets.spanFor(info.minHeight, cellDp(desk.cellH())));
        }
        List<Desk.Item> items = new ArrayList<Desk.Item>(desk.items());
        Desk.Item it = new Desk.Item(Desk.widgetKey(id), 0, 0, sx, sy);
        if (!Desk.add(items, it, desk.cols(), desk.rows())) {
            // No room: give the id straight back rather than keeping a widget nobody can see.
            widgets.release(id);
            toast(getString(R.string.home_desktop_full));
            return;
        }
        prefs.setDesk(Desk.serialize(items));
        redrawDesk();
        onResized(it, desk.cellW(), desk.cellH());
        toast(getString(R.string.home_widget_added));
    }

    // ---------------------------------------------------------------- now playing

    private void paintNowPlaying(String title, String artist, boolean playing) {
        if (nowRow == null) return;
        boolean have = title != null && !title.trim().isEmpty();
        nowRow.setVisibility(have ? View.VISIBLE : View.GONE);
        if (!have) return;
        String who = artist == null || artist.trim().isEmpty() ? "" : "   ·   " + artist;
        nowText.setText(title + who);
        nowToggle.setImageDrawable(tinted(playing ? R.drawable.ic_pc_pause : R.drawable.ic_pc_play, pal.accent));
        nowToggle.setContentDescription(getString(playing ? R.string.home_pause : R.string.home_play));
    }

    /**
     * Through the WIDGET's broadcast, not the service directly: that path is receipt-checked
     * (MusicService.fromWidget), because the WebView holding the audio may have been rebuilt and a
     * press into a dead page has to be noticed rather than reported as success. Nothing here
     * requests audio focus — a second request from this same app takes it from the WebView, at which
     * point Chromium pauses the very element the controls exist to keep playing.
     */
    private void toggleMusic() {
        try {
            sendBroadcast(new Intent(this, MusicWidget.class).setAction(MusicService.ACTION_TOGGLE));
        } catch (Throwable ignored) { }
    }

    // ---------------------------------------------------------------- plumbing

    private interface Pick { void pick(int which); }

    private void show(String title, List<String> labels, final Pick cb) {
        CharSequence[] items = labels.toArray(new CharSequence[0]);
        try {
            new AlertDialog.Builder(this).setTitle(title)
                .setItems(items, new android.content.DialogInterface.OnClickListener() {
                    @Override public void onClick(android.content.DialogInterface d, int w) { cb.pick(w); }
                }).show();
        } catch (Throwable ignored) { }
    }

    private void fire(Intent i) {
        try { startActivity(i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)); }
        catch (Throwable t) { toast(getString(R.string.home_cannot_open)); }
    }

    private void hideKeyboard() {
        try {
            InputMethodManager im = (InputMethodManager) getSystemService(INPUT_METHOD_SERVICE);
            if (im != null && search != null) im.hideSoftInputFromWindow(search.getWindowToken(), 0);
        } catch (Throwable ignored) { }
    }

    private void toast(String s) {
        try { Toast.makeText(this, s, Toast.LENGTH_SHORT).show(); } catch (Throwable ignored) { }
    }

    private void bindCell(View v, AppShelf.Entry e) {
        ImageView icon = (ImageView) v.findViewById(R.id.pc_cell_icon);
        TextView label = (TextView) v.findViewById(R.id.pc_cell_label);
        if (e == null) { label.setText(""); icon.setImageDrawable(null); return; }
        label.setText(e.label);
        label.setTextColor(pal.text);
        Skin.legible(label, pal);
        if (e.isOurs()) {
            icon.setImageDrawable(ourGlyph(e));
            icon.setBackground(Skin.pill(this, pal, Skin.alpha(pal.accent, 0.14), true));
            int p = Skin.dp(this, 9);
            icon.setPadding(p, p, p, p);
            return;
        }
        icon.setBackground(null);
        icon.setPadding(0, 0, 0, 0);
        Drawable d = repo.cachedIcon(e);
        if (d != null) { icon.setImageDrawable(d); return; }
        icon.setImageDrawable(null);
        final AppShelf.Entry entry = e;
        repo.icon(entry, main, new Runnable() { @Override public void run() { redrawDesk(); } });
    }

    /**
     * The drawer's adapter. AppShelf already decided what and in what order; this is careful about
     * two things that decide whether a launcher feels fast: a HOLDER, so scrolling does not call
     * findViewById twice per cell per frame, and AN ARRIVING ICON REDRAWS ONE CELL rather than the
     * whole list — notifyDataSetChanged per icon is a full re-layout of every visible cell, which on
     * a phone with two hundred apps is thousands of layout passes during the first scroll.
     */
    private final class Shelf extends BaseAdapter {
        List<AppShelf.Entry> rows = new ArrayList<AppShelf.Entry>();

        void set(List<AppShelf.Entry> r) {
            rows = r == null ? new ArrayList<AppShelf.Entry>() : r;
            notifyDataSetChanged();
        }

        AppShelf.Entry at(int i) { return i >= 0 && i < rows.size() ? rows.get(i) : null; }

        void iconArrived(AppShelf.Entry e) {
            if (grid == null || e == null) return;
            for (int i = 0; i < grid.getChildCount(); i++) {
                View v = grid.getChildAt(i);
                Object tag = v == null ? null : v.getTag();
                if (!(tag instanceof Holder)) continue;
                Holder h = (Holder) tag;
                if (!e.key().equals(h.key)) continue;
                Drawable d = repo.cachedIcon(e);
                if (d != null) h.icon.setImageDrawable(d);
                return;
            }
        }

        @Override public int getCount() { return rows.size(); }
        @Override public Object getItem(int i) { return at(i); }
        @Override public long getItemId(int i) { return i; }

        @Override
        public View getView(int i, View reuse, ViewGroup parent) {
            View v = reuse;
            Holder h;
            if (v == null) {
                v = LayoutInflater.from(HomeActivity.this).inflate(R.layout.home_cell, parent, false);
                h = new Holder();
                h.icon = (ImageView) v.findViewById(R.id.pc_cell_icon);
                h.label = (TextView) v.findViewById(R.id.pc_cell_label);
                v.setTag(h);
            } else {
                h = (Holder) v.getTag();
            }
            final AppShelf.Entry e = at(i);
            h.key = e == null ? null : e.key();
            bindCell(v, e);
            if (e != null && !e.isOurs() && repo.cachedIcon(e) == null) {
                repo.icon(e, main, new Runnable() { @Override public void run() { iconArrived(e); } });
            }
            return v;
        }
    }

    private static final class Holder {
        ImageView icon;
        TextView label;
        String key;
    }
}
