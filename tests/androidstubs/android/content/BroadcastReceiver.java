package android.content;

public abstract class BroadcastReceiver {
  /** Kept alive past onReceive so a receiver can do its work off the main thread. The real one is
   *  what stops the process being cached mid-decision; here it just records that finish() ran. */
  public static class PendingResult {
    public volatile boolean finished = false;
    public void finish() { finished = true; }
  }

  private final PendingResult pending = new PendingResult();

  public PendingResult goAsync() { return pending; }

  /** Test-only reach-in: did the receiver finish the broadcast it kept open. */
  public PendingResult pendingResult() { return pending; }

  public abstract void onReceive(Context ctx, Intent intent);
}
