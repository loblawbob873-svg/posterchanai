package place.poster.app.sms;

import android.text.method.LinkMovementMethod;
import android.text.util.Linkify;
import android.widget.TextView;

/** Keep text selectable while making HTTP download links actionable, including key fragments. */
final class SmsLinks {
    private SmsLinks() { }

    static void bind(TextView view, CharSequence body) {
        // Replacing the text also discards spans from a recycled message row.
        view.setText(body);
        Linkify.addLinks(view, Linkify.WEB_URLS);
        view.setMovementMethod(LinkMovementMethod.getInstance());
        view.setLinksClickable(true);
    }
}
