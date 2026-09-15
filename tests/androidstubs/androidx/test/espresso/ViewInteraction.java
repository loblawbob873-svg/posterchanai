package androidx.test.espresso;
public final class ViewInteraction {
  public ViewInteraction perform(ViewAction... actions) { return this; }
  public ViewInteraction check(ViewAssertion assertion) { return this; }
}
