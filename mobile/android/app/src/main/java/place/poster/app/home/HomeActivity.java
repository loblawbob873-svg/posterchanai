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
import android.os.SystemClock;
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

    /**
     * THE SHAPE OF THE GRID THE DESKTOP IS CURRENTLY STORED UNDER — "4x5" on a phone, "6x4" on a
     * tablet turned sideways. Every read and every write of the arrangement goes through it, which
     * is what lets a rotation be reversible instead of a re-flow. See HomeMetrics.geometry.
     */
    private String geom = "";

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

    /* Android does not promise that bringing an existing HOME activity forward arrives through
     * onNewIntent.  Some launchers resume it through onStart only; others call onNewIntent and then
     * onStart for the same press.  Track the visible transition so both shapes count ONE press. */
    private boolean homeVisible = false;
    private boolean homeIntentBeforeStart = false;
    private boolean homeStartPending = false;
    private final Runnable countHomeStart = new Runnable() {
        @Override public void run() {
            if (!homeStartPending) return;
            homeStartPending = false;
            HomeDoublePress.arrived(SystemClock.elapsedRealtime());
        }
    };

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
        // The drawer's GridView is `numColumns="auto_fit"`, so this number and the screen width are
        // its column count. 80dp across a 10-inch tablet is a mosaic of thumbnails.
        grid.setColumnWidth(Skin.dp(this, HomeMetrics.drawerColumnDp(swDp())));
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
        resizeSoon();
    }

    /**
     * THE ROW COUNT AFTER THE FIRST LAYOUT PASS, which is the only moment it can be known.
     *
     * `deskRows()` divides the height the desktop actually got, and during onCreate and onStart that
     * height is zero — so every first draw used the fallback and the grid only ever adapted to the
     * screen if something later happened to redraw it (a package installed, the drawer closing). On
     * a tall phone that meant five rows on a screen with room for seven, permanently, unless you
     * installed an app. `post` runs after layout, and this redraws only if the shape actually
     * changed, so it costs nothing when the fallback was already right.
     */
    private void resizeSoon() {
        if (desk == null) return;
        desk.post(new Runnable() {
            @Override public void run() {
                // Always redraw once after measurement. Even when the grid SHAPE was already right,
                // maxResizeWidth/Height are converted through the final cell pixels; the first draw
                // uses provisional dimensions and can leave a phone widget one column too wide.
                redrawDesk();
            }
        });
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
        // A hidden -> visible transition is the first Home press on launchers which resume an
        // existing activity without onNewIntent.  If onNewIntent already reported this same
        // transition, do not manufacture a second press from one physical tap.
        if (!homeVisible && !homeIntentBeforeStart) {
            /* Some Android builds deliver ONE physical HOME as onStart followed by onNewIntent.
             * Count on the next loop turn so that echo can cancel this pending count; two genuine
             * onNewIntent deliveries still pass through independently and remain a double press. */
            homeStartPending = true;
            main.post(countHomeStart);
        }
        homeVisible = true;
        homeIntentBeforeStart = false;
        // WE ARE THE RESTING STATE OF THE PHONE NOW. Folder sync reads this to keep a due sweep from
        // starting in the half-second before somebody taps an icon — see LauncherState.
        LauncherState.homeShown(System.currentTimeMillis());
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
        homeVisible = false;
        homeIntentBeforeStart = false;
        homeStartPending = false;
        main.removeCallbacks(countHomeStart);
        LauncherState.homeHidden();
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
        // NOT filtered by whether we hold the dialer/SMS role any more — see HomeTiles.ours. The
        // only question left is whether the screen exists in THIS build, which is `canLaunch`.
        List<AppShelf.Entry> offered = HomeTiles.ours();
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
        // onStart and onNewIntent may describe the same physical HOME in either order. Suppress the
        // pending onStart count when it came first; suppress the later onStart when this came first.
        if (homeStartPending) {
            homeStartPending = false;
            main.removeCallbacks(countHomeStart);
        } else if (!homeVisible) {
            homeIntentBeforeStart = true;
        }
        // One HOME remains an ordinary launcher action. A quick second one explicitly opens Social
        // at its top through the same consume-once carrier every launcher tile uses.
        if (HomeDoublePress.arrived(SystemClock.elapsedRealtime())) {
            Log.i(TAG, "home double press: opening active feed at top");
            openApp("__feed_top");
        }
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
                        // AFTER redrawAll, because it needs `ourTiles` — which is what THIS build
                        // actually offers and can launch — and it changes the desktop, which then
                        // has to be drawn again.
                        adoptTiles();
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
        prefs.setDesk(HomeMetrics.geometry(cols, rows), Desk.serialize(items));
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

    /**
     * A TILE THAT BECAME AVAILABLE AFTER THIS PHONE WAS SET UP GETS A PLACE, ONCE.
     *
     * `seedHome` runs on the FIRST RUN and never again, which is what stops a removed icon coming
     * back — and it means an install that already exists never sees a tile added later. Messages and
     * Phone were withheld from `HomeTiles.ours` until the app held the SMS / dialer role, so an
     * install seeded before that gate was lifted has them on neither the desktop nor the dock, with
     * nothing that will ever put them there: "posterchan is the default messaging app but still no
     * desktop / app icon ... for text".
     *
     * IT MUST NOT RE-ADD SOMETHING SOMEBODY DELETED, and that is the whole difficulty: removing an
     * icon from the desktop does not hide it, so a removal leaves no trace and is indistinguishable
     * from a tile that was never offered. So the record is kept explicitly (`prefs.adopted`) and only
     * ever grows — placed once, remembered for ever, whatever happens to the icon afterwards.
     *
     * The baseline for an install that predates the record is DELIBERATELY GENEROUS: everything the
     * catalogue already had counts as offered, except Phone and Messages, whose absence has a written
     * cause. A looser baseline would re-place icons people had removed on purpose.
     */
    private void adoptTiles() {
        try {
            java.util.Set<String> offered = prefs.adopted();
            if (!prefs.adoptSeeded()) {
                offered.addAll(HomeTiles.alreadyOffered());
                prefs.setAdopted(offered);
                prefs.setAdoptSeeded(true);
            }
            List<String> want = HomeTiles.unadopted(ourTiles, offered, prefs.hidden(),
                                                    prefs.desk(geom), prefs.dock());
            if (want.isEmpty()) return;
            int cols = deskCols(), rows = deskRows();
            List<Desk.Item> items = Desk.parse(prefs.desk(HomeMetrics.geometry(cols, rows)));
            boolean any = false;
            for (String key : want) {
                // THE DESKTOP, NOT THE DOCK. The dock is capped and already full on a phone somebody
                // has arranged, so joining it means pushing one of their choices out — a fix that
                // takes something away is not one. A desktop that has no room simply does not get
                // the icon; it is still in the drawer, which is where it was already reachable.
                if (Desk.add(items, new Desk.Item(key, 0, 0, 1, 1), cols, rows)) any = true;
                offered.add(key);
            }
            prefs.setAdopted(offered);
            if (!any) return;
            prefs.setDesk(HomeMetrics.geometry(cols, rows), Desk.serialize(items));
            redrawDesk();
        } catch (Throwable t) {
            Log.w(TAG, "home: could not place a newly available tile", t);
        }
    }

    /** The device's short side in dp — the same number in both orientations. See HomeMetrics. */
    private int swDp() {
        try { return getResources().getConfiguration().smallestScreenWidthDp; }
        catch (Throwable t) { return 360; }
    }

    private int widthDp() {
        try { return getResources().getConfiguration().screenWidthDp; }
        catch (Throwable t) { return 360; }
    }

    private int deskCols() { return HomeMetrics.deskCols(swDp()); }

    /** Rows from the space actually available, so a tall phone gets more and a rotation adapts. */
    private int deskRows() {
        View host = findViewById(R.id.pc_home_desk);
        int h = host == null ? 0 : host.getHeight();
        return HomeMetrics.deskRows(cellDp(h), swDp());
    }

    private int dockMax() { return HomeMetrics.dockMax(widthDp(), swDp()); }

    /**
     * A ROTATION IS A DIFFERENT HOME SCREEN, NOT A REDRAW OF THE SAME ONE — and until now it was
     * neither. `configChanges` lists orientation and screenSize so the activity is not torn down,
     * which is right; but nothing recomputed the grid, so a tablet turned sideways kept its portrait
     * row count and simply stretched every cell. The dock kept a phone's five slots at a phone's
     * size across a foot of bar.
     *
     * Recomputing is only safe because the arrangement is stored per grid shape: the landscape
     * desktop is its own arrangement, inherited from the portrait one the first time and its own
     * from then on, so turning the tablet back puts everything exactly where it was.
     */
    @Override
    public void onConfigurationChanged(android.content.res.Configuration cfg) {
        super.onConfigurationChanged(cfg);
        applyTheme();
        if (grid != null) grid.setColumnWidth(Skin.dp(this, HomeMetrics.drawerColumnDp(swDp())));
        redrawAll();
        // The new height is not laid out yet at this point — the width in the Configuration is,
        // which is why the columns are right immediately and the rows need the pass below.
        resizeSoon();
    }

    private void redrawAll() {
        everything = AppShelf.arrange(installed, ourTiles, null, null, "");
        redrawDesk();
        redrawDock();
        redrawDrawer();
    }

    // ---------------------------------------------------------------- the desktop

    private void redrawDesk() {
        int cols = deskCols(), rows = deskRows();
        geom = HomeMetrics.geometry(cols, rows);
        String stored = prefs.desk(geom);
        List<Desk.Item> items = Desk.parse(stored);

        // THE DEAD ARE REMOVED BEFORE THE LIVING ARE FITTED. An item whose app is gone (uninstalled)
        // or whose widget id no longer binds still occupies cells, and fitting around a corpse is
        // how a widget that would have fitted gets pushed into overflow. A widget's id is given back
        // here — that is the one place a release is safe, because the system has already said the
        // id binds to nothing.
        List<Desk.Item> live = new ArrayList<Desk.Item>();
        for (Desk.Item it : items) {
            if (it.isWidget()) {
                if (widgets.infoOf(it.widgetId()) == null) { widgets.release(it.widgetId()); continue; }
            } else if (AppShelf.byKey(everything, it.key) == null) {
                continue;
            }
            live.add(it);
        }

        // NOTHING IS DROPPED FOR NOT FITTING — and until now that promise was only kept by Desk,
        // not by this method. `Desk.fit` hands back what it could not place and the old code simply
        // did not carry it forward, then SAVED the shortened arrangement: on a phone grid (4 columns
        // against a tablet's 5-7) a widget that no longer had room was deleted from the desktop,
        // permanently, with its id still bound to a widget nobody could see and nothing said. That
        // is the "widgets look great on tablet" half of "widgets need support to fit on mobile phone
        // screen".
        //
        // So an overflowed item is offered a smaller shape first — down to the floor the provider
        // itself declares, never below it — and anything that still has nowhere to go is KEPT IN
        // THE SAVED ARRANGEMENT rather than erased. It is off the screen for this draw and back on
        // the next grid that can hold it, which is what a rotation, a fold or an uninstall provides.
        int cellW = 0, cellH = 0;
        View deskHost = findViewById(R.id.pc_home_desk);
        if (deskHost != null) {
            cellW = deskHost.getWidth() / Math.max(1, cols);
            cellH = deskHost.getHeight() / Math.max(1, rows);
        }
        List<Desk.Item> stranded = new ArrayList<Desk.Item>();
        for (Desk.Item it : Desk.fit(live, cols, rows)) {
            if (Desk.addShrinking(live, it, minCells(it, cols, cellW, true),
                                  minCells(it, rows, cellH, false), cols, rows)) continue;
            stranded.add(it);
        }
        List<Desk.Item> saved = new ArrayList<Desk.Item>(live);
        saved.addAll(stranded);
        // Compared as text rather than by counting: `fit` can shrink a span without dropping
        // anything, and a size change that is not written down comes back on the next draw.
        String after = Desk.serialize(saved);
        if (!after.equals(stored)) prefs.setDesk(geom, after);
        desk.setGrid(cols, rows);
        desk.setItems(live);
        // EVERY WIDGET IS TOLD ITS SIZE AGAIN, not only the one that was just added or dragged.
        //
        // A provider that is not told draws the layout for its old size inside the new hole — the
        // "too wide and I can't see the text for the city name" shape. `onResized` used to run only
        // at placement and after a drag-resize, so a rotation, a fold, a different launcher grid or
        // simply a build that changed how spans are computed left every existing widget rendering
        // for a box it no longer has. Posted, because cellW()/cellH() are zero until the desk has
        // been laid out and a size derived from zero is worse than no size at all.
        final List<Desk.Item> told = new ArrayList<Desk.Item>(live);
        desk.post(new Runnable() {
            @Override public void run() {
                if (desk.getWidth() <= 0 || desk.getHeight() <= 0) return;
                // PUT ANYTHING OVERSIZED BACK INSIDE WHAT ITS PROVIDER ASKED FOR — and it happens
                // HERE, not up in the body of redrawDesk, because up there the desk has not been
                // laid out yet: `cellW` is zero on the draw that follows a launch, a ceiling
                // derived from zero is no ceiling, and nothing redraws again once the grid SHAPE
                // has settled (see resizeSoon). The first version of this clamp sat in that dead
                // spot and a full-width widget stayed full width, which is the bug it was written
                // for. Measured first, then acted on.
                boolean changed = false;
                for (Desk.Item it : told) {
                    if (!it.isWidget()) continue;
                    int mx = maxCells(it, desk.cols(), desk.cellW(), true);
                    int my = maxCells(it, desk.rows(), desk.cellH(), false);
                    if (it.spanX > mx) { it.spanX = mx; changed = true; }
                    if (it.spanY > my) { it.spanY = my; changed = true; }
                }
                if (changed) {
                    // Written and redrawn ONCE — the next pass finds nothing over its ceiling and
                    // stops, so this cannot become a loop.
                    prefs.setDesk(geom, Desk.serialize(told));
                    redrawDesk();
                    return;
                }
                for (Desk.Item it : told) if (it.isWidget()) onResized(it, desk.cellW(), desk.cellH());
            }
        });
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

    @Override public void onChanged() { prefs.setDesk(geom, Desk.serialize(desk.items())); }

    /** What is on the desktop right now. For the device test that drives placeWidget for real. */
    java.util.List<Desk.Item> deskItemsForTest() { return desk.items(); }

    @Override public int minSpanX(Desk.Item item) {
        return minCells(item, desk.cols(), desk.cellW(), true);
    }

    @Override public int minSpanY(Desk.Item item) {
        return minCells(item, desk.rows(), desk.cellH(), false);
    }

    @Override public int maxSpanX(Desk.Item item) {
        return maxCells(item, desk.cols(), desk.cellW(), true);
    }

    @Override public int maxSpanY(Desk.Item item) {
        return maxCells(item, desk.rows(), desk.cellH(), false);
    }

    /**
     * THE FEWEST CELLS THIS ITEM MAY OCCUPY, on a grid that is not necessarily the one the DeskView
     * is currently showing.
     *
     * The grid is passed in rather than read off `desk`, because `redrawDesk` needs this answer for
     * the grid it is ABOUT to apply — reading `desk.cols()` there returns the previous shape, which
     * on the one draw that matters (a rotation, a fold, the first draw after an update) is the wrong
     * screen. An unmeasured cell (`cellPx <= 0`, the very first layout pass) yields 1 rather than a
     * number derived from a zero, so an item is never shrunk on the strength of a measurement that
     * has not happened.
     *
     * `minResizeWidth`/`minResizeHeight` are the provider's own floor and are preferred over
     * `minWidth`/`minHeight`: a resizable widget declaring both is saying "this is what I asked for,
     * and this is the smallest I can still draw".
     */
    /**
     * THE MOST CELLS THIS ITEM MAY OCCUPY — the provider's own ceiling, and on a phone it is the
     * half that matters.
     *
     * "the weather widget is just too gigantic on phones." A placed widget is stored with a SPAN and
     * nothing ever re-derives it, so a span produced by the density-inflated arithmetic that used to
     * run here — a 180dp card asking for six of a four-column grid, capped to the full width —
     * stayed full width for ever, on every draw, on every build after the arithmetic was fixed. The
     * only way out was to remove it, and removing it was broken too.
     *
     * `maxResizeWidth`/`maxResizeHeight` are the widget SAYING how big it wants to get, so putting
     * it back inside them is not the launcher overruling a person: it is the launcher stopping
     * overruling the widget. A provider that declares nothing gets no opinion and is left alone,
     * which is every third-party widget on the phone.
     *
     * FLOOR, not ceil — a ceiling rounded up is not a ceiling — and never below the minimum, or a
     * widget with a silly manifest would be clamped into a shape it cannot draw.
     */
    private int maxCells(Desk.Item item, int gridSpan, int cellPx, boolean wide) {
        int grid = Math.max(1, gridSpan);
        if (item == null || !item.isWidget() || cellPx <= 0) return grid;
        if (android.os.Build.VERSION.SDK_INT < 31) return grid;
        AppWidgetProviderInfo i = widgets.infoOf(item.widgetId());
        if (i == null) return grid;
        int declared;
        try { declared = wide ? i.maxResizeWidth : i.maxResizeHeight; }
        catch (Throwable t) { return grid; }
        if (declared <= 0) return grid;              // the provider has no opinion
        // PIXELS, like every other size on this class, and that was worth measuring rather than
        // reasoning about — twice. `maxResizeWidth` (API 31) is resolved against the display density
        // by the platform exactly as `minWidth` is: the device printed `ceiling=715x550` for a
        // manifest saying `260dp` at density 2.75, beside `min=495x220` for `180dp`. A previous
        // version of this line multiplied by the density a second time, which put every ceiling past
        // the width of the grid and made the clamp a no-op — the same shape as the px/dp bug it was
        // written to clean up after. WidgetDeviceTest prints both families side by side so the next
        // person reads a number instead of guessing.
        int cells = Math.max(1, declared / cellPx);
        int lo = minCells(item, grid, cellPx, wide);
        /* NEVER THE WHOLE WIDTH OF A PHONE, and that rule belongs to the GRID rather than to any
         * number a provider can write down.
         *
         * A ceiling in dp means two different things on a 4-column phone and a 6-column tablet: set
         * it small enough that a phone cannot be filled and a tablet has almost no room to adjust
         * ("on tablet, impossible to adjust right"); set it large enough for a tablet and the phone
         * is back to a card across every column, which is where this started. So the provider's
         * ceiling is generous and the phone's own narrowness supplies the limit — one column is kept
         * free, so the widget is always visibly a card on a page rather than a band.
         *
         * WIDTH ONLY. A tall widget is an ordinary thing to want and nothing was ever reported about
         * one. And never below the floor: a grid so narrow that `grid - 1` falls under the smallest
         * shape the provider will draw must yield to the provider, or the widget is pinned — which
         * is its own bug, reported the day this ceiling was introduced. */
        if (wide && grid <= 4) cells = Math.min(cells, grid - 1);
        return Math.max(lo, Math.min(grid, Math.max(1, cells)));
    }

    private int minCells(Desk.Item item, int gridSpan, int cellPx, boolean wide) {
        if (item == null || !item.isWidget() || cellPx <= 0) return 1;
        AppWidgetProviderInfo i = widgets.infoOf(item.widgetId());
        if (i == null) return 1;
        // PIXELS, both of them. These fields are resolved against the display density by the
        // platform, so comparing them to a cell measured in dp multiplied every demand by the
        // density — see Widgets.spanFor.
        int px = wide ? (i.minResizeWidth > 0 ? i.minResizeWidth : i.minWidth)
                      : (i.minResizeHeight > 0 ? i.minResizeHeight : i.minHeight);
        return Math.min(Math.max(1, gridSpan), Widgets.spanFor(px, cellPx));
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
        List<AppShelf.Entry> row = AppShelf.dock(everything, prefs.dock(), dockMax());
        for (final AppShelf.Entry e : row) dock.addView(dockIcon(e));
        // NO ALL-APPS BUTTON. The drawer opens by swiping up from the home surface (DeskView), which
        // is what every Android launcher has done since Pixel dropped the button and what people's
        // hands already expect. Taking it off gives the slot back to an app somebody actually uses,
        // which is the point of a dock.
    }

    private LinearLayout.LayoutParams dockParams() {
        int s = Skin.dp(this, HomeMetrics.dockIconDp(swDp()));
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

    /**
     * The only WebView start in this package.
     *
     * NOT DRESSED AS A LAUNCHER PRESS. This used to add ACTION_MAIN and CATEGORY_LAUNCHER, which is
     * the intent the system delivers when somebody taps an icon on their home screen — and its
     * contract is "bring this app back the way I left it". On a warm start the extras therefore went
     * nowhere and every tile opened whatever had been on screen last: "on tablet, email app is
     * loading News!", "same for other apps". The component is explicit, so those two decorations
     * bought nothing and cost the payload. See LaunchView.
     */
    private void openApp(String view) {
        try {
            boolean particular = view != null && !view.isEmpty() && !HomeTiles.VIEW_APP.equals(view);
            // Parked BEFORE the start, never after: on a fast device the target can resume and read
            // before this method's next line runs.
            if (particular) LaunchView.request(view, System.currentTimeMillis());
            else LaunchView.clear();
            Intent i = new Intent(this, MainActivity.class)
                    .setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
            if (particular) {
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
                    case 0: addWidget(); break;
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
        // THE TITLE NAMES WHAT WAS PRESSED. A menu about an existing widget headed "Add a widget"
        // reads as the wrong menu, which on the one item whose Remove was unreachable is the last
        // thing it should say.
        showAppMenu(item.isWidget() ? widgetLabel(item) : (e == null ? "" : e.label),
             labels, new Pick() {
            @Override public void pick(int w) {
                switch (acts.get(w)) {
                    case 0: removeFromDesk(item); break;
                    case 1: addToDock(e); break;
                    case 2: repo.appInfo(e); break;
                    case 3: addWidget(); break;
                    default: break;                        // 9 = the hint; the frame is already up
                }
             }
        }, e);
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
        showAppMenu(e.label, labels, new Pick() {
            @Override public void pick(int w) {
                switch (acts.get(w)) {
                    case 0: addToDesk(e); break;
                    case 1: addToDock(e); break;
                    case 2: if (!repo.appInfo(e)) toast(getString(R.string.home_cannot_open)); break;
                    case 3: if (!repo.uninstall(e)) toast(getString(R.string.home_no_uninstaller)); break;
                    case 4: prefs.setHidden(AppShelf.hide(prefs.hidden(), e)); redrawDrawer(); break;
                    case 5: pickOurApps(); break;
                    case 6: closeDrawer(); addWidget(); break;
                }
            }
        }, e);
    }

    /** What a placed widget calls itself, for the menu that removes it. */
    private String widgetLabel(Desk.Item item) {
        AppWidgetProviderInfo i = widgets.infoOf(item.widgetId());
        if (i == null) return getString(R.string.home_widget_gone);
        try {
            CharSequence c = i.loadLabel(getPackageManager());
            if (c != null && c.length() > 0) return c.toString();
        } catch (Throwable ignored) { }
        return getString(R.string.home_add_widget);
    }

    /** EVERY dock item can be removed, including the ones seeded on the first run. */
    private void dockMenu(final AppShelf.Entry e) {
        List<String> labels = new ArrayList<String>();
        labels.add(getString(R.string.home_remove_from_dock));
        labels.add(getString(R.string.home_add_to_home));
        showAppMenu(e.label, labels, new Pick() {
            @Override public void pick(int w) {
                if (w == 0) {
                    List<String> d = new ArrayList<String>(prefs.dock());
                    d.remove(e.key());
                    prefs.setDock(d);
                    redrawDock();
                } else { addToDesk(e); }
            }
        }, e);
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
        prefs.setDesk(geom, Desk.serialize(items));
        closeDrawer();
        redrawDesk();
    }

    // Package-private so the device test can hand it a STALE item — an object with the right key
    // that is not the one on the desk — which is the shape the redraw actually produces.
    void removeFromDesk(Desk.Item item) {
        if (item == null) return;
        // BY KEY, NEVER BY OBJECT IDENTITY. `desk.items()` is rebuilt from stored preferences by
        // every `setItems`, and this activity redraws its own desktop after layout — so the item the
        // menu was opened about is frequently no longer the object on the desk by the time somebody
        // taps Remove. `List.remove(Object)` then matches nothing, removes nothing, and Remove does
        // nothing at all, silently, with the menu closing exactly as if it had worked: "i can't
        // remove widgets".
        List<Desk.Item> items = new ArrayList<Desk.Item>(desk.items());
        for (java.util.Iterator<Desk.Item> it = items.iterator(); it.hasNext(); ) {
            if (item.key.equals(it.next().key)) it.remove();
        }
        // A widget removed from the desktop must give its id back, or it is a row in the system's
        // own table that nothing will ever reclaim.
        if (item.isWidget()) widgets.release(item.widgetId());
        prefs.setDesk(geom, Desk.serialize(items));
        redrawDesk();
    }

    private void addToDock(AppShelf.Entry e) {
        if (e == null) return;
        List<String> d = new ArrayList<String>(prefs.dock());
        if (d.contains(e.key())) { toast(getString(R.string.home_already_there)); return; }
        if (d.size() >= dockMax() - 1) { toast(getString(R.string.home_dock_full)); return; }
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
            // The same reversal as HomeTiles.ours, and it mattered more here: this checklist is
            // where somebody turns a hidden screen back ON, so gating it meant Messages could not be
            // un-hidden without already being the default SMS app — the exact circularity behind
            // "still no SMS app".
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

    /**
     * "ADD A WIDGET" — OUR OWN LIST, and that is the fix for the top complaint.
     *
     * This used to fire ACTION_APPWIDGET_PICK and let Android draw the list. Nothing on a modern
     * Android answers that intent — the system picker belonged to the era when a dialog owned "Add
     * to Home screen" — so the very first step threw, the id was freed, a toast said "this phone has
     * no widget picker", and every route into the flow ended there. Making it findable from three
     * menus did not help, because all three arrived at the same dead end.
     *
     * So the list is drawn here, from `getInstalledProviders()`, the way every third-party launcher
     * has done it for a decade. AND AN EMPTY LIST SAYS SO: "no widgets" and "the picker is missing"
     * are different sentences, and telling them apart is the whole difference between the last three
     * rounds of this bug and this one.
     */
    private void addWidget() {
        final List<Widgets.Choice> rows =
                widgets.providers(desk.cellW(), desk.cellH());
        if (rows.isEmpty()) { toast(getString(R.string.home_no_widgets)); return; }
        final WidgetChoices adapter = new WidgetChoices(rows);
        try {
            new AlertDialog.Builder(this)
                .setTitle(R.string.home_add_widget)
                .setAdapter(adapter,
                        new android.content.DialogInterface.OnClickListener() {
                    @Override public void onClick(android.content.DialogInterface d, int w) {
                        Widgets.Choice c = adapter.choiceAt(w);
                        if (c == null) return;               // a group header
                        int id = widgets.add(HomeActivity.this, c);
                        // >= 0 means it was already allowed to bind and wants no configuration: it
                        // is ready now and there is no activity result coming. -1 means an activity
                        // is asking, or it was refused and has already said so.
                        if (id >= 0) placeWidget(id);
                    }
                })
                .setNegativeButton(android.R.string.cancel, null)
                .show();
        } catch (Throwable t) {
            Log.w(TAG, "home: the widget list would not open", t);
            toast(getString(R.string.home_widget_refused));
        }
    }

    /**
     * THE ROWS OF THE WIDGET LIST: a header per app, then that app's widgets with the real picture
     * of each one.
     *
     * The header is why this is an adapter rather than a list of strings. Thirty rows in one flat
     * column is not scannable, and three of those rows were PosterChan's own — sitting between
     * Photos and System UI, both labelled "PosterChan", which is how "i want the calendar widget and
     * weather widget!" turned out to be a report about a list rather than about missing features.
     * Ours are grouped and first (Widgets.providers), so the three that belong to this launcher are
     * the three you see.
     *
     * A header is NOT selectable — `areAllItemsEnabled`/`isEnabled` — or tapping the word "Clock"
     * silently adds whatever row happened to be under it.
     *
     * The picture is `widgets.preview`, four fallbacks deep. A provider that offers nothing at all
     * still gets a row with its app's initial, because a widget missing from this list is
     * indistinguishable from the bug this screen exists to fix.
     */
    private final class WidgetChoices extends BaseAdapter {
        private final List<Object> rows = new ArrayList<Object>();   // String header | Widgets.Choice

        WidgetChoices(List<Widgets.Choice> choices) {
            String app = null;
            for (Widgets.Choice c : choices) {
                if (!c.appLabel.equals(app)) { app = c.appLabel; rows.add(app); }
                rows.add(c);
            }
        }

        /** The index in the ORIGINAL list, or -1 for a header. */
        Widgets.Choice choiceAt(int i) {
            Object o = i >= 0 && i < rows.size() ? rows.get(i) : null;
            return o instanceof Widgets.Choice ? (Widgets.Choice) o : null;
        }

        @Override public int getCount() { return rows.size(); }
        @Override public Object getItem(int i) { return rows.get(i); }
        @Override public long getItemId(int i) { return i; }
        @Override public int getViewTypeCount() { return 2; }
        @Override public int getItemViewType(int i) { return rows.get(i) instanceof String ? 0 : 1; }
        @Override public boolean areAllItemsEnabled() { return false; }
        @Override public boolean isEnabled(int i) { return choiceAt(i) != null; }

        @Override public View getView(int i, View reuse, ViewGroup parent) {
            int p = Skin.dp(HomeActivity.this, 12);
            Widgets.Choice c = choiceAt(i);
            if (c == null) {
                TextView head = new TextView(HomeActivity.this);
                head.setText(String.valueOf(rows.get(i)));
                head.setTextColor(pal.accent);
                head.setTextSize(12);
                head.setAllCaps(true);
                head.setPadding(p, Skin.dp(HomeActivity.this, 14), p, Skin.dp(HomeActivity.this, 4));
                return head;
            }
            LinearLayout row = new LinearLayout(HomeActivity.this);
            row.setOrientation(LinearLayout.HORIZONTAL);
            row.setGravity(Gravity.CENTER_VERTICAL);
            row.setPadding(p, p, p, p);

            // A WIDGET PREVIEW IS WIDE. A square thumbnail crops a 4x1 clock into a smear; this is
            // the shape of the thing that will actually land on the home screen.
            ImageView art = new ImageView(HomeActivity.this);
            art.setLayoutParams(new LinearLayout.LayoutParams(
                    Skin.dp(HomeActivity.this, 76), Skin.dp(HomeActivity.this, 52)));
            art.setScaleType(ImageView.ScaleType.FIT_CENTER);
            Drawable d = widgets.preview(c);
            if (d != null) art.setImageDrawable(d);
            else art.setImageDrawable(Skin.letter(HomeActivity.this, pal, c.appLabel));
            row.addView(art);

            LinearLayout text = new LinearLayout(HomeActivity.this);
            text.setOrientation(LinearLayout.VERTICAL);
            LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(0,
                    ViewGroup.LayoutParams.WRAP_CONTENT, 1f);
            lp.setMargins(p, 0, 0, 0);
            text.setLayoutParams(lp);
            TextView name = new TextView(HomeActivity.this);
            name.setText(c.label);
            name.setTextColor(pal.text);
            name.setTextSize(15);
            TextView sub = new TextView(HomeActivity.this);
            sub.setText(c.spanX + " x " + c.spanY);
            sub.setTextColor(pal.muted);
            sub.setTextSize(12);
            text.addView(name);
            text.addView(sub);
            row.addView(text);
            return row;
        }
    }

    @Override
    protected void onActivityResult(int request, int result, Intent data) {
        super.onActivityResult(request, result, data);
        if (request != Widgets.REQ_BIND && request != Widgets.REQ_CONFIGURE) return;
        int id = widgets.onResult(this, request, result, data);
        if (id < 0) return;                       // still asking, or the person changed their mind
        placeWidget(id);
    }

    /**
     * A bound, configured widget onto the grid — at the biggest size that fits, and its id straight
     * back only when even the smallest shape it will draw at has nowhere to go.
     *
     * IT USED TO ASK ONCE. `Desk.add` at the size the provider asked for, and on a refusal the id was
     * released and the person told the desktop was full. On a tablet (5-7 columns, 6-8 rows) that is
     * nearly always a true statement; on a phone (4 columns, 3-6 rows) the same widget asking for the
     * same rectangle is refused by a desktop with eight icons on it. See Desk.addShrinking.
     */
    // Package-private so the device test can drive the REAL placement rather than re-deriving it:
    // "still can't add widget to phone" is about this method's answer, and arithmetic copied into a
    // test is arithmetic that can agree with itself while the product refuses.
    void placeWidget(int id) {
        AppWidgetProviderInfo info = widgets.infoOf(id);
        int cols = desk.cols(), rows = desk.rows();
        int sx = 1, sy = 1;
        if (info != null) {
            sx = Math.min(cols, Widgets.spanFor(info.minWidth, desk.cellW()));
            sy = Math.min(rows, Widgets.spanFor(info.minHeight, desk.cellH()));
        }
        List<Desk.Item> items = new ArrayList<Desk.Item>(desk.items());
        Desk.Item it = new Desk.Item(Desk.widgetKey(id), 0, 0, sx, sy);
        if (!Desk.addShrinking(items, it, minCells(it, cols, desk.cellW(), true),
                               minCells(it, rows, desk.cellH(), false), cols, rows)) {
            // No room at ANY size it will accept: give the id straight back rather than keeping a
            // widget nobody can see.
            widgets.release(id);
            toast(getString(R.string.home_desktop_full));
            return;
        }
        prefs.setDesk(geom, Desk.serialize(items));
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

    /**
     * An app menu's heading is also its App info shortcut, matching Android's stock launchers. The
     * title is deliberately a real button-shaped TextView instead of AlertDialog.setTitle(): the
     * platform title is not actionable or reliably addressable across Samsung/Google themes.
     */
    private void showAppMenu(String title, List<String> labels, final Pick cb,
                             final AppShelf.Entry app) {
        if (app == null) { show(title, labels, cb); return; }
        CharSequence[] items = labels.toArray(new CharSequence[0]);
        try {
            final TextView heading = appMenuTitle(title);
            final AlertDialog dialog = new AlertDialog.Builder(this)
                    .setCustomTitle(heading)
                    .setItems(items, new android.content.DialogInterface.OnClickListener() {
                        @Override public void onClick(android.content.DialogInterface d, int w) { cb.pick(w); }
                    }).create();
            heading.setOnClickListener(new View.OnClickListener() {
                @Override public void onClick(View ignored) {
                    dialog.dismiss();
                    if (!repo.appInfo(app)) toast(getString(R.string.home_cannot_open));
                }
            });
            dialog.show();
        } catch (Throwable ignored) { }
    }

    private TextView appMenuTitle(String title) {
        TextView v = new TextView(this);
        v.setText(title + "  ⓘ");
        v.setTextColor(pal.text);
        v.setTextSize(18);
        v.setGravity(Gravity.CENTER_VERTICAL);
        int h = Skin.dp(this, 24), y = Skin.dp(this, 18);
        v.setPadding(h, y, h, y);
        v.setBackground(Skin.panel(this, pal));
        v.setContentDescription(title + ". " + getString(R.string.home_app_info));
        v.setClickable(true);
        v.setFocusable(true);
        return v;
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
