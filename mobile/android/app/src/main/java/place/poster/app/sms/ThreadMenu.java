package place.poster.app.sms;

/**
 * WHAT A LONG PRESS ON A CONVERSATION OFFERS -- pure, so a test can run it.
 *
 * It used to go straight to "Delete this conversation", so the phone had no way to archive one: the
 * archive existed only on the web Texts screen ("why is sms archive not on apk"). Archive comes first
 * because it is the one that loses nothing; an archived conversation offers Unarchive in its place.
 */
public final class ThreadMenu {
    public static final int ARCHIVE = 0, UNARCHIVE = 1, DELETE = 2, BLOCK = 3, UNBLOCK = 4;

    private ThreadMenu() { }

    public static int[] actions(boolean archived) {
        return actions(archived, null);
    }

    /** Block/Unblock sit between Archive and Delete when the phone's blocked-numbers list can be asked
     * about this conversation's sender (`blocked` non-null -- never for a group, see Blocklist). */
    public static int[] actions(boolean archived, Boolean blocked) {
        if (blocked == null) return new int[]{archived ? UNARCHIVE : ARCHIVE, DELETE};
        return new int[]{archived ? UNARCHIVE : ARCHIVE, blocked ? UNBLOCK : BLOCK, DELETE};
    }
}
