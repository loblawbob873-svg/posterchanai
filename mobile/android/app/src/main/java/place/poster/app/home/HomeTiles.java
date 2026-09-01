package place.poster.app.home;

import java.util.ArrayList;
import java.util.HashSet;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;

/**
 * POSTERCHAN'S OWN SCREENS, AS HOME-SCREEN ICONS — and the one tile that is the way back from
 * everything.
 *
 * Every view in the app's sidebar can sit on the home screen beside the phone's other apps: Social,
 * Notes, Files, Messages, the games, all of it. That is the difference between a launcher and a
 * kiosk, taken from the other end — the phone's apps belong on PosterChan's home screen, and
 * PosterChan's screens belong there too, as equals.
 *
 * NOT ALL OF THEM AT ONCE. Forty tiles on first run is a worse home screen than none, so `DEFAULT_ON`
 * is the handful that ship visible and the rest arrive hidden, offered in one place: long-press →
 * "PosterChan apps", a checklist. After the first run the person's choices are the only thing that
 * decides, and a view added to the app later joins the checklist unchecked rather than appearing on a
 * home screen somebody had already arranged.
 *
 * "Phone settings" is the exception to all of it: PRESENT ALWAYS, ESSENTIAL ALWAYS. It opens the
 * system Settings app by intent action, which works when the package query returned nothing, when the
 * arrangement is corrupt, when the WebView is dead and when this app's own UI will not start. It is
 * the single thing standing between a bad build and a phone whose owner cannot change their home
 * screen back. tests/test_android_launcher.py asserts it, because a refactor that quietly makes it
 * hideable is a refactor that can brick somebody's phone.
 *
 * Pure — no Android — so all of that is run rather than grepped.
 */
public final class HomeTiles {

    /** Opens the app itself with no particular screen. */
    public static final String VIEW_APP = "app";
    /** Native PosterChan screens, drawn by Android and reachable with the WebView dead. */
    public static final String VIEW_PHONE = "_phone";
    public static final String VIEW_TEXTS = "_texts";
    /** The phone's own Settings. Never ours, always present, never hideable. */
    public static final String VIEW_SETTINGS = "_settings";

    /** One offerable tile: the view slug the client's switchView takes, and how to label and draw it. */
    public static final class Tile {
        public final String view;
        public final String label;
        /** The sprite symbol name — `ic_pc_<icon with dashes as underscores>` in res/drawable. */
        public final String icon;
        public final boolean defaultOn;
        Tile(String view, String label, String icon, boolean defaultOn) {
            this.view = view; this.label = label; this.icon = icon; this.defaultOn = defaultOn;
        }
    }

    /**
     * The catalogue, in the sidebar's own order. Labels and icons match `templates/client.html`, so a
     * tile is recognisably the same thing as its row in the app — that is the whole point of
     * transcribing the sprite (scripts/gen_android_icons.py) rather than drawing new glyphs.
     */
    private static final Tile[] CATALOGUE = {
        new Tile(VIEW_APP,        "PosterChan",    "flower",   true),
        new Tile(VIEW_PHONE,      "Phone",         "call",     true),
        new Tile(VIEW_TEXTS,      "Texts",         "chat",     true),
        new Tile("global",        "Social",        "globe",    true),
        new Tile("notifications", "Notifications", "bell",     true),
        new Tile("messages",      "Messages",      "speech",   true),
        new Tile("notes",         "Notes",         "note",     true),
        new Tile("blossom",       "Files",         "folder",   true),
        new Tile("music",         "Music",         "music",    true),
        new Tile("calendar",      "Calendar",      "clock",    true),
        new Tile("contacts",      "Contacts",      "user",     true),
        new Tile("ai",            "AI",            "ai",       false),
        new Tile("websearch",     "Web Search",    "search",   false),
        new Tile("mail",          "Email",         "mail",     false),
        new Tile("bookmarks",     "Bookmarks",     "bookmark", false),
        new Tile("calls",         "Calls",         "phone",    false),
        new Tile("vault",         "Passwords",     "key",      false),
        new Tile("wallet",        "Monero Wallet", "coin",     false),
        new Tile("signer",        "Signer",        "key",      false),
        new Tile("drafts",        "Drafts",        "draft",    false),
        new Tile("sync",          "Folder Sync",   "refresh",  false),
        new Tile("communities",   "Communities",   "users",    false),
        new Tile("articles",      "Articles",      "article",  false),
        new Tile("news",          "News",          "news",     false),
        new Tile("markets",       "Markets",       "chart",    false),
        new Tile("budget",        "Budget",        "bars",     false),
        new Tile("market",        "Shopping",      "bag",      false),
        new Tile("streams",       "Streams",       "tv",       false),
        new Tile("shorts",        "Shorts",        "tv",       false),
        new Tile("meme",          "Meme Builder",  "tv",       false),
        new Tile("translate",     "Live Translate","translate",false),
        new Tile("torrents",      "Torrents",      "magnet",   false),
        new Tile("repos",         "Git",           "git",      false),
        new Tile("terminal",      "Terminal",      "terminal", false),
        new Tile("stats",         "Server Stats",  "bars",     false),
        new Tile("xdc",           "Mini apps",     "gamepad",  false),
        new Tile("chess",         "Chess",         "pawn",     false),
        new Tile("ttt",           "Tic-Tac-Toe",   "hash",     false),
        new Tile("hangman",       "Hangman",       "target",   false),
        new Tile("connect4",      "Connect Four",  "discs",    false),
        new Tile("blackjack",     "Blackjack",     "cards",    false),
        new Tile("holdem",        "Hold'em",       "spade",    false),
        new Tile("settings",      "App settings",  "gear",     false),
        // Last, and never hideable. See the class comment.
        new Tile(VIEW_SETTINGS,   "Phone settings","gear",     true),
    };

    private HomeTiles() { }

    public static Tile[] catalogue() { return CATALOGUE.clone(); }

    public static Tile tile(String view) {
        for (Tile t : CATALOGUE) if (t.view.equals(view)) return t;
        return null;
    }

    /**
     * The keys that start hidden on a phone that has never arranged its home screen. Written once, on
     * first run, so that a view the app gains LATER joins the checklist unchecked instead of turning
     * up on a home screen somebody had already made their own.
     */
    public static Set<String> defaultHidden() {
        Set<String> out = new HashSet<String>();
        for (Tile t : CATALOGUE) if (!t.defaultOn && !isEssential(t.view)) out.add("pc:" + t.view);
        return out;
    }

    public static boolean isEssential(String view) { return VIEW_SETTINGS.equals(view); }

    /**
     * TILES NOBODY HAS EVER HAD THE CHANCE TO DECIDE ABOUT.
     *
     * The home screen is seeded once and never again, which is what stops a removed icon coming
     * back. It also means a tile that becomes available LATER — a gate lifted, a screen added to the
     * catalogue — lands nowhere on an install that already exists. Messages and Phone were withheld
     * until the app held the SMS / dialer role, so an older install has them on neither surface and
     * nothing will ever put them there: "posterchan is the default messaging app but still no
     * desktop / app icon ... for text".
     *
     * A DECISION IS A RECORD, NOT AN ABSENCE. Removing an icon from the desktop does not hide it, so
     * "not on the desktop" cannot tell a removal from a tile that was never offered. `offered` is
     * that record (LauncherPrefs.adopted) and it only grows. Everything else here is belt: a tile
     * already on the desk, in the dock, or hidden has plainly been dealt with whatever the record
     * says.
     *
     * Order is the catalogue's, so two tiles arriving together land in a predictable order rather
     * than whichever the set iterated first.
     */
    public static List<String> unadopted(List<AppShelf.Entry> ours, Set<String> offered,
                                         Set<String> hidden, String deskSerialized,
                                         List<String> dock) {
        Set<String> known = new HashSet<String>();
        if (offered != null) known.addAll(offered);
        if (hidden != null) known.addAll(hidden);
        if (dock != null) known.addAll(dock);
        if (deskSerialized != null) {
            for (String line : deskSerialized.split("\n")) {
                int bar = line.indexOf('|');
                String key = bar < 0 ? line.trim() : line.substring(0, bar).trim();
                if (!key.isEmpty()) known.add(key);
            }
        }
        List<String> out = new ArrayList<String>();
        if (ours == null) return out;
        Set<String> have = new HashSet<String>();
        for (AppShelf.Entry e : ours) have.add(e.key());
        for (Tile t : CATALOGUE) {
            String key = "pc:" + t.view;
            // ESSENTIAL IS NEVER PLACED. "Phone settings" is the way back and lives in the long-press
            // menu; putting it on somebody's desktop uninvited is not a fix for anything.
            if (isEssential(t.view)) continue;
            if (!have.contains(key)) continue;      // not offered in THIS build, or does not resolve
            if (known.contains(key)) continue;
            out.add(key);
        }
        return out;
    }

    /**
     * THE ONE-TIME BASELINE for an install that predates the record above: everything the catalogue
     * already had is treated as offered, EXCEPT the two tiles that provably could not have been.
     *
     * Anything looser would re-place icons somebody had deliberately removed, which is the failure
     * this whole mechanism exists to avoid — and a removal leaves no trace, so there is no way to
     * tell one from a tile that was never offered. Phone and Messages are the exception because the
     * reason they are missing is written down: `ours()` withheld them until the app held the role,
     * and `seedHome` skips them from the desktop on purpose.
     */
    public static Set<String> alreadyOffered() {
        Set<String> out = new LinkedHashSet<String>();
        for (Tile t : CATALOGUE) {
            if (VIEW_PHONE.equals(t.view) || VIEW_TEXTS.equals(t.view)) continue;
            out.add("pc:" + t.view);
        }
        return out;
    }

    /** The native class a tile opens, or "" when it opens the app or the phone's own Settings. */
    public static String nativeTarget(String view) {
        if (VIEW_PHONE.equals(view)) return "place.poster.app.phone.DialerActivity";
        if (VIEW_TEXTS.equals(view)) return "place.poster.app.sms.ThreadListActivity";
        return "";
    }

    /**
     * PHONE AND MESSAGES ARE OFFERED WHETHER OR NOT WE HOLD THE ROLE, and that is a reversal.
     *
     * They used to be hidden until this app was the default dialer / default SMS app, on the
     * reasoning that a tile opening an empty call log is worse than no tile. The reasoning was
     * sound and the result was a DEAD END, reported as "still no SMS app": our own launcher aliases
     * are filtered out of the drawer by AppRepo (it skips our own package, so PosterChan is not
     * listed forty times), so this list was the only way to reach either screen — and it withheld
     * them until a role that is normally granted by opening the app and being asked. There was no
     * path from our home screen to the messages app at all, on the launcher this feature exists for.
     *
     * The premise was wrong too, not just the consequence. Neither screen is empty without its role:
     * both read the system providers with READ_SMS / READ_CALL_LOG, both draw an amber notice
     * saying they are not the default and how to change it, and being able to READ somebody's texts
     * while not being the app that RECEIVES them is the ordinary state of an SMS app somebody is
     * trying out. The screens answer the question this filter was guessing at, on screen, in a
     * sentence — see ThreadListActivity.draw and DialerActivity.
     */
    public static List<AppShelf.Entry> ours() {
        List<AppShelf.Entry> out = new ArrayList<AppShelf.Entry>();
        for (Tile t : CATALOGUE) {
            out.add(AppShelf.Entry.ours(t.view, t.label, isEssential(t.view)));
        }
        return out;
    }
}
