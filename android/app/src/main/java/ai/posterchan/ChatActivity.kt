package ai.posterchan

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import android.speech.tts.TextToSpeech
import android.util.Base64
import android.view.Menu
import android.view.MenuItem
import android.widget.EditText
import android.widget.ImageButton
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.FileProvider
import androidx.core.content.ContextCompat
import androidx.recyclerview.widget.LinearLayoutManager
import com.google.android.material.appbar.MaterialToolbar
import com.google.android.material.button.MaterialButton
import com.google.android.material.textfield.TextInputEditText
import okhttp3.WebSocket
import ai.posterchan.api.ApiClient
import ai.posterchan.MessageAdapter
import java.io.File
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
    private var isStreaming = false
    private var lastSentUserText: String? = null
    private var tts: TextToSpeech? = null
    private lateinit var sendButton: MaterialButton
    private var cameraCaptureFile: File? = null
    private var speechRecognizer: SpeechRecognizer? = null
    private lateinit var messageInput: TextInputEditText

    private val requestRecordAudioLauncher = registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        if (granted) startVoiceInput(messageInput)
        else Toast.makeText(this, getString(R.string.voice_input), Toast.LENGTH_SHORT).show()
    }

    private val takePictureLauncher = registerForActivityResult(ActivityResultContracts.TakePicture()) { success ->
        if (success) {
            cameraCaptureFile?.let { file ->
                try {
                    val bytes = file.readBytes()
                    val base64 = Base64.encodeToString(bytes, Base64.NO_WRAP)
                    sendMessageWithAttachments("[Photo]", imageData = base64, pdfData = null, fileContent = null)
                } catch (_: Exception) {
                    Toast.makeText(this, getString(R.string.load_error), Toast.LENGTH_SHORT).show()
                }
                file.delete()
            }
            cameraCaptureFile = null
        }
    }

    private val attachLauncher = registerForActivityResult(ActivityResultContracts.GetContent()) { uri: Uri? ->
        if (uri == null) return@registerForActivityResult
        try {
            val mime = contentResolver.getType(uri) ?: ""
            when {
                mime.startsWith("image/") -> {
                    contentResolver.openInputStream(uri)?.use { input ->
                        val bytes = input.readBytes()
                        val base64 = Base64.encodeToString(bytes, Base64.NO_WRAP)
                        sendMessageWithAttachments("[Image]", imageData = base64, pdfData = null, fileContent = null)
                    } ?: run {
                        Toast.makeText(this, getString(R.string.load_error), Toast.LENGTH_SHORT).show()
                    }
                }
                mime == "application/pdf" -> {
                    contentResolver.openInputStream(uri)?.use { input ->
                        val bytes = input.readBytes()
                        val base64 = Base64.encodeToString(bytes, Base64.NO_WRAP)
                        sendMessageWithAttachments("[PDF]", imageData = null, pdfData = base64, fileContent = null)
                    } ?: run {
                        Toast.makeText(this, getString(R.string.load_error), Toast.LENGTH_SHORT).show()
                    }
                }
                else -> {
                    contentResolver.openInputStream(uri)?.use { input ->
                        val text = input.bufferedReader().readText()
                        if (text.length > 100_000) {
                            Toast.makeText(this, getString(R.string.load_error), Toast.LENGTH_SHORT).show()
                            return@registerForActivityResult
                        }
                        sendMessageWithAttachments("[File]", imageData = null, pdfData = null, fileContent = text)
                    } ?: run {
                        Toast.makeText(this, getString(R.string.load_error), Toast.LENGTH_SHORT).show()
                    }
                }
            }
        } catch (e: Exception) {
            Toast.makeText(this, getString(R.string.load_error), Toast.LENGTH_SHORT).show()
        }
    }

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

        adapter = MessageAdapter(
            lastAssistantMessageId = { messages.lastOrNull { !it.isUser }?.id },
            onCopy = { copyToClipboard(it) },
            onShare = { shareText(it) },
            onRegenerateAssistant = { regenerateResponse() },
            onEditUser = { id, content -> editUserMessage(id, content) },
            onResendUser = { id -> resendUserMessage(id) },
        )
        findViewById<androidx.recyclerview.widget.RecyclerView>(R.id.messages_list).apply {
            layoutManager = LinearLayoutManager(this@ChatActivity).apply { stackFromEnd = true }
            adapter = this@ChatActivity.adapter
        }

        messageInput = findViewById(R.id.message_input)
        val input = messageInput
        sendButton = findViewById(R.id.send_button)
        sendButton.setOnClickListener {
            if (isStreaming) {
                sendStop()
                return@setOnClickListener
            }
            val text = input.text?.toString()?.trim() ?: return@setOnClickListener
            if (text.isBlank()) return@setOnClickListener
            input.text?.clear()
            sendMessage(text)
        }

        findViewById<ImageButton>(R.id.attach_button).setOnClickListener {
            if (isStreaming) return@setOnClickListener
            attachLauncher.launch("*/*")
        }

        findViewById<ImageButton>(R.id.camera_button).setOnClickListener {
            if (isStreaming) return@setOnClickListener
            cameraCaptureFile = File(cacheDir, "chat_capture_${System.currentTimeMillis()}.jpg")
            val uri = FileProvider.getUriForFile(this, "${packageName}.fileprovider", cameraCaptureFile!!)
            takePictureLauncher.launch(uri)
        }

        findViewById<ImageButton>(R.id.voice_button).setOnClickListener {
            if (isStreaming) return@setOnClickListener
            if (ContextCompat.checkSelfPermission(this, android.Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
                requestRecordAudioLauncher.launch(android.Manifest.permission.RECORD_AUDIO)
                return@setOnClickListener
            }
            startVoiceInput(input)
        }

        tts = TextToSpeech(this) { status ->
            if (status == TextToSpeech.SUCCESS) {
                tts?.language = Locale.getDefault()
            }
        }

        loadMessages()
        updateSendButtonState()
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
        speechRecognizer?.destroy()
        speechRecognizer = null
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
                    val content = MarkdownUtils.stripThinkingTags(m.content)
                    ChatMessage(id = m.id.toLong(), role = m.role, content = content, isStreaming = false)
                }
                mainHandler.post {
                    messages.clear()
                    messages.addAll(chatMessages)
                    lastSentUserText = chatMessages.lastOrNull { it.isUser }?.content
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

        lastSentUserText = text
        isStreaming = true
        updateSendButtonState()
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
                            val raw = messages[idx].content + chunk
                            val display = MarkdownUtils.stripThinkingTags(raw)
                            messages[idx] = messages[idx].copy(content = display)
                            adapter.submitList(messages.toList())
                            scrollToBottom()
                        }
                    }
                }
                override fun onStreamEnd() {
                    mainHandler.post {
                        isStreaming = false
                        updateSendButtonState()
                        val idx = messages.indexOfFirst { it.id == streamingMessageId }
                        if (idx >= 0) {
                            val finalContent = MarkdownUtils.stripThinkingTags(messages[idx].content)
                            messages[idx] = messages[idx].copy(content = finalContent, isStreaming = false)
                            adapter.submitList(messages.toList())
                            speakIfEnabled(finalContent)
                        }
                    }
                }
                override fun onResponse(fullContent: String, data: org.json.JSONObject?) {
                    mainHandler.post {
                        isStreaming = false
                        updateSendButtonState()
                        val idx = messages.indexOfFirst { it.id == streamingMessageId }
                        if (idx >= 0) {
                            val display = MarkdownUtils.stripThinkingTags(fullContent)
                            messages[idx] = messages[idx].copy(content = display, isStreaming = false)
                            adapter.submitList(messages.toList())
                            scrollToBottom()
                            speakIfEnabled(display)
                        }
                    }
                }
                override fun onError(message: String) {
                    mainHandler.post {
                        isStreaming = false
                        updateSendButtonState()
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

    private fun sendMessageWithAttachments(
        displayContent: String,
        imageData: String?,
        pdfData: String?,
        fileContent: String?
    ) {
        val baseUrl = Prefs.getServerUrl(this)
        val token = Prefs.getAccessToken(this)
        if (baseUrl.isBlank() || token.isBlank()) {
            Toast.makeText(this, getString(R.string.load_error), Toast.LENGTH_SHORT).show()
            return
        }
        lastSentUserText = displayContent
        isStreaming = true
        updateSendButtonState()
        messages.add(ChatMessage(id = System.currentTimeMillis(), role = "user", content = displayContent))
        adapter.submitList(messages.toList())
        scrollToBottom()

        streamingMessageId = -(System.currentTimeMillis())
        messages.add(ChatMessage(id = streamingMessageId, role = "assistant", content = "", isStreaming = true))
        adapter.submitList(messages.toList())
        scrollToBottom()

        val content = ""
        if (webSocket == null) {
            val client = ApiClient(baseUrl, token)
            webSocket = client.connectChatWebSocket(conversationId, object : ai.posterchan.api.ChatWebSocketListener {
                override fun onOpen() {
                    mainHandler.post {
                        client.sendChatMessage(webSocket!!, content, imageData, pdfData, fileContent)
                    }
                }
                override fun onStreamChunk(chunk: String) {
                    mainHandler.post {
                        val idx = messages.indexOfFirst { it.id == streamingMessageId }
                        if (idx >= 0) {
                            val raw = messages[idx].content + chunk
                            val display = MarkdownUtils.stripThinkingTags(raw)
                            messages[idx] = messages[idx].copy(content = display)
                            adapter.submitList(messages.toList())
                            scrollToBottom()
                        }
                    }
                }
                override fun onStreamEnd() {
                    mainHandler.post {
                        isStreaming = false
                        updateSendButtonState()
                        val idx = messages.indexOfFirst { it.id == streamingMessageId }
                        if (idx >= 0) {
                            val finalContent = MarkdownUtils.stripThinkingTags(messages[idx].content)
                            messages[idx] = messages[idx].copy(content = finalContent, isStreaming = false)
                            adapter.submitList(messages.toList())
                            speakIfEnabled(finalContent)
                        }
                    }
                }
                override fun onResponse(fullContent: String, data: org.json.JSONObject?) {
                    mainHandler.post {
                        isStreaming = false
                        updateSendButtonState()
                        val idx = messages.indexOfFirst { it.id == streamingMessageId }
                        if (idx >= 0) {
                            val display = MarkdownUtils.stripThinkingTags(fullContent)
                            messages[idx] = messages[idx].copy(content = display, isStreaming = false)
                            adapter.submitList(messages.toList())
                            scrollToBottom()
                            speakIfEnabled(display)
                        }
                    }
                }
                override fun onError(message: String) {
                    mainHandler.post {
                        isStreaming = false
                        updateSendButtonState()
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
            ApiClient(baseUrl, token).sendChatMessage(webSocket!!, content, imageData, pdfData, fileContent)
        }
    }

    private fun startVoiceInput(input: TextInputEditText) {
        if (!SpeechRecognizer.isRecognitionAvailable(this)) {
            Toast.makeText(this, getString(R.string.voice_input), Toast.LENGTH_SHORT).show()
            return
        }
        speechRecognizer?.destroy()
        speechRecognizer = SpeechRecognizer.createSpeechRecognizer(this).apply {
            setRecognitionListener(object : RecognitionListener {
                override fun onReadyForSpeech(params: android.os.Bundle?) {}
                override fun onBeginningOfSpeech() {}
                override fun onRmsChanged(rmsdB: Float) {}
                override fun onBufferReceived(buffer: ByteArray?) {}
                override fun onEndOfSpeech() {}
                override fun onError(error: Int) {
                    mainHandler.post {
                        if (error != SpeechRecognizer.ERROR_CLIENT) {
                            Toast.makeText(this@ChatActivity, getString(R.string.voice_input), Toast.LENGTH_SHORT).show()
                        }
                    }
                }
                override fun onResults(results: android.os.Bundle?) {
                    val matches = results?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)
                    val text = matches?.firstOrNull()?.trim()
                    if (!text.isNullOrBlank()) {
                        mainHandler.post {
                            val current = input.text?.toString() ?: ""
                            input.setText(if (current.isBlank()) text else "$current $text")
                            input.setSelection(input.text?.length ?: 0)
                        }
                    }
                }
                override fun onPartialResults(partialResults: android.os.Bundle?) {}
                override fun onEvent(eventType: Int, params: android.os.Bundle?) {}
            })
        }
        val intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
            putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            putExtra(RecognizerIntent.EXTRA_LANGUAGE, Locale.getDefault())
            putExtra(RecognizerIntent.EXTRA_PROMPT, getString(R.string.voice_input))
        }
        speechRecognizer?.startListening(intent)
    }

    private fun scrollToBottom() {
        val list = findViewById<androidx.recyclerview.widget.RecyclerView>(R.id.messages_list) ?: return
        list.post {
            if (adapter.itemCount > 0) list.smoothScrollToPosition(adapter.itemCount - 1)
        }
    }

    private fun updateSendButtonState() {
        sendButton.text = if (isStreaming) getString(R.string.stop) else getString(R.string.send)
    }

    private fun sendStop() {
        webSocket?.let {
            ApiClient(Prefs.getServerUrl(this), Prefs.getAccessToken(this)).sendStop(it)
        }
    }

    private fun copyToClipboard(text: String) {
        val cm = getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
        cm.setPrimaryClip(ClipData.newPlainText(null, text))
        Toast.makeText(this, getString(R.string.copy_toast), Toast.LENGTH_SHORT).show()
    }

    private fun shareText(text: String) {
        startActivity(Intent.createChooser(
            Intent(Intent.ACTION_SEND).apply {
                type = "text/plain"
                putExtra(Intent.EXTRA_TEXT, text)
            },
            null
        ))
    }

    private fun regenerateResponse() {
        val lastId = messages.lastOrNull { !it.isUser }?.id ?: return
        val text = lastSentUserText
        if (text.isNullOrBlank()) {
            Toast.makeText(this, getString(R.string.load_error), Toast.LENGTH_SHORT).show()
            return
        }
        messages.removeAll { it.id == lastId }
        adapter.submitList(messages.toList())
        sendMessage(text)
    }

    private fun editUserMessage(messageId: Long, currentContent: String) {
        val input = EditText(this).apply {
            setText(currentContent)
            setSelection(currentContent.length)
            setPadding(48, 32, 48, 32)
            minHeight = 120
        }
        AlertDialog.Builder(this)
            .setTitle(R.string.edit_message)
            .setView(input)
            .setPositiveButton(R.string.save_resubmit) { _, _ ->
                val newContent = input.text?.toString()?.trim()
                if (!newContent.isNullOrBlank()) {
                    val idx = messages.indexOfFirst { it.id == messageId }
                    if (idx >= 0) {
                        while (messages.size > idx + 1) messages.removeAt(messages.size - 1)
                        messages[idx] = messages[idx].copy(content = newContent)
                        adapter.submitList(messages.toList())
                        sendMessage(newContent)
                    }
                }
            }
            .setNegativeButton(android.R.string.cancel, null)
            .show()
    }

    private fun resendUserMessage(messageId: Long) {
        val msg = messages.find { it.id == messageId } ?: return
        if (msg.isUser.not()) return
        val idx = messages.indexOfFirst { it.id == messageId }
        if (idx < 0) return
        while (messages.size > idx + 1) messages.removeAt(messages.size - 1)
        adapter.submitList(messages.toList())
        sendMessage(msg.content)
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
