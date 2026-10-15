package place.poster.app.sms;

import android.app.Activity;
import android.content.Intent;
import android.os.Bundle;

/**
 * "TEXT THIS NUMBER", asked by another app.
 *
 * The third of the four things Android requires before an app may hold the SMS role: an activity for
 * ACTION_SENDTO on `sms:`, `smsto:`, `mms:` and `mmsto:`. It is what a `Text` link in a web page, a
 * contact card's message button and a share sheet all land on.
 *
 * It holds no UI of its own — it reads the URI (SendTo, which is pure and tested against the six
 * shapes these arrive in) and hands off to the conversation, then finishes so it never appears in
 * the back stack. Pressing back from the thread returns to whatever asked, which is what somebody
 * who tapped a link expects.
 *
 * A PLAIN Activity with a TRANSLUCENT theme, and both matter. It draws nothing, so it should SHOW
 * nothing: given an opaque theme the person tapping a link in a web page gets a black rectangle for
 * a frame before the conversation appears. And it extends Activity rather than PcActivity because it
 * has no palette to apply — there is no view to apply one to.
 */
public class SendToActivity extends Activity {

    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        Intent in = getIntent();
        String type = in == null ? null : in.getType();
        if (in != null && Intent.ACTION_SEND.equals(in.getAction()) && type != null
                && (type.startsWith("image/") || type.startsWith("video/")) && SmsShare.stream(in) == null) {
            android.widget.Toast.makeText(this, place.poster.app.R.string.sms_attachment_bad,
                    android.widget.Toast.LENGTH_LONG).show();
            finish(); return;
        }
        boolean messageUri = in != null && SendTo.isMessageUri(in.getData());
        String to = messageUri ? SendTo.numberFrom(in.getData()) : "";
        String body = messageUri ? SendTo.bodyFrom(in.getData()) : "";
        if (body.isEmpty() && in != null) {
            // A share sheet sends the text as an extra rather than in the URI.
            CharSequence extra = in.getCharSequenceExtra(Intent.EXTRA_TEXT);
            if (extra != null) body = extra.toString();
        }
        if (to.isEmpty() && in != null && Intent.ACTION_SEND.equals(in.getAction())) {
            pickConversation(in, body);   // a share: choose who it goes to, from your conversations
            return;
        }
        if (to.isEmpty()) askNumber(in, body);
        else openThread(in, to, body);
    }

    /* A SHARE ASKS "TO WHOM?" WITH YOUR CONVERSATIONS, NOT A BLANK NUMBER BOX. Sharing a photo into Texts used to
     * open a dialog that wanted a phone number typed in -- "should be able to share stuff from other apps with
     * posterchan texts" (2026-10-15). The recent conversations are read OFF the main thread (the conversation list
     * is a cross-process provider query, with contact names), then offered with "New number…" first; picking one
     * opens it exactly as the conversation list does (thread ids + address). Nothing readable -> the number box. */
    private void pickConversation(final Intent in, final String body) {
        final android.content.Context app = getApplicationContext();
        new Thread(() -> {
            java.util.List<SmsStore.Thread> threads;
            try { threads = Messages.threads(app, 30, true); } catch (Throwable t) { threads = null; }
            final java.util.List<SmsStore.Thread> got = threads;
            runOnUiThread(() -> {
                if (isFinishing()) return;
                if (got == null || got.isEmpty()) { askNumber(in, body); return; }
                CharSequence[] rows = new CharSequence[got.size() + 1];
                rows[0] = getString(place.poster.app.R.string.sms_new_number);
                for (int i = 0; i < got.size(); i++) rows[i + 1] = SendToPick.label(got.get(i).label, got.get(i).address);
                new android.app.AlertDialog.Builder(this)
                        .setTitle(place.poster.app.R.string.sms_share_to)
                        .setItems(rows, (d, which) -> {
                            if (which == 0) { askNumber(in, body); return; }
                            openExisting(in, got.get(which - 1), body);
                        })
                        .setOnCancelListener(d -> finish())
                        .show();
            });
        }, "pc-sms-share-pick").start();
    }

    private void askNumber(final Intent in, final String body) {
        {
            final android.widget.EditText number = new android.widget.EditText(this);
            number.setHint(place.poster.app.R.string.sms_number_hint);
            number.setInputType(android.text.InputType.TYPE_CLASS_PHONE);
            final String caption = body;   // (askNumber's own copy; the share path picks a conversation first)
            android.app.AlertDialog dialog = new android.app.AlertDialog.Builder(this)
                    .setTitle(place.poster.app.R.string.sms_to).setView(number)
                    .setPositiveButton(android.R.string.ok, null)
                    .setNegativeButton(android.R.string.cancel, (d, which) -> finish())
                    .setOnCancelListener(d -> finish()).create();
            dialog.setOnShowListener(d -> dialog.getButton(android.app.AlertDialog.BUTTON_POSITIVE)
                    .setOnClickListener(v -> {
                        String recipient = SmsKeys.normalize(number.getText().toString());
                        if (recipient.isEmpty() || "+".equals(recipient)) { number.setError(getString(place.poster.app.R.string.sms_number_hint)); return; }
                        openThread(in, recipient, caption);
                        dialog.dismiss();
                    }));
            dialog.show();
        }
    }

    private void openExisting(Intent in, SmsStore.Thread t, String body) {
        Intent open = new Intent(this, ThreadActivity.class)
                .putExtra(ThreadActivity.EXTRA_THREAD, t.id)
                .putExtra(ThreadActivity.EXTRA_THREADS, t.ids)
                .putExtra(ThreadActivity.EXTRA_ADDRESS, t.address);
        if (!body.isEmpty()) open.putExtra(Intent.EXTRA_TEXT, body);
        SmsShare.forward(in, open);
        try { startActivity(open); } catch (Throwable ignored) { }
        finish();
    }

    private void openThread(Intent in, String to, String body) {
        Intent open = new Intent(this, ThreadActivity.class)
                .putExtra(ThreadActivity.EXTRA_ADDRESS, to)
                .putExtra(ThreadActivity.EXTRA_THREAD, SmsStore.threadIdFor(this, to));
        if (!body.isEmpty()) open.putExtra(Intent.EXTRA_TEXT, body);
        if (in != null && SendTo.isMessageUri(in.getData())) open.setData(in.getData());
        SmsShare.forward(in, open);
        try { startActivity(open); } catch (Throwable ignored) { }
        finish();
    }
}
