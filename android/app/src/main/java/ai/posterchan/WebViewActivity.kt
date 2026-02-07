package ai.posterchan

import android.annotation.SuppressLint
import android.content.Intent
import android.graphics.Bitmap
import android.net.Uri
import android.os.Bundle
import android.os.Environment
import android.view.View
import android.webkit.CookieManager
import android.webkit.DownloadListener
import android.webkit.JavascriptInterface
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.ProgressBar
import android.widget.Toast
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.FileProvider
import com.google.android.material.appbar.MaterialToolbar
import ai.posterchan.api.ApiClient
import ai.posterchan.api.ApiException
import java.io.File
import java.net.URLDecoder
import java.nio.charset.StandardCharsets
import java.lang.ref.WeakReference

/**
 * Full web UI in a WebView. Injects access_token cookie so user is logged in.
 * Same-origin navigation; external links open in browser. Battery-friendly (pause/resume).
 */
class WebViewActivity : AppCompatActivity() {

    companion object {
        const val EXTRA_INITIAL_COMMAND = "ai.posterchan.extra.INITIAL_COMMAND"
    }

    private lateinit var webView: WebView
    private lateinit var progressBar: ProgressBar
    private var pendingInitialCommand: String? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_webview)

        webView = findViewById(R.id.webview)
        progressBar = findViewById(R.id.progress_bar)

        findViewById<MaterialToolbar>(R.id.toolbar)?.let {
            setSupportActionBar(it)
            it.setNavigationOnClickListener { onBackPressedDispatcher.onBackPressed() }
        }
        supportActionBar?.setDisplayHomeAsUpEnabled(true)
        supportActionBar?.title = getString(R.string.web_app)

        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (webView.canGoBack()) webView.goBack() else finish()
            }
        })

        pendingInitialCommand = intent.getStringExtra(EXTRA_INITIAL_COMMAND)?.takeIf { it.isNotBlank() }
        setupWebView()
        loadUrl()
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun setupWebView() {
        webView.setLayerType(View.LAYER_TYPE_HARDWARE, null)
        webView.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            databaseEnabled = true
            cacheMode = android.webkit.WebSettings.LOAD_DEFAULT
            mixedContentMode = android.webkit.WebSettings.MIXED_CONTENT_COMPATIBILITY_MODE
            allowFileAccess = false
            setGeolocationEnabled(false)
            javaScriptCanOpenWindowsAutomatically = false
            loadWithOverviewMode = true
            useWideViewPort = true
            builtInZoomControls = false
            displayZoomControls = false
            setSupportZoom(true)
            // Use default Chrome mobile UA only; appending "PosterchanAI/1.0" can trigger 403 from some proxies when opening /ws/chat
            safeBrowsingEnabled = false
        }
        CookieManager.getInstance().apply {
            setAcceptThirdPartyCookies(webView, true)
        }

        webView.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(
                view: WebView?,
                request: WebResourceRequest?
            ): Boolean {
                val url = request?.url ?: return false
                val urlStr = url.toString()
                if (!urlStr.startsWith("http://") && !urlStr.startsWith("https://")) return false
                if (isSameOrigin(urlStr)) {
                    // Intercept file view URLs (torrent download, file manager Download) and handle with auth
                    val filePath = extractPathFromFileViewUrl(urlStr)
                    if (filePath != null) {
                        startFileDownload(filePath)
                        return true
                    }
                    view?.loadUrl(urlStr)
                    return true
                }
                try {
                    startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(urlStr)))
                } catch (_: Exception) {
                    Toast.makeText(this@WebViewActivity, getString(R.string.cannot_open_link), Toast.LENGTH_SHORT).show()
                }
                return true
            }

            override fun onPageStarted(view: WebView?, url: String?, favicon: Bitmap?) {
                progressBar.visibility = View.VISIBLE
            }

            override fun onPageFinished(view: WebView?, url: String?) {
                progressBar.visibility = View.GONE
                pendingInitialCommand?.let { cmd ->
                    pendingInitialCommand = null
                    injectAndSendCommand(view, cmd)
                }
            }

            override fun onReceivedError(
                view: WebView?,
                request: WebResourceRequest?,
                error: WebResourceError?
            ) {
                progressBar.visibility = View.GONE
                if (request?.isForMainFrame == true && error?.errorCode != -2) {
                    val msg = error?.description?.toString()?.takeIf { it.isNotBlank() }
                        ?: getString(R.string.load_error)
                    Toast.makeText(this@WebViewActivity, msg, Toast.LENGTH_LONG).show()
                }
            }

            override fun onReceivedHttpError(
                view: WebView?,
                request: WebResourceRequest?,
                errorResponse: WebResourceResponse?
            ) {
                if (request?.isForMainFrame == true) {
                    progressBar.visibility = View.GONE
                    val code = errorResponse?.statusCode ?: 0
                    val msg = if (code > 0) {
                        getString(R.string.load_error) + " (HTTP $code)"
                    } else {
                        getString(R.string.load_error)
                    }
                    Toast.makeText(this@WebViewActivity, msg, Toast.LENGTH_LONG).show()
                }
            }
        }

        webView.webChromeClient = object : WebChromeClient() {
            override fun onProgressChanged(view: WebView?, newProgress: Int) {
                if (newProgress >= 90) progressBar.visibility = View.GONE
            }
        }

        // Handle file downloads (e.g. torrents list Download button, file manager Download)
        // Same-origin /api/files/view/... needs auth → use ApiClient; external → system download
        webView.setDownloadListener(object : DownloadListener {
            override fun onDownloadStart(
                url: String?,
                userAgent: String?,
                contentDisposition: String?,
                mimeType: String?,
                contentLength: Long
            ) {
                val urlStr = url ?: return
                if (isSameOrigin(urlStr)) {
                    val filePath = extractPathFromFileViewUrl(urlStr)
                    if (filePath != null) {
                        val fileName = parseFileNameFromContentDisposition(contentDisposition)
                            ?: filePath.substringAfterLast("/").ifBlank { "download" }
                        startFileDownload(filePath, fileName, mimeType)
                    } else {
                        runOnUiThread { Toast.makeText(this@WebViewActivity, getString(R.string.file_manager_download_failed), Toast.LENGTH_SHORT).show() }
                    }
                } else {
                    try {
                        startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(urlStr)))
                    } catch (_: Exception) {
                        runOnUiThread { Toast.makeText(this@WebViewActivity, getString(R.string.cannot_open_link), Toast.LENGTH_SHORT).show() }
                    }
                }
            }
        })

        // Let the web trigger downloads directly (torrent Download button uses this when link click doesn't navigate)
        webView.addJavascriptInterface(WebViewDownloadBridge(this), "PosterchanAndroid")
    }

    /** Called from WebViewDownloadBridge when the web calls PosterchanAndroid.downloadFile(path, name). */
    fun requestFileDownload(filePath: String, fileName: String?) {
        val path = filePath.trim()
        if (path.isBlank()) {
            Toast.makeText(this, getString(R.string.file_manager_download_failed), Toast.LENGTH_SHORT).show()
            return
        }
        startFileDownload(path, fileName?.trim()?.takeIf { it.isNotBlank() }, null)
    }

    /** Start authenticated download for same-origin file (torrent download button, file manager). */
    private fun startFileDownload(
        filePath: String,
        fileName: String? = null,
        mimeType: String? = null
    ) {
        val safeName = (fileName ?: filePath.substringAfterLast("/").ifBlank { "download" })
            .replace(Regex("[\\\\/:*?\"<>|]"), "_")
        Toast.makeText(this, getString(R.string.file_manager_downloading), Toast.LENGTH_SHORT).show()
        Thread {
            try {
                val baseUrl = Prefs.getServerUrl(this@WebViewActivity)
                val token = Prefs.getAccessToken(this@WebViewActivity)
                if (baseUrl.isBlank() || token.isNullOrBlank()) {
                    runOnUiThread { Toast.makeText(this@WebViewActivity, getString(R.string.file_manager_download_login), Toast.LENGTH_LONG).show() }
                    return@Thread
                }
                val dir = getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS) ?: cacheDir
                val destFile = File(dir, "web_${System.currentTimeMillis()}_$safeName")
                val client = ApiClient(baseUrl, token)
                client.downloadFileTo(filePath, destFile, asAttachment = true)
                if (destFile.exists()) {
                    runOnUiThread {
                        Toast.makeText(this@WebViewActivity, "Downloaded: $safeName", Toast.LENGTH_SHORT).show()
                        try {
                            val uri = FileProvider.getUriForFile(this@WebViewActivity, "${packageName}.fileprovider", destFile)
                            startActivity(Intent(Intent.ACTION_VIEW).apply {
                                setDataAndType(uri, mimeType ?: "*/*")
                                addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                            })
                        } catch (_: Exception) { }
                    }
                } else {
                    runOnUiThread { Toast.makeText(this@WebViewActivity, getString(R.string.file_manager_download_failed), Toast.LENGTH_LONG).show() }
                }
            } catch (e: ApiException) {
                val msg = when (e.code) {
                    401 -> getString(R.string.file_manager_download_login)
                    404 -> getString(R.string.file_manager_download_not_found)
                    else -> getString(R.string.file_manager_download_server)
                }
                runOnUiThread { Toast.makeText(this@WebViewActivity, msg, Toast.LENGTH_LONG).show() }
            } catch (e: Exception) {
                runOnUiThread { Toast.makeText(this@WebViewActivity, getString(R.string.file_manager_download_failed) + " " + (e.message ?: ""), Toast.LENGTH_LONG).show() }
            }
        }.start()
    }

    /** Extract path from /api/files/view/{path} URL (decoded). */
    private fun extractPathFromFileViewUrl(urlStr: String): String? {
        return try {
            val uri = Uri.parse(urlStr)
            val path = uri.path ?: return null
            if (!path.contains("/api/files/view/")) return null
            val encoded = path.substringAfter("/api/files/view/").trim('/')
            if (encoded.isBlank()) return null
            URLDecoder.decode(encoded, StandardCharsets.UTF_8.name())
        } catch (_: Exception) {
            null
        }
    }

    private fun parseFileNameFromContentDisposition(contentDisposition: String?): String? {
        if (contentDisposition.isNullOrBlank()) return null
        val filename = Regex("filename[*]?=(?:\"?)([^\";]+)").find(contentDisposition)?.groupValues?.get(1)?.trim()
        return filename?.takeIf { it.isNotBlank() }
    }

    /** Run a chat command in the loaded web page (torrents, nyaa, etc.) after a short delay so chatHandler is ready. */
    private fun injectAndSendCommand(view: WebView?, command: String) {
        view ?: return
        val escaped = command.replace("\\", "\\\\").replace("'", "\\'").replace("\r", "").replace("\n", " ")
        val js = """
            (function(){
                function run() {
                    var c = window.chatHandler;
                    if (c && c.messageInput && typeof c.sendMessage === 'function') {
                        c.messageInput.value = '$escaped';
                        c.sendMessage();
                        return true;
                    }
                    return false;
                }
                if (run()) return;
                setTimeout(function(){ run(); }, 1500);
            })();
        """.trimIndent()
        view.postDelayed({ view.evaluateJavascript(js, null) }, 800)
    }

    private fun loadUrl() {
        val baseUrl = Prefs.getServerUrl(this)
        val token = Prefs.getAccessToken(this)
        val url = baseUrl.trim().removeSuffix("/") + "/"
        val cookieManager = CookieManager.getInstance()
        val encodedToken = java.net.URLEncoder.encode(token, "UTF-8")
        cookieManager.setCookie(url, "access_token=$encodedToken; Path=/")
        cookieManager.flush()
        webView.loadUrl(url)
    }

    private fun isSameOrigin(urlStr: String): Boolean {
        val base = Prefs.getServerUrl(this).trim().removeSuffix("/")
        if (base.isBlank()) return false
        return try {
            val baseUri = Uri.parse(base)
            val linkUri = Uri.parse(urlStr)
            baseUri.scheme == linkUri.scheme &&
                baseUri.host?.lowercase() == linkUri.host?.lowercase() &&
                effectivePort(baseUri) == effectivePort(linkUri)
        } catch (_: Exception) {
            false
        }
    }

    private fun effectivePort(uri: Uri): Int {
        val p = uri.port
        if (p != -1) return p
        return when (uri.scheme?.lowercase()) {
            "https" -> 443
            else -> 80
        }
    }

    override fun onResume() {
        super.onResume()
        webView.onResume()
        webView.resumeTimers()
    }

    override fun onPause() {
        webView.onPause()
        webView.pauseTimers()
        super.onPause()
    }

    /** Called from bridge when the web calls PosterchanAndroid.addTorrent(magnet). */
    fun addTorrentFromWeb(magnet: String) {
        val m = magnet.trim()
        if (m.isBlank() || !m.startsWith("magnet:")) {
            Toast.makeText(this, getString(R.string.file_manager_download_failed), Toast.LENGTH_SHORT).show()
            return
        }
        Toast.makeText(this, getString(R.string.file_manager_downloading), Toast.LENGTH_SHORT).show()
        Thread {
            val baseUrl = Prefs.getServerUrl(this@WebViewActivity)
            val token = Prefs.getAccessToken(this@WebViewActivity)
            if (baseUrl.isBlank() || token.isNullOrBlank()) {
                runOnUiThread { Toast.makeText(this@WebViewActivity, getString(R.string.file_manager_error_login), Toast.LENGTH_LONG).show() }
                return@Thread
            }
            try {
                ApiClient(baseUrl, token).addTorrent(m)
                runOnUiThread { Toast.makeText(this@WebViewActivity, getString(R.string.torrent_added), Toast.LENGTH_SHORT).show() }
            } catch (e: ApiException) {
                val msg = when (e.code) {
                    401 -> getString(R.string.file_manager_error_login)
                    503 -> getString(R.string.torrent_error)
                    else -> e.message ?: getString(R.string.file_manager_download_failed)
                }
                runOnUiThread { Toast.makeText(this@WebViewActivity, msg, Toast.LENGTH_LONG).show() }
            } catch (e: Exception) {
                runOnUiThread { Toast.makeText(this@WebViewActivity, e.message ?: getString(R.string.file_manager_download_failed), Toast.LENGTH_LONG).show() }
            }
        }.start()
    }

    /** JavaScript interface so the web can trigger file download (avoids link navigation / target=_blank issues). */
    private class WebViewDownloadBridge(activity: WebViewActivity) {
        private val activityRef = WeakReference(activity)

        @JavascriptInterface
        fun downloadFile(filePath: String, fileName: String?) {
            activityRef.get()?.runOnUiThread {
                activityRef.get()?.requestFileDownload(
                    filePath.trim(),
                    fileName?.trim()?.takeIf { it.isNotBlank() }
                )
            }
        }

        @JavascriptInterface
        fun addTorrent(magnet: String?) {
            activityRef.get()?.runOnUiThread {
                activityRef.get()?.addTorrentFromWeb(magnet ?: "")
            }
        }
    }
}
