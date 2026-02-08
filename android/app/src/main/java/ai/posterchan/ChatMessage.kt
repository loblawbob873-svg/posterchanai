package ai.posterchan

/** One image result from "images" (web image search) command. */
data class ImageSearchItem(
    val thumbnailUrl: String,
    val url: String,
    val title: String,
    /** When set, load via POST with {"url": postBodyUrl}. */
    val postBodyUrl: String? = null,
    /** When set, load via POST with {"thumb_id": postBodyThumbId}. */
    val postBodyThumbId: String? = null,
    val authToken: String? = null,
    /** When set, try this URL with a plain GET if proxy load fails (many CDNs allow direct access). */
    val directThumbUrl: String? = null
)

data class ChatMessage(
    val id: Long,
    val role: String,
    val content: String,
    val isStreaming: Boolean = false,
    /** Base64 image data for geni (generated image) responses; null otherwise. */
    val generatedImageBase64: String? = null,
    /** Thumbnail + link items for "images" (web image search) command; null otherwise. */
    val imageSearchResults: List<ImageSearchItem>? = null
) {
    val isUser: Boolean get() = role == "user"
}
