# Code review: Native Torrents & Nyaa

Review of the native torrents flow (backend API, Android TorrentsActivity, ApiClient, and related changes).

---

## 1. Backend (`app/routers/torrent.py`)

### Strengths
- **Auth**: Catalog, search, nyaa, list, add, pause, resume, remove all use `get_torrent_user` (JWT or load-balanced). Unauthenticated clients cannot add or list torrents.
- **Validation**: `catalog` restricts `category` to a fixed set; `add_torrent` checks `magnet.startswith("magnet:")`; query params use `Query(..., ge=1, le=50)` for limits.
- **Separation**: Catalog/search/nyaa run on the main server (scraping); list/add/pause/resume/remove forward to `bt_server_url` when set. Clear and consistent.
- **Errors**: `ValueError` from scraping (e.g. proxy) is mapped to 400 with a clear message.

### Issues / suggestions

1. **Magnet length**  
   `AddTorrentRequest(magnet: str)` has no max length. A huge body could be sent. Consider:
   ```python
   class AddTorrentRequest(BaseModel):
       magnet: str
   ```
   and in the endpoint, reject e.g. `if len(body.magnet) > 2000` with 400, or add a Pydantic validator with `max_length=2000`.

2. **Rate limiting**  
   Catalog/search/nyaa trigger external scraping (and proxy). There is no rate limit; a client could spam requests. Consider per-user or per-IP rate limits for `/catalog`, `/search`, `/nyaa` (and optionally `/add`).

3. **Nyaa/catalog/search timeout**  
   Scraping can be slow. If the HTTP client timeout is only in the service (e.g. 15–30s), the FastAPI handler may still wait that long. Document expected latency or add a slightly higher server timeout so clients don’t get a generic gateway timeout without a body.

4. **Remote server and `/catalog`**  
   When `bt_server_url` is set, `/list`, `/add`, etc. forward to the remote server. `/catalog`, `/search`, `/nyaa` correctly do **not** forward (they run locally). If the main app is behind a reverse proxy that only allows certain paths to the “torrent” backend, ensure `/catalog`, `/search`, `/nyaa` are still routed to the main app, not to the remote torrent box.

---

## 2. Android `TorrentsActivity`

### Strengths
- **Tabs**: Downloading / Movies / TV / Anime / Nyaa are clear; Nyaa uses a search bar and reuses the same catalog list + Download action.
- **Auth**: Every API call uses `Prefs.getServerUrl` and `Prefs.getAccessToken`; missing token is handled with a login message.
- **Errors**: ApiException is mapped to user-facing strings (401 → login, 503 → torrent not configured, etc.).
- **Adapter**: Single adapter with two view types (active vs catalog); `setActiveItems` / `setCatalogItems` keep state and notify correctly.

### Issues / suggestions

1. **Threading**  
   All network calls use `Thread { ... }.start()`. This works but duplicates logic and makes cancellation and lifecycle harder. Consider:
   - `CoroutineScope(Dispatchers.Main.immediate).launch { withContext(Dispatchers.IO) { ... } }`, or
   - A small `Executor`/`ExecutorService` and `Future` so you can cancel on `onDestroy` if needed.
   So: not wrong, but moving to coroutines (or a single executor with cancellation) would improve consistency and avoid work after the activity is destroyed.

2. **Activity lifecycle**  
   If the user leaves the activity while a `Thread` is running, `runOnUiThread { showCatalogList(...) }` can run after the activity is destroyed/finished. You can:
   - Check `isDestroyed` before updating UI (e.g. before `runOnUiThread { ... }`), or
   - Use a `LifecycleScope` and cancel jobs in `onDestroy`.
   Same for `doNyaaSearch`, `load`, `onDownloadCatalog`, `onPauseResume`, `onRemove`.

3. **Nyaa empty response**  
   If the server returns `{ "query": "...", "items": [] }`, `showCatalogList(emptyList())` shows “No torrents here. Browse a category…”. For Nyaa it might be clearer to show “No results for ‘…’. Try another search.” Reusing the same empty string is acceptable; a Nyaa-specific empty string would improve UX slightly.

4. **Progress bar on Nyaa search**  
   `doNyaaSearch()` sets `progress.visibility = View.VISIBLE` and later `showCatalogList` / `showError` set it to `View.GONE`. That’s correct; no change required.

5. **Tab selection fallback**  
   `if (tabs.selectedTabPosition < 0)` is a reasonable fallback; in practice with 5 tabs the selected index is always ≥ 0. Safe to keep.

---

## 3. Android `ApiClient` (torrent methods)

### Strengths
- **Consistency**: Torrent list, catalog, nyaa, search all return typed data classes; parsing is in one place and throws `ApiException` on non-2xx.
- **Encoding**: `URLEncoder.encode` is used for query parameters (`category`, `q`); avoids broken URLs for special characters.

### Issues / suggestions

1. **JSON array missing**  
   If the server returns `{ "torrents": null }` or omits `"items"`, `json.getJSONArray("torrents")` or `getJSONArray("items")` can throw. You already require 2xx and a body; if the server ever returns 200 with a different shape, the app will crash. Defensive option:
   - Use `json.optJSONArray("torrents") ?: JSONArray()` (and same for `"items"`) so you get an empty list instead of a crash when the key is missing or null.

2. **Torrent list response**  
   Backend returns `torrents` as a list of objects with snake_case keys. ApiClient maps them to `TorrentActiveItem` with the correct names. No issue; just ensure any new field added on the server is either optional in the client or the server remains backward compatible.

3. **Duplicate parsing**  
   `getTorrentCatalog`, `getNyaaSearch`, and `searchTorrents` share the same “items → List<TorrentCatalogItem>” parsing. You could extract a private helper `parseCatalogItems(json: JSONObject): List<TorrentCatalogItem>` to avoid duplication and keep parsing behavior in one place.

---

## 4. WebView download bridge (`WebViewActivity`)

### Strengths
- **Bridge**: `WebViewDownloadBridge` with `WeakReference` to the activity avoids leaking the activity; `requestFileDownload` is called on the UI thread from the bridge.
- **Path**: Blank path is rejected with a toast; `startFileDownload` uses a sanitized filename and downloads via `ApiClient.downloadFileTo`.

### Suggestions
- No critical issues. The same lifecycle note as TorrentsActivity applies: if the user leaves the activity while a download `Thread` is running, consider checking `isDestroyed` before showing toasts or opening intents.

---

## 5. Security summary

| Area              | Status | Note |
|-------------------|--------|------|
| Torrent API auth  | OK     | JWT or load-balanced only. |
| Magnet validation | OK     | Server checks `magnet:` prefix; consider max length. |
| Catalog/search    | OK     | No injection; params passed to scraping layer. |
| Android token     | OK     | Token from Prefs, not logged. |
| WebView bridge    | OK     | Only `downloadFile(path, name)` exposed; path used for server request. |

---

## 6. Recommended follow-ups (short list)

1. **Backend**: Add optional `max_length` or explicit check for `body.magnet` in `add_torrent`; consider rate limiting for scraping endpoints.
2. **Android**: Guard UI updates in background threads with `isDestroyed` (or use lifecycle-aware concurrency) so you don’t update UI after the activity is gone.
3. **ApiClient**: Use `optJSONArray` (or a safe getter) for `torrents`/`items` and optionally extract a single “parse catalog items” helper to reduce duplication and parse errors.

---

## 7. Conclusion

The native torrents and Nyaa flow is consistent, auth is in place, and the split between “scraping on main server” and “list/add/control on optional remote” is clear. The main improvements are: hardening the add-torrent payload (magnet length), protecting background threads from updating destroyed activities, and making JSON parsing more defensive and DRY. No blocking issues for a normal deployment.
