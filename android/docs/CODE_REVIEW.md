# Code review: Poster-chan AI Android app

**Scope:** Native app (login, conversation list, chat, WebView, API client, Prefs, Settings). Includes quick actions (PIM/Files/Web/Generate/Translate/RAG), server TTS, launcher icon, and **native File Manager** (list/open files via `/api/files/list` and `/api/files/view`).

**Current snapshot:** No critical or high open issues. Token/prefs encrypted; conversation and TTS lifecycle handled; attachment size capped; WebView cookie encoded. File Manager: list/refresh/cache/JSON/path-sanitize/lifecycle and open-file UX (download vs open toasts, MIME fallback, intent flags) addressed. Optional items: streaming list perf, ProGuard for security-crypto, external storage in file manager UI, download error detail.

---

## Summary

| Area | Finding | Status |
|------|---------|--------|
| Token storage | Plain SharedPreferences | ✅ Fixed: EncryptedSharedPreferences + migration |
| conversationId ≤ 0 | Missing extra → 404 | ✅ Fixed: validate and finish() |
| TTS after activity destroyed | mainHandler.post can run after destroy | ✅ Fixed: isDestroyed check before play |
| Send while streaming | UI allows second send (server handles one-by-one) | Optional: already shows Stop |
| Chat list submitList | New list every chunk (allocations) | Optional: notifyItemChanged for streaming row |
| Large attachments | Images/PDFs read fully into memory | ✅ Fixed: 15 MB limit for image/PDF |
| WebView cookie | Token with `;` could break cookie | ✅ Fixed: URL-encode token |

---

## What’s working well

- **Flow:** No server URL → Settings; no token → Login; token present → conversation list. Drawer (Web app, Settings, Log out) is clear.
- **API client:** Sync REST + WebSocket, Bearer for REST, token in query for WS. Stream/stream_end/response/error handled. TTS endpoint integrated.
- **Chat:** User/assistant bubbles, streaming via submitList, WebSocket connect-on-first-send. Quick actions mirror web (PIM, Files, Web, Generate, Translate, RAG); commands and mode prefix match backend.
- **TTS:** Server edge_tts used (same voice as web). Base64 MP3 → temp file → MediaPlayer; cleanup on stop/destroy and on completion/error.
- **WebView:** Same-origin only, external links in browser, cookie auth, pause/resume.
- **Prefs:** Application context, no Activity leak.
- **Threading:** Network on background Thread; UI via runOnUiThread / mainHandler.post. No network on main thread.
- **Manifest:** Exported/parentActivityName correct; INTERNET, RECORD_AUDIO, ACCESS_NETWORK_STATE justified; FileProvider for camera.

---

## Critical / high

### 1. ~~TTS playback after activity destroyed~~ ✅ Fixed

**File:** `ChatActivity.kt` – `speakIfEnabled`

The posted runnable now checks `isDestroyed` before calling `playServerTtsFile(file)` and deletes the temp file if the activity is destroyed.

---

## Important

### 2. ~~Token storage (plain text)~~ ✅ Fixed

**File:** `Prefs.kt`

Uses `EncryptedSharedPreferences` for token and server URL. Legacy plain prefs are migrated once to encrypted; fallback to plain if encryption fails.

---

### 3. ~~Invalid conversation ID~~ ✅ Fixed

**File:** `ChatActivity.kt` – `onCreate`

If `conversationId <= 0`, a Toast is shown and `finish()` is called.

---

### 4. Send while streaming (optional)

**File:** `ChatActivity.kt`

Sending a second message while the first is streaming sends on the same WebSocket; server processes one at a time. The button shows “Stop” and stops the stream on click, which is correct. Optionally disable the input field while streaming to make state clearer.

**Status:** Optional; current behavior is safe.

---

## Minor / cleanup

### 5. ~~WebView cookie token~~ ✅ Fixed

**File:** `WebViewActivity.kt` – `loadUrl()`

Token is URL-encoded when setting the cookie so special characters do not break the cookie.

---

### 6. Chat list updates during stream

**File:** `ChatActivity.kt` – `onStreamChunk`

Each chunk does `adapter.submitList(messages.toList())`, which allocates a new list. For high chunk rate, consider keeping a single streaming item and calling `notifyItemChanged(index)` for that item only.

**Status:** Optional performance improvement.

---

### 7. ~~Attachment size and memory~~ ✅ Fixed

**File:** `ChatActivity.kt` – `attachLauncher`

Images and PDFs are limited to `Prefs.MAX_ATTACHMENT_MB` (15 MB) via `openFileDescriptor(uri, "r").statSize` before reading. Toast shows if file is too large.

---

### 8. Quick action hint reset

**File:** `ChatActivity.kt` – `setupQuickActions`

When the user selects “Generate” or “Web Search” / “Images”, the hint is updated. When they tap “Chat” we reset to `message_hint`. If they use a PIM/Files/Web item that sends a command or opens WebView, the hint is not reset (it stays “Message…” or the previous mode hint). That is acceptable; only “Chat” explicitly clears mode and hint.

**Status:** No change required.

---

### 9. Duplicate ChatActivity click listener context

**File:** `ChatActivity.kt` – `setupQuickActions`

`quick_btn_chat` clears mode and hint but does not clear `currentMode`-related UI state beyond the hint (e.g. no “active” style on buttons). `updateQuickActionHighlight` is a no-op. If you add visual state for “active mode” later, ensure Chat clears it.

**Status:** Documented; no code change now.

---

## API client

- **OkHttp:** Single client per ApiClient; timeouts set; no leak.
- **WebSocket:** Listener runs on OkHttp executor; UI updates posted to main handler.
- **TTS:** `generateTts(text, voice?)` returns base64 MP3 or null; swallows exceptions (matches web “silently fail”).
- **Errors:** ApiException for login/API; TTS and optional flows use null/Toast.

---

## Recent changes (quick actions, TTS, icon)

- **Quick actions:** Layout and menus (PIM, Files, Web, Generate, Translate, RAG) match web behavior. Commands sent as messages; mode prefix for search/images/geni; Translate/RAG and file-related actions open WebView. Hint updates for mode are consistent.
- **TTS:** Switched from device TextToSpeech to server `/api/tts` (edge_tts). Same voice as web. MediaPlayer lifecycle and temp file cleanup are correct; isDestroyed check applied in TTS callback.
- **Launcher icon:** Web `icon-192.png` (Poster Chan mascot) used as adaptive icon foreground; vector foreground removed.

---

## Recommended next steps

1. **Optional:** `notifyItemChanged` for streaming row (performance).
2. **Optional:** ProGuard rules for `security-crypto` if release build strips required classes.

---

## Native File Manager (recent addition)

**Scope:** File Manager and Photos quick actions now open a native `FileManagerActivity` that uses `/api/files/list` and `/api/files/view` instead of the web app.

### Summary

| Area | Finding | Status |
|------|---------|--------|
| downloadFileTo return value | Returned true when response.body was null | ✅ Fixed: explicit null check, return false |
| List/refresh race | Multiple loads can run if user taps refresh or navigates quickly | ✅ Fixed: ignore refresh while load in progress |
| Cache growth | Downloaded files stay in cacheDir | ✅ Fixed: cleanup fm_* files older than 24h on open |
| Folder subtitle | Hardcoded "—" | ✅ Fixed: string resource file_manager_folder_subtitle |
| listFiles JSON | Parse errors unhandled | ✅ Fixed: catch JSONException, throw ApiException |
| File name in path | Path separators in name could escape cache dir | ✅ Fixed: sanitize with replace(Regex("[\\\\/]"), "_") |

### What’s working well

- **API:** `listFiles(path)`, `downloadFileTo(path, file, asAttachment)`, `getExternalStorageMounts()` use Bearer auth; path encoding (query for list, segment-wise for view) matches backend.
- **Navigation:** Path stack for back; toolbar title shows current folder; Refresh reloads list.
- **Threading:** Network on background `Thread`, UI via `runOnUiThread`; no network on main thread.
- **Opening files:** Download to `cacheDir`, `FileProvider.getUriForFile`, `ACTION_VIEW` with MIME from extension (unknown → `*/*`); `FLAG_GRANT_READ_URI_PERMISSION` and `FLAG_GRANT_WRITE_URI_PERMISSION`; fallback try with `setDataAndType(uri, "*/*")` if first `startActivity` throws.
- **Manifest:** `FileManagerActivity` has `parentActivityName`; existing `cache-path` in `file_paths.xml` covers file-manager downloads.
- **UX:** Loading/empty/error states; **distinct toasts**: "Could not download file" when download fails, "Could not open file" when no app handles or open throws; folders sorted first, then by name.
- **Back / lifecycle:** `OnBackPressedCallback` used correctly (object with `handleOnBackPressed`); `FileItem` referenced as `ApiClient.FileItem` (nested class).

### Critical / high

- **None.** Token and server URL are read from Prefs (encrypted); paths come from server response, not raw user input.

### Important

- **downloadFileTo when body is null:** Previously returned `true` without writing. Now returns `false` when `response.body` is null so callers do not assume success.

### Minor / cleanup

1. **Rapid refresh or navigation:** ✅ Refresh is ignored while a load is in progress (`loadList(ignoreIfLoading = true)` from the menu). Navigation and back still start a new load (last to finish wins); optional future improvement: cancel previous request or use a request id to ignore stale results.

2. **Cache dir cleanup:** ✅ On opening the file manager, `cleanupOldCacheFiles()` runs in a background thread and deletes `fm_*` files in `cacheDir` older than 24 hours.

3. **Folder subtitle:** ✅ Adapter now uses `getString(R.string.file_manager_folder_subtitle)`.

4. **Adapter visibility:** `FileManagerAdapter` is a `private` top-level class in `FileManagerActivity.kt`. Fine for single use; if reused elsewhere, consider moving to its own file or making internal.

5. **listFiles JSON errors:** ✅ Parsing wrapped in try/catch; `JSONException` rethrown as `ApiException(-1, "Invalid response: ...")`. Empty body throws `ApiException(-1, "Empty response")`.

6. **File name in dest path:** ✅ Download filename built from `item.name.replace(Regex("[\\\\/]"), "_")` so path separators cannot escape cache dir.

7. **Lifecycle (runOnUiThread after destroy):** ✅ All `runOnUiThread` blocks in `loadList()` and `openFile()` now check `isDestroyed` before updating views or starting activities, so no UI work or `startActivity` runs after the user has left the activity.

### API client (files)

- **listFiles:** GET `/api/files/list?path=...` (path URL-encoded); parses `items`, `path`, `is_external`, `external_name`. Uses `optString`/`optBoolean`/`optLong`/`optDouble` for resilience.
- **downloadFileTo:** GET `/api/files/view/<encoded-path>?download=true`; segment-wise encoding preserves slashes; returns false on non-2xx or null body; streams into `destFile`.
- **getExternalStorageMounts:** GET `/api/files/external-storage`; not yet used in UI (reserved for showing mounts in file manager).

### Open-file UX (latest)

- **Download vs open:** Download failure shows `file_manager_download_failed` ("Could not download file"); open failure (no handler or exception) shows `file_manager_open_error` ("Could not open file").
- **MIME:** Known extensions map to specific types; unknown use `*/*` so the system can resolve by extension.
- **Intent fallback:** If `startActivity(Intent.createChooser(intent, null))` throws, code retries with `intent.setDataAndType(uri, "*/*")` before showing the open-error toast.
- **Permissions:** Intent carries both `FLAG_GRANT_READ_URI_PERMISSION` and `FLAG_GRANT_WRITE_URI_PERMISSION` for viewers that need write.

### Recommended next steps (file manager)

1. **Optional:** Use `getExternalStorageMounts()` in the file manager UI to show external roots (e.g. at top of list when at root).
2. **Optional:** In `ApiClient.getExternalStorageMounts()`, wrap JSON parsing in try/catch and throw `ApiException` on parse error (same pattern as `listFiles`) for consistency.
3. **Optional:** If download fails, surface server reason (e.g. 404/403) by having `downloadFileTo` return a result type or throw with message, and show in toast or dialog.
