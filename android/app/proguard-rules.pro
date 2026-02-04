# Poster-chan AI - keep WebView and app classes
-keep class ai.posterchan.** { *; }
-keepattributes *Annotation*

# WebView
-keepclassmembers class * extends android.webkit.WebViewClient { *; }
-keepclassmembers class * extends android.webkit.WebChromeClient { *; }
