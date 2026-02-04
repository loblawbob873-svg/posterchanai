package ai.posterchan

import ai.posterchan.R
import android.text.method.LinkMovementMethod
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
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
            AssistantViewHolder(v, lastAssistantMessageId, onCopy, onShare, onRegenerateAssistant)
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
        lastAssistantMessageId: () -> Long?,
        private val onCopy: (String) -> Unit,
        private val onShare: (String) -> Unit,
        private val onRegenerateAssistant: () -> Unit,
    ) : RecyclerView.ViewHolder(itemView) {
        private val content: TextView = itemView.findViewById(R.id.item_content)
        private val btnCopy: MaterialButton = itemView.findViewById(R.id.btn_copy)
        private val btnShare: MaterialButton = itemView.findViewById(R.id.btn_share)
        private val btnRegenerate: MaterialButton = itemView.findViewById(R.id.btn_regenerate)

        init {
            content.movementMethod = LinkMovementMethod.getInstance()
        }

        fun bind(msg: ChatMessage) {
            content.text = MarkdownUtils.toSpannable(msg.content.ifBlank { "…" })
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
