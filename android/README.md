# Poster-chan AI – Android app

Native Android app for Poster-chan AI: **native login**, **conversation list**, **native chat** (with streaming), and an optional **Web app** screen that loads the full web UI. Battery-friendly: no background services; WebView is paused when not visible.

---

## How to run the Android project

1. **Install Android Studio** (if needed): [developer.android.com/studio](https://developer.android.com/studio).
2. **Open this project in Android Studio**
   - Launch Android Studio.
   - **File → Open…** (or **Open** on the welcome screen).
   - Browse to and select the **`android`** folder (the folder that contains this README, `app/`, `build.gradle.kts`, and `settings.gradle.kts`).  
   - Click **OK**.
3. **Wait for Gradle sync** (bottom status bar). If you’re prompted to install an SDK, Gradle, or the Android Gradle Plugin, accept and wait for sync to finish.
4. **Run the app**
   - Connect an Android device with USB debugging enabled, or use **Tools → Device Manager** to create/start an emulator.
   - Click the green **Run** button (▶) in the toolbar, or use **Run → Run 'app'**.
   - The app will install and start on the device or emulator.

**From the command line (optional):**

From the `android` directory run:

- **Windows:** `gradlew.bat assembleDebug`
- **macOS/Linux:** `./gradlew assembleDebug`

If `gradlew` / `gradlew.bat` are missing, open the project in Android Studio once and let Gradle sync; it can create the wrapper. The debug APK is at `app/build/outputs/apk/debug/app-debug.apk`. You can install that APK manually, or use **Run** in Android Studio as above.

---

## Requirements

- Android Studio (Ladybug or newer recommended)
- Android SDK 26+ (min), 34 (target)
- Poster-chan AI server running and reachable (e.g. on your LAN or with port forwarding)

## First run

1. On first launch, if no **server URL** is set, the app opens **Settings**. Enter the base URL of your Poster-chan AI instance, e.g.:
   - Same Wi‑Fi: `http://192.168.1.10:3051`
   - Emulator → host machine: `http://10.0.2.2:3051`
   - Device with server on same device: `http://127.0.0.1:3051`
2. Save, then **log in** with your username and password (native login screen).
3. You’ll see the **conversation list**. Use **New chat** (FAB) or tap a conversation to open **native chat**. Use the drawer menu for **Web app** (full web UI), **Settings**, or **Log out**.

## Battery and performance

- **No background services** – nothing runs when the app is in the background.
- **WebView lifecycle** – `onPause()` / `pauseTimers()` and `onResume()` / `resumeTimers()` so the WebView doesn’t keep doing work when the app is not visible.
- **Hardware acceleration** – WebView uses the GPU.
- **No keep-awake** – screen is not forced on.
- **Safe Browsing disabled** – no background Safe Browsing lookups (you load your own server).
- **Cleartext allowed** – for local HTTP servers; use HTTPS in production if needed.

## Launcher icon (optional)

The project uses a simple adaptive icon. To use the same icon as the web app:

- Copy `../static/icon-192.png` (and optionally `icon-512.png`) and add them via **File → New → Image Asset** (Launcher Icons) in Android Studio, or replace the contents of `app/src/main/res/mipmap-*/` with your own icons.

## Build from command line

From the `android` directory:

```bash
# Debug APK
./gradlew assembleDebug

# Release (needs signing config)
./gradlew assembleRelease
```

Output: `app/build/outputs/apk/`.

## Project structure

- `app/src/main/java/ai/posterchan/` – Kotlin: **MainActivity** (conversation list + drawer), **LoginActivity**, **ChatActivity** (native chat + WebSocket), **WebViewActivity** (full web UI), **SettingsActivity**, **Prefs**, **ApiClient** (REST + WebSocket).
- `app/src/main/java/ai/posterchan/api/` – **ApiClient** for `/api/auth/login`, `/api/conversations`, `/api/conversations/{id}/messages`, and WebSocket `/ws/chat/{id}`.
- `app/src/main/res/` – layouts, menus, strings, theme, launcher icons.
- Server URL and access token are stored in **SharedPreferences**. Drawer: **Conversations**, **Web app**, **Settings**, **Log out**.
