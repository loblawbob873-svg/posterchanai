package androidx.test.core.app;

/** Stub — the only entry point used by instrumented tests on this host. */
public class ApplicationProvider {
  @SuppressWarnings("unchecked")
  public static <T extends android.content.Context> T getApplicationContext() { return null; }
}
