package ai.posterchan

import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.widget.Button
import androidx.appcompat.app.AppCompatActivity
import com.google.android.material.appbar.MaterialToolbar
import com.google.android.material.textfield.TextInputEditText
import com.google.android.material.textfield.TextInputLayout

class SettingsActivity : AppCompatActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_settings)
        findViewById<MaterialToolbar>(R.id.toolbar)?.let {
            setSupportActionBar(it)
            it.setNavigationOnClickListener { onBackPressedDispatcher.onBackPressed() }
        }
        supportActionBar?.setDisplayHomeAsUpEnabled(true)
        supportActionBar?.title = getString(R.string.settings)

        val urlLayout = findViewById<TextInputLayout>(R.id.server_url_layout)
        val urlEdit = findViewById<TextInputEditText>(R.id.server_url)
        urlEdit.setText(Prefs.getServerUrl(this))

        findViewById<Button>(R.id.save).setOnClickListener {
            urlLayout.error = null
            urlLayout.isErrorEnabled = false
            val url = urlEdit.text?.toString()?.trim() ?: ""
            if (url.isBlank()) {
                urlLayout.isErrorEnabled = true
                urlLayout.error = getString(R.string.server_url_required)
                return@setOnClickListener
            }
            val normalized = if (!url.startsWith("http://") && !url.startsWith("https://")) {
                "http://$url"
            } else url
            val uri = Uri.parse(normalized)
            if (uri.host.isNullOrBlank()) {
                urlLayout.isErrorEnabled = true
                urlLayout.error = getString(R.string.server_url_invalid)
                return@setOnClickListener
            }
            Prefs.setServerUrl(this, normalized)
            startActivity(Intent(this, MainActivity::class.java).apply {
                flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK
            })
            finish()
        }
    }

    override fun onSupportNavigateUp(): Boolean {
        onBackPressedDispatcher.onBackPressed()
        return true
    }
}
