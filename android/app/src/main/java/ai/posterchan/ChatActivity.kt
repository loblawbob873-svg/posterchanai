package ai.posterchan

import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.speech.tts.TextToSpeech
import android.view.Menu
import android.view.MenuItem
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.recyclerview.widget.LinearLayoutManager
import com.google.android.material.appbar.MaterialToolbar
import com.google.android.material.button.MaterialButton
import com.google.android.material.textfield.TextInputEditText
import okhttp3.WebSocket
import ai.posterchan.api.ApiClient
import java.util.Locale

class ChatActivity : AppCompatActivity() {

    companion object {
        const val EXTRA_CONVERSATION_ID = "conversation_id"
        const val EXTRA_TITLE = "title"
    }

    private var conversationId: Int = 0
    private val messages = mutableListOf<ChatMessage>()
    private lateinit var adapter: MessageAdapter
    private var webSocket: WebSocket? = null
    private val mainHandler = Handler(Looper.getMainLooper())
    private var streamingMessageId = -1L
    private var tts: TextToSpeech? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_chat)

        conversationId = intent.getIntExtra(EXTRA_CONVERSATION_ID, 0)
        val title = intent.getStringExtra(EXTRA_TITLE) ?: getString(R.string.app_name)

        findViewById<MaterialToolbar>(R.id.toolbar)?.let {
            setSupportActionBar(it)
            it.setNavigationOnClickListener { onBackPressedDispatcher.onBackPressed() }
        }
        supportActionBar?.setDisplayHomeAsUpEnabled(true)
        supportActionBar?.title = title

        adapter = MessageAdapter()
        findViewById<androidx.recyclerview.widget.RecyclerView>(R.id.messages_list).apply {
            layoutManager = LinearLayoutManager(this@ChatActivity).apply { stackFromEnd = true }
            adapter = this@ChatActivity.adapter
        }

        val input = findViewById<TextInputEditText>(R.id.message_input)
        findViewById<MaterialButton>(R.id.send_button).setOnClickListener {
            val text = input.text?.toString()?.trim() ?: return@setOnClickListener
            if (text.isBlank()) return@setOnClickListener
            input.text?.clear()
            sendMessage(text)
        }

        tts = TextToSpeech(this) { status ->
            if (status == TextToSpeech.SUCCESS) {
                tts?.language = Locale.getDefault()
            }
        }

        loadMessages()
    }

    override fun onCreateOptionsMenu(menu: Menu): Boolean {
        menuInflater.inflate(R.menu.chat_menu, menu)
        return true
    }

    override fun onPrepareOptionsMenu(menu: Menu): Boolean {
        val item = menu.findItem(R.id.action_tts_mute)
        val enabled = Prefs.getTtsEnabled(this)
        item?.setIcon(if (enabled) R.drawable.ic_volume_24 else R.drawable.ic_volume_off_24)
        item?.setTitle(if (enabled) getString(R.string.tts_mute) else getString(R.string.tts_unmute))
        return super.onPrepareOptionsMenu(menu)
    }

    override fun onOptionsItemSelected(item: MenuItem): Boolean {
        if (item.itemId == R.id.action_tts_mute) {
            val newState = !Prefs.getTtsEnabled(this)
            Prefs.setTtsEnabled(this, newState)
            invalidateOptionsMenu()
            tts?.stop()
            Toast.makeText(this, if (newState) getString(R.string.tts_unmute) else getString(R.string.tts_mute), Toast.LENGTH_SHORT).show()
            return true
        }
        return super.onOptionsItemSelected(item)
    }

    override fun onDestroy() {
        tts?.stop()
        tts?.shutdown()
        tts = null
        webSocket?.close(1000, null)
        webSocket = null
        super.onDestroy()
    }

    private fun loadMessages() {
        val baseUrl = Prefs.getServerUrl(this)
        val token = Prefs.getAccessToken(this)
        if (baseUrl.isBlank() || token.isBlank()) return
        Thread {
            try {
                val client = ApiClient(baseUrl, token)
                val list = client.getMessages(conversationId)
                val chatMessages = list.map { m ->
                    ChatMessage(id = m.id.toLong(), role = m.role, content = m.content, isStreaming = false)
                }
                mainHandler.post {
                    messages.clear()
                    messages.addAll(chatMessages)
                    adapter.submitList(messages.toList())
                    scrollToBottom()
                }
            } catch (_: Exception) {
                mainHandler.post {
                    Toast.makeText(this, getString(R.string.load_error), Toast.LENGTH_SHORT).show()
                }
            }
        }.start()
    }

    private fun sendMessage(text: String) {
        val baseUrl = Prefs.getServerUrl(this)
        val token = Prefs.getAccessToken(this)
        if (baseUrl.isBlank() || token.isBlank()) {
            Toast.makeText(this, getString(R.string.load_error), Toast.LENGTH_SHORT).show()
            return
        }

        messages.add(ChatMessage(id = System.currentTimeMillis(), role = "user", content = text))
        adapter.submitList(messages.toList())
        scrollToBottom()

        streamingMessageId = -(System.currentTimeMillis())
        messages.add(ChatMessage(id = streamingMessageId, role = "assistant", content = "", isStreaming = true))
        adapter.submitList(messages.toList())
        scrollToBottom()

        if (webSocket == null) {
            val client = ApiClient(baseUrl, token)
            webSocket = client.connectChatWebSocket(conversationId, object : ai.posterchan.api.ChatWebSocketListener {
                override fun onOpen() {
                    mainHandler.post { client.sendChatMessage(webSocket!!, text) }
                }
                override fun onStreamChunk(chunk: String) {
                    mainHandler.post {
                        val idx = messages.indexOfFirst { it.id == streamingMessageId }
                        if (idx >= 0) {
                            messages[idx] = messages[idx].copy(content = messages[idx].content + chunk)
                            adapter.submitList(messages.toList())
                            scrollToBottom()
                        }
                    }
                }
                override fun onStreamEnd() {
                    mainHandler.post {
                        val idx = messages.indexOfFirst { it.id == streamingMessageId }
                        if (idx >= 0) {
                            messages[idx] = messages[idx].copy(isStreaming = false)
                            adapter.submitList(messages.toList())
                            speakIfEnabled(messages[idx].content)
                        }
                    }
                }
                override fun onResponse(fullContent: String, data: org.json.JSONObject?) {
                    mainHandler.post {
                        val idx = messages.indexOfFirst { it.id == streamingMessageId }
                        if (idx >= 0) {
                            messages[idx] = messages[idx].copy(content = fullContent, isStreaming = false)
                            adapter.submitList(messages.toList())
                            scrollToBottom()
                            speakIfEnabled(fullContent)
                        }
                    }
                }
                override fun onError(message: String) {
                    mainHandler.post {
                        Toast.makeText(this@ChatActivity, message, Toast.LENGTH_SHORT).show()
                        val idx = messages.indexOfFirst { it.id == streamingMessageId }
                        if (idx >= 0) {
                            messages[idx] = messages[idx].copy(content = "Error: $message", isStreaming = false)
                            adapter.submitList(messages.toList())
                        }
                    }
                }
            })
        } else {
            ApiClient(baseUrl, token).sendChatMessage(webSocket!!, text)
        }
    }

    private fun scrollToBottom() {
        val list = findViewById<androidx.recyclerview.widget.RecyclerView>(R.id.messages_list) ?: return
        list.post {
            if (adapter.itemCount > 0) list.smoothScrollToPosition(adapter.itemCount - 1)
        }
    }

    private fun speakIfEnabled(text: String?) {
        if (text.isNullOrBlank() || !Prefs.getTtsEnabled(this)) return
        val cleaned = text
            .replace(Regex("""\*\*|__|##+|```[\s\S]*?```"""), " ")
            .replace(Regex("""\[([^]]+)\]\([^)]+\)"""), "$1")
            .replace(Regex("<[^>]+>"), " ")
            .replace(Regex("""\s+"""), " ")
            .trim()
        if (cleaned.length > 5000) return
        if (cleaned.isNotEmpty()) {
            tts?.stop()
            tts?.speak(cleaned, TextToSpeech.QUEUE_FLUSH, null, null)
        }
    }
}
