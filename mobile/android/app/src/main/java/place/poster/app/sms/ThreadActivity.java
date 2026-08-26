package place.poster.app.sms;

import android.app.AlertDialog;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.Intent;
import android.database.ContentObserver;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
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
import android.widget.ImageView;
import android.widget.Button;
import android.widget.GridLayout;
import android.util.LruCache;

import place.poster.app.signer.SignerRelayService;

import java.util.ArrayList;
import java.util.List;
import java.io.InputStream;
import java.io.ByteArrayOutputStream;

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

    private static final int PICK_MMS_IMAGE = 7312;

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
    private Uri attachment;
    private TextView count, name, sub, avatar, context;
    private Msgs adapter;
    private final Handler main = new Handler(Looper.getMainLooper());
    private ContentObserver watcher;
    /* Thumbnails only. A conversation may contain years of pictures; bounding this cache prevents
     * the Messages screen from becoming the reason Android kills the app. */
    private final LruCache<Long, Bitmap> thumbs = new LruCache<Long, Bitmap>(24) {
        @Override protected int sizeOf(Long k, Bitmap b) { return Math.max(1, b.getByteCount() / (256 * 1024)); }
    };

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
        findViewById(R.id.pc_th_attach).setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { pickAttachment(); }
        });
        findViewById(R.id.pc_th_emoji).setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { pickEmoji(); }
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
        boolean observed = false;
        try { getContentResolver().registerContentObserver(Telephony.Sms.CONTENT_URI, true, watcher);
              observed = true; } catch (Throwable ignored) { }
        /* MMS writes and status transitions notify content://mms, not content://sms. Watching only
         * the latter left a newly sent picture and its Sending/Failed state frozen until the user
         * backed out and reopened the conversation. One observer may be registered on both URIs;
         * unregisterContentObserver removes all of its registrations. */
        try { getContentResolver().registerContentObserver(Telephony.Mms.CONTENT_URI, true, watcher);
              observed = true; } catch (Throwable ignored) { }
        if (!observed) watcher = null;
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
        android.graphics.drawable.Drawable photo = PhoneBook.photoDrawable(this, address);
        avatar.setText(photo == null ? initials(label) : "");
        avatar.setBackground(photo == null ? Skin.avatar(this, pal, label) : photo);
        icon(R.id.pc_th_back, R.drawable.ic_pc_arrow_left, pal.text);
        icon(R.id.pc_th_call, R.drawable.ic_pc_call, pal.accent);
        icon(R.id.pc_th_menu, R.drawable.ic_pc_menu, pal.muted);
        icon(R.id.pc_th_send, R.drawable.ic_pc_send, pal.onAccent());
        icon(R.id.pc_th_attach, R.drawable.ic_pc_paperclip, pal.accent);
        icon(R.id.pc_th_emoji, R.drawable.ic_pc_smile, pal.accent);
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
        if (body.trim().isEmpty() && attachment == null) return;
        if (address.isEmpty()) { say(getString(R.string.sms_not_default)); return; }
        if (attachment != null) {
            sendMms(body);
            return;
        }
        SmsSender.Result r = SmsSender.send(this, address, body, threadId);
        if (!r.ok) { say(r.error.isEmpty() ? getString(R.string.sms_failed) : r.error); return; }
        // Cleared only once the row exists. If the send throws, what somebody typed is still there.
        input.setText("");
        updateCount();
        reload();
    }

    /** Pick through Android's document provider: no broad photo permission and no copied plaintext. */
    private void pickAttachment() {
        Intent i = new Intent(Intent.ACTION_OPEN_DOCUMENT)
                .addCategory(Intent.CATEGORY_OPENABLE)
                .setType("image/*");
        try { startActivityForResult(i, PICK_MMS_IMAGE); }
        catch (Throwable t) { say(getString(R.string.sms_attachment_bad)); }
    }

    /** A compact carrier-safe Unicode picker. Strings are built from code points so joined emoji
     * (skin tones, professions and families) are inserted as one selection, never split at a UTF-16
     * boundary. The system keyboard remains available for its complete/searchable emoji catalogue. */
    private void pickEmoji() {
        final int[][] choices = new int[][]{
            {0x1F600}, {0x1F602}, {0x1F60A}, {0x1F60D}, {0x1F618}, {0x1F62D},
            {0x1F642}, {0x1F644}, {0x1F914}, {0x1F973}, {0x1F44D}, {0x1F44F},
            {0x1F64F}, {0x1F4AA}, {0x2764, 0xFE0F}, {0x1F525}, {0x1F389}, {0x2728},
            {0x1F44B}, {0x1F91D}, {0x1F440}, {0x1F4AF}, {0x1F680}, {0x1F923}
        };
        final AlertDialog dialog = new AlertDialog.Builder(this)
                .setTitle(R.string.sms_emoji_title).create();
        GridLayout grid = new GridLayout(this);
        grid.setColumnCount(6);
        int pad = dp(8); grid.setPadding(pad, pad, pad, pad);
        for (int[] points : choices) {
            final String emoji = new String(points, 0, points.length);
            Button b = new Button(this);
            b.setText(emoji); b.setTextSize(24); b.setMinWidth(dp(48)); b.setMinHeight(dp(48));
            b.setBackgroundColor(android.graphics.Color.TRANSPARENT);
            b.setOnClickListener(v -> { insertEmoji(emoji); dialog.dismiss(); });
            grid.addView(b, new ViewGroup.LayoutParams(dp(52), dp(52)));
        }
        dialog.setView(grid); dialog.show();
    }

    private void insertEmoji(String emoji) {
        int start = Math.max(0, input.getSelectionStart());
        int end = Math.max(0, input.getSelectionEnd());
        input.getText().replace(Math.min(start, end), Math.max(start, end), emoji);
        input.requestFocus();
    }

    @Override
    protected void onActivityResult(int request, int result, Intent data) {
        super.onActivityResult(request, result, data);
        if (request != PICK_MMS_IMAGE || result != RESULT_OK || data == null) return;
        Uri picked = data.getData();
        if (picked == null) return;
        try {
            getContentResolver().takePersistableUriPermission(picked,
                    data.getFlags() & (Intent.FLAG_GRANT_READ_URI_PERMISSION
                                     | Intent.FLAG_GRANT_WRITE_URI_PERMISSION));
        } catch (Throwable ignored) { }
        attachment = picked;
        say(getString(R.string.sms_attachment_ready));
    }

    /** Build a carrier MMS PDU and hand it to Android's public system MMS transport. */
    private void sendMms(String body) {
        byte[] raw = null;
        try (InputStream in = getContentResolver().openInputStream(attachment)) {
            if (in != null) {
                ByteArrayOutputStream out = new ByteArrayOutputStream();
                byte[] buf = new byte[64 * 1024]; int n, total = 0;
                while ((n = in.read(buf)) >= 0) {
                    total += n;
                    if (total > 8 * 1024 * 1024) throw new Exception("picture message is too large");
                    out.write(buf, 0, n);
                }
                raw = out.toByteArray();
            }
        } catch (Throwable ignored) { }
        if (raw == null || raw.length == 0) { say(getString(R.string.sms_attachment_bad)); return; }
        try {
            /* One transport for native and WebUI sends. MmsSender selects the active SMS/data
             * subscription; this screen's old duplicate omitted it, producing a provider row that
             * remained at Sending on dual-SIM and stale-default devices. */
            SmsSender.Result result = MmsSender.send(this, address, body, raw);
            if (!result.ok) { say(result.error == null || result.error.isEmpty()
                    ? getString(R.string.sms_failed) : result.error); return; }
            attachment = null;
            input.setText("");
            updateCount();
            say(getString(R.string.sms_mms_sent));
            reload();
        } catch (Throwable t) {
            say(t.getMessage() == null ? getString(R.string.sms_failed) : t.getMessage());
        }
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
            /* Rows created before the completion receiver was shipped can remain OUTBOX forever.
             * They are every bit as retryable as FAILED rows; restricting this action to failed
             * made the repair unreachable for the exact messages it was added to recover. Keep it
             * explicit (long-press → Retry) because an old carrier submission may have escaped even
             * though its provider row never advanced. */
            final boolean retry = m.mms && (m.failed() || m.pending()) && !m.parts.isEmpty()
                    && !m.error.startsWith("delivery unknown");
            final CharSequence[] actions = retry
                    ? new CharSequence[]{ getString(R.string.sms_retry_send),
                                          getString(R.string.sms_copy),
                                          getString(R.string.sms_delete_msg) }
                    : new CharSequence[]{ getString(R.string.sms_copy),
                                          getString(R.string.sms_delete_msg) };
            new AlertDialog.Builder(this)
                .setItems(actions,
                    new android.content.DialogInterface.OnClickListener() {
                        @Override public void onClick(android.content.DialogInterface d, int w) {
                            if (retry && w == 0) { retryMms(m); return; }
                            int action = retry ? w - 1 : w;
                            if (action == 0) {
                                try {
                                    ClipboardManager cb = (ClipboardManager)
                                            getSystemService(Context.CLIPBOARD_SERVICE);
                                    if (cb != null) cb.setPrimaryClip(ClipData.newPlainText("sms", m.body));
                                } catch (Throwable ignored) { }
                                return;
                            }
                            deleteMessage(m);
                        }
                    }).show();
        } catch (Throwable ignored) { }
    }

    /** Retry is explicit: automatically replaying a timed-out carrier send can duplicate it. */
    private void retryMms(final SmsMsg m) {
        SmsPart image = null;
        for (SmsPart p : m.parts) if (p.ct != null && p.ct.startsWith("image/")) { image = p; break; }
        if (image == null) { say(getString(R.string.sms_attachment_bad)); return; }
        byte[] raw = MmsStore.partBytes(this, image.id, 8 * 1024 * 1024);
        if (raw == null || raw.length == 0) { say(getString(R.string.sms_attachment_bad)); return; }
        SmsSender.Result result = MmsSender.send(this, m.address, m.body, raw);
        if (!result.ok) { say(result.error == null || result.error.isEmpty()
                ? getString(R.string.sms_failed) : result.error); return; }
        // A new outbox row now owns this attempt; remove the old FAILED rendering only after the
        // platform accepted the retry, so a synchronous refusal loses nothing.
        if (MmsStore.delete(this, new long[]{m.id}) > 0)
            SignerRelayService.archiveDelete(this, m.docId());
        say(getString(R.string.sms_retrying));
        reload();
    }

    private void deleteMessage(final SmsMsg m) {
        if (m == null) return;
        // A PICTURE MESSAGE LIVES AT A DIFFERENT URI. Handed to SmsStore it deletes nothing and
        // comes straight back on reload, so pending/failed MMS must use content://mms as well.
        int n = m.mms ? MmsStore.delete(this, new long[]{ m.id })
                      : SmsStore.delete(this, new long[]{ m.id });
        if (n > 0) SignerRelayService.archiveDelete(this, m.docId());
        say(n > 0 ? getString(R.string.sms_deleted_here) : getString(R.string.sms_delete_failed));
        reload();
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

    private Bitmap picture(long id) {
        byte[] bytes = MmsStore.partBytes(this, id, 24 * 1024 * 1024);
        if (bytes == null || bytes.length == 0) return null;
        BitmapFactory.Options o = new BitmapFactory.Options(); o.inJustDecodeBounds = true;
        BitmapFactory.decodeByteArray(bytes, 0, bytes.length, o);
        o.inSampleSize = 1;
        while (o.outWidth / o.inSampleSize > 900 || o.outHeight / o.inSampleSize > 900) o.inSampleSize *= 2;
        o.inJustDecodeBounds = false; return BitmapFactory.decodeByteArray(bytes, 0, bytes.length, o);
    }

    private void showPicture(final Bitmap bm) {
        ImageView full = new ImageView(this); full.setImageBitmap(bm);
        full.setAdjustViewBounds(true); full.setScaleType(ImageView.ScaleType.FIT_CENTER);
        new AlertDialog.Builder(this).setView(full).setPositiveButton(android.R.string.ok, null).show();
    }

    private void drawParts(final LinearLayout host, SmsMsg m) {
        host.removeAllViews();
        for (final SmsPart p : m.parts) {
            if (p.ct == null || !p.ct.toLowerCase().startsWith("image/")) continue;
            final ImageView image = new ImageView(this);
            image.setAdjustViewBounds(true); image.setScaleType(ImageView.ScaleType.CENTER_CROP);
            image.setMinimumWidth(dp(180)); image.setMinimumHeight(dp(110));
            image.setMaxWidth(dp(320)); image.setMaxHeight(dp(260));
            final String token = m.id + ":" + p.id; image.setTag(token); host.addView(image);
            Bitmap cached = thumbs.get(p.id);
            if (cached != null) { image.setImageBitmap(cached); image.setOnClickListener(v -> showPicture(cached)); continue; }
            new Thread(() -> {
                final Bitmap bm = picture(p.id); if (bm != null) thumbs.put(p.id, bm);
                main.post(() -> { if (!token.equals(image.getTag()) || bm == null) return;
                    image.setImageBitmap(bm); image.setOnClickListener(v -> showPicture(bm)); });
            }, "pc-mms-thumb").start();
        }
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
            Button retry = (Button) v.findViewById(R.id.pc_b_retry);
            Button delete = (Button) v.findViewById(R.id.pc_b_delete);
            LinearLayout attachments = (LinearLayout) v.findViewById(R.id.pc_b_attachments);
            if (m == null) return v;

            boolean mine = !m.incoming();
            // A PICTURE MESSAGE WITH NO CAPTION IS AN EMPTY BUBBLE, which reads as a message that
            // failed rather than one this screen cannot draw. This screen labels attachments; the
            // app's own Texts view shows them.
            text.setText(bubbleText(m));
            drawParts(attachments, m);
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
                meta.setText(getString(R.string.sms_failed)
                        + (m.error.isEmpty() ? "" : ": " + m.error) + "  ·  " + when);
                meta.setTextColor(pal.danger);
            } else if (m.pending()) {
                meta.setText(getString(R.string.sms_sending) + "  ·  " + when);
                meta.setTextColor(pal.amber);
            } else {
                meta.setText(when);
                meta.setTextColor(pal.muted);
            }
            /* Long-press remains the full copy/delete menu, but it is not discoverable. A stuck
             * outgoing carrier row is urgent and common enough to expose directly. Rebind on every
             * recycled view so an incoming row can never inherit the previous row's listener. */
            boolean retryable = mine && m.mms && (m.pending() || m.failed()) && !m.parts.isEmpty()
                    && !m.error.startsWith("delivery unknown");
            retry.setVisibility(retryable ? View.VISIBLE : View.GONE);
            retry.setEnabled(retryable);
            retry.setText(getString(R.string.sms_retry_send));
            retry.setTextColor(pal.accent);
            retry.setBackground(Skin.ghost(ThreadActivity.this, pal, pal.accent, false));
            retry.setOnClickListener(retryable ? view -> retryMms(m) : null);
            boolean removable = mine && (m.pending() || m.failed());
            delete.setVisibility(removable ? View.VISIBLE : View.GONE);
            delete.setEnabled(removable);
            delete.setText(getString(R.string.sms_delete_msg));
            delete.setTextColor(pal.danger);
            delete.setBackground(Skin.ghost(ThreadActivity.this, pal, pal.danger, false));
            delete.setOnClickListener(removable ? view -> new AlertDialog.Builder(ThreadActivity.this)
                    .setMessage(R.string.sms_delete_confirm)
                    .setNegativeButton(android.R.string.cancel, null)
                    .setPositiveButton(R.string.sms_delete_msg, (dialog, which) -> deleteMessage(m))
                    .show() : null);
            return v;
        }
    }
}
