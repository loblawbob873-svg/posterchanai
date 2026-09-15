package androidx.test.espresso;
public final class ViewInteraction {
  public ViewInteraction inRoot(org.hamcrest.Matcher<Root> root) { return this; }
  public ViewInteraction perform(ViewAction... actions) { return this; }
  public ViewInteraction check(ViewAssertion assertion) { return this; }
}
