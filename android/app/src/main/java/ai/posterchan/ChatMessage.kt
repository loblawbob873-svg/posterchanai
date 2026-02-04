package ai.posterchan

data class ChatMessage(
    val id: Long,
    val role: String,
    val content: String,
    val isStreaming: Boolean = false
) {
    val isUser: Boolean get() = role == "user"
}
