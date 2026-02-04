package ai.posterchan.api

import android.util.Log
import okhttp3.Interceptor
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/**
 * REST + WebSocket client for Poster-chan AI backend.
 * All methods are synchronous; call from a background thread and post to UI.
 */
class ApiClient(
    private val baseUrl: String,
    private val token: String?
) {
    private val client = OkHttpClient.Builder()
        .connectTimeout(30, TimeUnit.SECONDS)
        .readTimeout(60, TimeUnit.SECONDS)
        .writeTimeout(60, TimeUnit.SECONDS)
        .addInterceptor(Interceptor { chain ->
            val request = chain.request().newBuilder()
                .addHeader("User-Agent", USER_AGENT)
                .addHeader("Accept", "application/json")
                .build()
            chain.proceed(request)
        })
        .build()

    companion object {
        /** Browser-like User-Agent; no app suffix to avoid 403 from proxies that block PosterchanAI UA on /ws/chat. */
        private const val USER_AGENT =
            "Mozilla/5.0 (Linux; Android 10; Mobile) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
    }

    private fun url(path: String): String {
        val base = baseUrl.trimEnd('/')
        val p = if (path.startsWith("/")) path else "/$path"
        return "$base$p"
    }

    private fun authRequest(path: String, method: String = "GET", body: String? = null): Request {
        val builder = Request.Builder().url(url(path))
        if (!token.isNullOrBlank()) {
            builder.addHeader("Authorization", "Bearer $token")
        }
        when (method) {
            "GET" -> { }
            "POST" -> builder.post((body ?: "{}").toRequestBody("application/json; charset=utf-8".toMediaType()))
            "DELETE" -> builder.delete()
            else -> { }
        }
        return builder.build()
    }

    fun login(username: String, password: String): TokenResponse {
        val body = JSONObject().apply {
            put("username", username)
            put("password", password)
        }.toString()
        val request = Request.Builder()
            .url(url("/api/auth/login"))
            .post(body.toRequestBody("application/json; charset=utf-8".toMediaType()))
            .build()
        client.newCall(request).execute().use { response ->
            if (!response.isSuccessful) {
                val err = response.body?.string() ?: response.message
                throw ApiException(response.code, err)
            }
            val json = JSONObject(response.body!!.string())
            return TokenResponse(
                accessToken = json.optString("access_token"),
                tokenType = json.optString("token_type", "bearer")
            )
        }
    }

    fun getMe(): UserResponse {
        val request = authRequest("/api/auth/me")
        client.newCall(request).execute().use { response ->
            if (!response.isSuccessful) throw ApiException(response.code, response.body?.string() ?: response.message)
            val json = JSONObject(response.body!!.string())
            return UserResponse(
                id = json.optInt("id"),
                username = json.optString("username"),
                email = json.optString("email").takeIf { it.isNotEmpty() },
                isAdmin = json.optBoolean("is_admin")
            )
        }
    }

    fun getConversations(): List<ConversationItem> {
        val request = authRequest("/api/conversations")
        client.newCall(request).execute().use { response ->
            if (!response.isSuccessful) throw ApiException(response.code, response.body?.string() ?: response.message)
            val arr = JSONArray(response.body!!.string())
            return (0 until arr.length()).map { i ->
                val o = arr.getJSONObject(i)
                ConversationItem(
                    id = o.optInt("id"),
                    title = o.optString("title"),
                    createdAt = o.optString("created_at"),
                    updatedAt = o.optString("updated_at")
                )
            }
        }
    }

    fun createConversation(title: String = "New Chat"): ConversationItem {
        val body = JSONObject().apply { put("title", title) }.toString()
        val request = authRequest("/api/conversations", "POST", body)
        client.newCall(request).execute().use { response ->
            if (!response.isSuccessful) throw ApiException(response.code, response.body?.string() ?: response.message)
            val o = JSONObject(response.body!!.string())
            return ConversationItem(
                id = o.optInt("id"),
                title = o.optString("title"),
                createdAt = o.optString("created_at"),
                updatedAt = o.optString("updated_at")
            )
        }
    }

    fun getMessages(conversationId: Int): List<MessageItem> {
        val request = authRequest("/api/conversations/$conversationId/messages")
        client.newCall(request).execute().use { response ->
            if (!response.isSuccessful) throw ApiException(response.code, response.body?.string() ?: response.message)
            val arr = JSONArray(response.body!!.string())
            return (0 until arr.length()).map { i ->
                val o = arr.getJSONObject(i)
                MessageItem(
                    id = o.optInt("id"),
                    role = o.optString("role"),
                    content = o.optString("content"),
                    imagePath = o.optString("image_path").takeIf { it.isNotEmpty() },
                    createdAt = o.optString("created_at")
                )
            }
        }
    }

    fun deleteConversation(conversationId: Int) {
        val request = authRequest("/api/conversations/$conversationId", "DELETE")
        client.newCall(request).execute().use { response ->
            if (!response.isSuccessful) throw ApiException(response.code, response.body?.string() ?: response.message)
        }
    }

    /**
     * Connect WebSocket for chat. Pass token in query so server authenticates.
     * Listener callbacks may be invoked on a background thread; post to main thread if updating UI.
     */
    fun connectChatWebSocket(
        conversationId: Int,
        listener: ChatWebSocketListener
    ): WebSocket {
        val wsUrl = url("/ws/chat/$conversationId") + if (!token.isNullOrBlank()) "?token=${java.net.URLEncoder.encode(token, "UTF-8")}" else ""
        val request = Request.Builder().url(wsUrl).build()
        return client.newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: okhttp3.Response) {
                listener.onOpen()
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                try {
                    val json = JSONObject(text)
                    val type = json.optString("type")
                    when (type) {
                        "stream" -> {
                            val data = json.optJSONObject("data")
                            val content = data?.optString("content") ?: ""
                            if (content.isNotEmpty()) listener.onStreamChunk(content)
                        }
                        "stream_end" -> listener.onStreamEnd()
                        "response" -> {
                            val data = json.optJSONObject("data")
                            val content = data?.optString("content") ?: ""
                            listener.onResponse(content, data)
                        }
                        "error" -> listener.onError(json.optString("message", "Unknown error"))
                    }
                } catch (e: Exception) {
                    Log.e("ApiClient", "WebSocket message parse error", e)
                    listener.onError(e.message ?: "Parse error")
                }
            }

            override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {}
            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                listener.onClosed()
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: okhttp3.Response?) {
                listener.onError(t.message ?: "Connection failed")
            }
        })
    }

    fun sendChatMessage(webSocket: WebSocket, content: String) {
        sendChatMessage(webSocket, content, imageData = null, pdfData = null, fileContent = null)
    }

    /**
     * Send a chat message with optional attachments (matches web UI payload).
     */
    fun sendChatMessage(
        webSocket: WebSocket,
        content: String,
        imageData: String? = null,
        pdfData: String? = null,
        fileContent: String? = null
    ) {
        val json = JSONObject().apply {
            put("type", "message")
            put("content", content)
            if (!imageData.isNullOrBlank()) put("image_data", imageData)
            if (!pdfData.isNullOrBlank()) put("pdf_data", pdfData)
            if (!fileContent.isNullOrBlank()) put("file_content", fileContent)
        }.toString()
        webSocket.send(json)
    }

    fun sendStop(webSocket: WebSocket) {
        webSocket.send(JSONObject().apply { put("type", "stop") }.toString())
    }

    data class TokenResponse(val accessToken: String, val tokenType: String)
    data class UserResponse(val id: Int, val username: String, val email: String?, val isAdmin: Boolean)
    data class ConversationItem(val id: Int, val title: String, val createdAt: String, val updatedAt: String)
    data class MessageItem(val id: Int, val role: String, val content: String, val imagePath: String?, val createdAt: String)
}

interface ChatWebSocketListener {
    fun onOpen() {}
    fun onStreamChunk(chunk: String) {}
    fun onStreamEnd() {}
    fun onResponse(fullContent: String, data: JSONObject?) {}
    fun onError(message: String) {}
    fun onClosed() {}
}

class ApiException(val code: Int, message: String) : Exception("API error $code: $message")
