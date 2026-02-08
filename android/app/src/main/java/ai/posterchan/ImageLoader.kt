package ai.posterchan

import android.graphics.BitmapFactory
import android.graphics.BitmapFactory.Options
import android.widget.ImageView
import android.os.Handler
import android.os.Looper
import okhttp3.OkHttpClient
import okhttp3.Request
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
     */
    fun load(url: String, imageView: ImageView, tag: Any, delayMs: Long = 0L) {
        if (url.isBlank()) return
        imageView.setTag(R.id.image_loader_tag, tag)
        val useSequential = delayMs > 0 || url.contains("/api/proxy-image")
        val run = Runnable {
            try {
                val request = Request.Builder()
                    .url(url)
                    .header("User-Agent", "Mozilla/5.0 (Linux; Android 10; Mobile) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36")
                    .header("Accept", "image/*,*/*")
                    .build()
                client.newCall(request).execute().use { response ->
                    if (!response.isSuccessful) return@Runnable
                    val bytes = response.body?.bytes() ?: return@Runnable
                    val opts = Options().apply { inSampleSize = 2 }
                    var bmp = BitmapFactory.decodeByteArray(bytes, 0, bytes.size, opts)
                    if (bmp == null) bmp = BitmapFactory.decodeByteArray(bytes, 0, bytes.size)
                    mainHandler.post {
                        if (imageView.getTag(R.id.image_loader_tag) == tag) {
                            imageView.setImageBitmap(bmp)
                        }
                    }
                }
            } catch (_: Exception) { /* ignore */ }
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
