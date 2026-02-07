package ai.posterchan

import android.content.Context
import android.content.SharedPreferences
import androidx.core.content.edit
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey

private const val PREFS_NAME = "posterchan_prefs_enc"
private const val LEGACY_PREFS_NAME = "posterchan_prefs"
private const val KEY_SERVER_URL = "server_url"
private const val KEY_ACCESS_TOKEN = "access_token"
private const val KEY_TTS_ENABLED = "tts_enabled"

object Prefs {
    /** Max size for image/PDF attachments (MB). */
    const val MAX_ATTACHMENT_MB = 15

    private fun legacyPlainPrefs(context: Context): SharedPreferences =
        context.applicationContext.getSharedPreferences(LEGACY_PREFS_NAME, Context.MODE_PRIVATE)

    private fun encryptedPrefs(context: Context): SharedPreferences {
        val app = context.applicationContext
        return try {
            val masterKey = MasterKey.Builder(app)
                .setKeyScheme(MasterKey.KeyScheme.AES256_GCM)
                .build()
            EncryptedSharedPreferences.create(
                app,
                PREFS_NAME,
                masterKey,
                EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
                EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM
            )
        } catch (e: Exception) {
            legacyPlainPrefs(context)
        }
    }

    /** Migrate from legacy plain prefs (old installs) to encrypted, then use encrypted for reads/writes. */
    private fun prefs(context: Context): SharedPreferences {
        val enc = encryptedPrefs(context)
        val legacy = legacyPlainPrefs(context)
        val encToken = enc.getString(KEY_ACCESS_TOKEN, "")?.isNotBlank() == true
        val legacyToken = legacy.getString(KEY_ACCESS_TOKEN, "")?.isNotBlank() == true
        if (!encToken && legacyToken) {
            enc.edit {
                putString(KEY_ACCESS_TOKEN, legacy.getString(KEY_ACCESS_TOKEN, "") ?: "")
                putString(KEY_SERVER_URL, legacy.getString(KEY_SERVER_URL, "") ?: "")
                putBoolean(KEY_TTS_ENABLED, legacy.getBoolean(KEY_TTS_ENABLED, true))
            }
            legacy.edit { clear() }
        }
        return enc
    }

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

    /** TTS (read aloud) enabled; default true. Mute button toggles this. */
    fun getTtsEnabled(context: Context): Boolean =
        prefs(context).getBoolean(KEY_TTS_ENABLED, true)

    fun setTtsEnabled(context: Context, enabled: Boolean) {
        prefs(context).edit { putBoolean(KEY_TTS_ENABLED, enabled) }
    }
}
