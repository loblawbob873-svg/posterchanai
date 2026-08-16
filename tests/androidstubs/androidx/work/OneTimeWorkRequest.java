package androidx.work;

public class OneTimeWorkRequest {
  public static class Builder {
    public Builder(Class<? extends ListenableWorker> cls) { }
    public Builder setConstraints(Constraints c) { return this; }
    public Builder setExpedited(OutOfQuotaPolicy policy) { return this; }
    public OneTimeWorkRequest build() { return null; }
  }
}
