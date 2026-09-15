package place.poster.app.sms;

import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.net.Uri;
import android.os.Build;
import android.util.AttributeSet;
import android.view.inputmethod.EditorInfo;
import android.view.inputmethod.InputConnection;
import android.view.inputmethod.InputConnectionWrapper;
import android.view.inputmethod.InputContentInfo;
import android.widget.EditText;

/** Native clipboard and image-keyboard entry points; attachment storage remains in ThreadActivity. */
public final class SmsComposeInput extends EditText {
    public interface Receiver {
        void image(Uri uri, Runnable release);
        void error(String message);
    }
    private Receiver receiver;
    public SmsComposeInput(Context context, AttributeSet attrs) { super(context, attrs); }
    public void setImageReceiver(Receiver value) { receiver = value; }

    @Override public boolean onTextContextMenuItem(int id) {
        if (id == android.R.id.paste && receiver != null) {
            ClipboardManager clipboard = (ClipboardManager) getContext().getSystemService(Context.CLIPBOARD_SERVICE);
            ClipData clip;
            try { clip = clipboard == null ? null : clipboard.getPrimaryClip(); }
            catch (RuntimeException denied) {
                receiver.error("Could not read the clipboard. Try copying the image again."); return true;
            }
            if (clip != null && clip.getDescription().hasMimeType("image/*")) {
                if (clip.getItemCount() != 1 || clip.getItemAt(0).getUri() == null) {
                    receiver.error("Paste one image at a time."); return true;
                }
                receive(clip.getItemAt(0).getUri(), () -> {});
                return true;
            }
        }
        return super.onTextContextMenuItem(id);
    }

    private void receive(Uri uri, Runnable permissionRelease) {
        // Ownership passes to the staging worker or replacement dialog. Cleanup must be safe
        // across cancellation/error paths, and a dead keyboard binder cannot strand the draft.
        final java.util.concurrent.atomic.AtomicBoolean released = new java.util.concurrent.atomic.AtomicBoolean();
        Runnable release = () -> {
            if (released.compareAndSet(false, true)) {
                try { permissionRelease.run(); } catch (RuntimeException ignored) { }
            }
        };
        if (receiver == null || uri == null || !"content".equals(uri.getScheme())) {
            release.run();
            if (receiver != null) receiver.error("Could not read the pasted image. Try copying it again.");
            return;
        }
        try { receiver.image(uri, release); }
        catch (RuntimeException denied) {
            release.run(); receiver.error("Could not read the pasted image. Try copying it again.");
        }
    }

    @Override public InputConnection onCreateInputConnection(EditorInfo info) {
        InputConnection connection = super.onCreateInputConnection(info);
        if (connection == null || Build.VERSION.SDK_INT < 25) return connection;
        return Api25.wrap(this, connection, info);
    }

    private static final class Api25 {
        static InputConnection wrap(SmsComposeInput view, InputConnection connection, EditorInfo info) {
            info.contentMimeTypes = new String[]{"image/*"};
            return new InputConnectionWrapper(connection, false) {
                @Override public boolean commitContent(InputContentInfo content, int flags, android.os.Bundle opts) {
                    if (content == null || !content.getDescription().hasMimeType("image/*") || view.receiver == null)
                        return super.commitContent(content, flags, opts);
                    final boolean grant = (flags & InputConnection.INPUT_CONTENT_GRANT_READ_URI_PERMISSION) != 0;
                    try { if (grant) content.requestPermission(); }
                    catch (RuntimeException denied) { view.receiver.error("Could not read the keyboard image."); return false; }
                    view.receive(content.getContentUri(), () -> {
                        if (grant) content.releasePermission();
                    });
                    return true;
                }
            };
        }
    }
}
