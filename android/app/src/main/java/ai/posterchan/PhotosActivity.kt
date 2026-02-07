package ai.posterchan

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.graphics.BitmapFactory
import android.os.Bundle
import android.util.Base64
import android.view.View
import android.widget.ImageView
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.recyclerview.widget.GridLayoutManager
import androidx.recyclerview.widget.RecyclerView
import com.google.android.material.appbar.MaterialToolbar
import ai.posterchan.api.ApiClient
import ai.posterchan.api.ApiException
import ai.posterchan.api.ApiClient.FileItem
import java.io.File
import androidx.core.content.FileProvider

/**
 * Photos screen: grid of images/videos from the server "Photos" folder.
 * This is separate from File Manager so the Photos menu opens a dedicated gallery, not the file list.
 */
class PhotosActivity : AppCompatActivity() {

    private val photoExtensions = setOf(
        "jpg", "jpeg", "png", "gif", "webp", "bmp",
        "mp4", "m4v", "webm", "mov", "avi", "mkv", "3gp", "wmv"
    )

    @Volatile
    private var loadInProgress = false

    @Volatile
    private var cachedFilesBaseUrl: String? = null
    @Volatile
    private var cachedFilesBaseUrlForServer: String? = null

    private lateinit var toolbar: MaterialToolbar
    private lateinit var recycler: RecyclerView
    private lateinit var progress: View
    private lateinit var emptyText: TextView
    private lateinit var errorText: TextView
    private lateinit var adapter: PhotosAdapter

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_photos)

        toolbar = findViewById(R.id.toolbar)
        recycler = findViewById(R.id.recycler)
        progress = findViewById(R.id.progress)
        emptyText = findViewById(R.id.empty_text)
        errorText = findViewById(R.id.error_text)

        setSupportActionBar(toolbar)
        supportActionBar?.setDisplayHomeAsUpEnabled(true)
        toolbar.setNavigationOnClickListener { onBackPressedDispatcher.onBackPressed() }

        val thumbCache = mutableMapOf<String, android.graphics.Bitmap>()
        val loadThumbnail: (String, (String, android.graphics.Bitmap?) -> Unit) -> Unit = { path, onLoaded ->
            Thread {
                val baseUrl = Prefs.getServerUrl(this@PhotosActivity)
                val token = Prefs.getAccessToken(this@PhotosActivity)
                if (baseUrl.isBlank() || token.isNullOrBlank()) {
                    runOnUiThread { onLoaded(path, null) }
                    return@Thread
                }
                val client = ApiClient(baseUrl, token)
                val bytes = client.getThumbnailBytes(path, 200)
                val bmp = if (!bytes.isNullOrEmpty()) {
                    try {
                        BitmapFactory.decodeByteArray(bytes, 0, bytes.size)
                    } catch (_: Exception) {
                        null
                    }
                } else null
                runOnUiThread { onLoaded(path, bmp) }
            }.start()
        }
        adapter = PhotosAdapter(emptyList(), thumbCache, loadThumbnail) { item -> openFile(item) }
        recycler.layoutManager = GridLayoutManager(this, 3)
        recycler.adapter = adapter

        loadPhotos()
    }

    private fun getFilesBaseUrl(serverUrl: String, token: String): String {
        if (serverUrl != cachedFilesBaseUrlForServer || cachedFilesBaseUrl == null) {
            cachedFilesBaseUrl = ApiClient(serverUrl, token).getFilesConfigBaseUrl() ?: serverUrl
            cachedFilesBaseUrlForServer = serverUrl
        }
        return cachedFilesBaseUrl!!
    }

    private fun loadPhotos() {
        if (loadInProgress) return
        progress.visibility = View.VISIBLE
        emptyText.visibility = View.GONE
        errorText.visibility = View.GONE
        recycler.visibility = View.GONE
        loadInProgress = true

        Thread {
            try {
                val baseUrl = Prefs.getServerUrl(this@PhotosActivity)
                val token = Prefs.getAccessToken(this@PhotosActivity)
                if (baseUrl.isBlank()) {
                    runOnUiThread {
                        if (!isDestroyed) {
                            loadInProgress = false
                            progress.visibility = View.GONE
                            showError(R.string.file_manager_error_no_server)
                        }
                    }
                    return@Thread
                }
                if (token.isNullOrBlank()) {
                    runOnUiThread {
                        if (!isDestroyed) {
                            loadInProgress = false
                            progress.visibility = View.GONE
                            showError(R.string.file_manager_error_login)
                        }
                    }
                    return@Thread
                }
                // Use same API as web picture viewer: /api/files/all-images (all images/videos, newest first)
                val client = ApiClient(baseUrl, token)
                val response = client.getAllImages(limit = 500, offset = 0)
                // API returns newest first; keep that order, images before videos in tie-break
                val photoItems = response.images.sortedWith(
                    compareByDescending<FileItem> { it.modified }.thenBy { it.name.lowercase().endsWith(".mp4") }
                )
                runOnUiThread {
                    if (!isDestroyed) {
                        loadInProgress = false
                        progress.visibility = View.GONE
                        if (photoItems.isEmpty()) {
                            emptyText.setText(R.string.photos_empty)
                            emptyText.visibility = View.VISIBLE
                            recycler.visibility = View.GONE
                        } else {
                            emptyText.visibility = View.GONE
                            recycler.visibility = View.VISIBLE
                            adapter.updateItems(photoItems)
                        }
                    }
                }
            } catch (e: ApiException) {
                runOnUiThread {
                    if (!isDestroyed) {
                        loadInProgress = false
                        progress.visibility = View.GONE
                        if (e.code == 404) {
                            emptyText.setText(R.string.photos_empty)
                            emptyText.visibility = View.VISIBLE
                            recycler.visibility = View.GONE
                            errorText.visibility = View.GONE
                        } else {
                            val res = when (e.code) {
                                401 -> R.string.file_manager_error_login
                                else -> R.string.file_manager_error_server
                            }
                            showError(res)
                        }
                    }
                }
            } catch (e: Exception) {
                runOnUiThread {
                    if (!isDestroyed) {
                        loadInProgress = false
                        progress.visibility = View.GONE
                        showError(R.string.file_manager_error_server)
                    }
                }
            }
        }.start()
    }

    private fun showError(reasonRes: Int) {
        errorText.setText(reasonRes)
        errorText.visibility = View.VISIBLE
        recycler.visibility = View.GONE
        emptyText.visibility = View.GONE
        Toast.makeText(this, getString(reasonRes), Toast.LENGTH_LONG).show()
    }

    private fun openFile(item: FileItem) {
        Toast.makeText(this, getString(R.string.file_manager_downloading), Toast.LENGTH_SHORT).show()
        Thread {
            try {
                val baseUrl = Prefs.getServerUrl(this@PhotosActivity)
                val token = Prefs.getAccessToken(this@PhotosActivity)
                if (baseUrl.isBlank() || token.isNullOrBlank()) {
                    runOnUiThread { if (!isDestroyed) Toast.makeText(this@PhotosActivity, getString(R.string.file_manager_open_error), Toast.LENGTH_SHORT).show() }
                    return@Thread
                }
                val filesBaseUrl = getFilesBaseUrl(baseUrl, token)
                val client = ApiClient(filesBaseUrl, token)
                val cacheDir = File(cacheDir, "file_manager").apply { if (!exists()) mkdirs() }
                val safeName = item.name.replace(Regex("[\\\\/]"), "_")
                val destFile = File(cacheDir, "fm_" + System.currentTimeMillis() + "_" + safeName)
                // Use server path when present (handles case e.g. "photos" vs "Photos"); else assume Photos/
                val pathToDownload = item.path.takeIf { it.isNotBlank() } ?: "Photos/${item.name}"
                try {
                    client.downloadFileTo(pathToDownload, destFile, asAttachment = true)
                } catch (e: ApiException) {
                    val msgRes = when (e.code) {
                        401 -> R.string.file_manager_download_login
                        404 -> R.string.file_manager_download_not_found
                        else -> R.string.file_manager_download_server
                    }
                    val pathDetail = "Path: $pathToDownload" + (e.message?.takeIf { it.isNotBlank() }?.let { "\n$it" } ?: "")
                    runOnUiThread { if (!isDestroyed) showDownloadError(msgRes, e.code, pathDetail) }
                    return@Thread
                }
                if (!destFile.exists()) {
                    runOnUiThread { if (!isDestroyed) showDownloadError(R.string.file_manager_download_server, null, null) }
                    return@Thread
                }
                val ext = safeName.substringAfterLast(".", "")
                val mime = when (ext.lowercase()) {
                    "jpg", "jpeg" -> "image/jpeg"
                    "png" -> "image/png"
                    "gif" -> "image/gif"
                    "webp" -> "image/webp"
                    "mp4", "m4v" -> "video/mp4"
                    "webm" -> "video/webm"
                    "mov" -> "video/quicktime"
                    else -> "*/*"
                }
                runOnUiThread {
                    if (!isDestroyed) {
                        val uri = try {
                            FileProvider.getUriForFile(this@PhotosActivity, "${packageName}.fileprovider", destFile)
                        } catch (_: IllegalArgumentException) {
                            Toast.makeText(this@PhotosActivity, getString(R.string.file_manager_open_error), Toast.LENGTH_SHORT).show()
                            return@runOnUiThread
                        }
                        val flags = Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_WRITE_URI_PERMISSION
                        fun tryOpen(intent: Intent): Boolean = try {
                            startActivity(Intent.createChooser(intent, null)); true
                        } catch (_: Exception) { false }
                        val opened = tryOpen(Intent(Intent.ACTION_VIEW).apply { setDataAndType(uri, mime); addFlags(flags) })
                            || (if (mime.startsWith("video/")) tryOpen(Intent(Intent.ACTION_VIEW).apply { setDataAndType(uri, "video/*"); addFlags(flags) }) else false)
                            || tryOpen(Intent(Intent.ACTION_VIEW).apply { setDataAndType(uri, "*/*"); addFlags(flags) })
                            || tryOpen(Intent(Intent.ACTION_VIEW).apply { setData(uri); addFlags(flags) })
                        if (!opened) Toast.makeText(this@PhotosActivity, getString(R.string.file_manager_open_error), Toast.LENGTH_SHORT).show()
                    }
                }
            } catch (e: Exception) {
                runOnUiThread { if (!isDestroyed) showDownloadError(R.string.file_manager_download_server, null, e.message ?: e.javaClass.simpleName) }
            }
        }.start()
    }

    private fun showDownloadError(messageRes: Int, httpCode: Int?, detail: String?) {
        val msg = getString(messageRes)
        if (messageRes == R.string.file_manager_download_server) {
            val server = Prefs.getServerUrl(this).takeIf { !it.isNullOrBlank() } ?: getString(R.string.file_manager_server_not_set)
            val codeLine = if (httpCode != null) getString(R.string.file_manager_error_code, httpCode) else getString(R.string.file_manager_error_connection)
            val detailLine = if (!detail.isNullOrBlank()) "\n\n${getString(R.string.file_manager_error_detail, detail)}" else ""
            AlertDialog.Builder(this)
                .setMessage("$msg\n\n$codeLine$detailLine\n\n${getString(R.string.file_manager_current_server, server)}\n\n${getString(R.string.file_manager_server_help)}")
                .setPositiveButton(R.string.file_manager_open_settings) { _, _ -> startActivity(Intent(this, SettingsActivity::class.java)) }
                .setNeutralButton(R.string.file_manager_copy_url) { _, _ ->
                    if (server != getString(R.string.file_manager_server_not_set)) {
                        (getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager).setPrimaryClip(ClipData.newPlainText(null, server))
                        Toast.makeText(this, getString(R.string.file_manager_url_copied), Toast.LENGTH_SHORT).show()
                    }
                }
                .setNegativeButton(android.R.string.ok, null)
                .show()
        } else if (messageRes == R.string.file_manager_download_not_found && !detail.isNullOrBlank()) {
            AlertDialog.Builder(this).setTitle(R.string.file_manager_download_not_found).setMessage(detail).setPositiveButton(android.R.string.ok, null).show()
        } else {
            Toast.makeText(this, msg, Toast.LENGTH_LONG).show()
        }
    }
}

private class PhotosAdapter(
    private var items: List<FileItem>,
    private val thumbCache: MutableMap<String, android.graphics.Bitmap>,
    private val loadThumbnail: (path: String, onLoaded: (String, android.graphics.Bitmap?) -> Unit) -> Unit,
    private val onPhotoClick: (FileItem) -> Unit
) : RecyclerView.Adapter<PhotosAdapter.VH>() {

    class VH(view: View) : RecyclerView.ViewHolder(view) {
        val thumb: ImageView = view.findViewById(R.id.photo_thumb)
    }

    override fun onCreateViewHolder(parent: android.view.ViewGroup, viewType: Int): VH {
        val view = android.view.LayoutInflater.from(parent.context).inflate(R.layout.item_photo, parent, false)
        return VH(view)
    }

    override fun onBindViewHolder(holder: VH, position: Int) {
        val item = items[position]
        val path = item.path
        when {
            !item.thumbnailBase64.isNullOrBlank() -> {
                try {
                    val bytes = Base64.decode(item.thumbnailBase64, Base64.DEFAULT)
                    val bmp = BitmapFactory.decodeByteArray(bytes, 0, bytes.size)
                    holder.thumb.setImageBitmap(bmp)
                } catch (_: Exception) {
                    holder.thumb.setImageResource(R.drawable.ic_file_24)
                }
            }
            thumbCache[path] != null -> holder.thumb.setImageBitmap(thumbCache[path])
            else -> {
                holder.thumb.setImageResource(R.drawable.ic_file_24)
                loadThumbnail(path) { loadedPath, bmp ->
                    if (bmp != null) {
                        thumbCache[loadedPath] = bmp
                        val idx = items.indexOfFirst { it.path == loadedPath }
                        if (idx >= 0) notifyItemChanged(idx)
                    }
                }
            }
        }
        holder.itemView.setOnClickListener { onPhotoClick(item) }
    }

    override fun getItemCount() = items.size

    fun updateItems(newItems: List<FileItem>) {
        items = newItems
        notifyDataSetChanged()
    }
}
