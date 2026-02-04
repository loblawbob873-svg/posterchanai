package ai.posterchan

import android.content.Context
import android.content.SharedPreferences
import androidx.core.content.edit

private const val PREFS_NAME = "posterchan_prefs"
private const val KEY_SERVER_URL = "server_url"
private const val KEY_ACCESS_TOKEN = "access_token"

object Prefs {

    private fun prefs(context: Context): SharedPreferences =
        context.applicationContext.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

    fun getServerUrl(context: Context): String =
        prefs(context).getString(KEY_SERVER_URL, "") ?: ""

    fun setServerUrl(context: Context, url: String) {
        prefs(context).edit { putString(KEY_SERVER_URL, url.trim()) }
    }

    fun getAccessToken(context: Context): String =
        prefs(context).getString(KEY_ACCESS_TOKEN, "") ?: ""

    fun setAccessToken(context: Context, token: String) {
        prefs(context).edit { putString(KEY_ACCESS_TOKEN, token) }
    }

    fun clearToken(context: Context) {
        prefs(context).edit { remove(KEY_ACCESS_TOKEN) }
    }
}
