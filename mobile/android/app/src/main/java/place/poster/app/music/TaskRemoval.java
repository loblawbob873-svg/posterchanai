package place.poster.app.music;

/**
 * Does removing THIS task from Recents take the player's WebView with it?
 *
 * Pure (no Android types) so tests/test_android_music_car_buttons.py can compile and RUN it with javac.
 *
 * WHY IT EXISTS. `Service.onTaskRemoved` is called when the user removes ANY task of this application
 * from Recents, not only the task the service was started from. This app runs several: the WebView
 * (MainActivity, where the music actually plays), Texts (`place.poster.app.texts`) and the Phone app
 * (`place.poster.app.phone`) each have their own task affinity and their own card in Recents. MusicService
 * used to answer every removal with shutdown(): swiping the Texts card away released the media session
 * and removed the notification while the song played on in the untouched WebView task -- so the car's
 * and the headset's buttons had no session to land in and did nothing at all, with nothing in any log.
 *
 * The removed task is named by its root intent's component. An activity-alias is resolved to its
 * target by the caller (PackageManager); an unknown/blank component is treated as the player's task,
 * because keeping a notification for a WebView that is really gone is the failure the shutdown exists
 * to prevent, and "could not tell" must not quietly undo it.
 */
public final class TaskRemoval {
  private TaskRemoval() {}

  /**
   * @param removedClass  class name of the removed task's root component (may be an alias), or null
   * @param aliasTarget   the alias's targetActivity when removedClass is an activity-alias, else null
   * @param playerClass   the activity that hosts the player's WebView (MainActivity)
   * @return true when the removal destroys the player, i.e. the session must go
   */
  public static boolean endsPlayer(String removedClass, String aliasTarget, String playerClass) {
    if (removedClass == null || removedClass.isEmpty()) return true;     // could not tell: old behaviour
    if (removedClass.equals(playerClass)) return true;
    if (aliasTarget != null && aliasTarget.equals(playerClass)) return true;
    return false;
  }
}
