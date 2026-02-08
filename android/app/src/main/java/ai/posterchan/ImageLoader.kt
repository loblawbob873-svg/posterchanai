package ai.posterchan

import android.graphics.BitmapFactory
import android.graphics.BitmapFactory.Options
import android.widget.ImageView
import android.os.Handler
import android.os.Looper
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.security.cert.X509Certificate
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import javax.net.ssl.SSLContext
import javax.net.ssl.X509TrustManager

/**
 * Loads images from URLs and sets them on ImageViews.
 * Uses a keyed tag so RecyclerView doesn't overwrite.
 * When delayMs > 0 (image search), loads run sequentially on one thread to avoid connection/rate limits.
 * Uses trust-all SSL so proxy-image requests work with self-signed server certs (same as ApiClient.downloadClient).
 */
object ImageLoader {
    private val client: OkHttpClient by lazy {
        val trustAll = object : X509TrustManager {
            override fun checkClientTrusted(chain: Array<out X509Certificate>, authType: String) {}
            override fun checkServerTrusted(chain: Array<out X509Certificate>, authType: String) {}
            override fun getAcceptedIssuers(): Array<X509Certificate> = arrayOf()
        }
        val sslContext = SSLContext.getInstance("TLS").apply {
            init(null, arrayOf(trustAll), java.security.SecureRandom())
        }
        OkHttpClient.Builder()
            .connectTimeout(15, TimeUnit.SECONDS)
            .readTimeout(15, TimeUnit.SECONDS)
            .sslSocketFactory(sslContext.socketFactory, trustAll)
            .hostnameVerifier { _, _ -> true }
            .build()
    }
    private val executor = Executors.newFixedThreadPool(4)
    /** One thread: image-search thumbnails run one after another so all 10 can complete (no per-host limit). */
    private val sequentialExecutor = Executors.newSingleThreadExecutor()
    private val scheduler = Executors.newScheduledThreadPool(1)
    private val mainHandler = Handler(Looper.getMainLooper())

    /**
     * @param directThumbUrl If set and primary load fails, try GET this URL (no auth). Lets thumbnails load when proxy fails.
     */
    fun load(url: String, imageView: ImageView, tag: Any, delayMs: Long = 0L, onError: (() -> Unit)? = null, postBodyUrl: String? = null, postBodyThumbId: String? = null, authToken: String? = null, directThumbUrl: String? = null) {
        if (url.isBlank()) return
        imageView.setTag(R.id.image_loader_tag, tag)
        val useSequential = delayMs > 0 || url.contains("/api/proxy-image")
        val run = Runnable {
            try {
                fun tryLoad(requestUrl: String, isPost: Boolean, bodyJson: String?, token: String?): ByteArray? {
                    val builder = Request.Builder()
                        .url(requestUrl)
                        .header("User-Agent", "Mozilla/5.0 (Linux; Android 10; Mobile) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36")
                        .header("Accept", "image/*,*/*")
                    if (isPost && bodyJson != null && token != null) {
                        builder.post(bodyJson.toRequestBody("application/json".toMediaType()))
                        builder.header("Authorization", "Bearer $token")
                    }
                    val req = builder.build()
                    val response = client.newCall(req).execute()
                    if (!response.isSuccessful) return null
                    return response.body?.bytes()
                }
                val bodyJson = when {
                    !postBodyThumbId.isNullOrBlank() && !authToken.isNullOrBlank() ->
                        JSONObject().put("thumb_id", postBodyThumbId).toString()
                    !postBodyUrl.isNullOrBlank() && !authToken.isNullOrBlank() ->
                        JSONObject().put("url", postBodyUrl).toString()
                    else -> null
                }
                val isPost = bodyJson != null && authToken != null
                var bytes = tryLoad(url, isPost, bodyJson, authToken)
                if (bytes == null && !directThumbUrl.isNullOrBlank() && directThumbUrl.startsWith("http")) {
                    bytes = tryLoad(directThumbUrl, false, null, null)
                }
                if (bytes == null) {
                    mainHandler.post { if (imageView.getTag(R.id.image_loader_tag) == tag) onError?.invoke() }
                    return@Runnable
                }
                val opts = Options().apply { inSampleSize = 2 }
                var bmp = BitmapFactory.decodeByteArray(bytes, 0, bytes.size, opts)
                if (bmp == null) bmp = BitmapFactory.decodeByteArray(bytes, 0, bytes.size)
                if (bmp == null) {
                    mainHandler.post { if (imageView.getTag(R.id.image_loader_tag) == tag) onError?.invoke() }
                    return@Runnable
                }
                mainHandler.post {
                    if (imageView.getTag(R.id.image_loader_tag) == tag) {
                        imageView.setImageBitmap(bmp)
                    }
                }
            } catch (_: Exception) {
                mainHandler.post { if (imageView.getTag(R.id.image_loader_tag) == tag) onError?.invoke() }
            }
        }
        if (delayMs > 0) {
            scheduler.schedule({ sequentialExecutor.execute(run) }, delayMs, TimeUnit.MILLISECONDS)
        } else if (useSequential) {
            sequentialExecutor.execute(run)
        } else {
            executor.execute(run)
        }
    }
}
