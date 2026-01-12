"""
Markdown parsing utilities for TUI.
"""

import re
from typing import List, Tuple
from rich.text import Text
from rich.markdown import Markdown


# Pattern for cmd: links: [Label](cmd:command args)
CMD_LINK_PATTERN = re.compile(r'\[([^\]]+)\]\(cmd:([^)]+)\)')

# Pattern for copy: links: [Label](copy:content)
COPY_LINK_PATTERN = re.compile(r'\[([^\]]+)\]\(copy:([^)]+)\)')

# Pattern for regular links: [Label](url)
LINK_PATTERN = re.compile(r'\[([^\]]+)\]\(([^)]+)\)')

# Pattern for bare URLs
URL_PATTERN = re.compile(r'https?://[^\s<>\[\]()]+')

# Pattern for code blocks
CODE_BLOCK_PATTERN = re.compile(r'```(\w*)\n(.*?)```', re.DOTALL)


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


def parse_markdown(text: str) -> Text:
    """
    Parse markdown text to Rich Text with basic formatting.

    Handles:
    - Bold: **text** or __text__
    - Italic: *text* or _text_
    - Inline code: `code`
    - Links are converted to [text](url) format
    - cmd: links are marked for special handling
    """
    result = Text()

    # Remove cmd: links entirely - they're rendered as buttons separately
    processed = CMD_LINK_PATTERN.sub('', text)

    # Convert copy: links similarly - remove
    processed = COPY_LINK_PATTERN.sub('', processed)

    # Convert regular links to display text
    def replace_link(match):
        label = match.group(1)
        url = match.group(2)
        if url.startswith(('http://', 'https://')):
            return f"{label} ({url})"
        return label

    processed = LINK_PATTERN.sub(replace_link, processed)

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

    return Text.from_markup(processed)


def strip_markdown(text: str) -> str:
    """
    Strip markdown formatting from text.

    Returns plain text.
    """
    # Remove code blocks
    text = CODE_BLOCK_PATTERN.sub(r'\2', text)

    # Remove links, keep label
    text = CMD_LINK_PATTERN.sub(r'\1', text)
    text = COPY_LINK_PATTERN.sub(r'\1', text)
    text = LINK_PATTERN.sub(r'\1', text)

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

    # Extract from markdown links [label](url)
    for match in LINK_PATTERN.finditer(text):
        url = match.group(2)
        if url.startswith(('http://', 'https://')):
            urls.add(url)

    # Extract bare URLs
    for match in URL_PATTERN.finditer(text):
        urls.add(match.group(0))

    return list(urls)
