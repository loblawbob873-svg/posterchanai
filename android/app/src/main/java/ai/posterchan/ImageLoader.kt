package ai.posterchan

import android.graphics.BitmapFactory
import android.os.Handler
import android.os.Looper
import android.widget.ImageView
import okhttp3.OkHttpClient
import okhttp3.Request
import java.util.concurrent.Executors

/**
 * Loads images from URLs and sets them on ImageViews.
 * Uses a tag on the ImageView to avoid applying to recycled views.
 */
object ImageLoader {
    private val client = OkHttpClient.Builder().build()
    private val executor = Executors.newFixedThreadPool(4)
    private val mainHandler = Handler(Looper.getMainLooper())

    fun load(url: String, imageView: ImageView, tag: Any) {
        if (url.isBlank()) return
        imageView.tag = tag
        executor.execute {
            try {
                val request = Request.Builder().url(url).build()
                client.newCall(request).execute().use { response ->
                    if (!response.isSuccessful) return@execute
                    val bytes = response.body?.bytes() ?: return@execute
                    val bmp = BitmapFactory.decodeByteArray(bytes, 0, bytes.size) ?: return@execute
                    mainHandler.post {
                        if (imageView.tag == tag) {
                            imageView.setImageBitmap(bmp)
                        }
                    }
                }
            } catch (_: Exception) { /* ignore */ }
        }
    }
}
