package place.poster.app.phone;

import java.util.ArrayList;
import java.util.List;

/**
 * Which actions a Recents / Contacts row's menu offers, and in what order -- pure, so a test runs it.
 *
 * "Recents -> clicking on number should also give you ability to add as contact in the menu": a number
 * that is NOT a contact gets Add to contacts (PosterChan's own editor, prefilled -- the route Texts'
 * Add contact button takes), a known one keeps Open contact. Never both: offering "add" for somebody
 * already saved makes a duplicate card.
 */
public final class RowMenu {
    public static final int CALL = 0, TEXT = 1, COPY = 2, VIEW_CONTACT = 3, DELETE = 4, ADD_CONTACT = 5,
            BLOCK = 6, UNBLOCK = 7;

    private RowMenu() { }

    public static List<Integer> actions(boolean isContact, boolean isCallLogEntry, String number) {
        return actions(isContact, isCallLogEntry, number, null);
    }

    /**
     * `blocked` is what the phone's blocked-numbers list says about this number (Blocklist.isBlocked):
     * TRUE offers Unblock, FALSE offers Block, and NULL -- the list could not be asked -- offers
     * neither, because a Block that cannot work is a button that lies.
     */
    public static List<Integer> actions(boolean isContact, boolean isCallLogEntry, String number, Boolean blocked) {
        List<Integer> out = new ArrayList<Integer>();
        boolean hasNumber = number != null && !number.trim().isEmpty();
        if (hasNumber) { out.add(CALL); out.add(TEXT); out.add(COPY); }
        if (isContact) out.add(VIEW_CONTACT);
        else if (hasNumber) out.add(ADD_CONTACT);
        if (hasNumber && blocked != null) out.add(blocked ? UNBLOCK : BLOCK);
        if (isCallLogEntry) out.add(DELETE);
        return out;
    }
}
