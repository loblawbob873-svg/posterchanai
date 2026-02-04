package ai.posterchan

import android.text.Spannable
import androidx.core.text.HtmlCompat

/**
 * Converts markdown to HTML and then to a Spannable for display in TextView.
 * Supports **bold**, *italic*, `code`, [links](url), code blocks, and newlines.
 */
object MarkdownUtils {

    fun toSpannable(markdown: String): Spannable {
        if (markdown.isBlank()) return Spannable.Factory.getInstance().newSpannable("…")
        var html = escapeHtml(markdown)
        // Code blocks first (so we don't process inside them)
        html = Regex("""```\n?([\s\S]*?)\n?```""").replace(html) { "<pre><code>${it.groupValues[1]}</code></pre>" }
        html = Regex("""\*\*(.+?)\*\*""").replace(html) { "<b>${it.groupValues[1]}</b>" }
        html = Regex("""\*(.+?)\*""").replace(html) { "<i>${it.groupValues[1]}</i>" }
        html = Regex("""_(.+?)_""").replace(html) { "<i>${it.groupValues[1]}</i>" }
        html = Regex("""`([^`]+)`""").replace(html) { "<code>${it.groupValues[1]}</code>" }
        html = Regex("""\[([^]]+)\]\(([^)]+)\)""").replace(html) { """<a href="${it.groupValues[2]}">${it.groupValues[1]}</a>""" }
        // Headlines (before newline conversion)
        html = Regex("""(?m)^##+\s*(.+)$""").replace(html) { "<b>${it.groupValues[1]}</b>" }
        html = html.replace("\n", "<br/>")
        return HtmlCompat.fromHtml(html, HtmlCompat.FROM_HTML_MODE_LEGACY) as Spannable
    }

    private fun escapeHtml(text: String): String {
        return text
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace("\"", "&quot;")
            .replace("'", "&#39;")
    }
}
