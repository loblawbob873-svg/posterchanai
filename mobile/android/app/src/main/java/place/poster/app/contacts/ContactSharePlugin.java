package place.poster.app.contacts;

import android.content.Intent;
import android.net.Uri;

import androidx.core.content.FileProvider;

import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

import java.io.File;
import java.io.FileOutputStream;
import java.nio.charset.StandardCharsets;

/** Android's share sheet for one vCard. Kept separate from ContactSyncPlugin so sharing UI can
 * never become part of the phone-book reconcile or its deletion-safety test surface. */
@CapacitorPlugin(name = "ContactShare")
public class ContactSharePlugin extends Plugin {
  @PluginMethod
  public void share(PluginCall call) {
    final String vcf = call.getString("vcf", "");
    final String label = call.getString("name", "Contact");
    if (vcf.trim().isEmpty()) { call.reject("contact card is empty"); return; }
    getActivity().runOnUiThread(() -> {
      try {
        File dir = new File(getContext().getCacheDir(), "shared-contacts");
        if (!dir.exists() && !dir.mkdirs()) throw new java.io.IOException("could not make share cache");
        String safe = label.replaceAll("[^A-Za-z0-9._ -]", "_").trim();
        if (safe.isEmpty()) safe = "Contact";
        File out = new File(dir, safe + ".vcf");
        try (FileOutputStream stream = new FileOutputStream(out, false)) {
          stream.write(vcf.getBytes(StandardCharsets.UTF_8));
        }
        Uri uri = FileProvider.getUriForFile(getContext(),
                getContext().getPackageName() + ".fileprovider", out);
        Intent send = new Intent(Intent.ACTION_SEND)
                .setType("text/vcard")
                .putExtra(Intent.EXTRA_STREAM, uri)
                .putExtra(Intent.EXTRA_SUBJECT, label)
                .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
        getActivity().startActivity(Intent.createChooser(send, "Share contact"));
        call.resolve();
      } catch (Exception e) {
        call.reject("could not share contact: " + e.getMessage(), e);
      }
    });
  }
}
