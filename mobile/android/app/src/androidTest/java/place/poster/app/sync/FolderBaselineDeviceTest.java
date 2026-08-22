package place.poster.app.sync;

import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNotEquals;
import static org.junit.Assert.assertTrue;

import androidx.test.core.app.ApplicationProvider;
import androidx.test.ext.junit.runners.AndroidJUnit4;

import org.junit.Test;
import org.junit.runner.RunWith;

import java.util.LinkedHashMap;
import java.util.Map;

/** Runs against Android's real files and SharedPreferences, not a source-text approximation. */
@RunWith(AndroidJUnit4.class)
public final class FolderBaselineDeviceTest {

    @Test
    public void recreatedSafFolderCannotInheritDeletionAuthority() throws Exception {
        SyncStore store = new SyncStore(ApplicationProvider.getApplicationContext());
        SyncStore.Folder oldFolder = new SyncStore.Folder();
        oldFolder.key = "Documents-emulator-baseline";
        oldFolder.id = "content://com.android.externalstorage.documents/tree/primary%3AOld";
        SyncStore.Folder newFolder = new SyncStore.Folder();
        newFolder.key = oldFolder.key;
        newFolder.id = "content://com.android.externalstorage.documents/tree/primary%3ANew";

        String oldReplica = NativeSweep.localReplicaKey(oldFolder);
        String newReplica = NativeSweep.localReplicaKey(newFolder);
        assertNotEquals(oldReplica, newReplica);

        Map<String, Map<String, Object>> agreement = new LinkedHashMap<>();
        agreement.put("large.jex", new LinkedHashMap<String, Object>());
        store.saveBase(oldReplica, agreement);
        store.markBaselineComplete(oldReplica);

        assertTrue(store.baselineComplete(oldReplica));
        assertFalse("a recreated empty SAF folder inherited an old baseline",
                store.baselineComplete(newReplica));
        assertTrue("a recreated SAF folder inherited an old file journal",
                store.base(newReplica).isEmpty());

        store.dropBase(oldReplica);
        store.dropBase(newReplica);
    }
}
