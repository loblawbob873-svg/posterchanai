package androidx.work;

public class Constraints {
  public static class Builder {
    public Builder setRequiresCharging(boolean b) { return this; }
    public Builder setRequiredNetworkType(NetworkType t) { return this; }
    public Builder setRequiresBatteryNotLow(boolean b) { return this; }
    public Constraints build() { return null; }
  }
}
