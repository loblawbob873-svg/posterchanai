package place.poster.app.sms;

/**
 * WHAT A LONG PRESS ON A CONVERSATION OFFERS -- pure, so a test can run it.
 *
 * It used to go straight to "Delete this conversation", so the phone had no way to archive one: the
 * archive existed only on the web Texts screen ("why is sms archive not on apk"). Archive comes first
 * because it is the one that loses nothing; an archived conversation offers Unarchive in its place.
 */
public final class ThreadMenu {
    public static final int ARCHIVE = 0, UNARCHIVE = 1, DELETE = 2;

    private ThreadMenu() { }

    public static int[] actions(boolean archived) {
        return new int[]{archived ? UNARCHIVE : ARCHIVE, DELETE};
    }
}
