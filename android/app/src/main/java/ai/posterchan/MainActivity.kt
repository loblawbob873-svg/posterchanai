package ai.posterchan

import android.content.Intent
import android.os.Bundle
import android.view.View
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.recyclerview.widget.LinearLayoutManager
import com.google.android.material.appbar.MaterialToolbar
import com.google.android.material.floatingactionbutton.FloatingActionButton
import com.google.android.material.navigation.NavigationView
import android.widget.TextView
import androidx.drawerlayout.widget.DrawerLayout
import ai.posterchan.api.ApiClient

/**
 * Native shell: conversation list, drawer (Web app, Settings, Log out).
 * If no server URL or no token, redirects to Settings or Login.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var drawerLayout: DrawerLayout
    private lateinit var recyclerView: androidx.recyclerview.widget.RecyclerView
    private lateinit var emptyText: TextView
    private lateinit var adapter: ConversationAdapter

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val baseUrl = Prefs.getServerUrl(this)
        if (baseUrl.isBlank()) {
            startActivity(Intent(this, SettingsActivity::class.java))
            finish()
            return
        }
        val token = Prefs.getAccessToken(this)
        if (token.isBlank()) {
            startActivity(Intent(this, LoginActivity::class.java))
            finish()
            return
        }

        setContentView(R.layout.activity_main_native)
        drawerLayout = findViewById(R.id.drawer_layout)
        recyclerView = findViewById(R.id.conversations_list)
        emptyText = findViewById(R.id.empty_text)

        findViewById<MaterialToolbar>(R.id.toolbar)?.let {
            setSupportActionBar(it)
            it.setNavigationIcon(R.drawable.ic_menu_24)
            it.setNavigationOnClickListener { drawerLayout.openDrawer(android.view.Gravity.START) }
        }
        supportActionBar?.setDisplayHomeAsUpEnabled(true)

        adapter = ConversationAdapter { item ->
            startActivity(Intent(this, ChatActivity::class.java).apply {
                putExtra(ChatActivity.EXTRA_CONVERSATION_ID, item.id)
                putExtra(ChatActivity.EXTRA_TITLE, item.title)
            })
            drawerLayout.closeDrawers()
        }
        recyclerView.layoutManager = LinearLayoutManager(this)
        recyclerView.adapter = adapter

        findViewById<NavigationView>(R.id.nav_view)?.setNavigationItemSelectedListener { item ->
            when (item.itemId) {
                R.id.nav_conversations -> { drawerLayout.closeDrawers(); true }
                R.id.nav_web_app -> {
                    startActivity(Intent(this, WebViewActivity::class.java))
                    drawerLayout.closeDrawers()
                    true
                }
                R.id.nav_settings -> {
                    startActivity(Intent(this, SettingsActivity::class.java))
                    drawerLayout.closeDrawers()
                    true
                }
                R.id.nav_logout -> {
                    Prefs.clearToken(this)
                    startActivity(Intent(this, LoginActivity::class.java).apply {
                        flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK
                    })
                    finish()
                    true
                }
                else -> false
            }
        }

        findViewById<FloatingActionButton>(R.id.fab_new_chat)?.setOnClickListener {
            createNewChat()
        }

        loadConversations()
    }

    override fun onResume() {
        super.onResume()
        if (::recyclerView.isInitialized && Prefs.getAccessToken(this).isNotBlank()) {
            loadConversations()
        }
    }

    private fun loadConversations() {
        val baseUrl = Prefs.getServerUrl(this)
        val token = Prefs.getAccessToken(this)
        if (baseUrl.isBlank() || token.isBlank()) return
        Thread {
            try {
                val client = ApiClient(baseUrl, token)
                val list = client.getConversations()
                runOnUiThread {
                    adapter.submitList(list)
                    emptyText.visibility = if (list.isEmpty()) View.VISIBLE else View.GONE
                }
            } catch (_: Exception) {
                runOnUiThread {
                    adapter.submitList(emptyList())
                    emptyText.visibility = View.VISIBLE
                    emptyText.text = getString(R.string.load_error)
                }
            }
        }.start()
    }

    private fun createNewChat() {
        val baseUrl = Prefs.getServerUrl(this)
        val token = Prefs.getAccessToken(this)
        if (baseUrl.isBlank() || token.isBlank()) return
        Thread {
            try {
                val client = ApiClient(baseUrl, token)
                val conv = client.createConversation()
                runOnUiThread {
                    startActivity(Intent(this, ChatActivity::class.java).apply {
                        putExtra(ChatActivity.EXTRA_CONVERSATION_ID, conv.id)
                        putExtra(ChatActivity.EXTRA_TITLE, conv.title)
                    })
                }
            } catch (e: Exception) {
                runOnUiThread {
                    Toast.makeText(this, getString(R.string.load_error), Toast.LENGTH_SHORT).show()
                }
            }
        }.start()
    }
}
