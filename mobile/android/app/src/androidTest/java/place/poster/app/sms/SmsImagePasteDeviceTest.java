package place.poster.app.sms;

import static org.junit.Assert.*;
import static androidx.test.espresso.Espresso.onView;
import static androidx.test.espresso.action.ViewActions.click;
import static androidx.test.espresso.matcher.ViewMatchers.withId;
import static androidx.test.espresso.matcher.RootMatchers.isDialog;

import android.content.ClipData;
import android.content.ClipDescription;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.Intent;
import android.graphics.Bitmap;
import android.net.Uri;
import android.view.inputmethod.EditorInfo;
import android.view.inputmethod.InputConnection;
import android.view.inputmethod.InputContentInfo;
import androidx.core.content.FileProvider;
import androidx.test.core.app.ActivityScenario;
import androidx.test.core.app.ApplicationProvider;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import java.io.File;
import java.io.FileOutputStream;
import org.junit.Test;
import org.junit.runner.RunWith;

/** Real clipboard/editor entry points and durable files; deliberately never presses Send. */
@RunWith(AndroidJUnit4.class)
public class SmsImagePasteDeviceTest {
    private final Context ctx = ApplicationProvider.getApplicationContext();
    private final String who = "+15550981122";

    private File picture(String name) throws Exception {
        File dir = new File(ctx.getCacheDir(), "mms-camera"); dir.mkdirs();
        File file = new File(dir, name);
        Bitmap bitmap = Bitmap.createBitmap(12, 12, Bitmap.Config.ARGB_8888);
        bitmap.eraseColor(0xff2277ee);
        try (FileOutputStream out = new FileOutputStream(file)) {
            assertTrue(bitmap.compress(Bitmap.CompressFormat.PNG, 100, out));
        } finally { bitmap.recycle(); }
        return file;
    }

    private Uri uri(File file) {
        return FileProvider.getUriForFile(ctx, ctx.getPackageName()+".fileprovider", file);
    }

    private Intent open() { return new Intent(ctx, ThreadActivity.class).putExtra(ThreadActivity.EXTRA_ADDRESS, who); }

    private void awaitDraft(String name) throws Exception {
        long deadline = System.currentTimeMillis()+5000;
        while (System.currentTimeMillis()<deadline) {
            MmsDraft.Value draft = MmsDraft.load(ctx, who);
            if (draft != null && name.equals(draft.name)) {
                assertEquals("paste must not send automatically", MmsDraft.READY, draft.state);
                assertTrue(draft.file.length()>0);
                InstrumentationRegistry.getInstrumentation().waitForIdleSync();
                return;
            }
            Thread.sleep(30);
        }
        fail("pasted image did not become an unsent durable draft");
    }

    private void keyboard(ThreadActivity activity, Uri image) {
        SmsComposeInput input = activity.findViewById(place.poster.app.R.id.pc_th_input);
        EditorInfo editor = new EditorInfo();
        InputConnection connection = input.onCreateInputConnection(editor);
        assertNotNull(connection);
        assertEquals(1, editor.contentMimeTypes.length);
        assertEquals("image/*", editor.contentMimeTypes[0]);
        assertTrue(connection.commitContent(new InputContentInfo(image,
                new ClipDescription("keyboard image", new String[]{"image/png"}), null),
                InputConnection.INPUT_CONTENT_GRANT_READ_URI_PERMISSION, null));
    }

    @Test public void clipboardImageKeepsCaptionAndSurvivesSourceDeletionAndRecreation() throws Exception {
        MmsDraft.remove(ctx, who); MmsDraft.setText(ctx, who, "");
        File photo = picture("clipboard-paste.png");
        try (ActivityScenario<ThreadActivity> scenario = ActivityScenario.launch(open())) {
            scenario.onActivity(activity -> {
                SmsComposeInput input = activity.findViewById(place.poster.app.R.id.pc_th_input);
                input.setText("Caption stays here");
                ClipboardManager clipboard = (ClipboardManager) activity.getSystemService(Context.CLIPBOARD_SERVICE);
                clipboard.setPrimaryClip(ClipData.newUri(activity.getContentResolver(), "photo", uri(photo)));
                assertTrue(input.onTextContextMenuItem(android.R.id.paste));
            });
            awaitDraft(photo.getName());
            byte[] durable = java.nio.file.Files.readAllBytes(MmsDraft.load(ctx, who).file.toPath());
            assertArrayEquals(java.nio.file.Files.readAllBytes(photo.toPath()), durable);
            assertTrue(photo.delete());
            scenario.recreate();
            awaitDraft("clipboard-paste.png");
            assertArrayEquals(durable, java.nio.file.Files.readAllBytes(MmsDraft.load(ctx, who).file.toPath()));
            scenario.onActivity(activity -> assertEquals("Caption stays here",
                    ((SmsComposeInput)activity.findViewById(place.poster.app.R.id.pc_th_input)).getText().toString()));
        } finally { MmsDraft.remove(ctx, who); MmsDraft.setText(ctx, who, ""); photo.delete(); }
    }

    @Test public void keyboardReplacementRequiresConsentAndMissingImageKeepsOriginal() throws Exception {
        MmsDraft.remove(ctx, who); MmsDraft.setText(ctx, who, "");
        MmsDraft.save(ctx, who, new byte[]{1,2,3}, "image/png", "original.png");
        File photo = picture("keyboard-paste.png");
        try (ActivityScenario<ThreadActivity> scenario = ActivityScenario.launch(open())) {
            scenario.onActivity(activity -> keyboard(activity, uri(photo)));
            onView(withId(android.R.id.button2)).inRoot(isDialog()).perform(click());
            assertEquals("original.png", MmsDraft.load(ctx, who).name);
            scenario.onActivity(activity -> keyboard(activity, uri(photo)));
            onView(withId(android.R.id.button1)).inRoot(isDialog()).perform(click());
            awaitDraft(photo.getName());
            scenario.onActivity(activity -> keyboard(activity, Uri.parse("content://invalid.poster.test/missing.png")));
            onView(withId(android.R.id.button1)).inRoot(isDialog()).perform(click());
            assertEquals(photo.getName(), MmsDraft.load(ctx, who).name);
            assertEquals(MmsDraft.READY, MmsDraft.load(ctx, who).state);
        } finally { MmsDraft.remove(ctx, who); MmsDraft.setText(ctx, who, ""); photo.delete(); }
    }

    @Test public void oldConfirmationCannotReplaceNewerDraftAtSamePath() throws Exception {
        MmsDraft.remove(ctx, who); MmsDraft.setText(ctx, who, "");
        MmsDraft.save(ctx, who, new byte[]{1}, "image/png", "original.png");
        File photo = picture("stale-paste.png");
        try (ActivityScenario<ThreadActivity> scenario = ActivityScenario.launch(open())) {
            scenario.onActivity(activity -> keyboard(activity, uri(photo)));
            File originalPath = MmsDraft.load(ctx, who).file;
            MmsDraft.save(ctx, who, new byte[]{8,9}, "image/png", "newer.png");
            assertEquals(originalPath, MmsDraft.load(ctx, who).file);
            onView(withId(android.R.id.button1)).inRoot(isDialog()).perform(click());
            assertEquals("newer.png", MmsDraft.load(ctx, who).name);
            assertArrayEquals(new byte[]{8,9}, java.nio.file.Files.readAllBytes(MmsDraft.load(ctx, who).file.toPath()));
            assertEquals(MmsDraft.READY, MmsDraft.load(ctx, who).state);
        } finally { MmsDraft.remove(ctx, who); MmsDraft.setText(ctx, who, ""); photo.delete(); }
    }

    @Test public void destroyingPendingReplacementDoesNotImportOrSend() throws Exception {
        MmsDraft.remove(ctx, who); MmsDraft.setText(ctx, who, "");
        MmsDraft.save(ctx, who, new byte[]{1,2,3}, "image/png", "original.png");
        File photo = picture("cancel-on-destroy.png");
        try {
            try (ActivityScenario<ThreadActivity> scenario = ActivityScenario.launch(open())) {
                scenario.onActivity(activity -> keyboard(activity, uri(photo)));
            }
            assertEquals("original.png", MmsDraft.load(ctx, who).name);
            assertEquals(MmsDraft.READY, MmsDraft.load(ctx, who).state);
        } finally { MmsDraft.remove(ctx, who); MmsDraft.setText(ctx, who, ""); photo.delete(); }
    }
}
