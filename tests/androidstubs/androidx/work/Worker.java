package androidx.work;

import android.content.Context;

public abstract class Worker extends ListenableWorker {
  public Worker(Context ctx, WorkerParameters params) { super(ctx, params); }
  public abstract Result doWork();
}
