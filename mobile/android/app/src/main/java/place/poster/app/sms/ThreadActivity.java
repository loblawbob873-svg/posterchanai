package place.poster.app.sms;

import android.app.AlertDialog;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
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
import android.view.Gravity;
import android.view.LayoutInflater;
import android.view.View;
import android.view.ViewGroup;
import android.widget.AdapterView;
import android.widget.BaseAdapter;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ListView;
import android.widget.TextView;

import java.util.ArrayList;
import java.util.List;

import place.poster.app.R;
import place.poster.app.ui.CalendarPeek;
import place.poster.app.ui.PcActivity;
import place.poster.app.ui.Skin;

/**
 * ONE CONVERSATION.
 *
 * Read from the phone's own message store and written back to it — the archive on the person's other
 * devices is published by the client from these same rows, never instead of them.
 *
 * THREE THINGS IT BORROWS RATHER THAN REBUILDING, because a phone should feel like one app:
 *   * the NAME comes from the phone's whole address book (PhoneBook), which is where PosterChan's
 *     own synced contacts already are — never a second contact store;
 *   * the CONTEXT LINE comes from the calendar the client has already decrypted (CalendarPeek);
 *   * the CALL button hands off to the dialer by ACTION_DIAL, so it works whether or not PosterChan
 *     is the phone app.
 *
 * NO POLLING — a ContentObserver, registered while the screen is up. See ThreadListActivity.
 */
public class ThreadActivity extends PcActivity {

    public static final String EXTRA_THREAD = "thread";
    /**
     * EVERY thread id this conversation covers. One person can own more than one (SmsStore.fold),
     * and reading only EXTRA_THREAD is what showed a conversation with your own replies missing.
     */
    public static final String EXTRA_THREADS = "threads";
    public static final String EXTRA_ADDRESS = "address";

    private long threadId;
    private long[] threadIds = new long[0];

    /** The conversation's thread ids, falling back to the single one it was opened with. */
    private long[] ids() {
        if (threadIds.length > 0) return threadIds;
        return threadId > 0 ? new long[]{ threadId } : new long[0];
    }
    private String address = "";
    private ListView list;
    private EditText input;
    private TextView count, name, sub, avatar, context;
    private Msgs adapter;
    private final Handler main = new Handler(Looper.getMainLooper());
    private ContentObserver watcher;

    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        setContentView(R.layout.sms_thread);
        list = (ListView) findViewById(R.id.pc_th_list);
        input = (EditText) findViewById(R.id.pc_th_input);
        count = (TextView) findViewById(R.id.pc_th_count);
        name = (TextView) findViewById(R.id.pc_th_name);
        sub = (TextView) findViewById(R.id.pc_th_sub);
        avatar = (TextView) findViewById(R.id.pc_th_avatar);
        context = (TextView) findViewById(R.id.pc_th_context);
        // AFTER the views exist, not before. readIntent prefills the compose box from a
        // `sms:+1555?body=…` link, and called first it wrote into a null EditText — guarded, so the
        // text was silently dropped and the screen opened empty. That is the whole point of the
        // `?body=` parameter, and it would have looked like the link not working.
        readIntent(getIntent());

        adapter = new Msgs();
        list.setAdapter(adapter);
        list.setOnItemLongClickListener(new AdapterView.OnItemLongClickListener() {
            @Override public boolean onItemLongClick(AdapterView<?> p, View v, int i, long id) {
                messageMenu(adapter.at(i));
                return true;
            }
        });

        findViewById(R.id.pc_th_back).setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { finish(); }
        });
        findViewById(R.id.pc_th_send).setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { send(); }
        });
        findViewById(R.id.pc_th_call).setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { call(); }
        });
        findViewById(R.id.pc_th_menu).setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { threadMenu(); }
        });
        input.addTextChangedListener(new TextWatcher() {
            @Override public void beforeTextChanged(CharSequence s, int a, int b, int c) { }
            @Override public void onTextChanged(CharSequence s, int a, int b, int c) { }
            @Override public void afterTextChanged(Editable e) { updateCount(); }
        });

        applySkin();
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        readIntent(intent);
        applySkin();
        reload();
    }

    private void readIntent(Intent i) {
        if (i == null) return;
        long t = i.getLongExtra(EXTRA_THREAD, 0);
        String a = i.getStringExtra(EXTRA_ADDRESS);
        // Started from a `sms:` link in another app, rather than from our own list.
        if ((a == null || a.isEmpty()) && SendTo.isMessageUri(i.getData())) {
            a = SendTo.numberFrom(i.getData());
        }
        // The prefilled text arrives two ways: in the URI as RFC 5724's `?body=`, and as an extra
        // from a share sheet. Both, because which one you get depends on the app that sent it.
        String body = SendTo.bodyFrom(i.getData());
        if (body.isEmpty()) {
            CharSequence extra = i.getCharSequenceExtra(Intent.EXTRA_TEXT);
            if (extra != null) body = extra.toString();
        }
        if (input != null && !body.isEmpty() && input.getText().length() == 0) input.setText(body);
        if (a != null && !a.isEmpty()) address = a;
        if (t > 0) threadId = t;
        else if (!address.isEmpty()) threadId = SmsStore.threadIdFor(this, address);
        long[] many = i.getLongArrayExtra(EXTRA_THREADS);
        // Opened from our own list, which already grouped the conversation. Opened from anywhere
        // else -- an `sms:` link, a share sheet, a notification -- there is only an address, so the
        // grouping is done here rather than trusting one id to be the whole conversation.
        if (many != null && many.length > 0) threadIds = many;
        else if (!address.isEmpty()) threadIds = SmsStore.idsFor(this, address, threadId);
        else if (threadId > 0) threadIds = new long[]{ threadId };
    }

    @Override
    protected void onStart() {
        super.onStart();
        applySkin();
        reload();
        watcher = new ContentObserver(main) {
            @Override public void onChange(boolean self) { reload(); }
            @Override public void onChange(boolean self, Uri uri) { reload(); }
        };
        try {
            getContentResolver().registerContentObserver(Telephony.Sms.CONTENT_URI, true, watcher);
        } catch (Throwable ignored) { watcher = null; }
        // Opening a conversation IS reading it — in the provider, so every other app on the phone
        // agrees, and in the shade, so the notification goes.
        SmsStore.markRead(this, ids());
        SmsNotifier.clear(this, threadId);
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
        paintPage(R.id.pc_th_root);
        View bar = findViewById(R.id.pc_th_bar);
        if (bar != null) bar.setBackground(Skin.bar(this, pal, false));
        View composeBar = findViewById(R.id.pc_th_compose);
        if (composeBar != null) composeBar.setBackground(Skin.panel(this, pal));
        String label = PhoneBook.label(this, address);
        name.setText(label);
        name.setTextColor(pal.text);
        Skin.glow(name, pal);
        sub.setText(label.equals(address) ? "" : address);
        sub.setTextColor(pal.muted);
        avatar.setText(initials(label));
        avatar.setBackground(Skin.avatar(this, pal, label));
        icon(R.id.pc_th_back, R.drawable.ic_pc_arrow_left, pal.text);
        icon(R.id.pc_th_call, R.drawable.ic_pc_call, pal.accent);
        icon(R.id.pc_th_menu, R.drawable.ic_pc_menu, pal.muted);
        icon(R.id.pc_th_send, R.drawable.ic_pc_send, pal.onAccent());
        findViewById(R.id.pc_th_send).setBackground(Skin.pill(this, pal, pal.accent, true));
        input.setTextColor(pal.text);
        input.setHintTextColor(pal.muted);
        count.setTextColor(pal.muted);
        context.setBackground(Skin.ghost(this, pal, pal.accent2, false));
        context.setTextColor(pal.text);
        if (adapter != null) adapter.notifyDataSetChanged();
        paintContext();
    }

    /** The calendar line. Off the main thread — it reads the address book. */
    private void paintContext() {
        final String who = address;
        new Thread(new Runnable() {
            @Override public void run() {
                final String line = CalendarPeek.nextWith(ThreadActivity.this, who);
                main.post(new Runnable() {
                    @Override public void run() {
                        if (!who.equals(address)) return;      // the screen moved on
                        context.setVisibility(line.isEmpty() ? View.GONE : View.VISIBLE);
                        if (!line.isEmpty()) context.setText(line);
                    }
                });
            }
        }, "pc-sms-context").start();
    }

    private void reload() {
        final long id = threadId;
        new Thread(new Runnable() {
            @Override public void run() {
                // BOTH PROVIDERS — see Messages. Texts and picture messages are one
                // conversation and always have been; read from SmsStore alone this screen
                // showed a thread with its pictures missing and no gap to say so.
                final List<SmsMsg> rows = Messages.thread(ThreadActivity.this, id, 500);
                main.post(new Runnable() {
                    @Override public void run() {
                        if (id != threadId) return;
                        boolean atEnd = list.getLastVisiblePosition() >= adapter.getCount() - 2;
                        adapter.set(rows);
                        // Only follow the conversation down when the person was ALREADY at the
                        // bottom. Yanking somebody out of what they were reading because a message
                        // arrived is the thing every messaging app gets wrong once.
                        if (atEnd) list.setSelection(adapter.getCount() - 1);
                    }
                });
            }
        }, "pc-sms-thread").start();
    }

    private void updateCount() {
        String body = input.getText().toString();
        int parts = SmsKeys.segments(body);
        // Only once it MATTERS. A counter on an empty box is noise; a counter that appears at the
        // moment a message becomes two texts is information.
        boolean show = parts > 1;
        count.setVisibility(show ? View.VISIBLE : View.GONE);
        if (show) count.setText(parts + "×");
    }

    private void send() {
        String body = input.getText().toString();
        if (body.trim().isEmpty()) return;
        if (address.isEmpty()) { say(getString(R.string.sms_not_default)); return; }
        SmsSender.Result r = SmsSender.send(this, address, body, threadId);
        if (!r.ok) { say(r.error.isEmpty() ? getString(R.string.sms_failed) : r.error); return; }
        // Cleared only once the row exists. If the send throws, what somebody typed is still there.
        input.setText("");
        updateCount();
        reload();
    }

    private void call() {
        try {
            startActivity(new Intent(Intent.ACTION_DIAL, Uri.parse("tel:" + Uri.encode(address))));
        } catch (Throwable t) {
            say(getString(R.string.sms_call_no_dialer));
        }
    }

    private void threadMenu() {
        try {
            new AlertDialog.Builder(this)
                .setItems(new CharSequence[]{ getString(R.string.sms_delete_thread) },
                    new android.content.DialogInterface.OnClickListener() {
                        @Override public void onClick(android.content.DialogInterface d, int w) {
                            int n = SmsStore.deleteThread(ThreadActivity.this, ids());
                            SmsNotifier.clear(ThreadActivity.this, threadId);
                            say(getString(R.string.sms_deleted_here) + " (" + n + ")");
                            finish();
                        }
                    }).show();
        } catch (Throwable ignored) { }
    }

    private void messageMenu(final SmsMsg m) {
        if (m == null) return;
        try {
            new AlertDialog.Builder(this)
                .setItems(new CharSequence[]{ getString(R.string.sms_copy),
                                              getString(R.string.sms_delete_msg) },
                    new android.content.DialogInterface.OnClickListener() {
                        @Override public void onClick(android.content.DialogInterface d, int w) {
                            if (w == 0) {
                                try {
                                    ClipboardManager cb = (ClipboardManager)
                                            getSystemService(Context.CLIPBOARD_SERVICE);
                                    if (cb != null) cb.setPrimaryClip(ClipData.newPlainText("sms", m.body));
                                } catch (Throwable ignored) { }
                                return;
                            }
                            // A PICTURE MESSAGE LIVES AT A DIFFERENT URI. Handed to SmsStore it
                            // deletes nothing and says nothing, and the bubble comes straight back
                            // on the reload below — a delete that silently did not happen.
                            if (m.mms) MmsStore.delete(ThreadActivity.this, new long[]{ m.id });
                            else SmsStore.delete(ThreadActivity.this, new long[]{ m.id });
                            say(getString(R.string.sms_deleted_here));
                            reload();
                        }
                    }).show();
        } catch (Throwable ignored) { }
    }

    /**
     * What a bubble says. The body when there is one, and otherwise a plain description of what was
     * attached — never an empty bubble, which is what "this message failed" looks like.
     */
    private String bubbleText(SmsMsg m) {
        if (m.parts.isEmpty()) {
            if (!m.body.isEmpty()) return m.body;
            // Nothing to show is still something to SAY. A blank bubble is indistinguishable from a
            // message the app lost, and a thread of them reads as the app losing a conversation.
            return getString(m.undownloaded ? R.string.sms_mms_pending : R.string.sms_no_content);
        }
        StringBuilder b = new StringBuilder(m.body);
        for (SmsPart p : m.parts) {
            if (b.length() > 0) b.append('\n');
            b.append(getString(R.string.sms_attachment)).append("  ·  ").append(p.ct);
        }
        return b.toString();
    }

    private final class Msgs extends BaseAdapter {
        private List<SmsMsg> rows = new ArrayList<SmsMsg>();

        void set(List<SmsMsg> r) { rows = r; notifyDataSetChanged(); }
        SmsMsg at(int i) { return i >= 0 && i < rows.size() ? rows.get(i) : null; }

        @Override public int getCount() { return rows.size(); }
        @Override public Object getItem(int i) { return at(i); }
        @Override public long getItemId(int i) { return i; }

        @Override
        public View getView(int i, View reuse, ViewGroup parent) {
            View v = reuse;
            if (v == null) v = LayoutInflater.from(ThreadActivity.this)
                    .inflate(R.layout.sms_bubble, parent, false);
            SmsMsg m = at(i);
            LinearLayout wrap = (LinearLayout) v.findViewById(R.id.pc_b_wrap);
            TextView text = (TextView) v.findViewById(R.id.pc_b_text);
            TextView meta = (TextView) v.findViewById(R.id.pc_b_meta);
            if (m == null) return v;

            boolean mine = !m.incoming();
            // A PICTURE MESSAGE WITH NO CAPTION IS AN EMPTY BUBBLE, which reads as a message that
            // failed rather than one this screen cannot draw. This screen labels attachments; the
            // app's own Texts view shows them.
            text.setText(bubbleText(m));
            text.setTextColor(pal.text);
            wrap.setBackground(Skin.bubble(ThreadActivity.this, pal, mine));
            // The bubble hugs its side and stops well short of the far edge, so a thread reads as a
            // conversation rather than as full-width blocks alternating colour.
            LinearLayout.LayoutParams lp = (LinearLayout.LayoutParams) wrap.getLayoutParams();
            lp.gravity = mine ? Gravity.END : Gravity.START;
            lp.width = ViewGroup.LayoutParams.WRAP_CONTENT;
            lp.rightMargin = mine ? 0 : dp(48);
            lp.leftMargin = mine ? dp(48) : 0;
            wrap.setLayoutParams(lp);

            String when = m.date > 0
                    ? DateUtils.getRelativeTimeSpanString(m.date, System.currentTimeMillis(),
                            DateUtils.MINUTE_IN_MILLIS).toString()
                    : "";
            if (m.failed()) {
                meta.setText(getString(R.string.sms_failed) + "  ·  " + when);
                meta.setTextColor(pal.danger);
            } else if (m.pending()) {
                meta.setText(getString(R.string.sms_sending) + "  ·  " + when);
                meta.setTextColor(pal.amber);
            } else {
                meta.setText(when);
                meta.setTextColor(pal.muted);
            }
            return v;
        }
    }
}
