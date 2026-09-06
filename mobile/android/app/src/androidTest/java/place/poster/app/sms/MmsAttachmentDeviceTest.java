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

    @Test
    public void largeVideoContentUriStagesWithoutTheOldTwelveMegabyteLimit() throws Exception {
        Context context = InstrumentationRegistry.getInstrumentation().getTargetContext();
        File file = new File(context.getCacheDir(), "mms-large-video.mp4");
        String who = "+15550200";
        try {
            try (FileOutputStream out = new FileOutputStream(file)) {
                byte[] block = new byte[65536];
                for (int i = 0; i < 400; i++) out.write(block);
            }
            Uri uri = FileProvider.getUriForFile(context, context.getPackageName() + ".fileprovider", file);
            try (InputStream in = context.getContentResolver().openInputStream(uri)) {
                MmsDraft.save(context, who, in, "video/mp4", "large.mp4");
            }
            file.delete();
            MmsDraft.Value restored = MmsDraft.load(context, who);
            assertNotNull(restored);
            assertEquals(25L * 1024 * 1024, restored.file.length());
            org.junit.Assert.assertTrue(MmsLink.required(restored.mime, restored.file.length(),
                    300 * 1024, MmsAttachment.MAX_STAGED_BYTES));
        } finally { file.delete(); MmsDraft.remove(context, who); }
    }

    @Test
    public void interruptedStreamingCopyPreservesThePreviousDraft() throws Exception {
        Context context = InstrumentationRegistry.getInstrumentation().getTargetContext();
        String who = "+15550201";
        byte[] original = new byte[]{1,2,3};
        try {
            MmsDraft.save(context, who, original, "image/jpeg", "original.jpg");
            try {
                MmsDraft.save(context, who, new InputStream() {
                    public int read() throws java.io.IOException { throw new java.io.IOException("provider lost"); }
                }, "video/mp4", "new.mp4");
                org.junit.Assert.fail("copy should fail");
            } catch (java.io.IOException expected) { }
            MmsDraft.Value restored = MmsDraft.load(context, who);
            assertEquals("original.jpg", restored.name);
            try (InputStream in = new java.io.FileInputStream(restored.file)) {
                assertArrayEquals(original, MmsAttachment.read(in, 100));
            }
        } finally { MmsDraft.remove(context, who); }
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
