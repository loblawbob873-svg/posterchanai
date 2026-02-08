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
import android.media.MediaPlayer
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import android.util.Base64
import android.view.Menu
import android.view.MenuItem
import android.widget.EditText
import android.widget.ImageButton
import android.widget.Toast
import android.widget.PopupMenu
import com.google.android.material.button.MaterialButton
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.FileProvider
import androidx.core.content.ContextCompat
import androidx.recyclerview.widget.LinearLayoutManager
import com.google.android.material.appbar.MaterialToolbar
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
    private var ttsMediaPlayer: MediaPlayer? = null
    private var ttsTempFile: File? = null
    private lateinit var sendButton: MaterialButton
    private var cameraCaptureFile: File? = null
    private var speechRecognizer: SpeechRecognizer? = null
    private lateinit var messageInput: TextInputEditText
    /** Current mode prepended to next message (e.g. "search", "images", "geni"). Empty = normal chat. */
    private var currentMode: String = ""

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
            val maxBytes = Prefs.MAX_ATTACHMENT_MB * 1024L * 1024L
            fun uriSize(uri: Uri): Long? = contentResolver.openFileDescriptor(uri, "r")?.use { it.getStatSize() }
            when {
                mime.startsWith("image/") -> {
                    if (uriSize(uri)?.let { it > maxBytes } == true) {
                        Toast.makeText(this, getString(R.string.file_too_large, Prefs.MAX_ATTACHMENT_MB), Toast.LENGTH_SHORT).show()
                        return@registerForActivityResult
                    }
                    contentResolver.openInputStream(uri)?.use { input ->
                        val bytes = input.readBytes()
                        val base64 = Base64.encodeToString(bytes, Base64.NO_WRAP)
                        sendMessageWithAttachments("[Image]", imageData = base64, pdfData = null, fileContent = null)
                    } ?: run {
                        Toast.makeText(this, getString(R.string.load_error), Toast.LENGTH_SHORT).show()
                    }
                }
                mime == "application/pdf" -> {
                    if (uriSize(uri)?.let { it > maxBytes } == true) {
                        Toast.makeText(this, getString(R.string.file_too_large, Prefs.MAX_ATTACHMENT_MB), Toast.LENGTH_SHORT).show()
                        return@registerForActivityResult
                    }
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
        if (conversationId <= 0) {
            Toast.makeText(this, getString(R.string.invalid_conversation), Toast.LENGTH_SHORT).show()
            finish()
            return
        }

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
            onOpenUrl = { url -> startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url))) },
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

        setupQuickActions()

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
            stopServerTts()
            Toast.makeText(this, if (newState) getString(R.string.tts_unmute) else getString(R.string.tts_mute), Toast.LENGTH_SHORT).show()
            return true
        }
        return super.onOptionsItemSelected(item)
    }

    override fun onDestroy() {
        speechRecognizer?.destroy()
        speechRecognizer = null
        stopServerTts()
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

    /** Build display string for command response (images, search, etc.) so results show as tappable links. */
    private fun buildDisplayContentForResponse(fullContent: String, data: org.json.JSONObject?): String {
        if (data == null) return fullContent
        val type = data.optString("type", "")
        val sb = StringBuilder(MarkdownUtils.stripThinkingTags(fullContent))
        when (type) {
            "images" -> {
                // Thumbnail strip is shown via imageSearchResults; no link list below.
                return sb.toString()
            }
            "search" -> {
                val arr = data.optJSONArray("results") ?: return sb.toString()
                for (i in 0 until arr.length()) {
                    val o = arr.optJSONObject(i) ?: continue
                    val title = o.optString("title", "Result").replace("[", "(").replace("]", ")").take(60)
                    val url = o.optString("url", "")
                    if (url.isNotBlank()) {
                        sb.append("\n\n• [").append(title).append("](").append(url).append(")")
                    }
                }
            }
        }
        return sb.toString()
    }

    /** Parse "images" command response. When baseUrl+token set, use proxy URL so server fetches thumbnails (reliable on Android). */
    private fun parseImageSearchResults(data: org.json.JSONObject?, baseUrl: String, token: String): List<ImageSearchItem>? {
        if (data?.optString("type") != "images") return null
        val arr = data.optJSONArray("images") ?: return null
        val list = mutableListOf<ImageSearchItem>()
        val useProxy = baseUrl.isNotBlank() && token.isNotBlank()
        val base = baseUrl.trimEnd('/')
        for (i in 0 until arr.length().coerceAtMost(10)) {
            val o = arr.optJSONObject(i) ?: continue
            val thumbId = o.optString("thumb_id", "").takeIf { it.isNotBlank() }
            val thumb = o.optString("img_src", "").takeIf { it.isNotBlank() }
                ?: o.optString("thumbnail_src", "").takeIf { it.isNotBlank() }
                ?: o.optString("thumbnail", "").takeIf { it.isNotBlank() }
            val url = o.optString("url", "").takeIf { it.isNotBlank() } ?: thumb ?: continue
            val thumbUrl = thumb ?: url
            val title = o.optString("title", "Image").take(60)
            when {
                thumbId != null && useProxy -> list.add(ImageSearchItem(thumbnailUrl = "$base/api/proxy-image", url = url, title = title, postBodyThumbId = thumbId, authToken = token, directThumbUrl = if (thumb != null && thumb.startsWith("http")) thumb else null))
                useProxy -> list.add(ImageSearchItem(thumbnailUrl = "$base/api/proxy-image", url = url, title = title, postBodyUrl = thumbUrl, authToken = token, directThumbUrl = if (thumb != null && thumb.startsWith("http")) thumb else null))
                else -> list.add(ImageSearchItem(thumbnailUrl = thumbUrl, url = url, title = title))
            }
        }
        return list.takeIf { it.isNotEmpty() }
    }

    private fun sendMessage(text: String) {
        val baseUrl = Prefs.getServerUrl(this)
        val token = Prefs.getAccessToken(this)
        if (baseUrl.isBlank() || token.isBlank()) {
            Toast.makeText(this, getString(R.string.load_error), Toast.LENGTH_SHORT).show()
            return
        }

        val contentToSend = if (currentMode.isNotBlank()) "$currentMode $text".trim() else text
        currentMode = ""
        updateQuickActionHighlight()
        messageInput.hint = getString(R.string.message_hint)

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
                    mainHandler.post { client.sendChatMessage(webSocket!!, contentToSend) }
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
                            val msg = messages[idx]
                            // Don't overwrite command result if response already arrived (stream_end can come after)
                            if (msg.imageSearchResults != null || msg.generatedImageBase64 != null) {
                                messages[idx] = msg.copy(isStreaming = false)
                            } else {
                                val finalContent = MarkdownUtils.stripThinkingTags(msg.content)
                                messages[idx] = msg.copy(content = finalContent, isStreaming = false)
                                speakIfEnabled(finalContent)
                            }
                            adapter.submitList(messages.toList())
                        }
                    }
                }
                override fun onResponse(fullContent: String, data: org.json.JSONObject?) {
                    mainHandler.post {
                        isStreaming = false
                        updateSendButtonState()
                        val idx = messages.indexOfFirst { it.id == streamingMessageId }
                        if (idx >= 0) {
                            val display = buildDisplayContentForResponse(fullContent, data)
                            val geniBase64 = if (data?.optString("type") == "generated_image") data.optString("image").takeIf { it.isNotBlank() } else null
                            val imageSearch = parseImageSearchResults(data, baseUrl, token)
                            messages[idx] = messages[idx].copy(content = display, isStreaming = false, generatedImageBase64 = geniBase64, imageSearchResults = imageSearch)
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
            ApiClient(baseUrl, token).sendChatMessage(webSocket!!, contentToSend)
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
                            val msg = messages[idx]
                            if (msg.imageSearchResults != null || msg.generatedImageBase64 != null) {
                                messages[idx] = msg.copy(isStreaming = false)
                            } else {
                                val finalContent = MarkdownUtils.stripThinkingTags(msg.content)
                                messages[idx] = msg.copy(content = finalContent, isStreaming = false)
                                speakIfEnabled(finalContent)
                            }
                            adapter.submitList(messages.toList())
                        }
                    }
                }
                override fun onResponse(fullContent: String, data: org.json.JSONObject?) {
                    mainHandler.post {
                        isStreaming = false
                        updateSendButtonState()
                        val idx = messages.indexOfFirst { it.id == streamingMessageId }
                        if (idx >= 0) {
                            val display = buildDisplayContentForResponse(fullContent, data)
                            val geniBase64 = if (data?.optString("type") == "generated_image") data.optString("image").takeIf { it.isNotBlank() } else null
                            val imageSearch = parseImageSearchResults(data, baseUrl, token)
                            messages[idx] = messages[idx].copy(content = display, isStreaming = false, generatedImageBase64 = geniBase64, imageSearchResults = imageSearch)
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

    private fun openWebApp() {
        startActivity(Intent(this, WebViewActivity::class.java))
    }

    /** Open full web UI and run a chat command there (nyaa, etc.). */
    private fun openWebWithCommand(command: String) {
        startActivity(Intent(this, WebViewActivity::class.java).apply {
            putExtra(WebViewActivity.EXTRA_INITIAL_COMMAND, command)
        })
    }

    /** Open native Torrents screen (Downloading list + Movies/TV/Anime browse). */
    private fun openNativeTorrents(initialTab: String?) {
        startActivity(Intent(this, TorrentsActivity::class.java).apply {
            if (initialTab != null) putExtra(TorrentsActivity.EXTRA_TAB, initialTab)
        })
    }

    private fun updateQuickActionHighlight() {
        // Optional: could style Chat/Generate buttons when mode is active (MaterialButton is not checkable by default)
    }

    private fun setupQuickActions() {
        findViewById<MaterialButton>(R.id.quick_btn_chat).setOnClickListener {
            currentMode = ""
            updateQuickActionHighlight()
            messageInput.hint = getString(R.string.message_hint)
        }

        findViewById<MaterialButton>(R.id.quick_btn_pim).setOnClickListener { v ->
            val popup = PopupMenu(this, v)
            popup.menuInflater.inflate(R.menu.quick_pim, popup.menu)
            popup.setOnMenuItemClickListener { item ->
                when (item.itemId) {
                    R.id.pim_mail -> { sendCommand("mail"); true }
                    R.id.pim_mail_folders -> { sendCommand("mail folders"); true }
                    R.id.pim_calendar -> { sendCommand("cal week"); true }
                    R.id.pim_add_event -> { openWebApp(); true }
                    R.id.pim_contacts -> { sendCommand("contacts all"); true }
                    R.id.pim_todo -> { sendCommand("todo"); true }
                    else -> false
                }
            }
            popup.show()
        }

        findViewById<MaterialButton>(R.id.quick_btn_files).setOnClickListener { v ->
            val popup = PopupMenu(this, v)
            popup.menuInflater.inflate(R.menu.quick_files, popup.menu)
            popup.setOnMenuItemClickListener { item ->
                when (item.itemId) {
                    R.id.files_manager -> {
                        startActivity(Intent(this, FileManagerActivity::class.java))
                        true
                    }
                    R.id.files_photos -> {
                        startActivity(Intent(this, PhotosActivity::class.java))
                        true
                    }
                    else -> false
                }
            }
            popup.show()
        }

        findViewById<MaterialButton>(R.id.quick_btn_web).setOnClickListener { v ->
            val popup = PopupMenu(this, v)
            popup.menuInflater.inflate(R.menu.quick_web, popup.menu)
            popup.setOnMenuItemClickListener { item ->
                when (item.itemId) {
                    R.id.web_search -> {
                        currentMode = "search"
                        updateQuickActionHighlight()
                        messageInput.hint = getString(R.string.quick_search_hint)
                        true
                    }
                    R.id.web_images -> {
                        currentMode = "images"
                        updateQuickActionHighlight()
                        messageInput.hint = getString(R.string.quick_images_hint)
                        true
                    }
                    R.id.web_news, R.id.web_fourchan -> { openWebApp(); true }
                    R.id.web_torrents -> { openNativeTorrents(null); true }
                    R.id.web_downloading -> { openNativeTorrents("downloading"); true }
                    R.id.web_nyaa -> { openNativeTorrents("nyaa"); true }
                    else -> false
                }
            }
            popup.show()
        }

        findViewById<MaterialButton>(R.id.quick_btn_generate).setOnClickListener {
            currentMode = "geni"
            updateQuickActionHighlight()
            messageInput.hint = getString(R.string.quick_generate_hint)
            if (messageInput.text?.isBlank() != false) messageInput.requestFocus()
        }

        findViewById<MaterialButton>(R.id.quick_btn_translate).setOnClickListener { openWebApp() }
        findViewById<MaterialButton>(R.id.quick_btn_rag).setOnClickListener { openWebApp() }

        updateQuickActionHighlight()
    }

    private fun sendCommand(cmd: String) {
        if (isStreaming) return
        sendMessage(cmd)
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
        if (cleaned.isEmpty()) return
        stopServerTts()
        val baseUrl = Prefs.getServerUrl(this)
        val token = Prefs.getAccessToken(this)
        if (baseUrl.isBlank() || token.isBlank()) return
        Thread {
            try {
                val client = ApiClient(baseUrl, token)
                val base64 = client.generateTts(cleaned)
                if (base64 != null) {
                    val bytes = Base64.decode(base64, Base64.DEFAULT)
                    val file = File(cacheDir, "tts_${System.currentTimeMillis()}.mp3")
                    file.writeBytes(bytes)
                    mainHandler.post {
                        if (isDestroyed) {
                            file.delete()
                            return@post
                        }
                        playServerTtsFile(file)
                    }
                }
            } catch (_: Exception) { /* same as web: silently fail */ }
        }.start()
    }

    private fun stopServerTts() {
        ttsMediaPlayer?.apply {
            try {
                if (isPlaying) stop()
                release()
            } catch (_: Exception) { }
        }
        ttsMediaPlayer = null
        ttsTempFile?.let { if (it.exists()) it.delete() }
        ttsTempFile = null
    }

    private fun playServerTtsFile(file: File) {
        stopServerTts()
        ttsTempFile = file
        ttsMediaPlayer = MediaPlayer().apply {
            setOnCompletionListener {
                release()
                ttsMediaPlayer = null
                if (file.exists()) file.delete()
                ttsTempFile = null
            }
            setOnErrorListener { _, _, _ ->
                release()
                ttsMediaPlayer = null
                if (file.exists()) file.delete()
                ttsTempFile = null
                true
            }
            setDataSource(file.absolutePath)
            prepareAsync()
            setOnPreparedListener { start() }
        }
    }
}
