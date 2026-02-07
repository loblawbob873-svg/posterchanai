# Android app – files tracked in git

All files below are the ones needed to build the Posterchan AI Android app. Build outputs (`.gradle/`, `build/`, `local.properties`) are in `.gitignore` and are not tracked.

## Root / Gradle
- `build.gradle.kts`
- `settings.gradle.kts`
- `gradle.properties`
- `gradle/wrapper/gradle-wrapper.properties`
- `README.md`
- `docs/CODE_REVIEW.md`

## App module
- `app/build.gradle.kts`
- `app/proguard-rules.pro`

## Source & manifest
- `app/src/main/AndroidManifest.xml`
- `app/src/main/java/ai/posterchan/api/ApiClient.kt`
- `app/src/main/java/ai/posterchan/ChatActivity.kt`
- `app/src/main/java/ai/posterchan/ChatMessage.kt`
- `app/src/main/java/ai/posterchan/ConversationAdapter.kt`
- `app/src/main/java/ai/posterchan/FileManagerActivity.kt`
- `app/src/main/java/ai/posterchan/LoginActivity.kt`
- `app/src/main/java/ai/posterchan/MainActivity.kt`
- `app/src/main/java/ai/posterchan/MarkdownUtils.kt`
- `app/src/main/java/ai/posterchan/MessageAdapter.kt`
- `app/src/main/java/ai/posterchan/PhotosActivity.kt`
- `app/src/main/java/ai/posterchan/PosterchanApp.kt`
- `app/src/main/java/ai/posterchan/Prefs.kt`
- `app/src/main/java/ai/posterchan/SettingsActivity.kt`
- `app/src/main/java/ai/posterchan/WebViewActivity.kt`

## Resources

**Drawables:**  
`res/drawable/` – bg_message_user.xml, ic_add_24.xml, ic_attach_24.xml, ic_camera_24.xml, ic_file_24.xml, ic_folder_24.xml, ic_launcher_background.xml, ic_menu_24.xml, ic_mic_24.xml, ic_volume_24.xml, ic_volume_off_24.xml  
`res/drawable-nodpi/` – ic_launcher_foreground.png  
`res/mipmap-anydpi-v26/` – ic_launcher.xml, ic_launcher_round.xml  

**Layouts:**  
activity_chat.xml, activity_file_manager.xml, activity_login.xml, activity_main.xml, activity_main_native.xml, activity_photos.xml, activity_settings.xml, activity_webview.xml  
item_conversation.xml, item_file_manager.xml, item_message_assistant.xml, item_message_user.xml, item_photo.xml  

**Menus:**  
chat_menu.xml, drawer_menu.xml, file_manager_menu.xml, main.xml, quick_files.xml, quick_pim.xml, quick_web.xml  

**Values:**  
values/colors.xml, values/strings.xml, values/themes.xml  

**Other:**  
res/xml/file_paths.xml  

---

*Generated from `git ls-files android/`. Build artifacts (android/.gradle/, android/build/, android/app/build/, android/local.properties) are ignored.*
