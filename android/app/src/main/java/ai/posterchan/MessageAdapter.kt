package ai.posterchan

import ai.posterchan.ChatMessage
import ai.posterchan.MarkdownUtils
import ai.posterchan.R
import android.graphics.BitmapFactory
import android.util.Base64
import android.text.method.LinkMovementMethod
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.ImageView
import kotlin.math.roundToInt
import android.widget.TextView
import androidx.recyclerview.widget.DiffUtil
import androidx.recyclerview.widget.ListAdapter
import androidx.recyclerview.widget.RecyclerView
import com.google.android.material.button.MaterialButton

class MessageAdapter(
    private val lastAssistantMessageId: () -> Long?,
    private val onCopy: (String) -> Unit,
    private val onShare: (String) -> Unit,
    private val onRegenerateAssistant: () -> Unit,
    private val onEditUser: (Long, String) -> Unit,
    private val onResendUser: (Long) -> Unit,
    private val onOpenUrl: (String) -> Unit = {},
) : ListAdapter<ChatMessage, RecyclerView.ViewHolder>(DiffCallback()) {

    companion object {
        private const val VIEW_USER = 0
        private const val VIEW_ASSISTANT = 1
    }

    override fun getItemViewType(position: Int): Int =
        if (getItem(position).isUser) VIEW_USER else VIEW_ASSISTANT

    override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): RecyclerView.ViewHolder {
        return if (viewType == VIEW_USER) {
            val v = LayoutInflater.from(parent.context).inflate(R.layout.item_message_user, parent, false)
            UserViewHolder(v, onEditUser, onResendUser)
        } else {
            val v = LayoutInflater.from(parent.context).inflate(R.layout.item_message_assistant, parent, false)
            AssistantViewHolder(v, lastAssistantMessageId, onCopy, onShare, onRegenerateAssistant, onOpenUrl)
        }
    }

    override fun onBindViewHolder(holder: RecyclerView.ViewHolder, position: Int) {
        when (holder) {
            is UserViewHolder -> holder.bind(getItem(position))
            is AssistantViewHolder -> holder.bind(getItem(position))
        }
    }

    private class AssistantViewHolder(
        itemView: View,
        private val lastAssistantMessageId: () -> Long?,
        private val onCopy: (String) -> Unit,
        private val onShare: (String) -> Unit,
        private val onRegenerateAssistant: () -> Unit,
        private val onOpenUrl: (String) -> Unit,
    ) : RecyclerView.ViewHolder(itemView) {
        private val content: TextView = itemView.findViewById(R.id.item_content)
        private val generatedImage: ImageView = itemView.findViewById(R.id.item_generated_image)
        private val imageSearchScroll: View = itemView.findViewById(R.id.item_image_search_scroll)
        private val imageSearchContainer: ViewGroup = itemView.findViewById<ViewGroup>(R.id.item_image_search_container)
        private val imageThumbnails: List<ImageView>
        private val btnCopy: MaterialButton = itemView.findViewById(R.id.btn_copy)
        private val btnShare: MaterialButton = itemView.findViewById(R.id.btn_share)
        private val btnRegenerate: MaterialButton = itemView.findViewById(R.id.btn_regenerate)

        init {
            content.movementMethod = LinkMovementMethod.getInstance()
            val density = itemView.context.resources.displayMetrics.density
            val sizePx = (88 * density).roundToInt().coerceAtLeast(1)
            val marginPx = (6 * density).roundToInt()
            imageThumbnails = (0 until 10).map {
                ImageView(itemView.context).apply {
                    layoutParams = ViewGroup.MarginLayoutParams(sizePx, sizePx).apply {
                        marginEnd = marginPx
                    }
                    scaleType = ImageView.ScaleType.CENTER_CROP
                }
            }
            imageThumbnails.forEach { imageSearchContainer.addView(it) }
        }

        fun bind(msg: ChatMessage) {
            content.text = MarkdownUtils.toSpannable(msg.content.ifBlank { "…" })
            val b64 = msg.generatedImageBase64
            if (!b64.isNullOrBlank()) {
                try {
                    val bytes = Base64.decode(b64, Base64.DEFAULT)
                    val bmp = BitmapFactory.decodeByteArray(bytes, 0, bytes.size)
                    if (bmp != null) {
                        generatedImage.setImageBitmap(bmp)
                        generatedImage.visibility = View.VISIBLE
                    } else {
                        generatedImage.visibility = View.GONE
                    }
                } catch (_: Exception) {
                    generatedImage.visibility = View.GONE
                }
            } else {
                generatedImage.setImageDrawable(null)
                generatedImage.visibility = View.GONE
            }
            val imageResults = msg.imageSearchResults
            if (!imageResults.isNullOrEmpty()) {
                imageSearchScroll.visibility = View.VISIBLE
                imageThumbnails.forEachIndexed { index, iv ->
                    if (index < imageResults.size) {
                        iv.visibility = View.VISIBLE
                        val tag = "img_${msg.id}_$index"
                        if (iv.getTag(R.id.image_loader_tag) != tag) iv.setImageDrawable(null)
                        iv.setOnClickListener { onOpenUrl(imageResults[index].url) }
                        val item = imageResults[index]
                        ImageLoader.load(
                            item.thumbnailUrl,
                            iv,
                            tag,
                            0L,
                            onError = { iv.visibility = View.GONE },
                            postBodyUrl = item.postBodyUrl,
                            postBodyThumbId = item.postBodyThumbId,
                            authToken = item.authToken,
                            directThumbUrl = item.directThumbUrl
                        )
                    } else {
                        iv.visibility = View.GONE
                        iv.setImageDrawable(null)
                    }
                }
            } else {
                imageSearchScroll.visibility = View.GONE
                imageThumbnails.forEach { iv ->
                    iv.visibility = View.GONE
                    iv.setImageDrawable(null)
                }
            }
            btnCopy.setOnClickListener { onCopy(msg.content) }
            btnShare.setOnClickListener { onShare(msg.content) }
            val showRegenerate = !msg.isStreaming && msg.id == lastAssistantMessageId()
            btnRegenerate.visibility = if (showRegenerate) View.VISIBLE else View.GONE
            btnRegenerate.setOnClickListener { onRegenerateAssistant() }
        }
    }

    private class UserViewHolder(
        itemView: View,
        private val onEditUser: (Long, String) -> Unit,
        private val onResendUser: (Long) -> Unit,
    ) : RecyclerView.ViewHolder(itemView) {
        private val content: TextView = itemView.findViewById(R.id.item_content)
        private val btnEdit: MaterialButton = itemView.findViewById(R.id.btn_edit)
        private val btnRegenerate: MaterialButton = itemView.findViewById(R.id.btn_regenerate)

        init {
            content.movementMethod = LinkMovementMethod.getInstance()
        }

        fun bind(msg: ChatMessage) {
            content.text = MarkdownUtils.toSpannable(msg.content.ifBlank { "…" })
            btnEdit.setOnClickListener { onEditUser(msg.id, msg.content) }
            btnRegenerate.setOnClickListener { onResendUser(msg.id) }
        }
    }

    private class DiffCallback : DiffUtil.ItemCallback<ChatMessage>() {
        override fun areItemsTheSame(a: ChatMessage, b: ChatMessage) = a.id == b.id
        override fun areContentsTheSame(a: ChatMessage, b: ChatMessage) = a == b
    }
}
