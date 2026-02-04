package ai.posterchan

import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.TextView
import androidx.recyclerview.widget.DiffUtil
import androidx.recyclerview.widget.ListAdapter
import androidx.recyclerview.widget.RecyclerView

class MessageAdapter : ListAdapter<ChatMessage, RecyclerView.ViewHolder>(DiffCallback()) {

    companion object {
        private const val VIEW_USER = 0
        private const val VIEW_ASSISTANT = 1
    }

    override fun getItemViewType(position: Int): Int =
        if (getItem(position).isUser) VIEW_USER else VIEW_ASSISTANT

    override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): RecyclerView.ViewHolder {
        return if (viewType == VIEW_USER) {
            val v = LayoutInflater.from(parent.context).inflate(R.layout.item_message_user, parent, false)
            ViewHolder(v, v.findViewById(R.id.item_content))
        } else {
            val v = LayoutInflater.from(parent.context).inflate(R.layout.item_message_assistant, parent, false)
            ViewHolder(v, v.findViewById(R.id.item_content))
        }
    }

    override fun onBindViewHolder(holder: RecyclerView.ViewHolder, position: Int) {
        (holder as ViewHolder).bind(getItem(position))
    }

    private class ViewHolder(itemView: View, private val content: TextView) : RecyclerView.ViewHolder(itemView) {
        fun bind(msg: ChatMessage) {
            content.text = msg.content.ifBlank { "…" }
        }
    }

    private class DiffCallback : DiffUtil.ItemCallback<ChatMessage>() {
        override fun areItemsTheSame(a: ChatMessage, b: ChatMessage) = a.id == b.id
        override fun areContentsTheSame(a: ChatMessage, b: ChatMessage) = a == b
    }
}
