package androidx.core.app;

/**
 * The reply box inside a notification.
 *
 * Stubbed to the two members the SMS notifier uses. The rule this class exists to keep honest is in
 * SmsNotifier.replyAction: a RemoteInput's PendingIntent must be MUTABLE, because RemoteInput
 * delivers what the person typed by writing it into that intent — every other PendingIntent in this
 * app is correctly immutable, and copying that here sends an empty reply, silently.
 */
public final class RemoteInput {
  public static final class Builder {
    public Builder(String resultKey) { }
    public Builder setLabel(CharSequence label) { return this; }
    public RemoteInput build() { return null; }
  }

  public static android.os.Bundle getResultsFromIntent(android.content.Intent intent) { return null; }
}
