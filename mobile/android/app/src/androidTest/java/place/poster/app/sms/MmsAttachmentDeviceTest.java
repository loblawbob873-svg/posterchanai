package place.poster.app.sms;

import static org.junit.Assert.assertArrayEquals;
import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNotNull;

import android.content.Context;
import android.net.Uri;
import androidx.core.content.FileProvider;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;

import org.junit.Test;
import org.junit.runner.RunWith;

import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;

/** Real ContentResolver coverage for the content:// path used by ACTION_OPEN_DOCUMENT results. */
@RunWith(AndroidJUnit4.class)
public class MmsAttachmentDeviceTest {
    @Test
    public void localContentUriStagesAndSurvivesActivityRecreation() throws Exception {
        Context context = InstrumentationRegistry.getInstrumentation().getTargetContext();
        byte[] image = new byte[]{1, 2, 3, 4, 5};
        File file = new File(context.getCacheDir(), "mms-picker-image.jpg");
        try (FileOutputStream out = new FileOutputStream(file)) { out.write(image); }
        Uri uri = FileProvider.getUriForFile(context,
                context.getPackageName() + ".fileprovider", file);
        try (InputStream in = context.getContentResolver().openInputStream(uri)) {
            byte[] loaded = MmsAttachment.read(in, MmsAttachment.MAX_STAGED_BYTES);
            assertArrayEquals(image, loaded);
            MmsDraft.save(context, "+15550199", loaded, "image/jpeg", "picked.jpg");
        }
        // A newly loaded value has no dependency on the picker URI or the old Activity instance.
        file.delete();
        MmsDraft.Value restored = MmsDraft.load(context, "+15550199");
        assertNotNull(restored);
        assertEquals(image.length, restored.file.length());
        MmsDraft.remove(context, "+15550199");
    }

    @Test(expected = java.io.FileNotFoundException.class)
    public void revokedOrRemovedContentIsAnOpenFailureNotAnOversizeFailure() throws Exception {
        Context context = InstrumentationRegistry.getInstrumentation().getTargetContext();
        File file = new File(context.getCacheDir(), "mms-picker-revoked.mp4");
        try (FileOutputStream out = new FileOutputStream(file)) { out.write(7); }
        Uri uri = FileProvider.getUriForFile(context,
                context.getPackageName() + ".fileprovider", file);
        file.delete();
        try (InputStream ignored = context.getContentResolver().openInputStream(uri)) { }
    }
}
