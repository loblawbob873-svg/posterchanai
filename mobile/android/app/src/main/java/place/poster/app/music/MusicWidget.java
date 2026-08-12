package place.poster.app.music;

import android.app.PendingIntent;
import android.appwidget.AppWidgetManager;
import android.appwidget.AppWidgetProvider;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.view.View;
import android.widget.RemoteViews;

import place.poster.app.MainActivity;
import place.poster.app.R;

/**
 * The home-screen widget: what is playing, and prev / play-pause / next.
 *
 * It is a VIEW of MusicService, never a second source of truth. The service pushes every state
 * change here (render), and a button press goes the other way — into the service, out to the
 * WebView, and back as the next render. Nothing about playback is decided in this file.
 *
 * THE APP MAY NOT BE RUNNING. A widget outlives the process it belongs to: Android keeps drawing the
 * last RemoteViews long after the app is gone, and taps on a dead app's widget still start the
 * process to deliver the broadcast. So every button has two paths — INSTANCE != null means the
 * player is live and the press is a transport command, and INSTANCE == null means there is nothing
 * to command and the press must open the app instead (carrying the intent along, so tapping ▶ on a
 * cold widget starts the music rather than merely opening a screen that could).
 */
public class MusicWidget extends AppWidgetProvider {

  /** Which button. The service's own actions are reused so there is one vocabulary, not two. */
  private static final String[] BUTTONS = {
      MusicService.ACTION_PREV, MusicService.ACTION_TOGGLE, MusicService.ACTION_NEXT
  };

  @Override
  public void onUpdate(Context ctx, AppWidgetManager mgr, int[] ids) {
    MusicService svc = MusicService.INSTANCE;
    if (svc != null) svc.renderWidget();
    else render(ctx, null, null, false, false);
  }

  @Override
  public void onReceive(Context ctx, Intent intent) {
    String action = intent == null ? null : intent.getAction();
    boolean ours = false;
    for (String b : BUTTONS) if (b.equals(action)) ours = true;
    if (!ours) { super.onReceive(ctx, intent); return; }

    MusicService svc = MusicService.INSTANCE;
    if (svc != null) {
      // In-process and direct. NOT startService(): from a widget the app is by definition in the
      // background, where Android 8+ refuses a service start — and the service we want is already
      // running, so asking the platform to start it again is both illegal and pointless.
      //
      // Through fromWidget(), NOT emit(): a widget is pressed with the app closed more often than
      // any other surface here, which makes it the one most likely to be pressed at a WebView that
      // Android has taken away — and a bare emit into that reports success and does nothing. It gets
      // the same receipt check the notification and the car's buttons now go through.
      svc.fromWidget(action);
      return;
    }
    /* The widget was showing a live player and the process has since been KILLED (killed, not closed —
     * a close goes through onDestroy and repaints the idle face). Repaint it now, which re-points
     * every button at the launch intent below instead of at this broadcast, and try the launch once.
     *
     * Trying is all it is: from Android 10 a background process may not start an activity, and a
     * broadcast receiver is a background process. That restriction is exactly why the IDLE face wires
     * its buttons to a getActivity() PendingIntent — one sent by the LAUNCHER, which is allowed —
     * rather than routing through here. This path only exists to heal the stale face. */
    render(ctx, null, null, false, false);
    try { ctx.startActivity(launch(ctx, "play")); } catch (Exception ignored) {}
  }

  private static Intent launch(Context ctx, String what) {
    return new Intent(ctx, MainActivity.class)
        .setAction(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER)
        .putExtra(MusicPlugin.EXTRA_LAUNCH_ACTION, what)
        .putExtra(MusicPlugin.EXTRA_LAUNCH_AT, System.currentTimeMillis())
        .setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
  }

  /**
   * Draw every placed widget.
   *
   * `active` false is the idle face — the app's name instead of a stale track title, and a ▶ that
   * opens the player. A widget that keeps showing the last song after playback ended reads as a
   * player that is still running, and its pause button as one that does nothing.
   */
  public static void render(Context ctx, String title, String artist, boolean playing, boolean active) {
    AppWidgetManager mgr = AppWidgetManager.getInstance(ctx);
    ComponentName me = new ComponentName(ctx, MusicWidget.class);
    int[] ids = mgr.getAppWidgetIds(me);
    if (ids == null || ids.length == 0) return;   // nobody has placed one — don't build views for nothing

    RemoteViews v = new RemoteViews(ctx.getPackageName(), R.layout.widget_music);
    v.setTextViewText(R.id.mw_title, active && title != null && !title.isEmpty() ? title : "PosterChan Music");
    v.setTextViewText(R.id.mw_artist, active ? (artist == null ? "" : artist) : "Tap to play");
    v.setImageViewResource(R.id.mw_play, playing ? R.drawable.ic_media_pause : R.drawable.ic_media_play);
    v.setContentDescription(R.id.mw_play, playing ? "Pause" : "Play");
    // Skip buttons are meaningless with no queue — hidden rather than dead, since a button that
    // visibly does nothing is the complaint this whole feature exists to answer.
    v.setViewVisibility(R.id.mw_prev, active ? View.VISIBLE : View.GONE);
    v.setViewVisibility(R.id.mw_next, active ? View.VISIBLE : View.GONE);

    /* WHICH KIND of PendingIntent depends on whether there is a player to talk to, and it is not a
     * detail: while one is running the press is a transport command and a broadcast is right, but
     * with nothing running the press has to OPEN the app — and an activity started from a receiver of
     * ours is a background activity start, which Android 10+ blocks. A getActivity() PendingIntent is
     * sent by the LAUNCHER instead, which is allowed. Same button, two different jobs. */
    if (active) {
      v.setOnClickPendingIntent(R.id.mw_prev, button(ctx, MusicService.ACTION_PREV));
      v.setOnClickPendingIntent(R.id.mw_play, button(ctx, MusicService.ACTION_TOGGLE));
      v.setOnClickPendingIntent(R.id.mw_next, button(ctx, MusicService.ACTION_NEXT));
    } else {
      v.setOnClickPendingIntent(R.id.mw_play, open(ctx, 2, "play"));
    }
    // The body of the widget opens the app on the Music screen.
    v.setOnClickPendingIntent(R.id.mw_body, open(ctx, 1, "open"));

    mgr.updateAppWidget(ids, v);
  }

  /** A transport press, delivered to this receiver (the player is running — see onReceive). */
  private static PendingIntent button(Context ctx, String action) {
    Intent i = new Intent(ctx, MusicWidget.class).setAction(action);
    return PendingIntent.getBroadcast(ctx, action.hashCode(), i,
        PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
  }

  /** Open the app, telling it what the press meant. Distinct request codes, or the second overwrites
   *  the first: PendingIntents are matched on requester + code + Intent, and extras are not part of
   *  that comparison — "open" and "play" would collapse into whichever was built last. */
  private static PendingIntent open(Context ctx, int code, String what) {
    return PendingIntent.getActivity(ctx, code, launch(ctx, what),
        PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
  }
}
