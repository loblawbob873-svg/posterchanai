package place.poster.app.sms;

import android.app.AlertDialog;
import android.content.Intent;
import android.database.ContentObserver;
import android.net.Uri;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.provider.Telephony;
import android.text.Editable;
import android.text.TextWatcher;
import android.text.format.DateUtils;
import android.view.LayoutInflater;
import android.view.View;
import android.view.ViewGroup;
import android.widget.AdapterView;
import android.widget.BaseAdapter;
import android.widget.EditText;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.ListView;
import android.widget.TextView;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

import place.poster.app.R;
import place.poster.app.ui.PcActivity;
import place.poster.app.ui.Skin;

/**
 * EVERY CONVERSATION ON THIS PHONE.
 *
 * Read straight from the system message store, which is authoritative on the device — so this screen
 * shows the same threads every other app on the phone shows, including the ones that arrived before
 * PosterChan was installed.
 *
 * NO POLLING. The list refreshes on a ContentObserver over `content://sms`, registered while the
 * screen is on and gone the moment it is not. With the HOME role this process is resident for the
 * life of the battery; a five-second refresh here would be a five-second refresh for ever.
 */
public class ThreadListActivity extends PcActivity {

    private ListView list;
    private EditText search;
    private TextView empty, notice, title;
    private Threads adapter;
    private final Handler main = new Handler(Looper.getMainLooper());
    private ContentObserver watcher;
    private List<SmsStore.Thread> all = new ArrayList<SmsStore.Thread>();

    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        setContentView(R.layout.sms_list);
        list = (ListView) findViewById(R.id.pc_sms_threads);
        search = (EditText) findViewById(R.id.pc_sms_search);
        empty = (TextView) findViewById(R.id.pc_sms_empty);
        notice = (TextView) findViewById(R.id.pc_sms_notice);
        title = (TextView) findViewById(R.id.pc_sms_title);

        adapter = new Threads();
        list.setAdapter(adapter);
        list.setOnItemClickListener(new AdapterView.OnItemClickListener() {
            @Override public void onItemClick(AdapterView<?> p, View v, int i, long id) {
                SmsStore.Thread t = adapter.at(i);
                if (t == null) return;
                startActivity(new Intent(ThreadListActivity.this, ThreadActivity.class)
                        .putExtra(ThreadActivity.EXTRA_THREAD, t.id)
                        .putExtra(ThreadActivity.EXTRA_ADDRESS, t.address));
            }
        });
        list.setOnItemLongClickListener(new AdapterView.OnItemLongClickListener() {
            @Override public boolean onItemLongClick(AdapterView<?> p, View v, int i, long id) {
                final SmsStore.Thread t = adapter.at(i);
                if (t == null) return true;
                confirmDeleteThread(t);
                return true;
            }
        });

        findViewById(R.id.pc_sms_new).setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { compose(); }
        });
        findViewById(R.id.pc_sms_search_btn).setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) {
                boolean showing = search.getVisibility() == View.VISIBLE;
                search.setVisibility(showing ? View.GONE : View.VISIBLE);
                if (showing) { search.setText(""); }
                else search.requestFocus();
            }
        });
        search.addTextChangedListener(new TextWatcher() {
            @Override public void beforeTextChanged(CharSequence s, int a, int b, int c) { }
            @Override public void onTextChanged(CharSequence s, int a, int b, int c) { }
            @Override public void afterTextChanged(Editable e) { draw(); }
        });

        applySkin();
    }

    /**
     * NOTHING EVER ASKED FOR READ_SMS, and this is the screen that needs it.
     *
     * The permission is declared in the manifest, but a dangerous permission is not granted by
     * being declared — it has to be requested at runtime on Android 6 and later, which is every
     * phone this runs on. `SmsPlugin` requests it for the WebView side; the NATIVE screen had no
     * such path, so the provider query was refused, `SmsStore.query` swallowed the refusal into an
     * empty list, and the screen drew "No messages yet" over a phone full of texts. Reported as
     * "i see 0 of my sms messages in Text".
     *
     * Asked from onStart rather than onCreate so that a person who declined once and later changed
     * their mind in system settings is picked up on the next visit without reinstalling.
     */
    private static final int ASK_READ_SMS = 4711;

    private boolean mayReadTexts() {
        if (android.os.Build.VERSION.SDK_INT < 23) return true;
        try {
            return checkSelfPermission(android.Manifest.permission.READ_SMS)
                    == android.content.pm.PackageManager.PERMISSION_GRANTED;
        } catch (Throwable t) {
            return false;
        }
    }

    @Override
    public void onRequestPermissionsResult(int code, String[] perms, int[] granted) {
        super.onRequestPermissionsResult(code, perms, granted);
        // Redrawn either way: a REFUSAL has to change the screen too, or declining looks exactly
        // like an empty inbox — the same confusion this whole change is about.
        if (code == ASK_READ_SMS) reload();
    }

    @Override
    protected void onStart() {
        super.onStart();
        applySkin();
        if (!mayReadTexts()) {
            try {
                requestPermissions(new String[]{ android.Manifest.permission.READ_SMS },
                                   ASK_READ_SMS);
            } catch (Throwable ignored) { }
        }
        reload();
        // THE ONLY REFRESH TRIGGER. See the class comment: a timer here would run for ever.
        watcher = new ContentObserver(main) {
            @Override public void onChange(boolean self) { reload(); }
            @Override public void onChange(boolean self, Uri uri) { reload(); }
        };
        try {
            getContentResolver().registerContentObserver(Telephony.Sms.CONTENT_URI, true, watcher);
        } catch (Throwable ignored) { watcher = null; }
    }

    @Override
    protected void onStop() {
        super.onStop();
        if (watcher != null) {
            try { getContentResolver().unregisterContentObserver(watcher); } catch (Throwable ignored) { }
            watcher = null;
        }
    }

    @Override protected void onThemeChanged() { applySkin(); }

    private void applySkin() {
        paintPage(R.id.pc_sms_root);
        // The header carries the neon edge, so Messages reads as the same product as the rest of the
        // client rather than as a stock Android list. Every light palette degrades it to a hairline —
        // a bloom behind dark text on a light background is the readability bug client.css turns
        // every text-shadow off to avoid.
        View bar = findViewById(R.id.pc_sms_bar);
        if (bar != null) bar.setBackground(Skin.bar(this, pal, false));
        title.setTextColor(pal.text);
        Skin.glow(title, pal);
        icon(R.id.pc_sms_search_btn, R.drawable.ic_pc_search, pal.muted);
        icon(R.id.pc_sms_new, R.drawable.ic_pc_plus, pal.accent);
        findViewById(R.id.pc_sms_new).setBackground(Skin.pill(this, pal, Skin.alpha(pal.accent, 0.16), true));
        search.setBackground(Skin.panel(this, pal));
        search.setTextColor(pal.text);
        search.setHintTextColor(pal.muted);
        int p = dp(10);
        search.setPadding(p, p, p, p);
        empty.setTextColor(pal.muted);
        notice.setBackground(Skin.ghost(this, pal, pal.amber, false));
        notice.setTextColor(pal.text);
        if (adapter != null) adapter.notifyDataSetChanged();
    }

    /** Whether the last read was REFUSED rather than answered empty. See draw(). */
    private boolean unreadable = false;

    private void reload() {
        new Thread(new Runnable() {
            @Override public void run() {
                final List<SmsStore.Thread> found = SmsStore.threads(ThreadListActivity.this, 800);
                // Read on this thread, immediately after the query, because it describes THAT read.
                final boolean refused = SmsStore.refused();
                main.post(new Runnable() {
                    @Override public void run() { all = found; unreadable = refused; draw(); }
                });
            }
        }, "pc-sms-threads").start();
    }

    private void draw() {
        String q = search.getText().toString().trim().toLowerCase(Locale.ROOT);
        List<SmsStore.Thread> rows = new ArrayList<SmsStore.Thread>();
        for (SmsStore.Thread t : all) {
            if (q.isEmpty()
                    || t.label.toLowerCase(Locale.ROOT).contains(q)
                    || t.address.toLowerCase(Locale.ROOT).contains(q)
                    || t.snippet.toLowerCase(Locale.ROOT).contains(q)) {
                rows.add(t);
            }
        }
        adapter.set(rows);
        empty.setVisibility(rows.isEmpty() ? View.VISIBLE : View.GONE);

        // THREE KINDS OF EMPTY, AND THEY ARE NOT THE SAME SENTENCE. "you have no texts", "you have
        // texts and I was refused permission to read them", and "I can read them but I am not the
        // app that receives them" all drew "No messages yet" over a full inbox. The refusal is the
        // one that matters most, because it is the one the person can fix and the one that made a
        // working screen look broken.
        boolean cannotRead = unreadable || !mayReadTexts();
        empty.setText(cannotRead ? R.string.sms_no_permission : R.string.sms_empty);

        // SAY WHY IT IS EMPTY. "PosterChan can read your texts but is not the messages app" and "you
        // have no texts" look identical, and the first one is fixable in two taps.
        boolean isDefault = HasRole.sms(this);
        if (cannotRead) {
            notice.setVisibility(View.VISIBLE);
            notice.setText(R.string.sms_no_permission);
        } else {
            /* IF THE MESSAGES ARE THERE, SAY NOTHING.
             *
             * This notice sat above somebody's texts telling them PosterChan was not their messages
             * app on a phone where it was — `isDefault` reads the message store's default-app ROW,
             * which Android does not keep in step with the ROLE on OEM builds. Replacing the verdict
             * with a description of what the screen was showing was no better: a caption narrating
             * the obvious, permanently.
             *
             * A phone that can read its own messages needs no line at all. What remains below is
             * for the case where reading works and DELIVERY does not, which is worth one sentence —
             * and only when there is actually something to name. */
            notice.setVisibility(View.GONE);
            // NAME WHAT ANDROID NAMED. "1.0.1336 says PosterChan is not this phone's messages app
            // still!" — and a flat verdict gives the person nothing to argue with or act on. The
            // role and the message store's default-app row are two different tables on Android 10+,
            // and OEM builds do not always keep them in step; the STORE's row is the one that
            // decides what is delivered, so the app cannot simply believe the role. Saying which
            // one says what is the only honest answer, and it is the one that can be acted on.
            if (!isDefault) notice.setText(whyNotDefault());
        }
    }

    /** The measured reason this app is not the messages app, in a sentence naming the packages. */
    private String whyNotDefault() {
        String cur = "";
        try { cur = android.provider.Telephony.Sms.getDefaultSmsPackage(this); }
        catch (Throwable ignored) { }
        boolean role = HasRole.roleHeld(this);
        /* NO SIM IS NOT "NOBODY HAS SET ONE". `getDefaultSmsPackage` returns null on a device with
         * no telephony, and this screen then told a tablet to go and choose a messages app —
         * reported, after the panel was added, as "Still Android has not named a messages app for
         * this phone yet." A tablet cannot be a messages app at all, and advice somebody cannot take
         * is worse than none: it reads as the app being broken rather than as the device being what
         * it is. Asked FIRST, because it explains the null the two branches below would otherwise
         * misread. */
        if (!HasRole.smsCapable(this)) return getString(R.string.sms_no_sim);
        /* AN EMPTY `getDefaultSmsPackage` NO LONGER GETS ITS OWN SENTENCE.
         *
         * It returns null on a device with no telephony — handled above — and ALSO on a phone where
         * the role simply has not been assigned. Two different states, one sentence, and it told
         * somebody to go and set a default they had been trying to set all day. On a phone that
         * cannot be given the role, being instructed to give it is the least useful thing the screen
         * can say. What is left below is accurate without claiming to know why. */
        /* Both remaining lines NAME the package Android reported, so neither can be used when there
         * is nothing to name — `sms_default_is` would render "messages app is , not PosterChan".
         * With no name, say the part that is true without it. */
        if (cur == null || cur.isEmpty()) return getString(R.string.sms_not_default);
        if (role) return getString(R.string.sms_role_split, cur);
        return getString(R.string.sms_default_is, cur);
    }

    private void compose() {
        final EditText input = new EditText(this);
        input.setHint(R.string.sms_number_hint);
        input.setInputType(android.text.InputType.TYPE_CLASS_PHONE);
        input.setTextColor(pal.text);
        input.setHintTextColor(pal.muted);
        int p = dp(14);
        input.setPadding(p, p, p, p);
        LinearLayout box = new LinearLayout(this);
        box.setPadding(dp(16), dp(8), dp(16), 0);
        box.addView(input);
        try {
            new AlertDialog.Builder(this)
                .setTitle(R.string.sms_to)
                .setView(box)
                .setPositiveButton(android.R.string.ok, new android.content.DialogInterface.OnClickListener() {
                    @Override public void onClick(android.content.DialogInterface d, int w) {
                        String to = input.getText().toString().trim();
                        if (to.isEmpty()) return;
                        startActivity(new Intent(ThreadListActivity.this, ThreadActivity.class)
                                .putExtra(ThreadActivity.EXTRA_ADDRESS, to)
                                .putExtra(ThreadActivity.EXTRA_THREAD,
                                          SmsStore.threadIdFor(ThreadListActivity.this, to)));
                    }
                })
                .setNegativeButton(android.R.string.cancel, null)
                .show();
        } catch (Throwable ignored) { }
    }

    private void confirmDeleteThread(final SmsStore.Thread t) {
        try {
            new AlertDialog.Builder(this)
                .setTitle(PhoneBook.label(this, t.address))
                .setMessage(R.string.sms_delete_thread)
                .setPositiveButton(android.R.string.ok, new android.content.DialogInterface.OnClickListener() {
                    @Override public void onClick(android.content.DialogInterface d, int w) {
                        int n = SmsStore.deleteThread(ThreadListActivity.this, t.id);
                        SmsNotifier.clear(ThreadListActivity.this, t.id);
                        // THIS PHONE ONLY, and it says so. The archive on the person's other devices
                        // is a separate copy with a separate delete, done from the app's Texts screen
                        // — claiming both here would be a claim this code cannot keep.
                        say(getString(R.string.sms_deleted_here) + " (" + n + ")");
                        reload();
                    }
                })
                .setNegativeButton(android.R.string.cancel, null)
                .show();
        } catch (Throwable ignored) { }
    }

    private final class Threads extends BaseAdapter {
        private List<SmsStore.Thread> rows = new ArrayList<SmsStore.Thread>();

        void set(List<SmsStore.Thread> r) { rows = r; notifyDataSetChanged(); }
        SmsStore.Thread at(int i) { return i >= 0 && i < rows.size() ? rows.get(i) : null; }

        @Override public int getCount() { return rows.size(); }
        @Override public Object getItem(int i) { return at(i); }
        @Override public long getItemId(int i) { return i; }

        @Override
        public View getView(int i, View reuse, ViewGroup parent) {
            View v = reuse;
            if (v == null) v = LayoutInflater.from(ThreadListActivity.this)
                    .inflate(R.layout.sms_row, parent, false);
            SmsStore.Thread t = at(i);
            TextView av = (TextView) v.findViewById(R.id.pc_row_avatar);
            TextView name = (TextView) v.findViewById(R.id.pc_row_name);
            TextView snip = (TextView) v.findViewById(R.id.pc_row_snippet);
            TextView when = (TextView) v.findViewById(R.id.pc_row_when);
            TextView un = (TextView) v.findViewById(R.id.pc_row_unread);
            View card = v.findViewById(R.id.pc_row_card);
            if (t == null) return v;

            // Already resolved, on the thread that read the provider — see SmsStore.Thread.label.
            String label = t.label.isEmpty() ? t.address : t.label;
            card.setBackground(Skin.panel(ThreadListActivity.this, pal));
            av.setBackground(Skin.avatar(ThreadListActivity.this, pal, label));
            av.setText(initials(label));
            name.setText(label);
            name.setTextColor(pal.text);
            snip.setText(t.snippet);
            snip.setTextColor(t.unread > 0 ? pal.text : pal.muted);
            when.setText(t.date > 0
                    ? DateUtils.getRelativeTimeSpanString(t.date, System.currentTimeMillis(),
                            DateUtils.MINUTE_IN_MILLIS).toString()
                    : "");
            when.setTextColor(pal.muted);
            if (t.unread > 0) {
                un.setVisibility(View.VISIBLE);
                un.setText(String.valueOf(t.unread));
                un.setTextColor(pal.onAccent());
                un.setBackground(Skin.pill(ThreadListActivity.this, pal, pal.accent, true));
            } else {
                un.setVisibility(View.GONE);
            }
            return v;
        }
    }
}
