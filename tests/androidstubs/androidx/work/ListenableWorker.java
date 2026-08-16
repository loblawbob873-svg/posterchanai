package androidx.work;

import android.content.Context;

public abstract class ListenableWorker {
  public ListenableWorker(Context ctx, WorkerParameters params) { }
  public Context getApplicationContext() { return null; }
  public ForegroundInfo getForegroundInfo() { return null; }

  public abstract static class Result {
    public static Result success() { return null; }
    public static Result failure() { return null; }
    public static Result retry() { return null; }
  }
}
