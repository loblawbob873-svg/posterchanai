"""
Markdown parsing utilities for TUI.
"""

import re
from typing import List, Tuple
from rich.text import Text


# Pattern for cmd: links: [Label](cmd:command args)
CMD_LINK_PATTERN = re.compile(r'\[([^\]]+)\]\(cmd:([^)]+)\)')

# Pattern for copy: links: [Label](copy:content)
COPY_LINK_PATTERN = re.compile(r'\[([^\]]+)\]\(copy:([^)]+)\)')

# Pattern for bare URLs - match until whitespace, angle brackets, or quotes
URL_PATTERN = re.compile(r'https?://[^\s<>\[\]"\']+')

# Pattern for code blocks
CODE_BLOCK_PATTERN = re.compile(r'```(\w*)\n(.*?)```', re.DOTALL)


def extract_markdown_links(text: str) -> List[Tuple[str, str, int, int]]:
    """
    Extract markdown links [label](url) with proper paren balancing.

    Returns list of (label, url, start_pos, end_pos) tuples.
    """
    links = []
    i = 0
    while i < len(text):
        # Find start of link: [
        if text[i] == '[':
            # Find the closing ]
            j = i + 1
            bracket_depth = 1
            while j < len(text) and bracket_depth > 0:
                if text[j] == '[':
                    bracket_depth += 1
                elif text[j] == ']':
                    bracket_depth -= 1
                j += 1

            if j < len(text) and text[j] == '(':
                # We have [label](
                label = text[i+1:j-1]
                url_start = j + 1

                # Find matching ) by counting parens
                k = url_start
                paren_depth = 1
                while k < len(text) and paren_depth > 0:
                    if text[k] == '(':
                        paren_depth += 1
                    elif text[k] == ')':
                        paren_depth -= 1
                    k += 1

                if paren_depth == 0:
                    url = text[url_start:k-1]
                    # Skip cmd: and copy: links
                    if not url.startswith('cmd:') and not url.startswith('copy:'):
                        links.append((label, url, i, k))
                    i = k
                    continue
        i += 1

    return links


def parse_cmd_links(text: str) -> List[Tuple[str, str, int, int]]:
    """
    Find all cmd: links in text.

    Returns:
        List of (label, command, start_pos, end_pos) tuples
    """
    links = []
    for match in CMD_LINK_PATTERN.finditer(text):
        links.append((
            match.group(1),  # label
            match.group(2),  # command
            match.start(),
            match.end()
        ))
    return links


def strip_html(text: str) -> str:
    """Strip HTML tags and convert to plain text."""
    import html as html_module

    # Check if text contains HTML
    lower = text.lower()
    html_indicators = ['<html', '<body', '<div', '<p>', '<table', '<span', '<!doctype', '<br', '<a href', '<td', '<tr', '<img', '<style', '&nbsp;']
    if not any(ind in lower for ind in html_indicators):
        return text

    result = text

    # Replace common block elements with newlines
    result = re.sub(r'<br\s*/?>', '\n', result, flags=re.IGNORECASE)
    result = re.sub(r'</p>', '\n\n', result, flags=re.IGNORECASE)
    result = re.sub(r'</div>', '\n', result, flags=re.IGNORECASE)
    result = re.sub(r'</li>', '\n', result, flags=re.IGNORECASE)
    result = re.sub(r'</tr>', '\n', result, flags=re.IGNORECASE)

    # Remove style and script content entirely
    result = re.sub(r'<style[^>]*>.*?</style>', '', result, flags=re.IGNORECASE | re.DOTALL)
    result = re.sub(r'<script[^>]*>.*?</script>', '', result, flags=re.IGNORECASE | re.DOTALL)

    # Remove CSS class/id definitions that leak through (e.g. .className { ... })
    result = re.sub(r'\.[a-zA-Z_][a-zA-Z0-9_-]*\s*\{[^}]*\}', '', result, flags=re.DOTALL)
    result = re.sub(r'#[a-zA-Z_][a-zA-Z0-9_-]*\s*\{[^}]*\}', '', result, flags=re.DOTALL)

    # Remove all remaining HTML tags
    result = re.sub(r'<[^>]+>', '', result)

    # Decode HTML entities
    result = html_module.unescape(result)

    # Clean up whitespace
    result = re.sub(r'[ \t]+', ' ', result)
    result = re.sub(r'\n\s*\n\s*\n+', '\n\n', result)

    return result.strip()


def parse_markdown(text: str) -> Text:
    """
    Parse markdown text to Rich Text with basic formatting.

    Handles:
    - Bold: **text** or __text__
    - Italic: *text* or _text_
    - Inline code: `code`
    - Links are converted to [text](url) format
    - cmd: links are marked for special handling
    - HTML is stripped and converted to plain text
    """
    result = Text()

    # Strip any HTML first
    processed = strip_html(text)

    # Keep cmd: link labels inline (icons like ✏️ 🗑️)
    # Just show the label text (emoji icons)
    processed = CMD_LINK_PATTERN.sub(r'\1', processed)

    # Convert copy: links similarly - keep label
    processed = COPY_LINK_PATTERN.sub(r'\1', processed)

    # Convert regular links - show URL for Ctrl+Click in terminal
    # Use proper paren-balanced extraction
    links = extract_markdown_links(processed)
    # Process in reverse order to preserve positions
    for label, url, start, end in reversed(links):
        safe_label = label.replace('[', '\\[').replace(']', '\\]')

        if url.startswith(('http://', 'https://')):
            # Show both label and URL so users can Ctrl+Click the URL
            if label == url or label.startswith('http'):
                replacement = f"[cyan underline]{url}[/cyan underline]"
            else:
                replacement = f"[bold]{safe_label}[/bold]: [cyan underline]{url}[/cyan underline]"
        elif url.startswith('mailto:'):
            # Email links - show as clickable
            if label == url[7:] or label == url:
                replacement = f"[cyan underline]{url}[/cyan underline]"
            else:
                replacement = f"[bold]{safe_label}[/bold]: [cyan underline]{url}[/cyan underline]"
        elif url.startswith('tel:'):
            # Phone links - show as clickable
            if label == url[4:] or label == url:
                replacement = f"[cyan underline]{url}[/cyan underline]"
            else:
                replacement = f"[bold]{safe_label}[/bold]: [cyan underline]{url}[/cyan underline]"
        elif url.startswith('/'):
            # Local API paths - just show the label
            replacement = label
        else:
            replacement = label

        processed = processed[:start] + replacement + processed[end:]

    # Handle bold
    processed = re.sub(r'\*\*(.+?)\*\*', r'[bold]\1[/bold]', processed)
    processed = re.sub(r'__(.+?)__', r'[bold]\1[/bold]', processed)

    # Handle italic
    processed = re.sub(r'\*(.+?)\*', r'[italic]\1[/italic]', processed)
    processed = re.sub(r'_(.+?)_', r'[italic]\1[/italic]', processed)

    # Handle inline code
    processed = re.sub(r'`([^`]+)`', r'[cyan]\1[/cyan]', processed)

    # Handle headers
    processed = re.sub(r'^### (.+)$', r'[bold magenta]\1[/bold magenta]', processed, flags=re.MULTILINE)
    processed = re.sub(r'^## (.+)$', r'[bold cyan]\1[/bold cyan]', processed, flags=re.MULTILINE)
    processed = re.sub(r'^# (.+)$', r'[bold white]\1[/bold white]', processed, flags=re.MULTILINE)

    # Escape any remaining square brackets that aren't part of Rich tags
    # Rich tags look like [tagname] or [/tagname] or [tagname attribute]
    # We need to escape [ that aren't followed by valid Rich syntax
    def escape_non_rich_brackets(text: str) -> str:
        """Escape square brackets that aren't valid Rich markup."""
        # Valid Rich tags we've created
        valid_tags = ['bold', 'italic', 'cyan', 'underline', 'magenta', 'white', '/bold', '/italic', '/cyan', '/underline', '/bold magenta', '/bold cyan', '/bold white', 'cyan underline', '/cyan underline']

        result = []
        i = 0
        while i < len(text):
            if text[i] == '[':
                # Find the closing bracket
                j = text.find(']', i)
                if j == -1:
                    # No closing bracket - escape this one
                    result.append('\\[')
                    i += 1
                else:
                    tag_content = text[i+1:j]
                    # Check if this is a valid Rich tag we created
                    if tag_content in valid_tags or tag_content.startswith('bold') or tag_content.startswith('/'):
                        result.append(text[i:j+1])
                        i = j + 1
                    else:
                        # Not a valid tag - escape the bracket
                        result.append('\\[')
                        i += 1
            else:
                result.append(text[i])
                i += 1
        return ''.join(result)

    processed = escape_non_rich_brackets(processed)

    return Text.from_markup(processed)


def strip_markdown(text: str) -> str:
    """
    Strip markdown formatting from text.

    Returns plain text.
    """
    # Remove code blocks
    text = CODE_BLOCK_PATTERN.sub(r'\2', text)

    # Remove cmd: and copy: links, keep label
    text = CMD_LINK_PATTERN.sub(r'\1', text)
    text = COPY_LINK_PATTERN.sub(r'\1', text)

    # Remove regular markdown links, keep label (with proper paren handling)
    links = extract_markdown_links(text)
    for label, url, start, end in reversed(links):
        text = text[:start] + label + text[end:]

    # Remove formatting
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'__(.+?)__', r'\1', text)
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    text = re.sub(r'_(.+?)_', r'\1', text)
    text = re.sub(r'`([^`]+)`', r'\1', text)
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)

    return text


def extract_code_blocks(text: str) -> List[Tuple[str, str]]:
    """
    Extract code blocks from text.

    Returns:
        List of (language, code) tuples
    """
    blocks = []
    for match in CODE_BLOCK_PATTERN.finditer(text):
        language = match.group(1) or "text"
        code = match.group(2).strip()
        blocks.append((language, code))
    return blocks


def extract_urls(text: str) -> List[str]:
    """
    Extract all URLs from text (both markdown links and bare URLs).

    Returns:
        List of unique URLs
    """
    urls = set()

    # Extract from markdown links [label](url) with proper paren balancing
    for label, url, start, end in extract_markdown_links(text):
        # Include http, https, mailto, and tel links
        if url.startswith(('http://', 'https://', 'mailto:', 'tel:')):
            urls.add(url)
        elif url.startswith('www.'):
            urls.add('https://' + url)

    # Extract bare URLs (http/https) - clean up trailing punctuation
    for match in URL_PATTERN.finditer(text):
        url = match.group(0)
        # Remove trailing punctuation that's likely not part of URL
        url = url.rstrip('.,;:!?\'"')
        # Remove trailing ) if unbalanced
        while url.count(')') > url.count('(') and url.endswith(')'):
            url = url[:-1]
        if url:
            urls.add(url)

    # Extract bare www. URLs
    for match in re.finditer(r'\bwww\.[^\s<>\[\]"\']+', text):
        url = match.group(0).rstrip('.,;:!?\'"')
        if not any(url in u for u in urls):  # Avoid duplicates
            urls.add('https://' + url)

    return list(urls)
