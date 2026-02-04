# Code review: Poster-chan AI Android app (native)

**Scope:** Native login, conversation list, chat (WebSocket), WebViewActivity, API client, Prefs, Settings.

---

## Summary

| Area | Finding | Status |
|------|---------|--------|
| Unused imports | ApiException (ChatActivity), Credentials, ByteString (ApiClient) | ✅ Fixed |
| Conversation list error | Old list stayed visible when load failed | ✅ Fixed: submitList(emptyList()) on error |
| Date formatting | dropLast(3) broke ISO date display | ✅ Fixed: take(16) / take(10) |
| Token storage | Plain SharedPreferences | Optional: use EncryptedSharedPreferences |
| Send while streaming | Second message sent before first ends | Optional: disable send or queue |
| conversationId == 0 | Missing extra could lead to 404 | Optional: validate and finish/error |
| Cookie token | Special chars in token could break cookie | Optional: encode token in cookie value |

---

## What’s working well

- **Flow:** No server URL → Settings; no token → Login; token present → conversation list. Drawer (Web app, Settings, Log out) is clear.
- **API client:** Sync REST + WebSocket, token in query for WS, stream/stream_end/response/error handled. Bearer token for REST.
- **Chat:** User/assistant bubbles, streaming via ListAdapter submitList, WebSocket connect-on-first-send, send in onOpen when connecting.
- **WebView:** Same-origin only, external links in browser, cookie injection for auth, pause/resume for battery.
- **Prefs:** Application context for SharedPreferences, no Activity leak.
- **Threading:** Network on background Thread, UI updates via runOnUiThread / mainHandler.post. No network on main thread.

---

## Critical / security

### 1. Token in WebSocket URL

**File:** `ApiClient.kt` – `connectChatWebSocket`

Token is passed as a query parameter. It can appear in logs (e.g. OkHttp/WebSocket). The server expects `token` in query or cookie; for WebSocket it doesn’t use headers. So this is acceptable; just be aware and avoid logging the full URL.

**Status:** Documented; no change required.

---

### 2. Token storage (plain text)

**File:** `Prefs.kt`

Token is stored in plain SharedPreferences. On a rooted or compromised device it can be read.

**Recommendation:** Use `EncryptedSharedPreferences` (AndroidX Security) for `KEY_ACCESS_TOKEN` (and optionally server URL if sensitive).

**Status:** Optional improvement.

---

## Important

### 3. Chat: send button while streaming

**File:** `ChatActivity.kt` – `sendMessage`

If the user sends a second message before the first reply finishes, we send it on the same WebSocket. The server processes one message at a time; behavior is correct but the UI doesn’t show “loading” or block double-send.

**Recommendation:** Disable the send button (or show a “sending” state) while `streamingMessageId` corresponds to an assistant message with `isStreaming == true`; re-enable on stream_end/response/error.

**Status:** Optional; current behavior is safe.

---

### 4. Missing conversation ID

**File:** `ChatActivity.kt` – `onCreate`

`conversationId = intent.getIntExtra(EXTRA_CONVERSATION_ID, 0)`. If the extra is missing we get 0; `getMessages(0)` will 404.

**Recommendation:** If `conversationId <= 0`, show a Toast and `finish()` (or show an error state).

**Status:** Optional.

---

### 5. LoginActivity lifecycle

**File:** `LoginActivity.kt` – `runOnUiThread { ... startActivity(...); finish() }`

If the user leaves the app or the activity is destroyed during login, the callback can run after the activity is finished. Starting an activity from a destroyed activity is allowed; `finish()` is a no-op if already finished.

**Optional:** Check `!isFinishing` before starting MainActivity and finishing, to avoid unnecessary work.

**Status:** No change required for correctness.

---

## Minor / cleanup

### 6. WebView cookie token value

**File:** `WebViewActivity.kt` – `loadUrl()`

`cookieManager.setCookie(url, "access_token=$token; Path=/")`. If the token contained `;` or other cookie-unsafe characters, the cookie could be malformed. JWTs are typically safe.

**Recommendation:** Use `URLEncoder.encode(token, "UTF-8")` for the token value in the cookie string.

**Status:** Optional.

---

### 7. MainActivity onResume refresh

**File:** `MainActivity.kt` – `onResume()`

Conversations are reloaded on every onResume (e.g. returning from Chat or another app). Good for freshness; could be more efficient with a debounce or “refresh only when returning from Chat” (e.g. result code).

**Status:** Optional; current behavior is acceptable.

---

### 8. Chat list updates during stream

**File:** `ChatActivity.kt` – `onStreamChunk`

Each chunk does `adapter.submitList(messages.toList())`. That creates a new list and replaces the adapter list; correct but allocates often. Alternative: keep one streaming item and call `notifyItemChanged(index)` for that item only.

**Status:** Optional performance improvement.

---

## API client

- **OkHttp:** Single client instance per ApiClient; timeouts set; no leak.
- **WebSocket:** Returned immediately; listener runs on OkHttp’s executor. We post to main handler for UI; good.
- **Errors:** ApiException with code and message; callers handle and show Toast where appropriate.
- **Unused imports:** Credentials, ByteString removed.

---

## Manifest and build

- Activities have appropriate `exported` and `parentActivityName`. Login has no parent (back exits).
- No extra permissions. INTERNET and ACCESS_NETWORK_STATE are justified.

---

## Applied fixes (this pass)

- Removed unused imports: `ApiException` (ChatActivity), `Credentials`, `ByteString` (ApiClient).
- MainActivity: on load error, call `adapter.submitList(emptyList())` so the list is cleared when the API fails.
- ConversationAdapter: date formatting now uses `take(16)` / `take(10)` on the normalized string instead of `dropLast(3)`.

Optional next steps: EncryptedSharedPreferences for token, validate `conversationId > 0` in ChatActivity, disable send (or show loading) while streaming, optional URL-encode of token in WebView cookie.
