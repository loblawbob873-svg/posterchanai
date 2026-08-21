package place.poster.app.home;

import android.content.BroadcastReceiver;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.pm.PackageManager;
import android.content.pm.ResolveInfo;
import android.graphics.drawable.Drawable;
import android.net.Uri;
import android.os.Build;
import android.util.Log;
import android.util.LruCache;

import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.ThreadFactory;

/**
 * WHAT IS INSTALLED ON THIS PHONE, and how the home screen hears that it changed.
 *
 * THE BATTERY RULE IS THE WHOLE DESIGN OF THIS CLASS. With the HOME role this process is resident
 * essentially for ever — it is whatever is on screen when nothing else is — so anything it polls, it
 * polls until the battery is flat. There is therefore NO timer here and no periodic refresh: the app
 * list is read once, and the only thing that reads it again is Android telling us a package arrived,
 * left or changed. That broadcast is registered in onStart and unregistered in onStop, so a home
 * screen that is not on screen costs exactly nothing.
 *
 * Icons are the other half of the same rule. Loading every icon on a phone with two hundred apps is
 * seconds of main-thread work and tens of megabytes; they are loaded lazily, on two background
 * threads, into a bounded cache, and only for cells that are actually on screen.
 */
public final class AppRepo {

    private static final String TAG = "PosterChan";

    /** Told when the set of installed apps changed. No arguments — the caller re-reads. */
    public interface Changed { void onPackagesChanged(); }

    private final Context ctx;
    private final PackageManager pm;
    private final LruCache<String, Drawable> icons = new LruCache<String, Drawable>(96);
    private final ExecutorService pool = Executors.newFixedThreadPool(2, new ThreadFactory() {
        @Override public Thread newThread(Runnable r) {
            Thread t = new Thread(r, "pc-home-icons");
            t.setPriority(Thread.MIN_PRIORITY);   // never compete with the scroll
            t.setDaemon(true);
            return t;
        }
    });
    private BroadcastReceiver watcher;

    public AppRepo(Context ctx) {
        this.ctx = ctx.getApplicationContext();
        this.pm = this.ctx.getPackageManager();
    }

    /**
     * Every launchable activity on the phone, EXCEPT our own — our tiles are supplied separately, and
     * a launcher listing itself is a loop the user has to think about.
     *
     * Never throws: a package manager that refuses the query returns an empty list here, and
     * AppShelf's essential tiles are what keeps the phone usable when it does. That is the whole
     * fallback story and it is why this method's failure mode is "fewer icons", never "no home
     * screen".
     */
    public List<AppShelf.Entry> installed() {
        List<AppShelf.Entry> out = new ArrayList<AppShelf.Entry>();
        try {
            Intent probe = new Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER);
            List<ResolveInfo> found = pm.queryIntentActivities(probe, 0);
            if (found == null) return out;
            String self = ctx.getPackageName();
            for (ResolveInfo ri : found) {
                if (ri == null || ri.activityInfo == null) continue;
                String pkg = ri.activityInfo.packageName;
                String act = ri.activityInfo.name;
                if (pkg == null || act == null) continue;
                if (self.equals(pkg)) continue;
                String label;
                try { label = String.valueOf(ri.loadLabel(pm)); } catch (Throwable t) { label = pkg; }
                if (label == null || label.trim().isEmpty()) label = pkg;
                out.add(AppShelf.Entry.app(pkg, act, label));
            }
        } catch (Throwable t) {
            Log.w(TAG, "home: could not list installed apps", t);
        }
        return out;
    }

    /** The icon if it is already in memory, else null — the caller then asks for it in the background. */
    public Drawable cachedIcon(AppShelf.Entry e) {
        return e == null ? null : icons.get(e.key());
    }

    /** Load off the main thread and call back on it. A failure caches nothing and simply never calls back. */
    public void icon(final AppShelf.Entry e, final android.os.Handler main, final Runnable done) {
        if (e == null || e.isOurs()) return;
        if (icons.get(e.key()) != null) return;
        pool.execute(new Runnable() {
            @Override public void run() {
                Drawable d = null;
                try {
                    d = pm.getActivityIcon(new ComponentName(e.pkg, e.activity));
                } catch (Throwable t) {
                    try { d = pm.getApplicationIcon(e.pkg); } catch (Throwable ignored) { }
                }
                if (d == null) return;
                icons.put(e.key(), d);
                if (main != null && done != null) main.post(done);
            }
        });
    }

    /**
     * Listen for packages arriving and leaving. THE ONLY refresh trigger there is — see the class
     * comment. Registered while the home screen is on screen and torn down the moment it is not.
     */
    public void watch(final Changed cb) {
        stopWatching();
        try {
            IntentFilter f = new IntentFilter();
            f.addAction(Intent.ACTION_PACKAGE_ADDED);
            f.addAction(Intent.ACTION_PACKAGE_REMOVED);
            f.addAction(Intent.ACTION_PACKAGE_CHANGED);
            f.addAction(Intent.ACTION_PACKAGE_REPLACED);
            // Without the data scheme these actions are never delivered: they are all package: URIs
            // and a filter with no scheme matches none of them. Nothing warns about it; the list
            // simply never updates.
            f.addDataScheme("package");
            watcher = new BroadcastReceiver() {
                @Override public void onReceive(Context c, Intent i) {
                    try { if (cb != null) cb.onPackagesChanged(); } catch (Throwable ignored) { }
                }
            };
            ctx.registerReceiver(watcher, f);
        } catch (Throwable t) {
            Log.w(TAG, "home: package watcher not registered", t);
            watcher = null;
        }
    }

    public void stopWatching() {
        if (watcher == null) return;
        try { ctx.unregisterReceiver(watcher); } catch (Throwable ignored) { }
        watcher = null;
    }

    /** Icons only — the labels are cheap and a rename is rare enough to ride the next cold read. */
    public void forgetIcons() { icons.evictAll(); }

    public boolean launch(AppShelf.Entry e) {
        if (e == null || e.isOurs()) return false;
        try {
            Intent i = new Intent(Intent.ACTION_MAIN)
                    .addCategory(Intent.CATEGORY_LAUNCHER)
                    .setComponent(new ComponentName(e.pkg, e.activity))
                    // NEW_TASK + RESET_TASK_IF_NEEDED is what a launcher is supposed to send: it
                    // brings an app back to where the user left it rather than starting it over.
                    .setFlags(Intent.FLAG_ACTIVITY_NEW_TASK
                            | Intent.FLAG_ACTIVITY_RESET_TASK_IF_NEEDED);
            ctx.startActivity(i);
            return true;
        } catch (Throwable t) {
            // A component that vanished between the draw and the tap. Fall back to whatever the
            // package's own launch intent is, then give up quietly — a home screen must not crash
            // because one icon went stale.
            try {
                Intent i = pm.getLaunchIntentForPackage(e.pkg);
                if (i != null) {
                    i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                    ctx.startActivity(i);
                    return true;
                }
            } catch (Throwable ignored) { }
            return false;
        }
    }

    public boolean appInfo(AppShelf.Entry e) {
        if (e == null) return false;
        return fire(new Intent(android.provider.Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                Uri.parse("package:" + (e.isOurs() ? ctx.getPackageName() : e.pkg))));
    }

    /**
     * The uninstall dialog. A SYSTEM app has no uninstaller and the intent fails; that is reported by
     * the caller rather than swallowed, because a menu item that does nothing is the bug this whole
     * file's error handling exists to avoid.
     */
    public boolean uninstall(AppShelf.Entry e) {
        if (e == null || e.isOurs()) return false;
        return fire(new Intent(Intent.ACTION_DELETE, Uri.parse("package:" + e.pkg)));
    }

    private boolean fire(Intent i) {
        try {
            ctx.startActivity(i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
            return true;
        } catch (Throwable t) { return false; }
    }

    /**
     * IS THERE ANOTHER HOME SCREEN ON THIS PHONE? The one question that must be asked before we ever
     * stop being one. Disabling our own HOME component while we are the only qualifying home app
     * leaves the device with no home screen at all — the exact brick this whole feature is written
     * to avoid — and Android does not stop you doing it.
     */
    public boolean anotherHomeExists() {
        try {
            Intent probe = new Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_HOME);
            List<ResolveInfo> found = pm.queryIntentActivities(probe, PackageManager.MATCH_DEFAULT_ONLY);
            if (found == null) return false;
            String self = ctx.getPackageName();
            for (ResolveInfo ri : found) {
                if (ri == null || ri.activityInfo == null) continue;
                if (!self.equals(ri.activityInfo.packageName)) return true;
            }
        } catch (Throwable ignored) { }
        return false;
    }

    /** Suppress the "unused on old API levels" lint reader; Build is imported for future gating. */
    static int sdk() { return Build.VERSION.SDK_INT; }
}
