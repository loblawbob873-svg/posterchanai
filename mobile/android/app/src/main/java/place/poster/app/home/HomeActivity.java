package place.poster.app.home;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.graphics.drawable.Drawable;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.text.Editable;
import android.text.TextWatcher;
import android.util.Log;
import android.view.KeyEvent;
import android.view.LayoutInflater;
import android.view.View;
import android.view.ViewGroup;
import android.view.inputmethod.InputMethodManager;
import android.widget.AdapterView;
import android.widget.BaseAdapter;
import android.widget.EditText;
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
 * WebView, starts the Capacitor bridge, reads a Nostr key or touches the network. It reads the
 * package manager and draws icons. PosterChan's own screens are tiles that START the browser engine;
 * they are never what the home screen is made of.
 *
 * The decisions live in AppShelf (pure java.util, run by tests); this class draws what it returns.
 *
 * BATTERY. With the HOME role this process is foreground whenever nothing else is, i.e. resident for
 * the life of the battery — so anything it polls, it polls for ever. There is therefore no timer, no
 * handler loop, no periodic refresh and no wake lock anywhere in this package. The app list changes
 * only when Android says a package changed (registered in onStart, gone in onStop); now-playing
 * changes only when MusicService pushes; icons load lazily on two low-priority threads and stop the
 * moment the grid is off screen. `adb shell dumpsys batterystats` is what the emulator check reads.
 *
 * OPT-IN. This component ships DISABLED (android:enabled="false") and is switched on by HomePlugin
 * when the person asks — so a phone whose owner never wants a launcher is never even offered
 * PosterChan in the "Select a Home app" chooser.
 */
public class HomeActivity extends Activity {

    private static final String TAG = "PosterChan";
    /** Which PosterChan screen a tile asked the app to open. Read by HomePlugin.consumeLaunchView. */
    public static final String EXTRA_VIEW = "pc_home_view";
    public static final String EXTRA_VIEW_AT = "pc_home_view_at";

    private AppRepo repo;
    private LauncherPrefs prefs;
    private GridView grid;
    private EditText search;
    private TextView empty;
    private LinearLayout nowRow;
    private TextView nowText;
    private ImageView nowToggle;
    private Shelf adapter;
    private PcTheme.Palette pal;
    private final Handler main = new Handler(Looper.getMainLooper());

    /** Everything the package manager reported, unsorted and unfiltered. Re-read only on a change. */
    private List<AppShelf.Entry> installed = new ArrayList<AppShelf.Entry>();
    /**
     * Our own tiles, and the role checks that decide whether Phone and Messages are among them.
     * Cached per foreground rather than recomputed on every keystroke: `getDefaultDialerPackage`
     * and `getDefaultSmsPackage` are binder calls, and the search box would make one pair per
     * character typed.
     */
    private List<AppShelf.Entry> ourTiles = new ArrayList<AppShelf.Entry>();

    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        repo = new AppRepo(this);
        prefs = new LauncherPrefs(this);
        prefs.seedOnce(HomeTiles.defaultHidden());
        pal = PcThemeStore.palette(this);
        setContentView(R.layout.home_activity);

        grid = (GridView) findViewById(R.id.pc_home_grid);
        search = (EditText) findViewById(R.id.pc_home_search);
        empty = (TextView) findViewById(R.id.pc_home_empty);
        nowRow = (LinearLayout) findViewById(R.id.pc_home_now);
        nowText = (TextView) findViewById(R.id.pc_home_now_text);
        nowToggle = (ImageView) findViewById(R.id.pc_home_now_toggle);

        adapter = new Shelf();
        grid.setAdapter(adapter);
        grid.setOnItemClickListener(new AdapterView.OnItemClickListener() {
            @Override public void onItemClick(AdapterView<?> p, View v, int i, long id) { open(adapter.at(i)); }
        });
        grid.setOnItemLongClickListener(new AdapterView.OnItemLongClickListener() {
            @Override public boolean onItemLongClick(AdapterView<?> p, View v, int i, long id) {
                longPress(adapter.at(i));
                return true;
            }
        });
        // A long press on the wallpaper is where every launcher puts its own settings.
        View root = findViewById(R.id.pc_home_root);
        if (root != null) root.setOnLongClickListener(new View.OnLongClickListener() {
            @Override public boolean onLongClick(View v) { homeMenu(); return true; }
        });

        search.addTextChangedListener(new TextWatcher() {
            @Override public void beforeTextChanged(CharSequence s, int a, int b, int c) { }
            @Override public void onTextChanged(CharSequence s, int a, int b, int c) { }
            @Override public void afterTextChanged(Editable e) { redraw(); }
        });

        nowToggle.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { toggleMusic(); }
        });
        nowText.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { openApp("music"); }
        });

        applyTheme();
        refreshRoles();
        reload();
    }

    /**
     * THE PERSON'S OWN THEME, ON A SCREEN THAT HAS NO STYLESHEET. All nine of the client's themes are
     * transcribed into PcTheme, so a home screen set to Cherry Blossom is pink here too and one set
     * to the flagship gets the grid and the scanlines. Re-applied in onStart, because the theme is
     * changed in the app and the launcher is what you come back to afterwards.
     */
    private void applyTheme() {
        pal = PcThemeStore.palette(this);
        View root = findViewById(R.id.pc_home_root);
        // A SCRIM, not a paint: the window declares windowShowWallpaper and this is what keeps the
        // wallpaper visible through the theme instead of replacing it.
        if (root != null) root.setBackground(Skin.page(pal, 0.55));
        if (search != null) {
            search.setBackground(Skin.panel(this, pal));
            search.setTextColor(pal.text);
            search.setHintTextColor(pal.muted);
            int pad = Skin.dp(this, 11);
            search.setPadding(pad, pad, pad, pad);
            search.setCompoundDrawablePadding(Skin.dp(this, 8));
            Drawable mag = tinted(R.drawable.ic_pc_search, pal.muted);
            search.setCompoundDrawablesRelativeWithIntrinsicBounds(mag, null, null, null);
        }
        if (empty != null) empty.setTextColor(pal.muted);
        if (nowRow != null) nowRow.setBackground(Skin.panel(this, pal));
        if (nowText != null) { nowText.setTextColor(pal.text); Skin.glow(nowText, pal); }
        if (nowToggle != null) nowToggle.setBackground(Skin.pill(this, pal, Skin.alpha(pal.accent, 0.18), true));
        if (adapter != null) adapter.notifyDataSetChanged();
    }

    private Drawable tinted(int res, int color) {
        try {
            Drawable d = getResources().getDrawable(res);
            if (d == null) return null;
            d = d.mutate();
            d.setColorFilter(color, android.graphics.PorterDuff.Mode.SRC_IN);
            return d;
        } catch (Throwable t) { return null; }
    }

    @Override
    protected void onStart() {
        super.onStart();
        repo.watch(new AppRepo.Changed() {
            @Override public void onPackagesChanged() {
                // Off the broadcast thread and coalesced: an app update fires several of these in a
                // row, and re-reading the package list three times is three times the work for one
                // answer.
                main.removeCallbacks(reloadSoon);
                main.postDelayed(reloadSoon, 400);
            }
        });
        MusicService.setWatcher(new MusicService.Watcher() {
            @Override public void onNowPlaying(final String t, final String a, final boolean p) {
                main.post(new Runnable() { @Override public void run() { paintNowPlaying(t, a, p); } });
            }
        });
        applyTheme();
        refreshRoles();
        redraw();
        paintNowPlaying(MusicService.nowTitle(), MusicService.nowArtist(), MusicService.nowPlaying());
    }

    @Override
    protected void onStop() {
        super.onStop();
        // EVERY subscription this screen holds goes away with the screen. See the class comment: a
        // home screen that is not on screen must cost nothing at all.
        repo.stopWatching();
        MusicService.setWatcher(null);
        main.removeCallbacks(reloadSoon);
    }

    private final Runnable reloadSoon = new Runnable() { @Override public void run() { reload(); } };

    private void refreshRoles() {
        ourTiles = HomeTiles.ours(HomeRoles.isDefaultDialer(this), HomeRoles.isDefaultSms(this));
    }

    /**
     * PRESSING HOME WHILE ALREADY HOME. Android delivers it here rather than restarting the activity,
     * and a launcher that does nothing with it feels broken: the expected behaviour is that the home
     * screen goes back to its top, with any search abandoned.
     */
    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        resetToTop();
    }

    private void resetToTop() {
        if (search != null && search.getText().length() > 0) search.setText("");
        if (grid != null) grid.smoothScrollToPosition(0);
        hideKeyboard();
    }

    /**
     * BACK MUST NOT FINISH THE HOME SCREEN. Finishing it leaves the phone showing whatever is behind
     * it — on a fresh boot, nothing at all. Every launcher swallows this key; the only thing back
     * does here is abandon a search.
     */
    @Override
    public void onBackPressed() {
        if (search != null && search.getText().length() > 0) { resetToTop(); return; }
        // Deliberately no super.onBackPressed().
    }

    /** The same rule for the hardware key on platforms that do not route it through onBackPressed. */
    @Override
    public boolean onKeyDown(int code, KeyEvent ev) {
        if (code == KeyEvent.KEYCODE_BACK) { onBackPressed(); return true; }
        return super.onKeyDown(code, ev);
    }

    // ---------------------------------------------------------------- the list

    private void reload() {
        // The package query is IO. Off the main thread, then back to draw — a home screen that
        // stutters when an app updates is a home screen people replace.
        new Thread(new Runnable() {
            @Override public void run() {
                final List<AppShelf.Entry> found = repo.installed();
                main.post(new Runnable() {
                    @Override public void run() {
                        installed = found;
                        redraw();
                        warmFirstScreen();
                    }
                });
            }
        }, "pc-home-scan").start();
    }

    /**
     * Ask for the first screenful of icons up front. Lazy loading is what keeps the grid smooth on a
     * phone with two hundred apps, but lazily loading the icons that are ALREADY on screen means the
     * home screen visibly fills in after it is drawn, which is the thing that reads as "slow".
     */
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

    private void redraw() {
        String q = search == null ? "" : search.getText().toString();
        List<AppShelf.Entry> rows = AppShelf.arrange(installed, ourTiles,
                prefs.hidden(), prefs.order(), q);
        adapter.set(rows);
        boolean none = rows.isEmpty();
        if (empty != null) empty.setVisibility(none ? View.VISIBLE : View.GONE);
        if (grid != null) grid.setVisibility(none ? View.GONE : View.VISIBLE);
    }

    // ---------------------------------------------------------------- opening things

    private void open(AppShelf.Entry e) {
        if (e == null) return;
        if (!e.isOurs()) {
            if (!repo.launch(e)) toast(getString(R.string.home_cannot_open));
            return;
        }
        if (HomeTiles.VIEW_SETTINGS.equals(e.view)) {
            // THE WAY BACK. By intent action, never by package name: the Settings app is called
            // something different on every OEM, and looking it up by name is how this would silently
            // stop being the escape hatch.
            try {
                startActivity(new Intent(Settings.ACTION_SETTINGS).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
            } catch (Throwable t) { toast(getString(R.string.home_cannot_open)); }
            return;
        }
        if (HomeTiles.VIEW_PHONE.equals(e.view)) { startNative("place.poster.app.phone.DialerActivity"); return; }
        if (HomeTiles.VIEW_TEXTS.equals(e.view)) { startNative("place.poster.app.sms.ThreadListActivity"); return; }
        openApp(e.view);
    }

    /**
     * A native PosterChan screen, looked up BY NAME so this file compiles and runs whether or not the
     * SMS and dialer halves are in the build. A missing screen is a toast, never a crash on the home
     * screen.
     */
    private void startNative(String className) {
        try {
            Intent i = new Intent();
            i.setClassName(getPackageName(), className);
            i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            startActivity(i);
        } catch (Throwable t) { toast(getString(R.string.home_cannot_open)); }
    }

    /** Open the app itself, optionally on a particular screen. The only WebView start in this package. */
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

    private void longPress(final AppShelf.Entry e) {
        if (e == null) return;
        final List<String> labels = new ArrayList<String>();
        final List<Integer> acts = new ArrayList<Integer>();
        if (!e.isOurs()) {
            labels.add(getString(R.string.home_app_info)); acts.add(0);
            labels.add(getString(R.string.home_uninstall)); acts.add(1);
        }
        // AppShelf rule 1: an essential tile is never hideable, so it is never offered.
        if (!e.essential) { labels.add(getString(R.string.home_hide)); acts.add(2); }
        labels.add(getString(R.string.home_apps)); acts.add(4);
        labels.add(getString(R.string.home_unhide)); acts.add(3);
        show(e.label, labels, new Pick() {
            @Override public void pick(int which) {
                switch (acts.get(which)) {
                    case 0: if (!repo.appInfo(e)) toast(getString(R.string.home_cannot_open)); break;
                    case 1: if (!repo.uninstall(e)) toast(getString(R.string.home_no_uninstaller)); break;
                    case 2: prefs.setHidden(AppShelf.hide(prefs.hidden(), e)); redraw(); break;
                    case 3: showHidden(); break;
                    case 4: pickOurApps(); break;
                }
            }
        });
    }

    /** Long press on the wallpaper — where a launcher's own settings live. */
    private void homeMenu() {
        final List<String> labels = new ArrayList<String>();
        labels.add(getString(R.string.home_apps));
        labels.add(getString(R.string.home_unhide));
        labels.add(getString(R.string.home_wallpaper));
        labels.add(getString(R.string.home_settings));
        show(getString(R.string.app_name), labels, new Pick() {
            @Override public void pick(int which) {
                switch (which) {
                    case 0: pickOurApps(); break;
                    case 1: showHidden(); break;
                    case 2: fire(new Intent(Intent.ACTION_SET_WALLPAPER)); break;
                    case 3: openApp("settings"); break;
                }
            }
        });
    }

    /**
     * WHICH POSTERCHAN SCREENS SIT ON THE HOME SCREEN. A checklist over the catalogue, writing the
     * same `hidden` set that hiding an ordinary app writes — one mechanism, so a PosterChan tile and
     * a phone app behave identically and there is no second rule to keep in step.
     */
    private void pickOurApps() {
        final HomeTiles.Tile[] cat = HomeTiles.catalogue();
        final List<HomeTiles.Tile> rows = new ArrayList<HomeTiles.Tile>();
        for (HomeTiles.Tile t : cat) {
            if (HomeTiles.isEssential(t.view)) continue;                 // never optional
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
                    @Override public void onClick(android.content.DialogInterface d, int i, boolean checked) {
                        on[i] = checked;
                    }
                })
                .setPositiveButton(android.R.string.ok, new android.content.DialogInterface.OnClickListener() {
                    @Override public void onClick(android.content.DialogInterface d, int w) {
                        Set<String> next = new HashSet<String>(prefs.hidden());
                        for (int i = 0; i < rows.size(); i++) {
                            String key = "pc:" + rows.get(i).view;
                            if (on[i]) next.remove(key); else next.add(key);
                        }
                        prefs.setHidden(next);
                        redraw();
                    }
                })
                .setNegativeButton(android.R.string.cancel, null)
                .show();
        } catch (Throwable ignored) { }
    }

    private void showHidden() {
        Set<String> hidden = prefs.hidden();
        // Resolve the keys back to labels through the SAME arrangement the grid uses, so a hidden app
        // that has since been uninstalled simply is not listed rather than showing as a raw key.
        List<AppShelf.Entry> all = AppShelf.arrange(installed, ourTiles, null, null, "");
        final List<AppShelf.Entry> rows = new ArrayList<AppShelf.Entry>();
        for (AppShelf.Entry e : all) if (hidden.contains(e.key())) rows.add(e);
        if (rows.isEmpty()) { toast(getString(R.string.home_hidden_none)); return; }
        List<String> labels = new ArrayList<String>();
        for (AppShelf.Entry e : rows) labels.add(e.label);
        show(getString(R.string.home_unhide), labels, new Pick() {
            @Override public void pick(int which) {
                prefs.setHidden(AppShelf.unhide(prefs.hidden(), rows.get(which).key()));
                redraw();
            }
        });
    }

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
     * Through the WIDGET's broadcast, not through the service directly. That path is receipt-checked
     * (MusicService.fromWidget), which is the whole reason it exists: the WebView holding the audio
     * may have been rebuilt since the last track, and a press into a dead page has to be noticed
     * rather than reported as success. Nothing here requests audio focus — a second request from this
     * same app takes it from the WebView, at which point Chromium pauses the very element the music
     * controls exist to keep playing.
     */
    private void toggleMusic() {
        try {
            sendBroadcast(new Intent(this, MusicWidget.class).setAction(MusicService.ACTION_TOGGLE));
        } catch (Throwable ignored) { }
    }

    // ---------------------------------------------------------------- plumbing

    private void hideKeyboard() {
        try {
            InputMethodManager im = (InputMethodManager) getSystemService(INPUT_METHOD_SERVICE);
            if (im != null && search != null) im.hideSoftInputFromWindow(search.getWindowToken(), 0);
            if (grid != null) grid.requestFocus();
        } catch (Throwable ignored) { }
    }

    private void toast(String s) {
        try { Toast.makeText(this, s, Toast.LENGTH_SHORT).show(); } catch (Throwable ignored) { }
    }

    /**
     * The grid's adapter. Deliberately dumb — AppShelf already decided what and in what order — and
     * deliberately careful about two things that decide whether a launcher feels fast:
     *
     *  * A HOLDER, so scrolling does not call findViewById twice per cell per frame.
     *  * AN ARRIVING ICON REDRAWS ONE CELL, not the list. notifyDataSetChanged() per icon is a full
     *    re-layout of every visible cell, ~30 of them, once per icon — on a phone with 200 apps that
     *    is thousands of layout passes during the first scroll, which is exactly the stutter people
     *    describe as a launcher being sluggish. The cell carries the key it is bound to, so a
     *    recycled view that has moved on is left alone.
     */
    private final class Shelf extends BaseAdapter {
        List<AppShelf.Entry> rows = new ArrayList<AppShelf.Entry>();

        void set(List<AppShelf.Entry> r) {
            rows = r == null ? new ArrayList<AppShelf.Entry>() : r;
            notifyDataSetChanged();
        }

        AppShelf.Entry at(int i) { return i >= 0 && i < rows.size() ? rows.get(i) : null; }

        /** Paint one arrived icon into whichever visible cell is still bound to it, if any. */
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
            if (e == null) { h.key = null; h.label.setText(""); h.icon.setImageDrawable(null); return v; }
            h.key = e.key();
            h.label.setText(e.label);
            h.label.setTextColor(pal.text);
            Skin.legible(h.label, pal);

            if (e.isOurs()) {
                HomeTiles.Tile t = HomeTiles.tile(e.view);
                int res = t == null ? 0 : TileIcons.of(t.icon);
                // Our tiles are the theme's accent, so PosterChan's own screens read as one family
                // among the phone's apps rather than as nine unrelated pictures.
                h.icon.setImageDrawable(res == 0 ? null : tinted(res, pal.accent));
                h.icon.setBackground(Skin.pill(HomeActivity.this, pal, Skin.alpha(pal.accent, 0.14), true));
                int p = Skin.dp(HomeActivity.this, 9);
                h.icon.setPadding(p, p, p, p);
                return v;
            }
            h.icon.setBackground(null);
            h.icon.setPadding(0, 0, 0, 0);
            Drawable d = repo.cachedIcon(e);
            if (d != null) { h.icon.setImageDrawable(d); return v; }
            // Blank rather than stale: a recycled cell that keeps the previous app's icon while the
            // real one loads is how a grid ends up showing the wrong picture next to the right name.
            h.icon.setImageDrawable(null);
            repo.icon(e, main, new Runnable() {
                @Override public void run() { iconArrived(e); }
            });
            return v;
        }
    }

    private static final class Holder {
        ImageView icon;
        TextView label;
        String key;
    }
}
