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
import org.json.JSONException
import org.json.JSONObject
import java.io.File
import java.net.URLEncoder
import java.nio.charset.StandardCharsets
import java.util.concurrent.TimeUnit
import javax.net.ssl.SSLContext
import javax.net.ssl.X509TrustManager
import java.security.cert.X509Certificate

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

    /** Longer timeouts and trust all certs for file downloads (works with self-signed / custom CA behind nginx). */
    private val downloadClient: OkHttpClient by lazy {
        val trustAll = object : X509TrustManager {
            override fun checkClientTrusted(chain: Array<out X509Certificate>, authType: String) {}
            override fun checkServerTrusted(chain: Array<out X509Certificate>, authType: String) {}
            override fun getAcceptedIssuers(): Array<X509Certificate> = arrayOf()
        }
        val sslContext = SSLContext.getInstance("TLS").apply {
            init(null, arrayOf(trustAll), java.security.SecureRandom())
        }
        OkHttpClient.Builder()
            .connectTimeout(30, TimeUnit.SECONDS)
            .readTimeout(15, TimeUnit.MINUTES)
            .writeTimeout(60, TimeUnit.SECONDS)
            .sslSocketFactory(sslContext.socketFactory, trustAll)
            .hostnameVerifier { _, _ -> true }
            .addInterceptor(Interceptor { chain ->
                val request = chain.request().newBuilder()
                    .addHeader("User-Agent", USER_AGENT)
                    .addHeader("Accept", "*/*")
                    .build()
                chain.proceed(request)
            })
            .build()
    }

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

    /**
     * Generate TTS audio via server (same edge_tts voice as web UI).
     * @return base64 MP3 string or null on failure
     */
    fun generateTts(text: String, voice: String? = null): String? {
        val body = JSONObject().apply {
            put("text", text)
            if (!voice.isNullOrBlank()) put("voice", voice)
        }.toString()
        val request = authRequest("/api/tts", "POST", body)
        return try {
            client.newCall(request).execute().use { response ->
                if (!response.isSuccessful) return null
                val json = JSONObject(response.body!!.string())
                json.optString("audio").takeIf { it.isNotEmpty() }
            }
        } catch (_: Exception) {
            null
        }
    }

    /**
     * Get the base URL to use for file operations (storage proxy).
     * Uses downloadClient (trust-all SSL, long timeout) so it works behind nginx/self-signed certs.
     * Returns the server's base_url, or null on failure; callers should fall back to Prefs.getServerUrl().
     */
    fun getFilesConfigBaseUrl(): String? {
        val request = authRequest("/api/files/config")
        return downloadClient.newCall(request).execute().use { response ->
            if (!response.isSuccessful) return@use null
            val body = response.body?.string() ?: return@use null
            try {
                JSONObject(body).optString("base_url").takeIf { it.isNotEmpty() }
            } catch (_: JSONException) {
                null
            }
        }
    }

    /**
     * List files and folders at the given path (empty string = root).
     * Uses downloadClient (trust-all SSL, long timeout) so file manager works behind nginx/self-signed certs.
     */
    fun listFiles(path: String): FileListResponse {
        val query = if (path.isBlank()) "" else "?path=" + URLEncoder.encode(path, StandardCharsets.UTF_8.name())
        val request = authRequest("/api/files/list$query")
        downloadClient.newCall(request).execute().use { response ->
            if (!response.isSuccessful) throw ApiException(response.code, response.body?.string() ?: response.message)
            val bodyStr = response.body?.string() ?: throw ApiException(-1, "Empty response")
            try {
                val json = JSONObject(bodyStr)
                val itemsArr = json.getJSONArray("items")
                val items = (0 until itemsArr.length()).map { i ->
                    val o = itemsArr.getJSONObject(i)
                    FileItem(
                        name = o.optString("name"),
                        path = o.optString("path"),
                        isDirectory = o.optBoolean("is_directory"),
                        size = o.optLong("size", 0L),
                        modified = o.optDouble("modified", 0.0),
                        isExternal = o.optBoolean("is_external"),
                        thumbnailBase64 = o.optString("thumbnail").takeIf { it.isNotEmpty() }
                    )
                }
                return FileListResponse(
                    items = items,
                    path = json.optString("path", ""),
                    isExternal = json.optBoolean("is_external"),
                    externalName = json.optString("external_name").takeIf { it.isNotEmpty() }
                )
            } catch (e: JSONException) {
                throw ApiException(-1, "Invalid response: ${e.message ?: "parse error"}")
            }
        }
    }

    /**
     * Fetch thumbnail image bytes for a file path (GET /api/files/thumbnail/{path}).
     * Returns null on failure or non-2xx. Use for on-demand thumbnail loading in Photos grid.
     */
    fun getThumbnailBytes(path: String, size: Int = 200): ByteArray? {
        val normalizedPath = path.trim().removeSurrounding("/")
        if (normalizedPath.isBlank()) return null
        val encodedPath = normalizedPath.split("/").joinToString("/") { segment ->
            URLEncoder.encode(segment, StandardCharsets.UTF_8.name())
        }
        val request = authRequest("/api/files/thumbnail/$encodedPath?size=$size")
        return downloadClient.newCall(request).execute().use { response ->
            if (!response.isSuccessful) return@use null
            response.body?.bytes()
        }
    }

    /**
     * Get all images/videos (same API as web picture viewer: /api/files/all-images).
     * Returns items sorted newest first; use for Photos screen to match web UI.
     */
    fun getAllImages(limit: Int = 500, offset: Int = 0): AllImagesResponse {
        val request = authRequest("/api/files/all-images?limit=$limit&offset=$offset")
        downloadClient.newCall(request).execute().use { response ->
            if (!response.isSuccessful) throw ApiException(response.code, response.body?.string() ?: response.message)
            val bodyStr = response.body?.string() ?: throw ApiException(-1, "Empty response")
            try {
                val json = JSONObject(bodyStr)
                val arr = json.optJSONArray("images") ?: JSONArray()
                val items = (0 until arr.length()).map { i ->
                    val o = arr.getJSONObject(i)
                    FileItem(
                        name = o.optString("name"),
                        path = o.optString("path"),
                        isDirectory = false,
                        size = o.optLong("size", 0L),
                        modified = o.optDouble("modified", 0.0),
                        isExternal = false,
                        thumbnailBase64 = o.optString("thumbnail").takeIf { it.isNotEmpty() }
                    )
                }
                return AllImagesResponse(
                    images = items,
                    total = json.optInt("total", items.size),
                    hasMore = json.optBoolean("has_more", false)
                )
            } catch (e: JSONException) {
                throw ApiException(-1, "Invalid response: ${e.message ?: "parse error"}")
            }
        }
    }

    /**
     * Get list of external storage mounts the user can access.
     */
    fun getExternalStorageMounts(): ExternalStorageResponse {
        val request = authRequest("/api/files/external-storage")
        client.newCall(request).execute().use { response ->
            if (!response.isSuccessful) throw ApiException(response.code, response.body?.string() ?: response.message)
            val json = JSONObject(response.body!!.string())
            val mountsArr = json.getJSONArray("mounts")
            val mounts = (0 until mountsArr.length()).map { i ->
                val o = mountsArr.getJSONObject(i)
                ExternalMount(
                    id = o.optInt("id"),
                    name = o.optString("name"),
                    mountPoint = o.optString("mount_point"),
                    description = o.optString("description").takeIf { it.isNotEmpty() },
                    mountPath = o.optString("mount_path")
                )
            }
            return ExternalStorageResponse(mounts = mounts)
        }
    }

    /**
     * Download a file to the given destination. Uses Bearer auth.
     * @param filePath Path relative to user root (e.g. "Documents/foo.pdf")
     * @param destFile Where to write the file
     * @param asAttachment If true, server returns Content-Disposition: attachment
     * @throws ApiException when response is not successful (caller can use e.code: 401=login, 404=not found)
     */
    fun downloadFileTo(filePath: String, destFile: File, asAttachment: Boolean) {
        // Normalize: trim and remove leading/trailing slashes so server path resolution is consistent
        val normalizedPath = filePath.trim().removeSurrounding("/")
        if (normalizedPath.isBlank()) {
            throw ApiException(400, "Invalid file path: empty")
        }
        val encodedPath = normalizedPath.split("/").joinToString("/") { segment ->
            URLEncoder.encode(segment, StandardCharsets.UTF_8.name())
        }
        val query = if (asAttachment) "?download=true" else ""
        val request = authRequest("/api/files/view/$encodedPath$query")
        downloadClient.newCall(request).execute().use { response ->
            if (!response.isSuccessful) {
                throw ApiException(response.code, response.body?.string() ?: response.message)
            }
            val body = response.body ?: throw ApiException(-1, "Empty response")
            body.use { b -> destFile.outputStream().use { b.byteStream().copyTo(it) } }
        }
    }

    data class TokenResponse(val accessToken: String, val tokenType: String)
    data class UserResponse(val id: Int, val username: String, val email: String?, val isAdmin: Boolean)
    data class ConversationItem(val id: Int, val title: String, val createdAt: String, val updatedAt: String)
    data class MessageItem(val id: Int, val role: String, val content: String, val imagePath: String?, val createdAt: String)
    data class FileItem(
        val name: String,
        val path: String,
        val isDirectory: Boolean,
        val size: Long,
        val modified: Double,
        val isExternal: Boolean,
        val thumbnailBase64: String?
    )
    data class FileListResponse(
        val items: List<FileItem>,
        val path: String,
        val isExternal: Boolean,
        val externalName: String?
    )
    data class AllImagesResponse(
        val images: List<FileItem>,
        val total: Int,
        val hasMore: Boolean
    )
    data class ExternalMount(
        val id: Int,
        val name: String,
        val mountPoint: String,
        val description: String?,
        val mountPath: String
    )
    data class ExternalStorageResponse(val mounts: List<ExternalMount>)
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
