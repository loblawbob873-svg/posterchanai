package androidx.test.core.app;

/** Stub — the two entry points and the two calls the device tests make on the result. */
public class ActivityScenario<A> implements AutoCloseable {
  public interface ActivityAction<A> { void perform(A activity); }
  public static <T> ActivityScenario<T> launch(Class<T> cls) { return null; }
  public static <T> ActivityScenario<T> launch(android.content.Intent i) { return null; }
  public ActivityScenario<A> onActivity(ActivityAction<A> action) { return this; }
  public ActivityScenario<A> moveToState(androidx.lifecycle.Lifecycle.State s) { return this; }
  @Override public void close() { }
}
