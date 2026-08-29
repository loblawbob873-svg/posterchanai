package place.poster.app.preview;

import android.content.ClipData;
import android.content.Intent;
import android.net.Uri;
import android.util.Base64;

import androidx.core.content.FileProvider;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

import java.io.File;
import java.io.FileOutputStream;

/** Opens an in-memory Preview file in an installed Android viewer without persistent storage. */
@CapacitorPlugin(name = "OpenFile")
public final class OpenFilePlugin extends Plugin {
  private static final int MAX = 32 * 1024 * 1024;
  /* Reject before Base64.decode allocates another full byte array. The Capacitor bridge already
   * materialises the encoded String, but an oversized document must not double its memory cost and
   * OOM the app merely to discover that it exceeds MAX. Callers produce canonical Base64 without
   * whitespace, so ceil(MAX / 3) groups of four is the exact largest accepted encoding. */
  private static final int MAX_ENCODED = ((MAX + 2) / 3) * 4;

  @PluginMethod public void open(PluginCall call) {
    final String encoded=call.getString("data", ""),mime=call.getString("mime", "application/pdf"),
        name=safeName(call.getString("name", "document.pdf"));
    getActivity().runOnUiThread(() -> {
      File out=null;
      try {
        if(encoded.length()>MAX_ENCODED)throw new IllegalArgumentException("file is empty or too large");
        byte[] bytes=Base64.decode(encoded,Base64.DEFAULT);
        if(bytes.length==0||bytes.length>MAX)throw new IllegalArgumentException("file is empty or too large");
        File dir=new File(getContext().getCacheDir(),"preview-open");
        if(!dir.exists()&&!dir.mkdirs())throw new java.io.IOException("could not make preview cache");
        File[] old=dir.listFiles();if(old!=null)for(File f:old)if(f!=null)f.delete();
        out=new File(dir,name);try(FileOutputStream stream=new FileOutputStream(out,false)){stream.write(bytes);}
        Uri uri=FileProvider.getUriForFile(getContext(),getContext().getPackageName()+".fileprovider",out);
        Intent view=new Intent(Intent.ACTION_VIEW).setDataAndType(uri,mime);
        /* Intent.setClipData is void on the Android SDK level this APK compiles against. Keep the
         * grant on the same Intent, but do not chain through a method which has no return value. */
        view.setClipData(ClipData.newRawUri(name,uri));
        view.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
        if(view.resolveActivity(getContext().getPackageManager())==null)throw new Exception("no app can open this file");
        getActivity().startActivity(Intent.createChooser(view,"Open "+name));
        JSObject result=new JSObject();result.put("ok",true);call.resolve(result);
      } catch(Exception e) {
        if(out!=null)out.delete();call.reject(e.getMessage()==null?"could not open file":e.getMessage(),e);
      }
    });
  }

  static String safeName(String value) {
    String s=(value==null?"":value).replaceAll("[^A-Za-z0-9._ -]","_").trim();
    if(s.isEmpty()||".".equals(s)||"..".equals(s))return "document.pdf";
    return s.length()>120?s.substring(s.length()-120):s;
  }
}
