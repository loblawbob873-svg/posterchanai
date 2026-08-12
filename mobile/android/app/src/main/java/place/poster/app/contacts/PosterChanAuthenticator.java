package place.poster.app.contacts;

import android.accounts.AbstractAccountAuthenticator;
import android.accounts.Account;
import android.accounts.AccountAuthenticatorResponse;
import android.accounts.AccountManager;
import android.content.Context;
import android.content.Intent;
import android.os.Bundle;

import place.poster.app.MainActivity;

/**
 * A STUB authenticator, on purpose.
 *
 * Android will not let a RawContact belong to an account type that has no authenticator, and the
 * account is the whole point (see ContactWriter): it is what groups our cards under "PosterChan" in
 * the phone's Contacts app, what the user hides them with, and what deletes every one of them when
 * it is removed. None of that needs authentication — the credentials for this app are a Nostr key
 * held in the WebView, and AccountManager will never see them.
 *
 * So every method here refuses politely rather than pretending. The one that matters is addAccount:
 * this account type IS offered in Settings → Accounts → Add account (Android offers every registered
 * type and there is no way to opt out), and an authenticator that returns null there gives the user a
 * dead entry. It opens the app instead, which is where the switch actually lives.
 */
public class PosterChanAuthenticator extends AbstractAccountAuthenticator {

  public PosterChanAuthenticator(Context ctx) {
    super(ctx);
    this.ctx = ctx;
  }

  private final Context ctx;

  @Override
  public Bundle addAccount(AccountAuthenticatorResponse response, String accountType,
                           String authTokenType, String[] requiredFeatures, Bundle options) {
    Intent i = new Intent(ctx, MainActivity.class)
        .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP);
    Bundle out = new Bundle();
    out.putParcelable(AccountManager.KEY_INTENT, i);
    return out;
  }

  @Override
  public Bundle editProperties(AccountAuthenticatorResponse response, String accountType) {
    return null;
  }

  @Override
  public Bundle confirmCredentials(AccountAuthenticatorResponse response, Account account,
                                   Bundle options) {
    return null;
  }

  @Override
  public Bundle getAuthToken(AccountAuthenticatorResponse response, Account account,
                             String authTokenType, Bundle options) {
    Bundle out = new Bundle();
    out.putInt(AccountManager.KEY_ERROR_CODE, AccountManager.ERROR_CODE_UNSUPPORTED_OPERATION);
    out.putString(AccountManager.KEY_ERROR_MESSAGE, "PosterChan contacts do not use auth tokens");
    return out;
  }

  @Override
  public String getAuthTokenLabel(String authTokenType) {
    return null;
  }

  @Override
  public Bundle updateCredentials(AccountAuthenticatorResponse response, Account account,
                                  String authTokenType, Bundle options) {
    return null;
  }

  @Override
  public Bundle hasFeatures(AccountAuthenticatorResponse response, Account account,
                            String[] features) {
    Bundle out = new Bundle();
    out.putBoolean(AccountManager.KEY_BOOLEAN_RESULT, false);
    return out;
  }
}
