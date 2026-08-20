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
import android.text.format.DateUtils;
import android.view.LayoutInflater;
import android.view.View;
import android.view.ViewGroup;
import android.widget.AdapterView;
import android.widget.BaseAdapter;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.ListView;
import android.widget.TextView;

import java.util.ArrayList;
import java.util.List;

import place.poster.app.R;
import place.poster.app.sms.PhoneBook;
import place.poster.app.ui.PcActivity;
import place.poster.app.ui.Skin;

/**
 * THE DIALER — a keypad, and the phone's own recent calls above it.
 *
 * Recents come from `CallLog.Calls`, which is authoritative on the device for the same reason the
 * message store is: every other app on the phone reads it. Nothing here keeps a second history.
 *
 * WHAT IT REFUSES TO DO. A GSM service code (`*#06#`, `*21*…#`) is handed to ACTION_DIAL rather than
 * placed as a call — the platform intercepts those and shows the result. Placed as a call they
 * either fail or, worse, change a network setting with nothing shown. That rule is in `Dial`, which
 * is pure and run by tests.
 */
public class DialerActivity extends PcActivity {

    private static final int REQ_PERMS = 4401;

    private TextView numberView, notice;
    private ListView recents;
    private LinearLayout pad;
    private ImageView callBtn, backBtn;
    private Recents adapter;
    private String typed = "";
    private final Handler main = new Handler(Looper.getMainLooper());

    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        setContentView(R.layout.tel_dialer);
        numberView = (TextView) findViewById(R.id.pc_dl_number);
        notice = (TextView) findViewById(R.id.pc_dl_notice);
        recents = (ListView) findViewById(R.id.pc_dl_recent);
        pad = (LinearLayout) findViewById(R.id.pc_dl_pad);
        callBtn = (ImageView) findViewById(R.id.pc_dl_call);
        backBtn = (ImageView) findViewById(R.id.pc_dl_back);

        adapter = new Recents();
        recents.setAdapter(adapter);
        recents.setOnItemClickListener(new AdapterView.OnItemClickListener() {
            @Override public void onItemClick(AdapterView<?> p, View v, int i, long id) {
                CallLogStore.Entry e = adapter.at(i);
                if (e == null) return;
                // A TAP FILLS THE PAD; it does not dial. Placing a call from a single tap in a list
                // is how somebody rings their ex from a pocket — the green button is the commitment.
                typed = e.number;
                drawNumber();
            }
        });
        recents.setOnItemLongClickListener(new AdapterView.OnItemLongClickListener() {
            @Override public boolean onItemLongClick(AdapterView<?> p, View v, int i, long id) {
                entryMenu(adapter.at(i));
                return true;
            }
        });

        callBtn.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { place(); }
        });
        backBtn.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { typed = Dial.backspace(typed); drawNumber(); }
        });
        backBtn.setOnLongClickListener(new View.OnLongClickListener() {
            @Override public boolean onLongClick(View v) { typed = ""; drawNumber(); return true; }
        });
        findViewById(R.id.pc_dl_texts).setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) {
                try {
                    startActivity(new Intent().setClassName(getPackageName(),
                            "place.poster.app.sms.ThreadListActivity"));
                } catch (Throwable ignored) { }
            }
        });

        readIntent(getIntent());
        applySkin();
        askForWhatIsMissing();
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
        if (d == null || d.getScheme() == null) return;
        if (!"tel".equalsIgnoreCase(d.getScheme())) return;
        String raw = d.getSchemeSpecificPart();
        typed = Dial.clean(raw == null ? "" : Uri.decode(raw));
    }

    @Override
    protected void onStart() {
        super.onStart();
        applySkin();
        reload();
        // Opening the recents list IS seeing the missed calls in it — in the provider, so the rest
        // of the phone agrees rather than keeping its own badge.
        new Thread(new Runnable() {
            @Override public void run() { CallLogStore.markSeen(DialerActivity.this); }
        }, "pc-tel-seen").start();
    }

    @Override protected void onThemeChanged() { applySkin(); }

    private void applySkin() {
        paintPage(R.id.pc_dl_root);
        TextView title = (TextView) findViewById(R.id.pc_dl_title);
        title.setTextColor(pal.text);
        Skin.glow(title, pal);
        icon(R.id.pc_dl_texts, R.drawable.ic_pc_chat, pal.muted);
        numberView.setTextColor(pal.text);
        notice.setBackground(Skin.ghost(this, pal, pal.amber, false));
        notice.setTextColor(pal.text);
        callBtn.setBackground(Skin.pill(this, pal, pal.green, true));
        callBtn.setImageDrawable(tint(R.drawable.ic_pc_call, 0xFF0B1A10));
        backBtn.setImageDrawable(tint(R.drawable.ic_pc_close, pal.muted));
        Keypad.build(this, pad, pal, 62, new Keypad.Press() {
            @Override public void onKey(char digit) { typed = Dial.press(typed, digit); drawNumber(); }
        });
        drawNumber();
        if (adapter != null) adapter.notifyDataSetChanged();
    }

    private void drawNumber() {
        numberView.setText(Dial.pretty(typed));
        backBtn.setVisibility(typed.isEmpty() ? View.INVISIBLE : View.VISIBLE);
    }

    private void reload() {
        new Thread(new Runnable() {
            @Override public void run() {
                final List<CallLogStore.Entry> rows = CallLogStore.recent(DialerActivity.this, 300);
                main.post(new Runnable() {
                    @Override public void run() {
                        adapter.set(rows);
                        boolean isDefault = HasDialerRole.yes(DialerActivity.this);
                        notice.setVisibility(isDefault ? View.GONE : View.VISIBLE);
                        if (!isDefault) notice.setText(R.string.tel_not_default);
                    }
                });
            }
        }, "pc-tel-recents").start();
    }

    /**
     * PLACING THE CALL.
     *
     * A service code goes through ACTION_DIAL so the platform can intercept it; anything else goes
     * through TelecomManager.placeCall, which is what a default dialer uses — ACTION_CALL would hand
     * our own call to whatever else answers `tel:`, which on a phone where we ARE the dialer is us,
     * in a loop.
     */
    private void place() {
        if (!Dial.dialable(typed)) { say(getString(R.string.tel_nothing_to_call)); return; }
        String num = Dial.telPart(typed);
        Uri tel = Uri.parse("tel:" + Uri.encode(num));

        if (Dial.isServiceCode(num)) {
            try {
                startActivity(new Intent(Intent.ACTION_DIAL, tel));
                return;
            } catch (Throwable ignored) { }
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
        // Every phone has SOMETHING that answers ACTION_DIAL, including the platform's own dialer,
        // so this is a real fallback rather than a shrug.
        try { startActivity(new Intent(Intent.ACTION_DIAL, tel)); }
        catch (Throwable t) { say(getString(R.string.tel_cannot_call)); }
    }

    /**
     * Ask for what this screen needs, once, when it opens — and NEVER for anything it does not.
     * CALL_PHONE is asked for at the moment somebody presses the green button instead, because that
     * is the one request a person can actually connect to something they did.
     */
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

    private void entryMenu(final CallLogStore.Entry e) {
        if (e == null) return;
        CharSequence[] items = {
            getString(R.string.tel_call), getString(R.string.tel_text_number),
            getString(R.string.tel_copy_number), getString(R.string.tel_delete_entry),
        };
        try {
            new AlertDialog.Builder(this).setTitle(PhoneBook.label(this, e.number))
                .setItems(items, new android.content.DialogInterface.OnClickListener() {
                    @Override public void onClick(android.content.DialogInterface d, int w) {
                        if (w == 0) { typed = e.number; drawNumber(); place(); return; }
                        if (w == 1) {
                            try {
                                startActivity(new Intent(Intent.ACTION_SENDTO,
                                        Uri.parse("smsto:" + Uri.encode(e.number))));
                            } catch (Throwable ignored) { }
                            return;
                        }
                        if (w == 2) {
                            try {
                                ClipboardManager cb = (ClipboardManager)
                                        getSystemService(Context.CLIPBOARD_SERVICE);
                                if (cb != null) cb.setPrimaryClip(ClipData.newPlainText("tel", e.number));
                            } catch (Throwable ignored) { }
                            return;
                        }
                        CallLogStore.delete(DialerActivity.this, e.id);
                        reload();
                    }
                }).show();
        } catch (Throwable ignored) { }
    }

    private final class Recents extends BaseAdapter {
        private List<CallLogStore.Entry> rows = new ArrayList<CallLogStore.Entry>();

        void set(List<CallLogStore.Entry> r) { rows = r; notifyDataSetChanged(); }
        CallLogStore.Entry at(int i) { return i >= 0 && i < rows.size() ? rows.get(i) : null; }

        @Override public int getCount() { return rows.size(); }
        @Override public Object getItem(int i) { return at(i); }
        @Override public long getItemId(int i) { return i; }

        @Override
        public View getView(int i, View reuse, ViewGroup parent) {
            View v = reuse;
            if (v == null) v = LayoutInflater.from(DialerActivity.this)
                    .inflate(R.layout.tel_recent_row, parent, false);
            final CallLogStore.Entry e = at(i);
            TextView who = (TextView) v.findViewById(R.id.pc_rc_who);
            TextView when = (TextView) v.findViewById(R.id.pc_rc_when);
            ImageView kind = (ImageView) v.findViewById(R.id.pc_rc_kind);
            ImageView call = (ImageView) v.findViewById(R.id.pc_rc_call);
            View card = v.findViewById(R.id.pc_rc_card);
            if (e == null) return v;

            // Already resolved, on the thread that read the call log — see CallLogStore.recent.
            String label = !e.name.isEmpty() ? e.name : e.number;
            if (label.isEmpty()) label = getString(R.string.tel_unknown);
            card.setBackground(Skin.panel(DialerActivity.this, pal));
            who.setText(label);
            who.setTextColor(e.missed() ? pal.danger : pal.text);
            String rel = e.date > 0 ? DateUtils.getRelativeTimeSpanString(e.date,
                    System.currentTimeMillis(), DateUtils.MINUTE_IN_MILLIS).toString() : "";
            when.setText(e.missed() ? getString(R.string.tel_missed) + "  ·  " + rel : rel);
            when.setTextColor(pal.muted);
            kind.setImageDrawable(tint(
                    e.type == CallLog.Calls.MISSED_TYPE ? R.drawable.ic_pc_close
                  : e.outgoing() ? R.drawable.ic_pc_send : R.drawable.ic_pc_call,
                    e.missed() ? pal.danger : pal.muted));
            call.setImageDrawable(tint(R.drawable.ic_pc_call, pal.accent));
            call.setOnClickListener(new View.OnClickListener() {
                @Override public void onClick(View x) { typed = e.number; drawNumber(); place(); }
            });
            return v;
        }
    }
}
