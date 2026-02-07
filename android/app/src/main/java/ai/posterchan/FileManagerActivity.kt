package ai.posterchan

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.view.MenuItem
import android.view.View
import android.widget.ImageView
import android.widget.TextView
import android.widget.Toast
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.FileProvider
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView
import com.google.android.material.appbar.MaterialToolbar
import ai.posterchan.api.ApiClient
import ai.posterchan.api.ApiException
import ai.posterchan.api.ApiClient.FileItem
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * Native file manager: lists files/folders from the server and opens files locally.
 */
class FileManagerActivity : AppCompatActivity() {

    private var pathStack = mutableListOf<String>()
    private var currentPath: String
        get() = pathStack.joinToString("/").trim()
        set(value) {
            pathStack.clear()
            if (value.isNotBlank()) pathStack.addAll(value.split("/").filter { it.isNotBlank() })
        }

    @Volatile
    private var loadInProgress = false

    /** Path we opened with (e.g. "Photos"); if it doesn't exist (404), we fall back to root. */
    private var initialPath = ""

    /** Cached storage-proxy base URL from GET /api/files/config; invalidated when server URL changes. */
    @Volatile
    private var cachedFilesBaseUrl: String? = null
    @Volatile
    private var cachedFilesBaseUrlForServer: String? = null

    private lateinit var toolbar: MaterialToolbar
    private lateinit var recycler: RecyclerView
    private lateinit var progress: View
    private lateinit var emptyText: TextView
    private lateinit var errorText: TextView
    private lateinit var adapter: FileManagerAdapter

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_file_manager)

        toolbar = findViewById<MaterialToolbar>(R.id.toolbar)
        recycler = findViewById<RecyclerView>(R.id.recycler)
        progress = findViewById<View>(R.id.progress)
        emptyText = findViewById<TextView>(R.id.empty_text)
        errorText = findViewById<TextView>(R.id.error_text)

        setSupportActionBar(toolbar)
        supportActionBar?.setDisplayHomeAsUpEnabled(true)
        toolbar.setNavigationOnClickListener { onBackPressedDispatcher.onBackPressed() }

        initialPath = intent.getStringExtra(EXTRA_INITIAL_PATH) ?: ""
        currentPath = initialPath

        adapter = FileManagerAdapter(
            items = emptyList(),
            onFolderClick = { path -> navigateInto(path) },
            onFileClick = { item -> openFile(item) }
        )
        recycler.layoutManager = LinearLayoutManager(this)
        recycler.adapter = adapter

        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (pathStack.isNotEmpty()) {
                    pathStack.removeAt(pathStack.lastIndex)
                    loadList()
                } else {
                    finish()
                }
            }
        })

        cleanupOldCacheFiles()
        loadList()
    }

    override fun onOptionsItemSelected(item: MenuItem): Boolean {
        return when (item.itemId) {
            R.id.action_refresh -> {
                loadList(ignoreIfLoading = true)
                true
            }
            else -> super.onOptionsItemSelected(item)
        }
    }

    /** Delete file-manager cache files older than 24 hours. */
    private fun cleanupOldCacheFiles() {
        Thread {
            try {
                val maxAgeMs = 24 * 60 * 60 * 1000L
                val now = System.currentTimeMillis()
                val dir = File(cacheDir, "file_manager")
                if (dir.isDirectory) {
                    dir.listFiles()?.filter { it.name.startsWith("fm_") && (now - it.lastModified()) > maxAgeMs }
                        ?.forEach { it.delete() }
                }
            } catch (_: Exception) { /* ignore */ }
        }.start()
    }

    private fun navigateInto(relativePath: String) {
        val nextPath = if (currentPath.isBlank()) relativePath else "$currentPath/$relativePath"
        pathStack.clear()
        pathStack.addAll(nextPath.split("/").filter { it.isNotBlank() })
        loadList()
    }

    /**
     * @param ignoreIfLoading If true, does nothing when a load is already in progress (e.g. from Refresh).
     */
    private fun loadList(ignoreIfLoading: Boolean = false) {
        if (ignoreIfLoading && loadInProgress) return
        updateToolbarTitle()
        progress.visibility = View.VISIBLE
        emptyText.visibility = View.GONE
        errorText.visibility = View.GONE
        recycler.visibility = View.GONE

        val path = currentPath
        loadInProgress = true
        Thread {
            try {
                val baseUrl = Prefs.getServerUrl(this@FileManagerActivity)
                val token = Prefs.getAccessToken(this@FileManagerActivity)
                if (baseUrl.isBlank()) {
                    runOnUiThread {
                        if (!isDestroyed) {
                            loadInProgress = false
                            if (path == initialPath && initialPath.isNotBlank()) {
                                pathStack.clear()
                                loadList()
                            } else {
                                showError(R.string.file_manager_error_no_server)
                            }
                        }
                    }
                    return@Thread
                }
                if (token.isNullOrBlank()) {
                    runOnUiThread {
                        if (!isDestroyed) {
                            loadInProgress = false
                            if (path == initialPath && initialPath.isNotBlank()) {
                                pathStack.clear()
                                loadList()
                            } else {
                                showError(R.string.file_manager_error_login)
                            }
                        }
                    }
                    return@Thread
                }
                // Use storage proxy URL for file operations (from GET /api/files/config), cached per session
                val filesBaseUrl = getFilesBaseUrl(baseUrl, token)
                val client = ApiClient(filesBaseUrl, token)
                val response = client.listFiles(path)
                runOnUiThread {
                    if (!isDestroyed) {
                        loadInProgress = false
                        progress.visibility = View.GONE
                        if (response.items.isEmpty()) {
                            emptyText.visibility = View.VISIBLE
                            recycler.visibility = View.GONE
                        } else {
                            emptyText.visibility = View.GONE
                            recycler.visibility = View.VISIBLE
                            val sorted = response.items.sortedWith(
                                compareBy<FileItem> { !it.isDirectory }.thenBy { it.name.lowercase() }
                            )
                            adapter.updateItems(sorted)
                        }
                    }
                }
            } catch (e: ApiException) {
                runOnUiThread {
                    if (!isDestroyed) {
                        loadInProgress = false
                        progress.visibility = View.GONE
                        if (path == initialPath && initialPath.isNotBlank()) {
                            pathStack.clear()
                            loadList()
                        } else {
                            val reasonRes = when (e.code) {
                                401 -> R.string.file_manager_error_login
                                else -> R.string.file_manager_error_server
                            }
                            showError(reasonRes)
                        }
                    }
                }
            } catch (e: Exception) {
                runOnUiThread {
                    if (!isDestroyed) {
                        loadInProgress = false
                        progress.visibility = View.GONE
                        if (path == initialPath && initialPath.isNotBlank()) {
                            pathStack.clear()
                            loadList()
                        } else {
                            showError(R.string.file_manager_error_server)
                        }
                    }
                }
            }
        }.start()
    }

    private fun updateToolbarTitle() {
        supportActionBar?.title = when {
            currentPath.isBlank() && initialPath == "Photos" -> getString(R.string.files_photos)
            currentPath.isBlank() -> getString(R.string.file_manager_title)
            else -> currentPath.substringAfterLast("/").ifBlank { currentPath }
        }
    }

    /** Returns the base URL to use for file API (storage proxy); caches result for the current server URL. */
    private fun getFilesBaseUrl(serverUrl: String, token: String): String {
        if (serverUrl != cachedFilesBaseUrlForServer || cachedFilesBaseUrl == null) {
            cachedFilesBaseUrl = ApiClient(serverUrl, token).getFilesConfigBaseUrl() ?: serverUrl
            cachedFilesBaseUrlForServer = serverUrl
        }
        return cachedFilesBaseUrl!!
    }

    private fun showError(reasonRes: Int = R.string.file_manager_error) {
        val msg = getString(reasonRes)
        errorText.text = msg
        errorText.visibility = View.VISIBLE
        recycler.visibility = View.GONE
        emptyText.visibility = View.GONE
        Toast.makeText(this, msg, Toast.LENGTH_LONG).show()
    }

    private fun openFile(item: FileItem) {
        Toast.makeText(this, getString(R.string.file_manager_downloading), Toast.LENGTH_SHORT).show()
        Thread {
            try {
                val baseUrl = Prefs.getServerUrl(this@FileManagerActivity)
                val token = Prefs.getAccessToken(this@FileManagerActivity)
                if (baseUrl.isBlank() || token.isNullOrBlank()) {
                    runOnUiThread { if (!isDestroyed) Toast.makeText(this@FileManagerActivity, getString(R.string.file_manager_open_error), Toast.LENGTH_SHORT).show() }
                    return@Thread
                }
                // Use storage proxy URL for file operations (from GET /api/files/config), cached per session
                val filesBaseUrl = getFilesBaseUrl(baseUrl, token)
                val client = ApiClient(filesBaseUrl, token)
                val fmCacheDir = File(cacheDir, "file_manager").apply { if (!exists()) mkdirs() }
                val safeName = item.name.replace(Regex("[\\\\/]"), "_")
                val destFile = File(fmCacheDir, "fm_" + System.currentTimeMillis() + "_" + safeName)
                // Build path from current folder + filename so it always matches what we're viewing (avoids 404 from path mismatch)
                val pathToDownload = if (currentPath.isBlank()) item.path else "$currentPath/${item.name}"
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
                    "pdf" -> "application/pdf"
                    "jpg", "jpeg" -> "image/jpeg"
                    "png" -> "image/png"
                    "gif" -> "image/gif"
                    "webp" -> "image/webp"
                    "txt" -> "text/plain"
                    "html", "htm" -> "text/html"
                    "json" -> "application/json"
                    "mp3" -> "audio/mpeg"
                    "m4a", "aac" -> "audio/mp4"
                    "ogg", "oga" -> "audio/ogg"
                    "wav" -> "audio/wav"
                    "flac" -> "audio/flac"
                    "opus" -> "audio/opus"
                    "mp4", "m4v" -> "video/mp4"
                    "avi" -> "video/x-msvideo"
                    "mkv" -> "video/x-matroska"
                    "webm" -> "video/webm"
                    "mov" -> "video/quicktime"
                    "3gp" -> "video/3gpp"
                    "wmv" -> "video/x-ms-wmv"
                    else -> "*/*"
                }
                runOnUiThread {
                    if (!isDestroyed) {
                        val uri = try {
                            FileProvider.getUriForFile(
                                this@FileManagerActivity,
                                "${packageName}.fileprovider",
                                destFile
                            )
                        } catch (_: IllegalArgumentException) {
                            Toast.makeText(this@FileManagerActivity, getString(R.string.file_manager_open_error), Toast.LENGTH_SHORT).show()
                            return@runOnUiThread
                        }
                        val flags = Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_WRITE_URI_PERMISSION
                        fun tryOpen(intent: Intent): Boolean {
                            return try {
                                startActivity(Intent.createChooser(intent, null))
                                true
                            } catch (_: Exception) {
                                false
                            }
                        }
                        val opened = tryOpen(Intent(Intent.ACTION_VIEW).apply { setDataAndType(uri, mime); addFlags(flags) })
                            || (if (mime.startsWith("video/")) tryOpen(Intent(Intent.ACTION_VIEW).apply { setDataAndType(uri, "video/*"); addFlags(flags) }) else false)
                            || (if (mime.startsWith("audio/")) tryOpen(Intent(Intent.ACTION_VIEW).apply { setDataAndType(uri, "audio/*"); addFlags(flags) }) else false)
                            || tryOpen(Intent(Intent.ACTION_VIEW).apply { setDataAndType(uri, "*/*"); addFlags(flags) })
                            || tryOpen(Intent(Intent.ACTION_VIEW).apply { setData(uri); addFlags(flags) })
                        if (!opened) {
                            Toast.makeText(this@FileManagerActivity, getString(R.string.file_manager_open_error), Toast.LENGTH_SHORT).show()
                        }
                    }
                }
            } catch (e: Exception) {
                val detail = e.message ?: e.javaClass.simpleName
                runOnUiThread { if (!isDestroyed) showDownloadError(R.string.file_manager_download_server, null, detail) }
            }
        }.start()
    }

    private fun showDownloadError(messageRes: Int, httpCode: Int? = null, detail: String? = null) {
        val msg = getString(messageRes)
        if (messageRes == R.string.file_manager_download_server) {
            val server = Prefs.getServerUrl(this).takeIf { !it.isNullOrBlank() } ?: getString(R.string.file_manager_server_not_set)
            val codeLine = when {
                httpCode != null -> getString(R.string.file_manager_error_code, httpCode)
                else -> getString(R.string.file_manager_error_connection)
            }
            val detailLine = if (!detail.isNullOrBlank()) "\n\n${getString(R.string.file_manager_error_detail, detail)}" else ""
            val help = getString(R.string.file_manager_server_help)
            val fullMsg = "$msg\n\n$codeLine$detailLine\n\n${getString(R.string.file_manager_current_server, server)}\n\n$help"
            AlertDialog.Builder(this)
                .setMessage(fullMsg)
                .setPositiveButton(R.string.file_manager_open_settings) { _, _ ->
                    startActivity(Intent(this, SettingsActivity::class.java))
                }
                .setNeutralButton(R.string.file_manager_copy_url) { _, _ ->
                    if (server != getString(R.string.file_manager_server_not_set)) {
                        (getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager)
                            .setPrimaryClip(ClipData.newPlainText(null, server))
                        Toast.makeText(this, getString(R.string.file_manager_url_copied), Toast.LENGTH_SHORT).show()
                    }
                }
                .setNegativeButton(android.R.string.ok, null)
                .show()
        } else if (messageRes == R.string.file_manager_download_not_found && !detail.isNullOrBlank()) {
            AlertDialog.Builder(this)
                .setTitle(R.string.file_manager_download_not_found)
                .setMessage(detail)
                .setPositiveButton(android.R.string.ok, null)
                .show()
        } else {
            Toast.makeText(this, msg, Toast.LENGTH_LONG).show()
        }
    }

    companion object {
        const val EXTRA_INITIAL_PATH = "initial_path"
    }
}

private class FileManagerAdapter(
    private var items: List<FileItem>,
    private val onFolderClick: (String) -> Unit,
    private val onFileClick: (FileItem) -> Unit
) : RecyclerView.Adapter<FileManagerAdapter.VH>() {

    class VH(view: View) : RecyclerView.ViewHolder(view) {
        val icon: ImageView = view.findViewById<ImageView>(R.id.item_icon)
        val name: TextView = view.findViewById<TextView>(R.id.item_name)
        val sub: TextView = view.findViewById<TextView>(R.id.item_sub)
    }

    override fun onCreateViewHolder(parent: android.view.ViewGroup, viewType: Int): VH {
        val view = android.view.LayoutInflater.from(parent.context)
            .inflate(R.layout.item_file_manager, parent, false)
        return VH(view)
    }

    override fun onBindViewHolder(holder: VH, position: Int) {
        val item = items[position]
        holder.name.text = item.name
        holder.icon.setImageResource(
            if (item.isDirectory) R.drawable.ic_folder_24 else R.drawable.ic_file_24
        )
        holder.sub.text = when {
            item.isDirectory -> holder.itemView.context.getString(R.string.file_manager_folder_subtitle)
            else -> formatSize(item.size) + " · " + formatDate(item.modified)
        }
        holder.itemView.setOnClickListener {
            if (item.isDirectory) onFolderClick(item.name) else onFileClick(item)
        }
    }

    override fun getItemCount() = items.size

    fun updateItems(newItems: List<FileItem>) {
        items = newItems
        notifyDataSetChanged()
    }

    private fun formatSize(bytes: Long): String {
        if (bytes < 1024) return "${bytes} B"
        if (bytes < 1024 * 1024) return "%.1f KB".format(Locale.US, bytes / 1024.0)
        if (bytes < 1024 * 1024 * 1024) return "%.1f MB".format(Locale.US, bytes / (1024.0 * 1024))
        return "%.1f GB".format(Locale.US, bytes / (1024.0 * 1024 * 1024))
    }

    private fun formatDate(timestamp: Double): String {
        if (timestamp <= 0) return ""
        return try {
            SimpleDateFormat("MMM d, yyyy", Locale.getDefault())
                .format(Date((timestamp * 1000).toLong()))
        } catch (_: Exception) {
            ""
        }
    }
}
