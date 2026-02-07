# Code review: Torrents Android UX & Web bridge

Supplement to [CODE_REVIEW_TORRENTS_NATIVE.md](CODE_REVIEW_TORRENTS_NATIVE.md). Covers recent UX fixes, WebView add-torrent bridge, and backend magnet links.

---

## 1. TorrentsActivity – catalog and “Downloading” tab

### What was reviewed
- **Catalog row**: Whole row (MaterialCardView) is clickable and triggers `onDownload(item)`; `btn_download` also has its own click listener. Both use the same callback so tap-anywhere and tap-button both add the torrent.
- **Download button**: `MaterialButton` with `style="@style/Widget.PosterchanAI.Button"` and `app:backgroundTint="#00c8c8"` so it renders as a filled button, not a text link.
- **Initial tab load**: After selecting the tab from `EXTRA_TAB`, the code always calls `load()` (or `loadNyaaTab()`), so opening with “Downloading” reliably shows the active list even when `onTabSelected` is not fired for programmatic selection.

### Strengths
- Row + button both trigger download; good for touch targets and accessibility.
- Explicit initial load removes dependence on listener behavior across devices.
- Card has `android:clickable="true"` and `focusable="true"` so the row is focusable and clickable.

### Suggestions
1. **Double load**: Selecting the tab can still call `onTabSelected` on some devices, so both the listener and the explicit `load()` might run. That yields two requests for the same tab. Consider a short debounce or a “lastLoadedTab” guard so you only load once per tab selection (e.g. skip `load()` in the listener when the tab was just set from intent).
2. **Lifecycle**: Same as in the main review: guard `runOnUiThread { ... }` with `!isDestroyed` (or use lifecycle-aware concurrency) so UI isn’t updated after the activity is destroyed.

---

## 2. Layouts – item_torrent_catalog.xml / item_torrent_active.xml

### Catalog item
- Root is `MaterialCardView` with clickable/focusable; inner `MaterialButton` has min height 48dp and min width 140dp. Good touch target.
- Single style override: `app:backgroundTint="#00c8c8"`; theme `Widget.PosterchanAI.Button` already sets backgroundTint, so this is redundant but harmless and keeps the catalog button visually distinct.

### Active item
- Uses plain `Button` (not `MaterialButton`) with `backgroundTint` and `textColor`. No `style` reference; relies on theme. Acceptable; if the app ever switches to text-button style globally, consider giving Pause/Resume/Remove an explicit filled style like the catalog Download button.

### No issues
- No obvious a11y or layout problems.

---

## 3. WebViewActivity – addTorrent bridge

### What was reviewed
- **`addTorrentFromWeb(magnet: String)`**: Trims and validates `magnet.startsWith("magnet:")`; shows “Downloading…” then runs `ApiClient.addTorrent(m)` on a background thread; toasts for success, 401, 503, or generic failure.
- **`WebViewDownloadBridge.addTorrent(magnet: String)`**: `@JavascriptInterface`; uses `WeakReference` to activity; calls `addTorrentFromWeb` on the main thread. Handles `magnet ?: ""` for null safety from JS.

### Strengths
- Same auth and error handling pattern as TorrentsActivity (Prefs, ApiException mapping).
- Bridge does not hold a strong reference to the activity.
- Validation rejects blank and non-magnet strings.

### Suggestions
1. **Lifecycle**: Before `runOnUiThread { Toast... }` (and before starting the thread), check `!isDestroyed` so you don’t show toasts or touch the activity after it’s gone. Same pattern as in the main review.
2. **Threading**: Same recommendation as TorrentsActivity: consider coroutines or a dedicated executor so work can be cancelled when the activity is destroyed.

---

## 4. Backend – magnet link in formatted output

### What was reviewed
- **torrent_service.py** `format_torrent_results`: Appends ` [Add](magnet:{magnet_enc})` with `magnet_enc = quote(t.magnet, safe="")` so parentheses and other characters in the magnet URI don’t break the markdown link.
- **nyaa_service.py** `format_nyaa_results`: Same pattern for Nyaa results.

### Strengths
- `urllib.parse.quote(..., safe="")` ensures the link destination is one token without unescaped `)`.
- Keeps existing `[Download](cmd:...)` so browser/desktop still use the command path.

### Suggestions
1. **Import**: `from urllib.parse import quote` is inside the loop in both files. Move it to the top of the function or module to avoid repeated import (style only).
2. **Empty magnet**: If `t.magnet` is ever empty, `quote("", safe="")` is `""`, so the link becomes `[Add](magnet:)`. The web would render a link; Android validation would reject it. Optional: skip appending the Add link when `not (t.magnet or "").strip().startswith("magnet:")`.

---

## 5. Web front-end – magnet link handling (gap)

### Current behavior
- Markdown is processed with a catch-all `\[([^\]]+)\]\(([^)]+)\)`. So `[Add](magnet:ENCODED)` is stored as `url: "magnet:ENCODED"`.
- In “Restore markdown links”, links that are not command/edit-event/copy fall through to the generic link branch and are rendered as `<a href="...">Add</a>` (with optional encoding).
- There is **no** branch that checks `link.url.startsWith("magnet:")` and renders a button that calls `PosterchanAndroid.addTorrent(decodedMagnet)` when `window.PosterchanAndroid` exists.

### Consequence
- Backend now sends both `[Download](cmd:...)` and `[Add](magnet:...)`. In the WebView on Android, the “Add” link is a normal anchor. Clicking it may try to navigate to the magnet URI (external app or no-op) and does **not** call `PosterchanAndroid.addTorrent`, so the native add-torrent path is unused from the web UI.

### Recommendation
- In `static/js/chat.js`, in the “Restore markdown links” block, add a branch **before** the generic link branch:
  - If `link.url && link.url.startsWith("magnet:")`:
    - Decode: `decodedMagnet = decodeURIComponent(link.url)` (or handle decode errors).
    - If `window.PosterchanAndroid && typeof window.PosterchanAndroid.addTorrent === "function"`: render a `<button>` that calls `PosterchanAndroid.addTorrent(decodedMagnet)` (e.g. store magnet in `data-magnet` and read in onclick to avoid quoting issues).
    - Else: render `<a href="${decodedMagnet}">...</a>` for desktop/browser.
- Ensure the markdown link parser does not break on encoded magnets (current catch-all is fine as long as the URL does not contain an unescaped `)`).

---

## 6. Security and robustness (additions)

| Area | Status | Note |
|------|--------|------|
| WebView `addTorrent(magnet)` | OK | Validates prefix; server will reject invalid magnets. Very long strings could be trimmed or rejected in Kotlin if desired. |
| Backend magnet in markdown | OK | Encoded so no injection into link text; server add endpoint still validates. |
| Catalog row clickable | OK | No extra permissions; same action as button. |

---

## 7. Summary

- **TorrentsActivity**: Catalog tap-anywhere and explicit initial load for the “Downloading” tab are solid; optional debounce/guard to avoid double load.
- **WebView addTorrent bridge**: Implemented and safe; add lifecycle checks and consider structured concurrency.
- **Backend magnet links**: Correct and safe; move `quote` import out of the loop; optionally skip Add link when magnet is empty/invalid.
- **Web chat.js**: Missing handling for `magnet:` links to call `PosterchanAndroid.addTorrent` in WebView; adding that branch will complete the “Add” flow on Android from the web UI.

No blocking issues; the main functional gap is the missing magnet-link branch in the chat message formatter.
