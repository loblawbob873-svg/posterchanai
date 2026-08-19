package place.poster.app.sync;

import java.util.List;
import java.util.Map;

/**
 * The two things a sweep touches that are not the sweep: the disk, and the network.
 *
 * THEY ARE INTERFACES SO THE SWEEP CAN BE RUN. Both were constructed inside `sweep()` — a SAF handle
 * and an HTTP client — which is why the native sweep had never been executed anywhere but on a
 * phone: it could be compiled and read, and that was all. It is the half of folder sync that runs
 * while the screen is off, on a document format the other half only agrees with by inspection.
 *
 * Nothing else changes. SafFs and SyncNet implement these exactly as they already were; `run()`
 * builds the real pair, and a test builds a fake one backed by a HashMap and an in-memory relay.
 * tests/test_android_native_sweep.py then drives a whole sweep — upload, publish, and a second
 * device downloading it — through the real reconciler, the real crypto and the real journal.
 */
public final class SyncIo {

    private SyncIo() { }

    /** The folder, as this device can see and change it. */
    public interface Files {
        SafFs.Scan scan(boolean hash, long maxBytes, List<String> excludes);
        byte[] readAll(String rel) throws Exception;
        byte[] readRange(String rel, long off, int len) throws Exception;
        void writePart(String rel, long off, byte[] bytes) throws Exception;
        long partSize(String rel);
        void discardPart(String rel);
        String hashPart(String rel);

        /** The sha256 of the real file, or null when it cannot be read. Never throws: an
         *  unanswered hash must fall through to the work that would have happened anyway. */
        String hashFile(String rel);
        long[] commitPart(String rel, long when) throws Exception;
        String trash(String rel, long when) throws Exception;
        /** Positive proof for a deletion claim: {gone, parentAlive}. See SafFs.confirmGone. */
        boolean[] confirmGone(String rel);
    }

    /** The node, and the encrypted store behind it. */
    public interface Net {
        Map<String, Object> views(String folder) throws Exception;
        Map<String, Object> state(String pair, Long era, Long since) throws Exception;
        Map<String, Object> putState(String pair, long era, java.util.List<Object> put,
                                     boolean confirmed) throws Exception;
        Map<String, Object> manifest(String folder, Map<String, Object> doc, boolean force,
                                     String device) throws Exception;
        byte[] getBlob(String sha) throws Exception;
        String putBlob(byte[] blob) throws Exception;
        boolean blobExists(String sha);
    }
}
