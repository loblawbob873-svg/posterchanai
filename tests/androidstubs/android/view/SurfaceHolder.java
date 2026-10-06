package android.view;

/** Signature-only. */
public interface SurfaceHolder {
  android.graphics.Canvas lockCanvas();
  android.graphics.Canvas lockHardwareCanvas();
  void unlockCanvasAndPost(android.graphics.Canvas canvas);
}
