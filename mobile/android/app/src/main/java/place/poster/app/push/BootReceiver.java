package place.poster.app.push;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.os.Build;

import place.poster.app.calendar.CalendarWidget;

/**
 * Start at boot — the way apps actually do it.
 *
 * WHAT THIS DOES NOT DO IS LAUNCH THE APP. An app that puts itself on screen after a reboot is an app
 * people uninstall, and Android has refused to allow it from the background for years anyway. What
 * "start at boot" means for a messaging app is that the part which was RUNNING before the reboot is
 * running after it, and here that is exactly one thing: the "stay connected" foreground service, if
 * the user turned it on. It is off by default, so on a normal install this receiver does nothing at
 * all and says so by doing nothing.
 *
 * The preference is read from disk rather than from any live state, because at this point there is no
 * live state — that is the entire problem being solved. `StayAwakeService.wanted()` is the same flag
 * the switch in Settings writes.
 *
 * BOOT_COMPLETED is one of the explicit exemptions to Android 12's ban on starting a foreground
 * service from the background, so this is allowed where the same call from anywhere else would throw.
 *
 * The folder-sync WorkManager job needs nothing here — it persists its own queue across a reboot and
 * re-enqueues. Its ALARM does not: an alarm is erased by a reboot, and the folder-sync clock is an
 * alarm precisely because a WorkManager job cannot fire in Doze. So it is re-armed here, from the
 * stored folder list rather than from anything a page has told us this session, because at this
 * point no page has run.
 */
public class BootReceiver extends BroadcastReceiver {

  @Override
  public void onReceive(Context ctx, Intent intent) {
    String a = intent != null ? intent.getAction() : null;
    if (!Intent.ACTION_BOOT_COMPLETED.equals(a)
        && !Intent.ACTION_MY_PACKAGE_REPLACED.equals(a)
        && !"android.intent.action.QUICKBOOT_POWERON".equals(a)) {
      return;
    }
    // The home-screen calendar is drawn from a cache that survives the reboot, but the LAUNCHER
    // rebuilds its widgets from scratch — so redraw, or the first thing seen after a restart is a
    // widget that has not decided what day it is.
    try {
      CalendarWidget.refresh(ctx);
    } catch (Throwable ignored) {
    }

    /* THE FOLDER-SYNC CLOCK, before every early return below and for the same reason the signer is:
     * it is its own feature with its own switch, and a reboot must not be able to turn it off. A
     * reboot erases alarms, so without this line background sync stops at the next restart and stays
     * stopped until somebody opens the app — which is a bug that only shows up days later. */
    try {
      place.poster.app.sync.SyncClock.followStore(ctx);
    } catch (Throwable ignored) {
    }

    /* The signer, BEFORE the stay-connected early return below and not folded into it. They are two
     * independent switches: someone can sign for their desktop without opting into a permanent relay
     * connection for notifications, and that is the common case. Ordered after the return — as this
     * was, the first time — a phone with the signer on and "stay connected" off came back from a
     * reboot answering nothing, which is indistinguishable from the signer being broken and is the
     * exact class of bug the whole service was written to remove. */
    if (place.poster.app.signer.SignerRelayService.wanted(ctx)) {
      try {
        place.poster.app.signer.SignerRelayService.kick(
            ctx, place.poster.app.signer.SignerRelayService.ACTION_START);
      } catch (Throwable ignored) {
      }
    }

    // PosterChan Direct owns its credentials and lifecycle. It must not depend on the unrelated
    // legacy "stay connected" media/WebView preference below; most users never enable that switch.
    if (DirectPushService.configured(ctx)) {
      try {
        DirectPushService.kick(ctx);
      } catch (Throwable ignored) {
        // The sealed configuration remains. Opening the app retries from a foreground context.
      }
    }

    if (!StayAwakeService.wanted(ctx)) return;
    try {
      Intent i = new Intent(ctx, StayAwakeService.class).setAction(StayAwakeService.ACTION_START);
      if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) ctx.startForegroundService(i);
      else ctx.startService(i);
    } catch (Throwable ignored) {
      // A refused start must not crash the boot broadcast. The switch is still on, so the next time
      // the app is opened it starts the service from the foreground, where it is always allowed.
    }
  }
}
