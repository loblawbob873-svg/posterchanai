package place.poster.app.sms;

import android.content.ClipData;
import android.content.Intent;
import android.net.Uri;

/** Single camera/gallery shares remain drafts until the user presses Send. */
final class SmsShare {
    private SmsShare() { }

    static Uri stream(Intent intent) {
        if (intent == null || !Intent.ACTION_SEND.equals(intent.getAction())) return null;
        String type = intent.getType();
        if (type == null || !(type.startsWith("image/") || type.startsWith("video/"))) return null;
        Uri uri;
        try {
            Object value = intent.getParcelableExtra(Intent.EXTRA_STREAM);
            uri = value instanceof Uri ? (Uri) value : null;
        } catch (RuntimeException malformed) { return null; }
        if (uri == null && intent.getClipData() != null && intent.getClipData().getItemCount() == 1)
            uri = intent.getClipData().getItemAt(0).getUri();
        return uri != null && "content".equals(uri.getScheme()) ? uri : null;
    }

    static void forward(Intent from, Intent to) {
        Uri uri = stream(from);
        if (uri == null) return;
        to.setAction(Intent.ACTION_SEND).setType(from.getType());
        to.putExtra(Intent.EXTRA_STREAM, uri);
        // Keep the URI grant alive after the routing activity finishes. Never forward write grants.
        to.setClipData(ClipData.newRawUri("shared attachment", uri));
        to.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
    }

    static void consumed(Intent intent) {
        intent.removeExtra(Intent.EXTRA_STREAM);
        intent.setClipData(null);
    }
}
