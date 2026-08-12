package android.accounts;

import android.content.Context;
import android.os.Bundle;

import java.util.ArrayList;
import java.util.List;

/**
 * The one stub here that is NOT signature-only, because the real class's return values are the whole
 * bug this models.
 *
 * addAccountExplicitly() returns FALSE when the account ALREADY EXISTS. It is not an error — the
 * account is there, which is what the caller wanted — but code that reads it as failure says "no
 * account" on every run after the first, while getAccountsByType() one line away says it is present.
 * A stub that always returned false (or always true) could not tell those apart, so it modelled the
 * one thing that mattered as the one thing that never happened.
 *
 * Accounts live in a static list because ContactWriter's calls are static and take a Context this
 * harness has no implementation of. Call reset() at the top of a driver.
 */
public class AccountManager {

  private static final List<Account> ACCOUNTS = new ArrayList<>();
  private static final AccountManager INSTANCE = new AccountManager();

  public static AccountManager get(Context ctx) { return INSTANCE; }

  /** Test control: an empty phone with no accounts on it. */
  public static void reset() { ACCOUNTS.clear(); }

  /**
   * Test control: this phone will not take the account at all — the authenticator is missing, the
   * user is restricted, the OEM has done something strange. The REAL failure, and the only case in
   * which "there is no account" is the honest answer. It has to stay distinguishable from the two
   * false alarms above it, because it is the one the client must report.
   */
  public static boolean wedged = false;

  private static int indexOf(Account a) {
    for (int i = 0; i < ACCOUNTS.size(); i++) {
      Account x = ACCOUNTS.get(i);
      if (x.type.equals(a.type) && x.name.equals(a.name)) return i;
    }
    return -1;
  }

  public Account[] getAccountsByType(String type) {
    List<Account> out = new ArrayList<>();
    for (Account a : ACCOUNTS) if (a.type.equals(type)) out.add(a);
    return out.toArray(new Account[0]);
  }

  /** false means "already there", exactly as on a device — or, when wedged, a genuine refusal. */
  public boolean addAccountExplicitly(Account a, String password, Bundle userdata) {
    if (wedged) return false;
    if (indexOf(a) >= 0) return false;
    ACCOUNTS.add(a);
    return true;
  }

  public boolean removeAccountExplicitly(Account a) {
    int i = indexOf(a);
    if (i < 0) return false;
    ACCOUNTS.remove(i);
    return true;
  }
}
