package ai.posterchan

import android.content.Intent
import android.os.Bundle
import android.widget.Button
import android.widget.ProgressBar
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import com.google.android.material.appbar.MaterialToolbar
import com.google.android.material.textfield.TextInputEditText
import ai.posterchan.api.ApiClient
import ai.posterchan.api.ApiException

class LoginActivity : AppCompatActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_login)

        findViewById<MaterialToolbar>(R.id.toolbar)?.let {
            setSupportActionBar(it)
            it.setNavigationOnClickListener { onBackPressedDispatcher.onBackPressed() }
        }
        supportActionBar?.setDisplayHomeAsUpEnabled(true)
        supportActionBar?.title = getString(R.string.login)

        val usernameEdit = findViewById<TextInputEditText>(R.id.username)
        val passwordEdit = findViewById<TextInputEditText>(R.id.password)
        val loginButton = findViewById<Button>(R.id.login_button)
        val progress = findViewById<ProgressBar>(R.id.login_progress)

        loginButton.setOnClickListener {
            val baseUrl = Prefs.getServerUrl(this)
            if (baseUrl.isBlank()) {
                Toast.makeText(this, getString(R.string.server_url_required), Toast.LENGTH_SHORT).show()
                startActivity(Intent(this, SettingsActivity::class.java))
                return@setOnClickListener
            }
            val username = usernameEdit.text?.toString()?.trim() ?: ""
            val password = passwordEdit.text?.toString() ?: ""
            if (username.isBlank() || password.isBlank()) {
                Toast.makeText(this, getString(R.string.login_username_password_required), Toast.LENGTH_SHORT).show()
                return@setOnClickListener
            }
            loginButton.isEnabled = false
            progress.visibility = android.view.View.VISIBLE
            Thread {
                try {
                    val client = ApiClient(baseUrl, null)
                    val result = client.login(username, password)
                    runOnUiThread {
                        progress.visibility = android.view.View.GONE
                        loginButton.isEnabled = true
                        Prefs.setAccessToken(this, result.accessToken)
                        startActivity(Intent(this, MainActivity::class.java).apply {
                            flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK
                        })
                        finish()
                    }
                } catch (e: ApiException) {
                    runOnUiThread {
                        progress.visibility = android.view.View.GONE
                        loginButton.isEnabled = true
                        Toast.makeText(this, getString(R.string.login_error), Toast.LENGTH_LONG).show()
                    }
                } catch (e: Exception) {
                    runOnUiThread {
                        progress.visibility = android.view.View.GONE
                        loginButton.isEnabled = true
                        Toast.makeText(this, getString(R.string.login_error), Toast.LENGTH_LONG).show()
                    }
                }
            }.start()
        }
    }
}
