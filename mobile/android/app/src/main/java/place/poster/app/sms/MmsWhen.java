package place.poster.app.sms;

/**
 * WHAT UNIT IS `mms.date` IN — asked ONCE, and answered the same way by the code that READS a row
 * and the code that SELECTS one.
 *
 * The specification says seconds, and `content://sms` says milliseconds, which is the whole reason
 * this file exists. But several OEM providers store MILLISECONDS in the MMS table too, and
 * MmsStore's reader has known that for a while:
 *
 *     m.date = raw > 100000000000L ? raw : raw * 1000L;
 *
 * while `MmsStore.since` divided its millisecond argument by a thousand UNCONDITIONALLY. On a
 * milliseconds provider those two disagree, and the disagreement is silent and total: the WHERE
 * clause compares a ~1.8e12 column against a ~1.8e9 value, so it matches EVERY row in the table,
 * and `date ASC LIMIT n` then hands back the OLDEST n picture messages on every sweep for ever.
 * They archive once and are cheap skips from then on, the high-water mark lands on the newest of
 * those n and the next sweep asks the identical question — so the archive pins itself to the
 * oldest corner of the store and no picture message behind it ever reaches encrypted storage.
 * Nothing is logged, because from the sweep's own point of view it read the provider and published
 * everything the provider gave it.
 *
 * So the rule lives in one place, in a class with no Android in it — which is also what lets
 * tests/test_android_mms.py COMPILE AND RUN it rather than matching the source with a regex.
 * MmsStore itself cannot be loaded on a plain JVM (its static initialiser calls Uri.parse).
 */
public final class MmsWhen {

    /**
     * The line between the two units, and it is not a close call: seconds now are ~1.8e9 and
     * milliseconds ~1.8e12. As seconds this constant is the year 5138; as milliseconds it is 1973.
     * Any real message lands unambiguously on one side.
     */
    static final long MS_FLOOR = 100000000000L;

    private MmsWhen() { }

    /** A stored `date` as milliseconds, whichever unit this provider chose to keep it in. */
    static long millis(long raw) {
        return raw > MS_FLOOR ? raw : raw * 1000L;
    }

    /**
     * A WHERE fragment matching rows STRICTLY AFTER a millisecond timestamp, in either unit — the
     * selection half of `millis` above, and deliberately written as its mirror image.
     *
     * Both branches are tested against the row's OWN magnitude rather than against a guess about
     * the provider, so a table that holds both (a restore that merged two phones, an OEM that
     * changed its mind across an upgrade) is still read correctly row by row. It is comparison and
     * boolean algebra only, with no function calls, so a provider that hands the selection to
     * SQLite parses it and one that pattern-matches its own small grammar has the best chance of
     * doing the same.
     */
    static String after(String col) {
        return "((" + col + ">" + MS_FLOOR + " AND " + col + ">?)"
             + " OR (" + col + "<=" + MS_FLOOR + " AND " + col + ">?))";
    }

    /** The two bound arguments `after` expects, in order: milliseconds, then seconds. */
    static String[] afterArgs(long dateMs) {
        long ms = Math.max(0L, dateMs);
        return new String[]{ String.valueOf(ms), String.valueOf(ms / 1000L) };
    }

    /** Mirror of {@link #after}: rows strictly BEFORE a millisecond timestamp in either unit. */
    static String before(String col) {
        return "((" + col + ">" + MS_FLOOR + " AND " + col + "<?)"
             + " OR (" + col + "<=" + MS_FLOOR + " AND " + col + "<?))";
    }

    static String[] beforeArgs(long dateMs) {
        long ms = Math.max(0L, dateMs);
        return new String[]{ String.valueOf(ms), String.valueOf(ms / 1000L) };
    }
}
