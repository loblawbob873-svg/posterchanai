package ai.posterchan

import android.os.Bundle
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.widget.ProgressBar
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView
import com.google.android.material.appbar.MaterialToolbar
import com.google.android.material.tabs.TabLayout
import ai.posterchan.api.ApiClient
import ai.posterchan.api.ApiException

/**
 * Native torrents screen: Downloading list + browse Movies/TV/Anime with Download button.
 */
class TorrentsActivity : AppCompatActivity() {

    companion object {
        const val EXTRA_TAB = "tab" // "downloading" | "movies" | "tv" | "anime" | "nyaa"
    }

    private lateinit var toolbar: MaterialToolbar
    private lateinit var tabs: TabLayout
    private lateinit var nyaaSearchBar: View
    private lateinit var nyaaQuery: EditText
    private lateinit var nyaaSearchBtn: Button
    private lateinit var recycler: RecyclerView
    private lateinit var progress: ProgressBar
    private lateinit var emptyText: TextView
    private lateinit var errorText: TextView

    private var currentTab = "downloading"
    private var adapter: TorrentAdapter? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_torrents)

        toolbar = findViewById(R.id.toolbar)
        tabs = findViewById(R.id.tabs)
        recycler = findViewById(R.id.recycler)
        progress = findViewById(R.id.progress)
        emptyText = findViewById(R.id.empty_text)
        errorText = findViewById(R.id.error_text)

        setSupportActionBar(toolbar)
        supportActionBar?.setDisplayHomeAsUpEnabled(true)
        toolbar.setNavigationOnClickListener { onBackPressedDispatcher.onBackPressed() }

        currentTab = intent.getStringExtra(EXTRA_TAB)?.takeIf { it in listOf("downloading", "movies", "tv", "anime", "nyaa") } ?: "downloading"

        nyaaSearchBar = findViewById(R.id.nyaa_search_bar)
        nyaaQuery = findViewById(R.id.nyaa_query)
        nyaaSearchBtn = findViewById(R.id.nyaa_search_btn)

        tabs.addTab(tabs.newTab().setText(R.string.torrents_tab_downloading).setTag("downloading"))
        tabs.addTab(tabs.newTab().setText(R.string.torrents_tab_movies).setTag("movies"))
        tabs.addTab(tabs.newTab().setText(R.string.torrents_tab_tv).setTag("tv"))
        tabs.addTab(tabs.newTab().setText(R.string.torrents_tab_anime).setTag("anime"))
        tabs.addTab(tabs.newTab().setText(R.string.torrents_tab_nyaa).setTag("nyaa"))

        tabs.addOnTabSelectedListener(object : TabLayout.OnTabSelectedListener {
            override fun onTabSelected(tab: TabLayout.Tab?) {
                (tab?.tag as? String)?.let { tag ->
                    currentTab = tag
                    nyaaSearchBar.visibility = if (tag == "nyaa") View.VISIBLE else View.GONE
                    if (tag == "nyaa") loadNyaaTab() else load()
                }
            }
            override fun onTabUnselected(tab: TabLayout.Tab?) {}
            override fun onTabReselected(tab: TabLayout.Tab?) {}
        })

        recycler.layoutManager = LinearLayoutManager(this)
        adapter = TorrentAdapter(currentTab, emptyList(), emptyList(), this::onDownloadCatalog, this::onPauseResume, this::onRemove)
        recycler.adapter = adapter

        nyaaSearchBtn.setOnClickListener { doNyaaSearch() }
        nyaaQuery.setOnEditorActionListener { _, actionId, _ ->
            if (actionId == android.view.inputmethod.EditorInfo.IME_ACTION_SEARCH) {
                doNyaaSearch()
                true
            } else false
        }

        // Select initial tab and ensure data loads (listener may not fire for programmatic select on some devices)
        nyaaSearchBar.visibility = if (currentTab == "nyaa") View.VISIBLE else View.GONE
        for (i in 0 until tabs.tabCount) {
            if ((tabs.getTabAt(i)?.tag as? String) == currentTab) {
                tabs.selectTab(tabs.getTabAt(i))
                break
            }
        }
        // Always load for the initial tab so "Downloading" opens with the list
        if (currentTab == "nyaa") loadNyaaTab() else load()
    }

    private fun loadNyaaTab() {
        progress.visibility = View.GONE
        errorText.visibility = View.GONE
        emptyText.text = getString(R.string.torrents_nyaa_empty)
        emptyText.visibility = View.VISIBLE
        recycler.visibility = View.GONE
    }

    private fun doNyaaSearch() {
        val q = nyaaQuery.text?.toString()?.trim() ?: ""
        if (q.isBlank()) {
            Toast.makeText(this, getString(R.string.torrents_nyaa_search_hint), Toast.LENGTH_SHORT).show()
            return
        }
        progress.visibility = View.VISIBLE
        emptyText.visibility = View.GONE
        errorText.visibility = View.GONE
        recycler.visibility = View.GONE
        Thread {
            val baseUrl = Prefs.getServerUrl(this@TorrentsActivity)
            val token = Prefs.getAccessToken(this@TorrentsActivity)
            if (baseUrl.isBlank() || token.isNullOrBlank()) {
                runOnUiThread { showError(getString(R.string.file_manager_error_login)) }
                return@Thread
            }
            val client = ApiClient(baseUrl, token)
            try {
                val res = client.getNyaaSearch(q, 25)
                runOnUiThread { showCatalogList(res.items) }
            } catch (e: ApiException) {
                val msg = when (e.code) {
                    401 -> getString(R.string.file_manager_error_login)
                    400 -> e.message ?: getString(R.string.file_manager_error)
                    else -> e.message ?: getString(R.string.file_manager_error)
                }
                runOnUiThread { showError(msg) }
            } catch (e: Exception) {
                runOnUiThread { showError(e.message ?: getString(R.string.file_manager_error)) }
            }
        }.start()
    }

    private fun load() {
        progress.visibility = View.VISIBLE
        emptyText.visibility = View.GONE
        errorText.visibility = View.GONE
        recycler.visibility = View.GONE

        Thread {
            val baseUrl = Prefs.getServerUrl(this@TorrentsActivity)
            val token = Prefs.getAccessToken(this@TorrentsActivity)
            if (baseUrl.isBlank() || token.isNullOrBlank()) {
                runOnUiThread { showError(getString(R.string.file_manager_error_login)) }
                return@Thread
            }
            val client = ApiClient(baseUrl, token)
            try {
                when (currentTab) {
                    "downloading" -> {
                        val res = client.getTorrentList()
                        runOnUiThread { showActiveList(res.torrents) }
                    }
                    "nyaa" -> {
                        // Nyaa uses doNyaaSearch(); should not reach here for initial load
                        runOnUiThread { loadNyaaTab() }
                    }
                    else -> {
                        val res = client.getTorrentCatalog(currentTab, 25)
                        runOnUiThread { showCatalogList(res.items) }
                    }
                }
            } catch (e: ApiException) {
                val msg = when (e.code) {
                    401 -> getString(R.string.file_manager_error_login)
                    503 -> getString(R.string.torrent_error)
                    else -> e.message ?: getString(R.string.file_manager_error)
                }
                runOnUiThread { showError(msg) }
            } catch (e: Exception) {
                runOnUiThread { showError(e.message ?: getString(R.string.file_manager_error)) }
            }
        }.start()
    }

    private fun showActiveList(torrents: List<ApiClient.TorrentActiveItem>) {
        progress.visibility = View.GONE
        if (torrents.isEmpty()) {
            emptyText.text = getString(R.string.torrents_empty_downloading)
            emptyText.visibility = View.VISIBLE
            recycler.visibility = View.GONE
        } else {
            emptyText.visibility = View.GONE
            recycler.visibility = View.VISIBLE
            adapter?.setActiveItems(torrents)
        }
    }

    private fun showCatalogList(items: List<ApiClient.TorrentCatalogItem>) {
        progress.visibility = View.GONE
        if (items.isEmpty()) {
            emptyText.text = getString(R.string.torrents_empty)
            emptyText.visibility = View.VISIBLE
            recycler.visibility = View.GONE
        } else {
            emptyText.visibility = View.GONE
            recycler.visibility = View.VISIBLE
            adapter?.setCatalogItems(items)
        }
    }

    private fun showError(msg: String) {
        progress.visibility = View.GONE
        emptyText.visibility = View.GONE
        recycler.visibility = View.GONE
        errorText.text = msg
        errorText.visibility = View.VISIBLE
    }

    private fun onDownloadCatalog(item: ApiClient.TorrentCatalogItem) {
        if (item.magnet.isBlank()) {
            Toast.makeText(this, getString(R.string.file_manager_download_failed), Toast.LENGTH_SHORT).show()
            return
        }
        Toast.makeText(this, getString(R.string.file_manager_downloading), Toast.LENGTH_SHORT).show()
        Thread {
            val baseUrl = Prefs.getServerUrl(this@TorrentsActivity)
            val token = Prefs.getAccessToken(this@TorrentsActivity)
            if (baseUrl.isBlank() || token.isNullOrBlank()) {
                runOnUiThread { Toast.makeText(this@TorrentsActivity, getString(R.string.file_manager_error_login), Toast.LENGTH_LONG).show() }
                return@Thread
            }
            try {
                ApiClient(baseUrl, token).addTorrent(item.magnet)
                runOnUiThread {
                    Toast.makeText(this@TorrentsActivity, getString(R.string.torrent_added), Toast.LENGTH_SHORT).show()
                    // Refresh downloading tab if user switches to it
                    if (currentTab == "downloading") load()
                }
            } catch (e: ApiException) {
                val msg = when (e.code) {
                    401 -> getString(R.string.file_manager_error_login)
                    503 -> getString(R.string.torrent_error)
                    else -> e.message ?: getString(R.string.file_manager_download_failed)
                }
                runOnUiThread { Toast.makeText(this@TorrentsActivity, msg, Toast.LENGTH_LONG).show() }
            } catch (e: Exception) {
                runOnUiThread { Toast.makeText(this@TorrentsActivity, e.message ?: getString(R.string.file_manager_download_failed), Toast.LENGTH_LONG).show() }
            }
        }.start()
    }

    private fun onPauseResume(item: ApiClient.TorrentActiveItem) {
        Thread {
            val baseUrl = Prefs.getServerUrl(this@TorrentsActivity)
            val token = Prefs.getAccessToken(this@TorrentsActivity)
            if (baseUrl.isBlank() || token.isNullOrBlank()) return@Thread
            try {
                val client = ApiClient(baseUrl, token)
                if (item.isPaused) client.resumeTorrent(item.num) else client.pauseTorrent(item.num)
                runOnUiThread { load() }
            } catch (_: Exception) {
                runOnUiThread { Toast.makeText(this@TorrentsActivity, getString(R.string.file_manager_error), Toast.LENGTH_SHORT).show() }
            }
        }.start()
    }

    private fun onRemove(item: ApiClient.TorrentActiveItem) {
        Thread {
            val baseUrl = Prefs.getServerUrl(this@TorrentsActivity)
            val token = Prefs.getAccessToken(this@TorrentsActivity)
            if (baseUrl.isBlank() || token.isNullOrBlank()) return@Thread
            try {
                ApiClient(baseUrl, token).removeTorrent(item.num, deleteFiles = false)
                runOnUiThread { load() }
            } catch (_: Exception) {
                runOnUiThread { Toast.makeText(this@TorrentsActivity, getString(R.string.file_manager_error), Toast.LENGTH_SHORT).show() }
            }
        }.start()
    }

    private class TorrentAdapter(
        private var mode: String,
        private var activeItems: List<ApiClient.TorrentActiveItem>,
        private var catalogItems: List<ApiClient.TorrentCatalogItem>,
        private val onDownload: (ApiClient.TorrentCatalogItem) -> Unit,
        private val onPauseResume: (ApiClient.TorrentActiveItem) -> Unit,
        private val onRemove: (ApiClient.TorrentActiveItem) -> Unit
    ) : RecyclerView.Adapter<RecyclerView.ViewHolder>() {

        companion object {
            private const val TYPE_ACTIVE = 0
            private const val TYPE_CATALOG = 1
        }

        fun setActiveItems(items: List<ApiClient.TorrentActiveItem>) {
            activeItems = items
            catalogItems = emptyList()
            mode = "downloading"
            notifyDataSetChanged()
        }

        fun setCatalogItems(items: List<ApiClient.TorrentCatalogItem>) {
            catalogItems = items
            activeItems = emptyList()
            mode = "catalog"
            notifyDataSetChanged()
        }

        override fun getItemViewType(position: Int): Int = if (mode == "downloading") TYPE_ACTIVE else TYPE_CATALOG

        override fun getItemCount(): Int = if (mode == "downloading") activeItems.size else catalogItems.size

        override fun onCreateViewHolder(parent: android.view.ViewGroup, viewType: Int): RecyclerView.ViewHolder {
            return if (viewType == TYPE_ACTIVE) {
                val v = android.view.LayoutInflater.from(parent.context).inflate(R.layout.item_torrent_active, parent, false)
                ActiveVH(v, onPauseResume, onRemove)
            } else {
                val v = android.view.LayoutInflater.from(parent.context).inflate(R.layout.item_torrent_catalog, parent, false)
                CatalogVH(v, onDownload)
            }
        }

        override fun onBindViewHolder(holder: RecyclerView.ViewHolder, position: Int) {
            when (holder) {
                is ActiveVH -> holder.bind(activeItems[position])
                is CatalogVH -> holder.bind(catalogItems[position])
            }
        }

        class ActiveVH(
            itemView: View,
            private val onPauseResume: (ApiClient.TorrentActiveItem) -> Unit,
            private val onRemove: (ApiClient.TorrentActiveItem) -> Unit
        ) : RecyclerView.ViewHolder(itemView) {
            private val name: TextView = itemView.findViewById(R.id.torrent_name)
            private val progressBar: ProgressBar = itemView.findViewById(R.id.torrent_progress_bar)
            private val state: TextView = itemView.findViewById(R.id.torrent_state)
            private val btnPauseResume: Button = itemView.findViewById(R.id.btn_pause_resume)
            private val btnRemove: Button = itemView.findViewById(R.id.btn_remove)

            fun bind(item: ApiClient.TorrentActiveItem) {
                name.text = item.name.ifBlank { "…" }
                progressBar.progress = (item.progress * 100).toInt().coerceIn(0, 100)
                state.text = "${item.state} · ${item.size} · ${item.progress * 100}%"
                btnPauseResume.text = if (item.isPaused) itemView.context.getString(R.string.torrent_resume) else itemView.context.getString(R.string.torrent_pause)
                btnPauseResume.setOnClickListener { onPauseResume(item) }
                btnRemove.setOnClickListener { onRemove(item) }
            }
        }

        class CatalogVH(
            itemView: View,
            private val onDownload: (ApiClient.TorrentCatalogItem) -> Unit
        ) : RecyclerView.ViewHolder(itemView) {
            private val title: TextView = itemView.findViewById(R.id.catalog_title)
            private val meta: TextView = itemView.findViewById(R.id.catalog_meta)
            private val btnDownload: Button = itemView.findViewById(R.id.btn_download)

            fun bind(item: ApiClient.TorrentCatalogItem) {
                title.text = item.title.ifBlank { "…" }
                meta.text = "${item.size} · S:${item.seeders} L:${item.leechers}"
                itemView.setOnClickListener { onDownload(item) }
                itemView.isClickable = true
                itemView.isFocusable = true
                btnDownload.setOnClickListener { onDownload(item) }
            }
        }
    }
}
