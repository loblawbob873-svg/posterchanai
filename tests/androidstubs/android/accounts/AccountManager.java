package android.accounts;

import android.content.Context;
import android.os.Bundle;

public class AccountManager {
  public static AccountManager get(Context ctx) { return null; }
  public Account[] getAccountsByType(String type) { return null; }
  public boolean addAccountExplicitly(Account a, String password, Bundle userdata) { return false; }
  public boolean removeAccountExplicitly(Account a) { return false; }
}
