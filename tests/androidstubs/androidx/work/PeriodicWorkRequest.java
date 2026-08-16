package androidx.work;

import java.util.concurrent.TimeUnit;

public class PeriodicWorkRequest {
  public static class Builder {
    public Builder(Class<? extends ListenableWorker> cls, long interval, TimeUnit unit) { }
    public Builder setConstraints(Constraints c) { return this; }
    public PeriodicWorkRequest build() { return null; }
  }
}
