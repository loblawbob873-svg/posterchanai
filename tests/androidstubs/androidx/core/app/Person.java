package androidx.core.app;
public class Person {
  public static class Builder {
    public Builder setName(CharSequence name) { return this; }
    public Person build() { return new Person(); }
  }
}
