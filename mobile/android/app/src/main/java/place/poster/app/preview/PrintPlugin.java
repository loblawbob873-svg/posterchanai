package place.poster.app.preview;

import android.content.Context;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Canvas;
import android.graphics.Paint;
import android.graphics.Rect;
import android.graphics.RectF;
import android.graphics.pdf.PdfDocument;
import android.os.Bundle;
import android.os.CancellationSignal;
import android.os.ParcelFileDescriptor;
import android.print.PageRange;
import android.print.PrintAttributes;
import android.print.PrintDocumentAdapter;
import android.print.PrintDocumentInfo;
import android.print.PrintManager;
import android.print.pdf.PrintedPdfDocument;
import android.util.Base64;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

import java.io.FileOutputStream;
import java.io.IOException;

/**
 * PRINT A PREVIEWED FILE. The WebView ignores window.print() -- Chromium's print path lives in the
 * browser, and a WebView prints only through PrintManager -- so the Preview's Print button needs this.
 * Platform APIs only (android.print), no androidx.print dependency.
 *
 * A PDF is handed to the spooler AS ITS OWN BYTES: re-rendering it would lose vector text and
 * resolution. An image is drawn onto one page, scaled to fit the printable area and centred, never
 * cropped and never enlarged past the page.
 */
@CapacitorPlugin(name = "Print")
public final class PrintPlugin extends Plugin {
  /* Same ceiling and the same reason as OpenFilePlugin: the bytes cross the bridge as Base64. */
  static final int MAX = 32 * 1024 * 1024;
  private static final int MAX_ENCODED = ((MAX + 2) / 3) * 4;
  /* Decode an image no larger than this on its long side. A 48 MP phone photo decoded at full size
   * is ~190 MB of ARGB; a printed page at 300 dpi needs about 3300 px. */
  static final int MAX_SIDE = 4096;

  @PluginMethod public void print(PluginCall call) {
    final String encoded = call.getString("data", "");
    final String mime = call.getString("mime", "");
    final String name = OpenFilePlugin.safeName(call.getString("name", "document"));
    final byte[] bytes;
    try {
      if (encoded.length() > MAX_ENCODED) throw new IllegalArgumentException("file is too large to print from here");
      bytes = Base64.decode(encoded, Base64.DEFAULT);
      if (bytes.length == 0 || bytes.length > MAX) throw new IllegalArgumentException("file is empty or too large");
    } catch (Exception e) {
      call.reject(e.getMessage() == null ? "could not print" : e.getMessage(), e);
      return;
    }
    final boolean pdf = isPdf(mime, name, bytes);
    final Bitmap image;
    if (pdf) image = null;
    else {
      image = decode(bytes);
      if (image == null) { call.reject("that file is not a picture this phone can print"); return; }
    }
    getActivity().runOnUiThread(() -> {
      try {
        PrintManager pm = (PrintManager) getActivity().getSystemService(Context.PRINT_SERVICE);
        if (pm == null) throw new IllegalStateException("this phone has no print service");
        PrintDocumentAdapter adapter = pdf ? new PdfBytes(name, bytes) : new OnePicture(getContext(), name, image);
        PrintAttributes.Builder attrs = new PrintAttributes.Builder();
        if (!pdf && image.getWidth() > image.getHeight())
          attrs.setMediaSize(PrintAttributes.MediaSize.UNKNOWN_LANDSCAPE);
        pm.print(name, adapter, attrs.build());
        JSObject ok = new JSObject(); ok.put("ok", true); call.resolve(ok);
      } catch (Exception e) {
        call.reject(e.getMessage() == null ? "could not print" : e.getMessage(), e);
      }
    });
  }

  /** By content first: a blob from the drive often carries application/octet-stream and a hash name. */
  static boolean isPdf(String mime, String name, byte[] b) {
    if (b != null && b.length >= 5 && b[0] == '%' && b[1] == 'P' && b[2] == 'D' && b[3] == 'F' && b[4] == '-') return true;
    String m = mime == null ? "" : mime.toLowerCase();
    if (m.startsWith("image/")) return false;
    return m.equals("application/pdf") || (name != null && name.toLowerCase().endsWith(".pdf"));
  }

  /** Power-of-two subsample so the long side lands at or under {@code max}. */
  static int sampleSize(int w, int h, int max) {
    int s = 1;
    while (Math.max(w, h) / s > max) s *= 2;
    return s;
  }

  /** Largest rect of the image's aspect ratio inside the content box, centred. Never upscales past
   * the box; a tiny image is centred at the box's scale rather than blown up into mush beyond it. */
  static float[] fit(float iw, float ih, float left, float top, float cw, float ch) {
    if (iw <= 0 || ih <= 0 || cw <= 0 || ch <= 0) return new float[] { left, top, left, top };
    float scale = Math.min(cw / iw, ch / ih);
    float w = iw * scale, h = ih * scale;
    float x = left + (cw - w) / 2f, y = top + (ch - h) / 2f;
    return new float[] { x, y, x + w, y + h };
  }

  private static Bitmap decode(byte[] bytes) {
    BitmapFactory.Options probe = new BitmapFactory.Options();
    probe.inJustDecodeBounds = true;
    BitmapFactory.decodeByteArray(bytes, 0, bytes.length, probe);
    if (probe.outWidth <= 0 || probe.outHeight <= 0) return null;
    BitmapFactory.Options opts = new BitmapFactory.Options();
    opts.inSampleSize = sampleSize(probe.outWidth, probe.outHeight, MAX_SIDE);
    return BitmapFactory.decodeByteArray(bytes, 0, bytes.length, opts);
  }

  /** The PDF's own bytes, untouched. The page count is the spooler's to discover. */
  static final class PdfBytes extends PrintDocumentAdapter {
    private final String name; private final byte[] bytes;
    PdfBytes(String name, byte[] bytes) { this.name = name; this.bytes = bytes; }

    @Override public void onLayout(PrintAttributes oldAttrs, PrintAttributes newAttrs, CancellationSignal cancel,
                                   LayoutResultCallback cb, Bundle extras) {
      if (cancel.isCanceled()) { cb.onLayoutCancelled(); return; }
      cb.onLayoutFinished(new PrintDocumentInfo.Builder(name)
          .setContentType(PrintDocumentInfo.CONTENT_TYPE_DOCUMENT)
          .setPageCount(PrintDocumentInfo.PAGE_COUNT_UNKNOWN).build(), false);
    }

    @Override public void onWrite(PageRange[] pages, ParcelFileDescriptor dest, CancellationSignal cancel,
                                  WriteResultCallback cb) {
      try (FileOutputStream out = new FileOutputStream(dest.getFileDescriptor())) {
        if (cancel.isCanceled()) { cb.onWriteCancelled(); return; }
        out.write(bytes);
        cb.onWriteFinished(new PageRange[] { PageRange.ALL_PAGES });
      } catch (IOException e) {
        cb.onWriteFailed(e.getMessage());
      }
    }
  }

  /** One picture on one page, laid out for whatever paper the user picks in the dialog. */
  static final class OnePicture extends PrintDocumentAdapter {
    private final Context ctx; private final String name; private final Bitmap bitmap;
    private PrintAttributes attrs;
    OnePicture(Context ctx, String name, Bitmap bitmap) { this.ctx = ctx; this.name = name; this.bitmap = bitmap; }

    @Override public void onLayout(PrintAttributes oldAttrs, PrintAttributes newAttrs, CancellationSignal cancel,
                                   LayoutResultCallback cb, Bundle extras) {
      if (cancel.isCanceled()) { cb.onLayoutCancelled(); return; }
      attrs = newAttrs;
      cb.onLayoutFinished(new PrintDocumentInfo.Builder(name)
          .setContentType(PrintDocumentInfo.CONTENT_TYPE_PHOTO).setPageCount(1).build(),
          oldAttrs == null || !newAttrs.equals(oldAttrs));
    }

    @Override public void onWrite(PageRange[] pages, ParcelFileDescriptor dest, CancellationSignal cancel,
                                  WriteResultCallback cb) {
      PrintedPdfDocument doc = new PrintedPdfDocument(ctx, attrs);
      try {
        PdfDocument.Page page = doc.startPage(0);
        /* PdfDocument.close() throws while a page is open, so a cancel still finishes the page. */
        if (cancel.isCanceled()) { doc.finishPage(page); cb.onWriteCancelled(); return; }
        Rect box = page.getInfo().getContentRect();
        float[] r = fit(bitmap.getWidth(), bitmap.getHeight(), box.left, box.top, box.width(), box.height());
        Canvas c = page.getCanvas();
        c.drawBitmap(bitmap, null, new RectF(r[0], r[1], r[2], r[3]), new Paint(Paint.FILTER_BITMAP_FLAG));
        doc.finishPage(page);
        try (FileOutputStream out = new FileOutputStream(dest.getFileDescriptor())) { doc.writeTo(out); }
        cb.onWriteFinished(new PageRange[] { PageRange.ALL_PAGES });
      } catch (IOException e) {
        cb.onWriteFailed(e.getMessage());
      } finally {
        doc.close();
      }
    }

    @Override public void onFinish() { /* the bitmap is the plugin call's; GC takes it with the adapter */ }
  }
}
