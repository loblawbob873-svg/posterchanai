package ai.posterchan

import android.annotation.SuppressLint
import android.content.Intent
import android.graphics.Bitmap
import android.net.Uri
import android.os.Bundle
import android.view.View
import android.webkit.CookieManager
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
import com.google.android.material.appbar.MaterialToolbar

/**
 * Full web UI in a WebView. Injects access_token cookie so user is logged in.
 * Same-origin navigation; external links open in browser. Battery-friendly (pause/resume).
 */
class WebViewActivity : AppCompatActivity() {

    private lateinit var webView: WebView
    private lateinit var progressBar: ProgressBar

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
}
