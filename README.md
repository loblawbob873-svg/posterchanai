# Poster-chan AI

AI chat application with LLM and image generation APIs (Python/FastAPI backend).

## Android app

The **Android app** lives in the [`android/`](android/) directory. It is a native app (login, conversation list, chat with streaming) that talks to this backend.

**How to run the Android project:**

1. Open the **[`android`](android/)** folder in **Android Studio** (**File → Open** → select the `android` folder).
2. Wait for Gradle sync to finish.
3. Connect a device or start an emulator, then click **Run** (▶).

For full steps, requirements, and command-line build, see **[android/README.md](android/README.md)**.

## Backend (web UI)

- Run the server (e.g. `python run.py` or `./start.sh`); default port **3051**.
- Open the web UI in a browser or use the Android app with the server URL set to your instance.
