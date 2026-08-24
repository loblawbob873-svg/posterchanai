package place.poster.app.phone;

import android.app.AlertDialog;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.provider.CallLog;
import android.telecom.TelecomManager;
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

import place.poster.app.R;
import place.poster.app.MainActivity;
import place.poster.app.home.LaunchView;
import place.poster.app.sms.PhoneBook;
import place.poster.app.ui.PcActivity;
import place.poster.app.ui.Skin;

/**
 * THE PHONE APP: recents, contacts, voicemail, a search across all of it, and a keypad.
 *
 * The first version was a keypad and a call log, which is the half of a dialer nobody opens it for —
 * reported as "should let you see contacts, voicemail, search through contacts". All four now share
 * one list and one search box, because they are the same question asked four ways: who do I want to
 * call.
 *
 * EVERY SOURCE IS THE PHONE'S OWN. Recents and voicemail are `CallLog.Calls`; contacts are
 * `ContactsContract` across all accounts, which is where PosterChan's synced cards already are.
 * Nothing here keeps a copy of anything: a dialer with its own contact store is the third one on the
 * phone and the one that is always out of date.
 *
 * WHAT IT REFUSES TO DO. A GSM service code (`*#06#`, `*21*…#`) is handed to ACTION_DIAL rather than
 * placed as a call — the platform intercepts those and shows the result. Placed as a call they
 * either fail or, worse, change a network setting with nothing shown. That rule is in `Dial`, which
 * is pure and run by tests.
 */
public class DialerActivity extends PcActivity {

    private static final int REQ_PERMS = 4401;
    /* KEYPAD FIRST, and it is the default. A phone app opens on the thing you came to do. */
    private static final int TAB_KEYPAD = 0, TAB_RECENT = 1, TAB_CONTACTS = 2, TAB_VOICEMAIL = 3;

    private TextView numberView, notice, empty, tPad, tRecent, tContacts, tVm;
    private LinearLayout padWrap;
    private ListView list;
    private EditText search;
    private LinearLayout pad;
    private ImageView callBtn, backBtn, padToggle;
    private Rows adapter;
    private String typed = "";
    private int tab = TAB_KEYPAD;
    private final Handler main = new Handler(Looper.getMainLooper());

    /** Open this person in PosterChan's Contacts screen, not in a second platform contacts UI. */
    private void openPosterContact(Row r) {
        if (r == null || r.number == null || r.number.trim().isEmpty()) return;
        LaunchView.request("contact:" + Uri.encode(r.number.trim()), System.currentTimeMillis());
        Intent i = new Intent(this, MainActivity.class);
        i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        try { startActivity(i); }
        catch (Throwable t) { say(getString(R.string.home_cannot_open)); }
    }

    /** One row of the list, whichever tab produced it. */
    static final class Row {
        String label = "", sub = "", number = "";
        int icon;
        boolean missed;
        long contactId = -1;
        CallLogStore.Entry entry;
    }

    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        setContentView(R.layout.tel_dialer);
        numberView = (TextView) findViewById(R.id.pc_dl_number);
        notice = (TextView) findViewById(R.id.pc_dl_notice);
        empty = (TextView) findViewById(R.id.pc_dl_empty);
        list = (ListView) findViewById(R.id.pc_dl_recent);
        search = (EditText) findViewById(R.id.pc_dl_search);
        pad = (LinearLayout) findViewById(R.id.pc_dl_pad);
        callBtn = (ImageView) findViewById(R.id.pc_dl_call);
        backBtn = (ImageView) findViewById(R.id.pc_dl_back);
        padToggle = (ImageView) findViewById(R.id.pc_dl_padtoggle);
        padWrap = (LinearLayout) findViewById(R.id.pc_dl_padwrap);
        tPad = (TextView) findViewById(R.id.pc_dl_t_pad);
        tRecent = (TextView) findViewById(R.id.pc_dl_t_recent);
        tContacts = (TextView) findViewById(R.id.pc_dl_t_contacts);
        tVm = (TextView) findViewById(R.id.pc_dl_t_vm);

        adapter = new Rows();
        list.setAdapter(adapter);
        list.setOnItemClickListener(new AdapterView.OnItemClickListener() {
            @Override public void onItemClick(AdapterView<?> p, View v, int i, long id) {
                Row r = adapter.at(i);
                if (r == null) return;
                if (tab == TAB_VOICEMAIL && r.entry != null) { openVoicemail(r.entry); return; }
                // A TAP FILLS THE PAD; it does not dial. Placing a call from a single tap in a list
                // is how somebody rings their ex from a pocket — the green button is the commitment.
                typed = r.number;
                drawNumber();
            }
        });
        list.setOnItemLongClickListener(new AdapterView.OnItemLongClickListener() {
            @Override public boolean onItemLongClick(AdapterView<?> p, View v, int i, long id) {
                rowMenu(adapter.at(i));
                return true;
            }
        });

        callBtn.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { place(); }
        });
        backBtn.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { typed = Dial.backspace(typed); drawNumber(); reload(); }
        });
        backBtn.setOnLongClickListener(new View.OnLongClickListener() {
            @Override public boolean onLongClick(View v) { typed = ""; drawNumber(); reload(); return true; }
        });
        padToggle.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { tab = TAB_KEYPAD; applySkin(); reload(); }
        });
        findViewById(R.id.pc_dl_texts).setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) {
                try {
                    startActivity(new Intent().setClassName(getPackageName(),
                            "place.poster.app.sms.ThreadListActivity"));
                } catch (Throwable ignored) { }
            }
        });
        findViewById(R.id.pc_dl_search_btn).setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) {
                boolean showing = search.getVisibility() == View.VISIBLE;
                search.setVisibility(showing ? View.GONE : View.VISIBLE);
                if (showing) search.setText(""); else search.requestFocus();
            }
        });
        search.addTextChangedListener(new TextWatcher() {
            @Override public void beforeTextChanged(CharSequence s, int a, int b, int c) { }
            @Override public void onTextChanged(CharSequence s, int a, int b, int c) { }
            @Override public void afterTextChanged(Editable e) { reload(); }
        });
        tPad.setOnClickListener(tabClick(TAB_KEYPAD));
        tRecent.setOnClickListener(tabClick(TAB_RECENT));
        tContacts.setOnClickListener(tabClick(TAB_CONTACTS));
        tVm.setOnClickListener(tabClick(TAB_VOICEMAIL));

        readIntent(getIntent());
        applySkin();
        askForWhatIsMissing();
    }

    private View.OnClickListener tabClick(final int which) {
        return new View.OnClickListener() {
            @Override public void onClick(View v) { tab = which; applySkin(); reload(); }
        };
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        readIntent(intent);
        drawNumber();
    }

    /** ACTION_DIAL / a `tel:` link from another app: prefill, never auto-dial. */
    private void readIntent(Intent i) {
        if (i == null) return;
        Uri d = i.getData();
        if (d == null || !"tel".equalsIgnoreCase(String.valueOf(d.getScheme()))) return;
        String raw = d.getSchemeSpecificPart();
        typed = Dial.clean(raw == null ? "" : Uri.decode(raw));
    }

    @Override
    protected void onStart() {
        super.onStart();
        applySkin();
        reload();
        new Thread(new Runnable() {
            @Override public void run() { CallLogStore.markSeen(DialerActivity.this); }
        }, "pc-tel-seen").start();
    }

    @Override protected void onThemeChanged() { applySkin(); }

    // ---------------------------------------------------------------- painting

    private void applySkin() {
        paintPage(R.id.pc_dl_root);
        TextView title = (TextView) findViewById(R.id.pc_dl_title);
        title.setTextColor(pal.text);
        Skin.glow(title, pal);
        icon(R.id.pc_dl_texts, R.drawable.ic_pc_chat, pal.muted);
        icon(R.id.pc_dl_search_btn, R.drawable.ic_pc_search, pal.muted);
        numberView.setTextColor(pal.text);
        Skin.glow(numberView, pal);
        notice.setBackground(Skin.ghost(this, pal, pal.amber, false));
        notice.setTextColor(pal.text);
        empty.setTextColor(pal.muted);
        search.setBackground(Skin.panel(this, pal));
        search.setTextColor(pal.text);
        search.setHintTextColor(pal.muted);
        int sp = dp(10);
        search.setPadding(sp, sp, sp, sp);

        // THE NEON EDGE under the header. The flagship theme is called Cyberpunk and a flat panel
        // with a grey hairline is not it; every light palette degrades to exactly that hairline,
        // because a bloom behind dark text on a light background destroys it.
        View bar = findViewById(R.id.pc_dl_tabs);
        if (bar != null) bar.setBackground(Skin.bar(this, pal, false));
        paintTab(tPad, tab == TAB_KEYPAD);
        paintTab(tRecent, tab == TAB_RECENT);
        paintTab(tContacts, tab == TAB_CONTACTS);
        paintTab(tVm, tab == TAB_VOICEMAIL);

        /* THE KEYPAD IS A WHOLE TAB, not a strip under a list. On its own tab it gets the screen and
         * the keys are sized from it; on the other three it is gone entirely and the list gets the
         * room. A dialpad squeezed under a list is what "should be an entire tab that looks like a
         * nice dialer" was about. */
        boolean onPad = tab == TAB_KEYPAD;
        padWrap.setVisibility(onPad ? View.VISIBLE : View.GONE);
        list.setVisibility(onPad ? View.GONE : View.VISIBLE);
        padToggle.setVisibility(onPad ? View.INVISIBLE : View.VISIBLE);

        callBtn.setBackground(Skin.pill(this, pal, pal.green, true));
        callBtn.setImageDrawable(tint(R.drawable.ic_pc_call, 0xFF0B1A10));
        backBtn.setImageDrawable(tint(R.drawable.ic_pc_close, pal.muted));
        padToggle.setImageDrawable(tint(R.drawable.ic_pc_grid, pal.accent));
        padToggle.setBackground(Skin.pill(this, pal, Skin.alpha(pal.accent, 0.16), true));
        Keypad.build(this, pad, pal, keySizeDp(), new Keypad.Press() {
            @Override public void onKey(char digit) { typed = Dial.press(typed, digit); drawNumber(); reload(); }
        });
        Keypad.onLongPress(pad, '1', new Runnable() {
            // HOLDING "1" CALLS VOICEMAIL. It has done since before smartphones, and the number is
            // the SIM's own — never the literal "1", which just dials a stranger.
            @Override public void run() { callVoicemail(); }
        });
        drawNumber();
        if (adapter != null) adapter.notifyDataSetChanged();
    }

    /**
     * How big a key should be, from the screen rather than from a constant. Three columns plus their
     * margins have to fit the width, and four rows plus the number and the call button have to fit
     * the height — a fixed dp that is generous on a tall phone clips the bottom row on a short one,
     * and a bottom row you cannot reach is a dialpad with nine keys.
     */
    private int keySizeDp() {
        android.util.DisplayMetrics m = getResources().getDisplayMetrics();
        int wdp = (int) (m.widthPixels / m.density);
        int hdp = (int) (m.heightPixels / m.density);
        int byWidth = (wdp - 40) / 3 - 18;
        int byHeight = (hdp - 300) / 4 - 18;
        return Math.max(52, Math.min(88, Math.min(byWidth, byHeight)));
    }

    private void paintTab(TextView t, boolean on) {
        Skin.heading(t, pal);
        t.setTextColor(on ? pal.accent : pal.muted);
        t.setBackground(on ? Skin.pill(this, pal, Skin.alpha(pal.accent, 0.14), false) : null);
    }

    private void drawNumber() {
        numberView.setText(Dial.pretty(typed));
        backBtn.setVisibility(typed.isEmpty() ? View.INVISIBLE : View.VISIBLE);
    }

    // ---------------------------------------------------------------- the list

    private void reload() {
        final String q = search.getVisibility() == View.VISIBLE
                ? search.getText().toString() : typed;
        final int which = tab;
        new Thread(new Runnable() {
            @Override public void run() {
                final List<Row> rows = build(which, q);
                main.post(new Runnable() {
                    @Override public void run() {
                        if (which != tab) return;
                        adapter.set(rows);
                        empty.setVisibility(rows.isEmpty() ? View.VISIBLE : View.GONE);
                        empty.setText(which == TAB_CONTACTS ? R.string.tel_contacts_empty
                                    : which == TAB_VOICEMAIL ? R.string.tel_vm_empty
                                    : R.string.tel_recent_empty);
                        boolean isDefault = HasDialerRole.yes(DialerActivity.this);
                        notice.setVisibility(isDefault ? View.GONE : View.VISIBLE);
                        if (!isDefault) notice.setText(R.string.tel_not_default);
                    }
                });
            }
        }, "pc-tel-list").start();
    }

    /** Built off the main thread — every source here is a cross-process query. */
    private List<Row> build(int which, String q) {
        List<Row> out = new ArrayList<Row>();
        if (which == TAB_CONTACTS) {
            for (ContactList.Person p : ContactList.search(this, q, 500)) {
                Row r = new Row();
                r.label = p.label();
                r.sub = p.name.isEmpty() ? "" : Dial.pretty(p.number);
                r.number = p.number;
                r.contactId = p.id;
                r.icon = R.drawable.ic_pc_user;
                out.add(r);
            }
            return out;
        }
        List<CallLogStore.Entry> src = which == TAB_VOICEMAIL
                ? Voicemail.messages(this, 300)
                : CallLogStore.recent(this, 300);
        String needle = q == null ? "" : q.trim().toLowerCase(java.util.Locale.ROOT);
        String digits = needle.replaceAll("[^0-9+]", "");
        for (CallLogStore.Entry e : src) {
            String label = !e.name.isEmpty() ? e.name : e.number;
            if (label.isEmpty()) label = getString(R.string.tel_unknown);
            if (!needle.isEmpty()) {
                boolean hit = label.toLowerCase(java.util.Locale.ROOT).contains(needle)
                        || (!digits.isEmpty() && e.number.replaceAll("[^0-9+]", "").contains(digits));
                if (!hit) continue;
            }
            Row r = new Row();
            r.label = label;
            r.number = e.number;
            r.entry = e;
            r.missed = e.missed();
            r.icon = e.type == CallLog.Calls.MISSED_TYPE ? R.drawable.ic_pc_close
                   : e.type == CallLog.Calls.VOICEMAIL_TYPE ? R.drawable.ic_pc_mic
                   : e.outgoing() ? R.drawable.ic_pc_send : R.drawable.ic_pc_call;
            String rel = e.date > 0 ? DateUtils.getRelativeTimeSpanString(e.date,
                    System.currentTimeMillis(), DateUtils.MINUTE_IN_MILLIS).toString() : "";
            r.sub = e.missed() ? getString(R.string.tel_missed) + "  ·  " + rel : rel;
            out.add(r);
        }
        return out;
    }

    // ---------------------------------------------------------------- actions

    private void place() {
        if (!Dial.dialable(typed)) { say(getString(R.string.tel_nothing_to_call)); return; }
        placeNumber(typed);
    }

    private void placeNumber(String raw) {
        String num = Dial.telPart(raw);
        if (num.isEmpty()) { say(getString(R.string.tel_nothing_to_call)); return; }
        Uri tel = Uri.parse("tel:" + Uri.encode(num));
        if (Dial.isServiceCode(num)) {
            try { startActivity(new Intent(Intent.ACTION_DIAL, tel)); return; }
            catch (Throwable ignored) { }
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M
                && checkSelfPermission(android.Manifest.permission.CALL_PHONE)
                   != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{ android.Manifest.permission.CALL_PHONE }, REQ_PERMS);
            return;
        }
        try {
            TelecomManager tm = (TelecomManager) getSystemService(Context.TELECOM_SERVICE);
            if (tm != null) { tm.placeCall(tel, null); typed = ""; drawNumber(); return; }
        } catch (SecurityException e) {
            say(getString(R.string.tel_no_permission));
            return;
        } catch (Throwable ignored) { }
        try { startActivity(new Intent(Intent.ACTION_DIAL, tel)); }
        catch (Throwable t) { say(getString(R.string.tel_cannot_call)); }
    }

    private void callVoicemail() {
        String n = Voicemail.number(this);
        // SAY SO rather than dialling "1" at a stranger. A phone with no voicemail configured has no
        // voicemail number, and guessing one is how a dialer places a call nobody asked for.
        if (n.isEmpty()) { say(getString(R.string.tel_vm_none)); return; }
        placeNumber(n);
    }

    private void openVoicemail(CallLogStore.Entry e) {
        try { startActivity(Voicemail.open(e)); }
        catch (Throwable t) { placeNumber(Voicemail.number(this)); }
    }

    private void askForWhatIsMissing() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.M) return;
        List<String> want = new ArrayList<String>();
        for (String p : new String[]{ android.Manifest.permission.READ_CALL_LOG,
                                      android.Manifest.permission.READ_CONTACTS }) {
            if (checkSelfPermission(p) != PackageManager.PERMISSION_GRANTED) want.add(p);
        }
        if (want.isEmpty()) return;
        try { requestPermissions(want.toArray(new String[0]), REQ_PERMS); } catch (Throwable ignored) { }
    }

    @Override
    public void onRequestPermissionsResult(int code, String[] perms, int[] granted) {
        super.onRequestPermissionsResult(code, perms, granted);
        if (code != REQ_PERMS) return;
        PhoneBook.forget();
        reload();
    }

    private void rowMenu(final Row r) {
        if (r == null) return;
        final List<String> labels = new ArrayList<String>();
        final List<Integer> acts = new ArrayList<Integer>();
        labels.add(getString(R.string.tel_call)); acts.add(0);
        labels.add(getString(R.string.tel_text_number)); acts.add(1);
        labels.add(getString(R.string.tel_copy_number)); acts.add(2);
        if (r.contactId >= 0) { labels.add(getString(R.string.tel_view_contact)); acts.add(3); }
        if (r.entry != null) { labels.add(getString(R.string.tel_delete_entry)); acts.add(4); }
        try {
            new AlertDialog.Builder(this).setTitle(r.label)
                .setItems(labels.toArray(new CharSequence[0]),
                    new android.content.DialogInterface.OnClickListener() {
                        @Override public void onClick(android.content.DialogInterface d, int w) {
                            switch (acts.get(w)) {
                                case 0: placeNumber(r.number); break;
                                case 1:
                                    try {
                                        startActivity(new Intent(Intent.ACTION_SENDTO,
                                                Uri.parse("smsto:" + Uri.encode(r.number))));
                                    } catch (Throwable ignored) { }
                                    break;
                                case 2:
                                    try {
                                        ClipboardManager cb = (ClipboardManager)
                                                getSystemService(Context.CLIPBOARD_SERVICE);
                                        if (cb != null) cb.setPrimaryClip(ClipData.newPlainText("tel", r.number));
                                    } catch (Throwable ignored) { }
                                    break;
                                case 3:
                                    openPosterContact(r);
                                    break;
                                case 4:
                                    CallLogStore.delete(DialerActivity.this, r.entry.id);
                                    reload();
                                    break;
                            }
                        }
                    }).show();
        } catch (Throwable ignored) { }
    }

    // ---------------------------------------------------------------- adapter

    private final class Rows extends BaseAdapter {
        private List<Row> rows = new ArrayList<Row>();

        void set(List<Row> r) { rows = r; notifyDataSetChanged(); }
        Row at(int i) { return i >= 0 && i < rows.size() ? rows.get(i) : null; }

        @Override public int getCount() { return rows.size(); }
        @Override public Object getItem(int i) { return at(i); }
        @Override public long getItemId(int i) { return i; }

        @Override
        public View getView(int i, View reuse, ViewGroup parent) {
            View v = reuse;
            if (v == null) v = LayoutInflater.from(DialerActivity.this)
                    .inflate(R.layout.tel_recent_row, parent, false);
            final Row r = at(i);
            TextView who = (TextView) v.findViewById(R.id.pc_rc_who);
            TextView when = (TextView) v.findViewById(R.id.pc_rc_when);
            ImageView kind = (ImageView) v.findViewById(R.id.pc_rc_kind);
            ImageView call = (ImageView) v.findViewById(R.id.pc_rc_call);
            View card = v.findViewById(R.id.pc_rc_card);
            if (r == null) return v;
            card.setBackground(Skin.panel(DialerActivity.this, pal));
            who.setText(r.label);
            who.setTextColor(r.missed ? pal.danger : pal.text);
            when.setText(r.sub);
            when.setTextColor(pal.muted);
            when.setVisibility(r.sub.isEmpty() ? View.GONE : View.VISIBLE);
            kind.setImageDrawable(tint(r.icon, r.missed ? pal.danger : pal.muted));
            call.setImageDrawable(tint(R.drawable.ic_pc_call, pal.accent));
            call.setOnClickListener(new View.OnClickListener() {
                @Override public void onClick(View x) { placeNumber(r.number); }
            });
            /* A contact row means "open this person"; the phone glyph is the explicit call action.
             * Previously the large row did nothing and Open contact was buried in a menu that was
             * not wired here either, so the Contacts tab looked selectable but was inert. */
            card.setOnClickListener(new View.OnClickListener() {
                @Override public void onClick(View x) {
                    if (r.contactId >= 0) {
                        openPosterContact(r);
                    } else rowMenu(r);
                }
            });
            card.setOnLongClickListener(new View.OnLongClickListener() {
                @Override public boolean onLongClick(View x) { rowMenu(r); return true; }
            });
            return v;
        }
    }
}
