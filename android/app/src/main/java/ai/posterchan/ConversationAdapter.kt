package ai.posterchan

import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.TextView
import androidx.recyclerview.widget.DiffUtil
import androidx.recyclerview.widget.ListAdapter
import androidx.recyclerview.widget.RecyclerView
import ai.posterchan.api.ApiClient

class ConversationAdapter(
    private val onItemClick: (ApiClient.ConversationItem) -> Unit
) : ListAdapter<ApiClient.ConversationItem, ConversationAdapter.ViewHolder>(DiffCallback()) {

    override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): ViewHolder {
        val v = LayoutInflater.from(parent.context).inflate(R.layout.item_conversation, parent, false)
        return ViewHolder(v, onItemClick)
    }

    override fun onBindViewHolder(holder: ViewHolder, position: Int) {
        holder.bind(getItem(position))
    }

    class ViewHolder(
        itemView: View,
        private val onItemClick: (ApiClient.ConversationItem) -> Unit
    ) : RecyclerView.ViewHolder(itemView) {
        private val title: TextView = itemView.findViewById(R.id.item_title)
        private val updated: TextView = itemView.findViewById(R.id.item_updated)

        fun bind(item: ApiClient.ConversationItem) {
            title.text = item.title.ifBlank { "New Chat" }
            updated.text = formatDate(item.updatedAt)
            itemView.setOnClickListener { onItemClick(item) }
        }

        private fun formatDate(iso: String): String {
            if (iso.isBlank()) return ""
            return try {
                val t = iso.replace("T", " ").trim()
                if (t.length >= 16) t.take(16) else t.take(10)
            } catch (_: Exception) {
                iso
            }
        }
    }

    private class DiffCallback : DiffUtil.ItemCallback<ApiClient.ConversationItem>() {
        override fun areItemsTheSame(a: ApiClient.ConversationItem, b: ApiClient.ConversationItem) = a.id == b.id
        override fun areContentsTheSame(a: ApiClient.ConversationItem, b: ApiClient.ConversationItem) = a == b
    }
}
