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
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

/**
 * Loads images from URLs and sets them on ImageViews.
 * Uses a keyed tag so RecyclerView doesn't overwrite.
 * When delayMs > 0 (image search), loads run sequentially on one thread to avoid connection/rate limits.
 */
object ImageLoader {
    private val client = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(15, TimeUnit.SECONDS)
        .build()
    private val executor = Executors.newFixedThreadPool(4)
    /** One thread: image-search thumbnails run one after another so all 10 can complete (no per-host limit). */
    private val sequentialExecutor = Executors.newSingleThreadExecutor()
    private val scheduler = Executors.newScheduledThreadPool(1)
    private val mainHandler = Handler(Looper.getMainLooper())

    /**
     * @param delayMs If > 0, submit to sequential executor with this start delay (stagger submit only); loads run one-by-one.
     *                When 0 and url contains proxy-image, also use sequential executor so all 10 thumbnails load reliably (no concurrent limit).
     * @param onError Optional callback on main thread when load fails (e.g. hide the ImageView).
     * @param postBodyUrl When set with authToken, use POST with JSON body {"url": postBodyUrl} (avoids GET query length limit).
     */
    fun load(url: String, imageView: ImageView, tag: Any, delayMs: Long = 0L, onError: (() -> Unit)? = null, postBodyUrl: String? = null, authToken: String? = null) {
        if (url.isBlank()) return
        imageView.setTag(R.id.image_loader_tag, tag)
        val useSequential = delayMs > 0 || url.contains("/api/proxy-image")
        val run = Runnable {
            try {
                val builder = Request.Builder()
                    .url(url)
                    .header("User-Agent", "Mozilla/5.0 (Linux; Android 10; Mobile) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36")
                    .header("Accept", "image/*,*/*")
                if (!postBodyUrl.isNullOrBlank() && !authToken.isNullOrBlank()) {
                    val body = JSONObject().put("url", postBodyUrl).toString()
                    builder.post(body.toRequestBody("application/json".toMediaType()))
                    builder.header("Authorization", "Bearer $authToken")
                } else if (!authToken.isNullOrBlank()) {
                    builder.header("Authorization", "Bearer $authToken")
                }
                val request = builder.build()
                client.newCall(request).execute().use { response ->
                    if (!response.isSuccessful) {
                        mainHandler.post { if (imageView.getTag(R.id.image_loader_tag) == tag) onError?.invoke() }
                        return@Runnable
                    }
                    val bytes = response.body?.bytes() ?: run {
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
