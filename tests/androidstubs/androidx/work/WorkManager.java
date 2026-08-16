package androidx.work;

import android.content.Context;

public abstract class WorkManager {
  public static WorkManager getInstance(Context ctx) { return null; }
  public abstract void cancelUniqueWork(String name);
  public abstract void enqueueUniquePeriodicWork(String name, ExistingPeriodicWorkPolicy policy,
                                                 PeriodicWorkRequest request);
  public abstract void enqueueUniqueWork(String name, ExistingWorkPolicy policy,
                                         OneTimeWorkRequest request);
}
