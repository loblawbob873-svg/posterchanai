package place.poster.app.contacts;

import android.accounts.AccountManager;
import android.app.Service;
import android.content.Intent;
import android.os.IBinder;

/**
 * The service the platform binds to reach PosterChanAuthenticator.
 *
 * It exists only so the account type is registered — nothing in this app ever starts it, and nothing
 * in it runs on any schedule. It MUST be exported (the binder is the system's) and it must answer
 * only ACTION_AUTHENTICATOR_INTENT: returning a binder for any other action would hand our
 * authenticator to whoever asked.
 */
public class AuthenticatorService extends Service {

  private PosterChanAuthenticator auth;

  @Override
  public void onCreate() {
    super.onCreate();
    auth = new PosterChanAuthenticator(this);
  }

  @Override
  public IBinder onBind(Intent intent) {
    return intent != null && AccountManager.ACTION_AUTHENTICATOR_INTENT.equals(intent.getAction())
        ? auth.getIBinder() : null;
  }
}
